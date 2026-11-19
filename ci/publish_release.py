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


def upsert_release(tag: str, title: str, notes_file: Path) -> None:
    res = run(["gh", "release", "view", tag], check=False)
    if res.returncode != 0:
        run(["gh", "release", "create", tag, "--title", title, "--notes-file", str(notes_file)])
    else:
        run(["gh", "release", "edit", tag, "--title", title, "--notes-file", str(notes_file)])


def build_release_notes(repo_slug: str, branch: str, tag: str, entries: List[Dict[str, Any]]) -> str:
    rows = "\n".join(
        f"| `{e['key']}` | `{e['distro']}` `{e['codename']}` | `{e['arch']}` | `{e['nginx_pkg_version']}` | `{e['sha256'][:16]}...` |"
        for e in sorted(entries, key=lambda x: x["key"])
    )
    return f"""Automated, PGP-verified builds of `openappsec/attachment` for **`{tag}`**.

### Quick Install / Upgrade (auto-detects OS, arch & NGINX version, verifies SHA-256, runs `nginx -t`)
```bash
curl -fsSL https://raw.githubusercontent.com/{repo_slug}/{branch}/install.sh | sudo bash
```

Other options:
```bash
# List all available builds
curl -fsSL https://raw.githubusercontent.com/{repo_slug}/{branch}/install.sh | bash -s -- --list

# Dry-run match & SHA-256 verification without modifying files
curl -fsSL https://raw.githubusercontent.com/{repo_slug}/{branch}/install.sh | bash -s -- --dry-run
```

### Manual Verification & Install
```bash
sha256sum -c --ignore-missing SHA256SUMS
tar -xzf openappsec-ngx-attachment_*.tar.gz
```
Then copy `lib/libngx_module.so` to `/usr/lib/nginx/modules/ngx_cp_attachment_module.so` (or `/usr/lib64/nginx/modules/` on RHEL/Rocky), copy the remaining `lib/*.so` to `/usr/lib/` (or `/usr/lib64/`), add `load_module .../ngx_cp_attachment_module.so;` to `/etc/nginx/nginx.conf`, verify with `nginx -t`, and reload NGINX.

### Included Targets
| Key | OS | Arch | NGINX Package | SHA-256 (prefix) |
| :--- | :--- | :--- | :--- | :--- |
{rows}
"""


def build_index_notes(repo_slug: str, branch: str, entries: List[Dict[str, Any]]) -> str:
    return f"""Rolling machine-readable index (`index.json` and `supported-nginx.txt`) covering **{len(entries)}** published NGINX attachment builds.

### Quick Install / Upgrade
```bash
curl -fsSL https://raw.githubusercontent.com/{repo_slug}/{branch}/install.sh | sudo bash
```

### List Available Builds
```bash
curl -fsSL https://raw.githubusercontent.com/{repo_slug}/{branch}/install.sh | bash -s -- --list
```
"""


def main() -> int:
    artifacts_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("artifacts")
    repo_slug = os.environ["GITHUB_REPOSITORY"]
    branch = os.getenv("GITHUB_REF_NAME", "release-ci")

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

    # Publish each NGINX version release with combined SHA256SUMS and usage notes
    for tag, files in sorted(by_release_tag.items()):
        sums_dir = artifacts_dir / "_sums" / tag
        sums_dir.mkdir(parents=True, exist_ok=True)
        sums_file = sums_dir / "SHA256SUMS"
        notes_file = sums_dir / "RELEASE_NOTES.md"

        tag_entries = [e for e in entries_by_key.values() if e.get("release_tag") == tag]
        notes_file.write_text(build_release_notes(repo_slug, branch, tag, tag_entries), encoding="utf-8")

        upsert_release(tag, f"open-appsec NGINX attachments for {tag}", notes_file)

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

    index_notes_path = index_dir / "INDEX_NOTES.md"
    index_notes_path.write_text(build_index_notes(repo_slug, branch, sorted_entries), encoding="utf-8")

    upsert_release("supported-index", "Supported NGINX Attachments Index", index_notes_path)
    run(["gh", "release", "upload", "supported-index", "--clobber", str(index_json_path), str(supported_txt_path)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
