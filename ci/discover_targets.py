#!/usr/bin/env python3
"""Discover active nginx package versions from nginx.org and output a GitHub Actions matrix."""

import gzip
import io
import json
import os
import re
import sys
import tarfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION_RE = re.compile(r"^([0-9]+\.[0-9]+\.[0-9]+)(?:[-~_+.a-zA-Z0-9]*)$")
RPM_ARCH = {"amd64": "x86_64", "arm64": "aarch64"}
APK_ARCH = {"amd64": "x86_64", "arm64": "aarch64"}


def fetch_bytes(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "openappsec-attachment-ci/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def version_key(ver: str) -> Tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", ver)]
    return tuple(nums)


def latest_deb_nginx(repo_url: str, codename: str, arch: str) -> Optional[str]:
    url = f"{repo_url}/dists/{codename}/nginx/binary-{arch}/Packages.gz"
    raw = gzip.decompress(fetch_bytes(url)).decode("utf-8", errors="replace")
    versions: List[str] = []
    pkg: Optional[str] = None
    ver: Optional[str] = None
    for line in raw.splitlines():
        if not line.strip():
            if pkg == "nginx" and ver:
                versions.append(ver)
            pkg = None
            ver = None
        elif line.startswith("Package: "):
            pkg = line.split(": ", 1)[1].strip()
        elif line.startswith("Version: "):
            ver = line.split(": ", 1)[1].strip()
    if pkg == "nginx" and ver:
        versions.append(ver)
    return max(versions, key=version_key) if versions else None


def latest_rpm_nginx(repo_url: str, arch: str) -> Optional[str]:
    rpm_arch = RPM_ARCH[arch]
    repomd_url = f"{repo_url}/{rpm_arch}/repodata/repomd.xml"
    repomd_xml = ET.fromstring(fetch_bytes(repomd_url))
    ns = {"r": "http://linux.duke.edu/metadata/repo"}
    primary_href: Optional[str] = None
    for data in repomd_xml.findall("r:data", ns):
        if data.attrib.get("type") == "primary":
            loc = data.find("r:location", ns)
            if loc is not None:
                primary_href = loc.attrib.get("href")
                break
    if not primary_href:
        return None
    primary_url = f"{repo_url}/{rpm_arch}/{primary_href}"
    primary_bytes = fetch_bytes(primary_url)
    if primary_href.endswith(".gz"):
        primary_bytes = gzip.decompress(primary_bytes)
    root = ET.fromstring(primary_bytes)
    cns = {"c": "http://linux.duke.edu/metadata/common"}
    versions: List[str] = []
    for pkg in root.findall("c:package", cns):
        name_el = pkg.find("c:name", cns)
        if name_el is None or name_el.text != "nginx":
            continue
        ver_el = pkg.find("c:version", cns)
        if ver_el is not None:
            ver = ver_el.attrib.get("ver")
            rel = ver_el.attrib.get("rel")
            if ver and rel:
                versions.append(f"{ver}-{rel}")
    return max(versions, key=version_key) if versions else None


def latest_apk_nginx(repo_url: str, arch: str) -> Optional[str]:
    apk_arch = APK_ARCH[arch]
    url = f"{repo_url}/{apk_arch}/APKINDEX.tar.gz"
    raw = fetch_bytes(url)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        member = tf.extractfile("APKINDEX")
        if member is None:
            return None
        text = member.read().decode("utf-8", errors="replace")
    versions: List[str] = []
    pkg: Optional[str] = None
    ver: Optional[str] = None
    for line in text.splitlines():
        if not line.strip():
            if pkg == "nginx" and ver:
                versions.append(ver)
            pkg = None
            ver = None
        elif line.startswith("P:"):
            pkg = line[2:].strip()
        elif line.startswith("V:"):
            ver = line[2:].strip()
    if pkg == "nginx" and ver:
        versions.append(ver)
    return max(versions, key=version_key) if versions else None


def load_existing_index(repo_slug: str) -> Dict[str, Dict[str, Any]]:
    if not repo_slug:
        return {}
    url = f"https://github.com/{repo_slug}/releases/download/supported-index/index.json"
    try:
        data = json.loads(fetch_bytes(url).decode("utf-8"))
        return {entry["key"]: entry for entry in data.get("attachments", []) if "key" in entry}
    except Exception:
        return {}


def main() -> int:
    targets_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("ci/targets.json")
    force_rebuild = os.getenv("FORCE_REBUILD", "false").lower() == "true"
    repo_slug = os.getenv("GITHUB_REPOSITORY", "")
    attachment_sha = os.getenv("ATTACHMENT_SHA", "")

    targets_cfg = json.loads(targets_path.read_text(encoding="utf-8"))
    existing = load_existing_index(repo_slug)

    matrix_items: List[Dict[str, str]] = []
    for t in targets_cfg.get("targets", []):
        for arch in t["arches"]:
            pkg_type = t["pkg_type"]
            try:
                if pkg_type == "deb":
                    pkg_version = latest_deb_nginx(t["repo_url"], t["codename"], arch)
                elif pkg_type == "rpm":
                    pkg_version = latest_rpm_nginx(t["repo_url"], arch)
                elif pkg_type == "apk":
                    pkg_version = latest_apk_nginx(t["repo_url"], arch)
                else:
                    raise ValueError(f"Unsupported pkg_type: {pkg_type}")
            except Exception as exc:
                print(f"::warning::Failed to query {t['id']} ({arch}): {exc}", file=sys.stderr)
                continue

            if not pkg_version:
                print(f"::warning::No nginx package found for {t['id']} ({arch})", file=sys.stderr)
                continue

            m = VERSION_RE.match(pkg_version)
            if not m:
                print(f"::warning::Invalid version string '{pkg_version}' for {t['id']} ({arch})", file=sys.stderr)
                continue
            nginx_version = m.group(1)

            key = f"{t['distro']}-{t['codename']}-{t['channel']}-{arch}"
            prev = existing.get(key)
            if (
                not force_rebuild
                and prev
                and prev.get("nginx_pkg_version") == pkg_version
                and "~" not in prev.get("url", "")
                and (not attachment_sha or prev.get("attachment_commit") == attachment_sha)
            ):
                print(f"Skipping {key}: already built for {pkg_version}", file=sys.stderr)
                continue

            runner = "ubuntu-24.04-arm" if arch == "arm64" else "ubuntu-latest"
            matrix_items.append(
                {
                    "key": key,
                    "target_id": t["id"],
                    "distro": t["distro"],
                    "codename": t["codename"],
                    "channel": t["channel"],
                    "pkg_type": pkg_type,
                    "image": t["image"],
                    "repo_url": t["repo_url"],
                    "arch": arch,
                    "runner": runner,
                    "pkg_version": pkg_version,
                    "nginx_version": nginx_version,
                }
            )

    print(json.dumps({"include": matrix_items}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
