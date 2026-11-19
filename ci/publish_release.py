#!/usr/bin/env python3
"""Verify built artifacts, publish per-version GitHub Releases, and update supported-index."""

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def fetch_existing_index(repo_slug: str) -> Dict[str, Any]:
    url = f"https://github.com/{repo_slug}/releases/download/supported-index/index.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "openappsec-attachment-ci/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {"schema_version": 1, "attachments": []}


def ensure_release(tag: str, title: str, notes: str) -> None:
    res = run(["gh", "release", "view", tag], check=False)
    if res.returncode != 0:
        run(["gh", "release", "create", tag, "--title", title, "--notes", notes])


def main() -> int:
    artifacts_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("artifacts")
    repo_slug = os.environ["GITHUB_REPOSITORY"]

    tarballs = sorted(artifacts_dir.rglob("*.tar.gz"))
    if not tarballs:
        print("No built tarballs found; nothing to publish.")
        return 0

    existing = fetch_existing_index(repo_slug)
    entries_by_key: Dict[str, Dict[str, Any]] = {
        e["key"]: e for e in existing.get("attachments", []) if "key" in e
    }

    by_release_tag: Dict[str, List[Path]] = {}
    for tb in tarballs:
        sha_file = tb.with_name(tb.name + ".sha256")
        meta_file = tb.with_name(tb.name + ".metadata.json")
        if not sha_file.is_file() or not meta_file.is_file():
            raise RuntimeError(f"Missing .sha256 or .metadata.json alongside {tb}")

        expected_sha = sha_file.read_text(encoding="utf-8").strip().split()[0]
        actual_sha = sha256_file(tb)
        if actual_sha != expected_sha:
            raise RuntimeError(f"Checksum mismatch for {tb.name}: expected {expected_sha}, got {actual_sha}")

        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        nginx_ver = meta["nginx_version"]
        release_tag = f"nginx-{nginx_ver}"
        url = f"https://github.com/{repo_slug}/releases/download/{release_tag}/{tb.name}"

        meta["sha256"] = actual_sha
        meta["release_tag"] = release_tag
        meta["url"] = url
        entries_by_key[meta["key"]] = meta

        by_release_tag.setdefault(release_tag, []).append(tb)

    # Publish each NGINX version release with combined SHA256SUMS
    for tag, files in sorted(by_release_tag.items()):
        ensure_release(
            tag,
            f"open-appsec NGINX attachments for {tag}",
            f"Automated, GPG-verified builds of `openappsec/attachment` for `{tag}`.\n\nVerify downloads with `sha256sum -c SHA256SUMS`.",
        )

        sums_dir = artifacts_dir / "_sums" / tag
        sums_dir.mkdir(parents=True, exist_ok=True)
        sums_file = sums_dir / "SHA256SUMS"

        # Preserve existing checksums on this release tag if re-running a subset
        existing_sums: Dict[str, str] = {}
        dl_res = run(["gh", "release", "download", tag, "-p", "SHA256SUMS", "-D", str(sums_dir), "--clobber"], check=False)
        if dl_res.returncode == 0 and sums_file.is_file():
            for line in sums_file.read_text(encoding="utf-8").splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2:
                    existing_sums[parts[1].lstrip("*")] = parts[0]

        upload_args: List[str] = []
        for tb in files:
            existing_sums[tb.name] = sha256_file(tb)
            upload_args.append(str(tb))
            upload_args.append(str(tb.with_name(tb.name + ".sha256")))

        sums_lines = [f"{digest}  {name}" for name, digest in sorted(existing_sums.items())]
        sums_file.write_text("\n".join(sums_lines) + "\n", encoding="utf-8")
        upload_args.append(str(sums_file))

        run(["gh", "release", "upload", tag, "--clobber", *upload_args])

    # Update rolling supported-index release
    sorted_entries = sorted(entries_by_key.values(), key=lambda x: x["key"])
    index_dir = artifacts_dir / "_index"
    index_dir.mkdir(parents=True, exist_ok=True)

    index_json_path = index_dir / "index.json"
    index_json_path.write_text(
        json.dumps({"schema_version": 1, "attachments": sorted_entries}, indent=2) + "\n",
        encoding="utf-8",
    )

    txt_lines = [
        "# key | distro | codename | arch | nginx_pkg_version | sha256 | url"
    ]
    for e in sorted_entries:
        txt_lines.append(
            f"{e['key']} | {e['distro']} | {e['codename']} | {e['arch']} | {e['nginx_pkg_version']} | {e['sha256']} | {e['url']}"
        )
    supported_txt_path = index_dir / "supported-nginx.txt"
    supported_txt_path.write_text("\n".join(txt_lines) + "\n", encoding="utf-8")

    ensure_release(
        "supported-index",
        "Supported NGINX Attachments Index",
        "Rolling machine-readable index (`index.json` and `supported-nginx.txt`) for all published open-appsec NGINX attachment builds.",
    )
    run(["gh", "release", "upload", "supported-index", "--clobber", str(index_json_path), str(supported_txt_path)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
