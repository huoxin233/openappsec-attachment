#!/usr/bin/env python3
"""Verify built artifacts, publish GitHub Releases, sync history against live tags, and stage Pages."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def version_key(ver: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", ver))


def fetch_json_asset(repo_slug: str, filename: str) -> Dict[str, Any]:
    url = f"https://github.com/{repo_slug}/releases/download/supported-index/{filename}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "openappsec-attachment-ci/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {"schema_version": 1, "attachments": []}


def list_live_nginx_releases() -> Set[str]:
    res = run(["gh", "release", "list", "--limit", "500", "--json", "tagName"], check=False)
    if res.returncode != 0:
        return set()
    releases = json.loads(res.stdout or "[]")
    return {r["tagName"] for r in releases if r.get("tagName", "").startswith("nginx-")}


def backfill_missing_release(tag: str) -> List[Dict[str, Any]]:
    """Inspect tarballs in an existing nginx-* release that is absent from history.json."""
    res = run(["gh", "release", "view", tag, "--json", "assets"], check=False)
    if res.returncode != 0:
        return []
    assets = [a for a in json.loads(res.stdout or "{}").get("assets", []) if a["name"].endswith(".tar.gz")]
    if not assets:
        return []

    recovered: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for asset in assets:
            tb_path = tmp_dir / asset["name"]
            req = urllib.request.Request(asset["url"], headers={"User-Agent": "openappsec-attachment-ci/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                tb_path.write_bytes(resp.read())
            actual_sha = sha256_file(tb_path)
            meta: Optional[Dict[str, Any]] = None
            try:
                with tarfile.open(tb_path, "r:gz") as tf:
                    for member_name in ("./metadata.json", "metadata.json"):
                        try:
                            f = tf.extractfile(member_name)
                            if f is not None:
                                meta = json.loads(f.read().decode("utf-8"))
                                break
                        except KeyError:
                            continue
            except Exception as exc:
                print(f"::warning::Failed reading metadata from {asset['name']}: {exc}", file=sys.stderr)
                continue

            if not meta or "key" not in meta:
                continue
            meta["sha256"] = actual_sha
            meta["release_tag"] = tag
            meta["url"] = asset["url"]
            recovered.append(meta)
    return recovered


def upsert_release(tag: str, title: str, notes_file: Path, is_latest: bool = False) -> None:
    latest_flag = "--latest" if is_latest else "--latest=false"
    res = run(["gh", "release", "view", tag], check=False)
    if res.returncode != 0:
        run(["gh", "release", "create", tag, latest_flag, "--title", title, "--notes-file", str(notes_file)])
    else:
        run(["gh", "release", "edit", tag, latest_flag, "--title", title, "--notes-file", str(notes_file)])


def pages_base_url(repo_slug: str) -> str:
    owner, _, repo = repo_slug.partition("/")
    return f"https://{owner}.github.io/{repo}"


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

**What `install.sh` does:**
<details>
<summary>Expand steps (auto-detect, SHA-256 check, backup, nginx -t rollback)</summary>

1. Detects your OS, architecture, and installed `nginx` package version.
2. Downloads the matching `.tar.gz` and verifies its `sha256sum` against `index.json` (or `history.json`) before unpacking.
3. Backs up existing attachment `.so` files and `/etc/nginx/nginx.conf` (with automatic rollback on error).
4. Installs `ngx_cp_attachment_module.so` + helper libraries, ensures `load_module` is in `/etc/nginx/nginx.conf`, runs `nginx -t`, and reloads NGINX.

</details>

### Manual Verification & Install
```bash
sha256sum -c --ignore-missing SHA256SUMS
gh attestation verify openappsec-ngx-attachment_*.tar.gz --repo {repo_slug}
tar -xzf openappsec-ngx-attachment_*.tar.gz
```
View all SLSA build provenance records under [Attestations](https://github.com/{repo_slug}/attestations).

Then copy `lib/libngx_module.so` to `/usr/lib/nginx/modules/ngx_cp_attachment_module.so` (or `/usr/lib64/nginx/modules/` on RHEL/Rocky), copy the remaining `lib/*.so` to `/usr/lib/` (or `/usr/lib64/`), add `load_module .../ngx_cp_attachment_module.so;` to `/etc/nginx/nginx.conf`, verify with `nginx -t`, and reload NGINX.

### Included Targets
| Key | OS | Arch | NGINX Package | SHA-256 (prefix) |
| :--- | :--- | :--- | :--- | :--- |
{rows}
"""


def build_index_notes(repo_slug: str, branch: str, latest_entries: List[Dict[str, Any]], total_history: int) -> str:
    pages_url = pages_base_url(repo_slug)
    rows = "\n".join(
        f"| `{e['key']}` | `{e['distro']}` `{e['codename']}` | `{e['arch']}` | `{e['nginx_pkg_version']}` | `{e['sha256'][:16]}...` | [tar.gz]({e['url']}) |"
        for e in sorted(latest_entries, key=lambda x: x["key"])
    )
    return f"""Rolling index showing **{len(latest_entries)}** latest NGINX attachment builds (**{total_history}** total historical builds tracked in `history.json` / `supported-nginx.txt`).

* Searchable Web Index (GitHub Pages): {pages_url}/
* Plain-text full list: {pages_url}/supported-nginx.txt
* Latest JSON index: {pages_url}/index.json
* Full history JSON: {pages_url}/history.json
* Build Provenance Attestations: https://github.com/{repo_slug}/attestations

### Quick Install / Upgrade
```bash
curl -fsSL https://raw.githubusercontent.com/{repo_slug}/{branch}/install.sh | sudo bash
```

<details>
<summary>What <code>install.sh</code> does (auto-detect, SHA-256 check, backup, rollback)</summary>

1. Detects OS, arch, and installed `nginx` package version.
2. Downloads the matching `.tar.gz` and verifies SHA-256 before unpacking.
3. Backs up existing `.so` files and `nginx.conf` (with automatic rollback on error).
4. Installs the module, adds `load_module`, runs `nginx -t`, and reloads NGINX.

</details>

### List Available Builds
```bash
curl -fsSL https://raw.githubusercontent.com/{repo_slug}/{branch}/install.sh | bash -s -- --list
```

### Latest Supported Builds
| Key | OS | Arch | NGINX Package | SHA-256 (prefix) | Download |
| :--- | :--- | :--- | :--- | :--- | :--- |
{rows}
"""


def compute_latest_by_key(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for e in entries:
        key = e["key"]
        cand_rank = (version_key(e.get("nginx_pkg_version", "")), e.get("built_at", ""))
        prev = latest.get(key)
        if prev is None:
            latest[key] = e
        else:
            prev_rank = (version_key(prev.get("nginx_pkg_version", "")), prev.get("built_at", ""))
            if cand_rank >= prev_rank:
                latest[key] = e
    return sorted(latest.values(), key=lambda x: x["key"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifacts_dir", nargs="?", default="artifacts")
    parser.add_argument("--sync-only", action="store_true", help="Only prune/sync index & history against live GitHub Releases")
    args = parser.parse_args()

    artifacts_dir = Path(args.artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    repo_slug = os.environ["GITHUB_REPOSITORY"]
    branch = os.getenv("RELEASE_BRANCH") or os.getenv("GITHUB_REF_NAME") or "release-ci"

    tarballs = [] if args.sync_only else sorted(artifacts_dir.rglob("*.tar.gz"))
    if not args.sync_only and not tarballs:
        print("No built tarballs found; nothing to publish.")
        return 0

    existing_latest = fetch_json_asset(repo_slug, "index.json")
    existing_history = fetch_json_asset(repo_slug, "history.json")

    # Key history uniquely by (key, nginx_pkg_version)
    history_by_id: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for e in existing_latest.get("attachments", []) + existing_history.get("attachments", []):
        if "key" in e and "nginx_pkg_version" in e:
            history_by_id[(e["key"], e["nginx_pkg_version"])] = e

    # Process newly built tarballs from this workflow run
    by_release_tag: Dict[str, List[Tuple[Path, Dict[str, Any]]]] = {}
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
        history_by_id[(meta["key"], meta["nginx_pkg_version"])] = meta
        by_release_tag.setdefault(release_tag, []).append((tb, meta))

    # Upload new tarballs to their versioned GitHub Releases
    for tag, items in sorted(by_release_tag.items()):
        sums_dir = artifacts_dir / "_sums" / tag
        sums_dir.mkdir(parents=True, exist_ok=True)
        sums_file = sums_dir / "SHA256SUMS"
        notes_file = sums_dir / "RELEASE_NOTES.md"

        tag_entries = [e for e in history_by_id.values() if e.get("release_tag") == tag]
        notes_file.write_text(build_release_notes(repo_slug, branch, tag, tag_entries), encoding="utf-8")

        upsert_release(tag, f"open-appsec NGINX attachments for {tag}", notes_file, is_latest=False)

        existing_sums: Dict[str, str] = {}
        dl_res = run(["gh", "release", "download", tag, "-p", "SHA256SUMS", "-D", str(sums_dir), "--clobber"], check=False)
        if dl_res.returncode == 0 and sums_file.is_file():
            for line in sums_file.read_text(encoding="utf-8").splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2:
                    existing_sums[parts[1].lstrip("*")] = parts[0]

        upload_args: List[str] = []
        for tb, _ in items:
            existing_sums[tb.name] = sha256_file(tb)
            upload_args.append(str(tb))

        sums_lines = [f"{digest}  {name}" for name, digest in sorted(existing_sums.items())]
        sums_file.write_text("\n".join(sums_lines) + "\n", encoding="utf-8")
        upload_args.append(str(sums_file))

        run(["gh", "release", "upload", tag, "--clobber", *upload_args])

        assets_res = run(["gh", "release", "view", tag, "--json", "assets"])
        assets_by_name = {a["name"]: a["url"] for a in json.loads(assets_res.stdout).get("assets", [])}
        for tb, meta in items:
            if tb.name not in assets_by_name:
                raise RuntimeError(f"Uploaded asset {tb.name} not found on release {tag} (available: {list(assets_by_name)})")
            meta["url"] = assets_by_name[tb.name]
            history_by_id[(meta["key"], meta["nginx_pkg_version"])] = meta

    # Sync history against live GitHub Releases (prune deleted tags + backfill missing tags)
    live_tags = list_live_nginx_releases()
    if live_tags:
        pruned_ids = [hid for hid, e in history_by_id.items() if e.get("release_tag") not in live_tags]
        for hid in pruned_ids:
            print(f"Pruning deleted release entry: {hid}", file=sys.stderr)
            history_by_id.pop(hid, None)

        present_tags = {e.get("release_tag") for e in history_by_id.values()}
        missing_tags = sorted(live_tags - present_tags)
        for mtag in missing_tags:
            print(f"Backfilling entries from existing release {mtag}...", file=sys.stderr)
            for meta in backfill_missing_release(mtag):
                history_by_id[(meta["key"], meta["nginx_pkg_version"])] = meta

    all_history = sorted(
        history_by_id.values(),
        key=lambda e: (version_key(e.get("nginx_pkg_version", "")), e.get("built_at", "")),
        reverse=True,
    )
    latest_entries = compute_latest_by_key(all_history)

    if args.sync_only and not all_history:
        print("No live nginx-* releases found; skipping index update.")
        return 0

    # Publish rolling supported-index release
    index_dir = artifacts_dir / "_index"
    index_dir.mkdir(parents=True, exist_ok=True)

    index_json_path = index_dir / "index.json"
    index_json_path.write_text(
        json.dumps({"schema_version": 1, "attachments": latest_entries}, indent=2) + "\n",
        encoding="utf-8",
    )

    history_json_path = index_dir / "history.json"
    history_json_path.write_text(
        json.dumps({"schema_version": 1, "attachments": all_history}, indent=2) + "\n",
        encoding="utf-8",
    )

    txt_lines = ["# key | distro | codename | arch | nginx_pkg_version | sha256 | url"]
    for e in all_history:
        txt_lines.append(
            f"{e['key']} | {e['distro']} | {e['codename']} | {e['arch']} | {e['nginx_pkg_version']} | {e['sha256']} | {e['url']}"
        )
    supported_txt_path = index_dir / "supported-nginx.txt"
    supported_txt_path.write_text("\n".join(txt_lines) + "\n", encoding="utf-8")

    index_notes_path = index_dir / "INDEX_NOTES.md"
    index_notes_path.write_text(build_index_notes(repo_slug, branch, latest_entries, len(all_history)), encoding="utf-8")

    upsert_release("supported-index", "Supported NGINX Attachments Index", index_notes_path, is_latest=True)
    run(
        [
            "gh", "release", "upload", "supported-index", "--clobber",
            str(index_json_path), str(history_json_path), str(supported_txt_path),
        ]
    )

    # Stage static files for GitHub Pages
    pages_dir = artifacts_dir / "_pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(index_json_path, pages_dir / "index.json")
    shutil.copy2(history_json_path, pages_dir / "history.json")
    shutil.copy2(supported_txt_path, pages_dir / "supported-nginx.txt")

    template_path = Path(__file__).with_name("pages_index.html")
    html = template_path.read_text(encoding="utf-8")
    html = html.replace("{{REPO}}", repo_slug).replace("{{BRANCH}}", branch)
    (pages_dir / "index.html").write_text(html, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
