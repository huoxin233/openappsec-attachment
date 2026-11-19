#!/usr/bin/env bash
set -euo pipefail

# Installs or upgrades the open-appsec NGINX dynamic attachment module with SHA-256 verification.
# Override repo via: OPENAPPSEC_ATTACHMENT_REPO="owner/repo" or --repo owner/repo

DEFAULT_REPO="${OPENAPPSEC_ATTACHMENT_REPO:-huoxin233/openappsec-attachment}"
ACTION="install"
CHANNEL=""
KEY_OVERRIDE=""
DRY_RUN="false"

usage() {
    cat <<'EOF'
Usage: install.sh --repo <owner/repo> [options]

Options:
  --repo <owner/repo>   GitHub repository hosting releases (or set OPENAPPSEC_ATTACHMENT_REPO)
  --channel <name>      Filter by nginx.org channel (mainline or stable)
  --key <target-key>    Explicitly select a build key from index.json
  --list                List all available prebuilt attachments in index.json
  --dry-run             Verify match and SHA-256 without modifying system files
  -h, --help            Show this help message
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --repo)
            DEFAULT_REPO="${2:?Missing value for --repo}"
            shift 2
            ;;
        --channel)
            CHANNEL="${2:?Missing value for --channel}"
            shift 2
            ;;
        --key)
            KEY_OVERRIDE="${2:?Missing value for --key}"
            shift 2
            ;;
        --list)
            ACTION="list"
            shift
            ;;
        --dry-run)
            DRY_RUN="true"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            exit 1
            ;;
    esac
done

if [ -z "${DEFAULT_REPO}" ]; then
    echo "ERROR: GitHub repository not specified. Pass --repo <owner/repo> or set OPENAPPSEC_ATTACHMENT_REPO." >&2
    exit 1
fi

for cmd in curl python3 tar sha256sum; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        echo "ERROR: Required command '${cmd}' not found." >&2
        exit 1
    fi
done

INDEX_URL="https://github.com/${DEFAULT_REPO}/releases/download/supported-index/index.json"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "${TMP_DIR}"' EXIT

curl -fsSL -o "${TMP_DIR}/index.json" "${INDEX_URL}"

if [ "${ACTION}" = "list" ]; then
    python3 - "${TMP_DIR}/index.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
print(f"{'KEY':<36} {'NGINX_PKG_VERSION':<24} {'SHA256 (prefix)'}")
for e in data.get("attachments", []):
    print(f"{e['key']:<36} {e['nginx_pkg_version']:<24} {e['sha256'][:16]}")
PY
    exit 0
fi

if [ "$(id -u)" != "0" ] && [ "${DRY_RUN}" != "true" ]; then
    echo "ERROR: Please run as root (or via sudo) to install into /usr/lib* and reload nginx." >&2
    exit 1
fi

if ! command -v nginx >/dev/null 2>&1; then
    echo "ERROR: nginx binary not found in PATH." >&2
    exit 1
fi

case "$(uname -m)" in
    x86_64|amd64) ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *) echo "ERROR: Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

# Detect OS & codename
. /etc/os-release
DISTRO="${ID}"
CODENAME="${VERSION_CODENAME:-}"
case "${DISTRO}" in
    rhel|rocky|almalinux|centos)
        DISTRO="rocky"
        MAJOR="${VERSION_ID%%.*}"
        CODENAME="el${MAJOR}"
        ;;
    alpine)
        MINOR="${VERSION_ID%.*}"
        CODENAME="v${MINOR}"
        ;;
esac

# Detect installed NGINX package version
NGINX_PKG_VER=""
if command -v dpkg-query >/dev/null 2>&1; then
    NGINX_PKG_VER=$(dpkg-query -W -f='${Version}' nginx 2>/dev/null || true)
elif command -v rpm >/dev/null 2>&1; then
    NGINX_PKG_VER=$(rpm -q --qf '%{VERSION}-%{RELEASE}' nginx 2>/dev/null || true)
elif command -v apk >/dev/null 2>&1; then
    NGINX_PKG_VER=$(apk info -v nginx 2>/dev/null | sed -n 's/^nginx-//p' | head -n1 || true)
fi

NGINX_SHORT_VER=$(nginx -v 2>&1 | sed -n 's/.*nginx\/\([0-9.]*\).*/\1/p')

MATCH_INFO=$(python3 - "${TMP_DIR}/index.json" "${DISTRO}" "${CODENAME}" "${ARCH}" "${NGINX_PKG_VER}" "${NGINX_SHORT_VER}" "${CHANNEL}" "${KEY_OVERRIDE}" <<'PY'
import json, sys
index_path, distro, codename, arch, pkg_ver, short_ver, channel, key_override = sys.argv[1:]
items = json.load(open(index_path)).get("attachments", [])

if key_override:
    matches = [e for e in items if e.get("key") == key_override]
else:
    matches = [
        e for e in items
        if e.get("distro") == distro
        and e.get("codename") == codename
        and e.get("arch") == arch
        and (not channel or e.get("channel") == channel)
        and (
            (pkg_ver and e.get("nginx_pkg_version") == pkg_ver)
            or (not pkg_ver and e.get("nginx_version") == short_ver)
        )
    ]

if not matches:
    sys.stderr.write(
        f"ERROR: No matching attachment build found for distro={distro} codename={codename} "
        f"arch={arch} pkg_ver={pkg_ver or 'unknown'} nginx={short_ver}\n"
        f"Run with --list to inspect available builds.\n"
    )
    sys.exit(1)

if len(matches) > 1:
    # Prefer exact pkg_ver match if multiple channels exist
    exact = [e for e in matches if e.get("nginx_pkg_version") == pkg_ver]
    if len(exact) == 1:
        matches = exact
    else:
        keys = ", ".join(e["key"] for e in matches)
        sys.stderr.write(f"ERROR: Multiple builds matched ({keys}). Re-run with --channel or --key.\n")
        sys.exit(1)

e = matches[0]
print(f"{e['key']}|{e['url']}|{e['sha256']}|{e['nginx_pkg_version']}")
PY
)

IFS='|' read -r MATCH_KEY MATCH_URL MATCH_SHA256 MATCH_PKG_VER <<< "${MATCH_INFO}"
echo "Matched build: ${MATCH_KEY} (nginx ${MATCH_PKG_VER})"

TARBALL_PATH="${TMP_DIR}/attachment.tar.gz"
curl -fsSL -o "${TARBALL_PATH}" "${MATCH_URL}"
ACTUAL_SHA256=$(sha256sum "${TARBALL_PATH}" | awk '{print $1}')

if [ "${ACTUAL_SHA256}" != "${MATCH_SHA256}" ]; then
    echo "ERROR: SHA-256 verification failed!" >&2
    echo "  Expected: ${MATCH_SHA256}" >&2
    echo "  Actual:   ${ACTUAL_SHA256}" >&2
    exit 1
fi
echo "Verified SHA-256: ${ACTUAL_SHA256}"

if [ "${DRY_RUN}" = "true" ]; then
    echo "Dry run complete; no files modified."
    exit 0
fi

UNPACK_DIR="${TMP_DIR}/unpack"
mkdir -p "${UNPACK_DIR}"
tar -xzf "${TARBALL_PATH}" -C "${UNPACK_DIR}"

if [ -d /usr/lib64/nginx/modules ] || [ "${DISTRO}" = "rocky" ]; then
    LIBDIR="/usr/lib64"
else
    LIBDIR="/usr/lib"
fi
MOD_DIR="${LIBDIR}/nginx/modules"
mkdir -p "${MOD_DIR}"

BACKUP_DIR="${TMP_DIR}/backup"
mkdir -p "${BACKUP_DIR}"
for f in \
    "${MOD_DIR}/ngx_cp_attachment_module.so" \
    "${LIBDIR}/libosrc_compression_utils.so" \
    "${LIBDIR}/libosrc_nginx_attachment_util.so" \
    "${LIBDIR}/libosrc_shmem_ipc.so" \
    "${LIBDIR}/libshmem_ipc_2.so"; do
    if [ -f "${f}" ]; then
        cp -a "${f}" "${BACKUP_DIR}/"
    fi
done
[ -f /etc/nginx/nginx.conf ] && cp -a /etc/nginx/nginx.conf "${BACKUP_DIR}/nginx.conf"

rollback() {
    echo "ERROR: Installation failed; restoring previous attachment files..." >&2
    for f in "${BACKUP_DIR}"/*.so; do
        [ -e "${f}" ] || continue
        base=$(basename "${f}")
        if [ "${base}" = "ngx_cp_attachment_module.so" ]; then
            cp -a "${f}" "${MOD_DIR}/"
        else
            cp -a "${f}" "${LIBDIR}/"
        fi
    done
    [ -f "${BACKUP_DIR}/nginx.conf" ] && cp -a "${BACKUP_DIR}/nginx.conf" /etc/nginx/nginx.conf
    command -v ldconfig >/dev/null 2>&1 && ldconfig || true
}

trap 'rollback' ERR

install -m 0755 "${UNPACK_DIR}/lib/libosrc_compression_utils.so" "${LIBDIR}/libosrc_compression_utils.so"
install -m 0755 "${UNPACK_DIR}/lib/libosrc_nginx_attachment_util.so" "${LIBDIR}/libosrc_nginx_attachment_util.so"
install -m 0755 "${UNPACK_DIR}/lib/libosrc_shmem_ipc.so" "${LIBDIR}/libosrc_shmem_ipc.so"
install -m 0755 "${UNPACK_DIR}/lib/libshmem_ipc_2.so" "${LIBDIR}/libshmem_ipc_2.so"
install -m 0755 "${UNPACK_DIR}/lib/libngx_module.so" "${MOD_DIR}/ngx_cp_attachment_module.so"

# Install bundled brotli shared libs only if missing from system libdir
for brotli_lib in "${UNPACK_DIR}"/lib/libbrotli*.so*; do
    [ -e "${brotli_lib}" ] || continue
    base=$(basename "${brotli_lib}")
    if [ ! -e "${LIBDIR}/${base}" ]; then
        cp -a "${brotli_lib}" "${LIBDIR}/"
    fi
done

command -v ldconfig >/dev/null 2>&1 && ldconfig || true

if [ -f /etc/nginx/nginx.conf ]; then
    if ! grep -qE 'load_module\s+.*ngx_cp_attachment_module\.so;' /etc/nginx/nginx.conf; then
        sed -i "1i load_module ${MOD_DIR}/ngx_cp_attachment_module.so;" /etc/nginx/nginx.conf
    fi
fi

nginx -t
trap - ERR

if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet nginx; then
    systemctl reload nginx
else
    nginx -s reload || true
fi

echo "Successfully installed ${MATCH_KEY} (${MATCH_PKG_VER})."
