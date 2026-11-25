#!/usr/bin/env bash
set -euo pipefail

# Expected env vars:
#   TARGET_KEY, TARGET_ID, DISTRO, CODENAME, CHANNEL, PKG_TYPE, REPO_URL, ARCH, PKG_VERSION, NGINX_VERSION
#   ATTACHMENT_COMMIT

WORKDIR="$(pwd)"
OUT_DIR="${WORKDIR}/dist"
BUILD_OUT="${WORKDIR}/build_out"
mkdir -p "${OUT_DIR}"

fetch_verified_nginx_signing_key() {
    local dest_key="$1"
    local tmp_gpg
    tmp_gpg=$(mktemp -d)
    chmod 700 "${tmp_gpg}"
    curl -fsSL -o "${dest_key}" https://nginx.org/keys/nginx_signing.key

    # Verify imported key contains only official NGINX signing key fingerprints
    local fprs
    fprs=$(GNUPGHOME="${tmp_gpg}" gpg --batch --show-keys --with-colons "${dest_key}" | awk -F: '$1 == "fpr" {print $10}')
    rm -rf "${tmp_gpg}"
    if [ -z "${fprs}" ]; then
        echo "ERROR: No PGP fingerprints found in nginx_signing.key" >&2
        return 1
    fi
    if ! printf '%s\n' "${fprs}" | grep -Eq '^(573BFD6B3D8FBC641079A6ABABF5BD827BD9BF62|8540A6F18833A80E9C1653A42FD21310B49F6B46|9E9BE90EACBCDE69FE9B204CBCDCD8A38D88A2B3)$'; then
        echo "ERROR: nginx_signing.key failed pinned fingerprint verification" >&2
        return 1
    fi
}

install_deb_deps() {
    export DEBIAN_FRONTEND=noninteractive
    APT_OPTS="-o Acquire::Retries=5 -o Acquire::http::Timeout=20 -o APT::Update::Error-Mode=any"

    switch_ubuntu_mirror() {
        if [ "${DISTRO}" = "ubuntu" ] && [ "${ARCH}" = "amd64" ]; then
            for src_file in /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources; do
                if [ -f "${src_file}" ]; then
                    sed -i 's|http://archive.ubuntu.com/ubuntu|http://azure.archive.ubuntu.com/ubuntu|g; s|http://security.ubuntu.com/ubuntu|http://azure.archive.ubuntu.com/ubuntu|g' "${src_file}"
                fi
            done
        fi
    }

    apt_update_retry() {
        if ! apt-get ${APT_OPTS} update; then
            switch_ubuntu_mirror
            sleep 5
            apt-get ${APT_OPTS} update
        fi
    }

    apt_install_retry() {
        local attempt
        for attempt in 1 2 3; do
            if apt-get ${APT_OPTS} install -y --no-install-recommends "$@"; then
                return 0
            fi
            echo "apt-get install failed (attempt ${attempt}/3); switching mirror/retrying..." >&2
            switch_ubuntu_mirror
            sleep 10
            apt-get ${APT_OPTS} update || true
        done
        return 1
    }

    # On GitHub Actions amd64 runners, Azure mirror avoids archive.ubuntu.com timeouts
    switch_ubuntu_mirror
    apt_update_retry
    apt_install_retry \
        ca-certificates curl wget gnupg2 lsb-release \
        build-essential cmake pkg-config python3 dos2unix \
        libpcre2-dev libxml2-dev zlib1g-dev libssl-dev libgeoip-dev libbrotli-dev
    apt_install_retry libpcre3-dev || true

    install -d -m 0755 /etc/apt/keyrings
    fetch_verified_nginx_signing_key /tmp/nginx_signing.key
    gpg --batch --dearmor -o /etc/apt/keyrings/nginx-archive-keyring.gpg /tmp/nginx_signing.key
    rm -f /tmp/nginx_signing.key
    chmod 0644 /etc/apt/keyrings/nginx-archive-keyring.gpg
    echo "deb [signed-by=/etc/apt/keyrings/nginx-archive-keyring.gpg] ${REPO_URL} ${CODENAME} nginx" > /etc/apt/sources.list.d/nginx.list
    cat <<'EOF' > /etc/apt/preferences.d/99nginx
Package: *
Pin: origin nginx.org
Pin-Priority: 900
EOF
    apt_update_retry
    apt_install_retry "nginx=${PKG_VERSION}"
}

install_rpm_deps() {
    dnf install -y --allowerasing 'dnf-command(config-manager)' ca-certificates curl wget gnupg2 \
        gcc gcc-c++ make cmake pkgconfig python3 \
        pcre2-devel libxml2-devel zlib-devel openssl-devel brotli-devel
    dnf install -y --allowerasing dos2unix pcre-devel || true

    # EPEL provides GeoIP-devel on Rocky/RHEL 9/10 if needed by configure flags
    dnf install -y epel-release || true
    dnf install -y GeoIP-devel || true

    fetch_verified_nginx_signing_key /tmp/nginx_signing.key
    rpm --import /tmp/nginx_signing.key
    cat <<EOF > /etc/yum.repos.d/nginx.repo
[nginx-target]
name=nginx target repo
baseurl=${REPO_URL}/\$basearch/
gpgcheck=1
enabled=1
gpgkey=file:///tmp/nginx_signing.key
module_hotfixes=true
EOF
    dnf install -y "nginx-${PKG_VERSION}"
    rm -f /tmp/nginx_signing.key
}

install_apk_deps() {
    apk update
    apk add --no-cache \
        ca-certificates curl wget gnupg bash dos2unix \
        build-base cmake pkgconfig python3 linux-headers \
        pcre-dev pcre2-dev libxml2-dev zlib-dev openssl-dev geoip-dev brotli-dev

    curl -fsSL -o /etc/apk/keys/nginx_signing.rsa.pub https://nginx.org/keys/nginx_signing.rsa.pub
    echo "${REPO_URL}" >> /etc/apk/repositories
    apk update
    apk add --no-cache "nginx=${PKG_VERSION}"
}

case "${PKG_TYPE}" in
    deb) install_deb_deps ;;
    rpm) install_rpm_deps ;;
    apk) install_apk_deps ;;
    *)
        echo "Unsupported PKG_TYPE: ${PKG_TYPE}" >&2
        exit 1
        ;;
esac

nginx -V &> /tmp/nginx.ver
cat /tmp/nginx.ver

export MAKEFLAGS="-j$(nproc)"
chmod +x ./attachments/nginx/ngx_module/nginx_version_configuration.sh ./attachments/nginx/ngx_module/nginx_version_extractor.sh
./attachments/nginx/ngx_module/nginx_version_configuration.sh --conf /tmp/nginx.ver "${BUILD_OUT}"

cmake -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_INSTALL_PREFIX="${BUILD_OUT}" .
make -j"$(nproc)"
make install

# Smoke test: verify compiled module loads cleanly into the target NGINX binary
if [ -d /usr/lib64/nginx/modules ] || [ "${PKG_TYPE}" = "rpm" ]; then
    LIBDIR="/usr/lib64"
else
    LIBDIR="/usr/lib"
fi
mkdir -p "${LIBDIR}/nginx/modules"

cp "${BUILD_OUT}/nginx_attachment/lib/libosrc_compression_utils.so" "${LIBDIR}/libosrc_compression_utils.so"
cp "${BUILD_OUT}/nginx_attachment/lib/libosrc_nginx_attachment_util.so" "${LIBDIR}/libosrc_nginx_attachment_util.so"
cp "${BUILD_OUT}/nginx_attachment/lib/libosrc_shmem_ipc.so" "${LIBDIR}/libosrc_shmem_ipc.so"
cp "${BUILD_OUT}/nginx_attachment/lib/libshmem_ipc_2.so" "${LIBDIR}/libshmem_ipc_2.so"
cp "${BUILD_OUT}/nginx_attachment/lib/libngx_module.so" "${LIBDIR}/nginx/modules/ngx_cp_attachment_module.so"
command -v ldconfig >/dev/null 2>&1 && ldconfig || true

if [ -f /etc/nginx/nginx.conf ]; then
    sed -i "1i load_module ${LIBDIR}/nginx/modules/ngx_cp_attachment_module.so;" /etc/nginx/nginx.conf
fi
nginx -t

# Package clean tarball without leaking full nginx-src
STAGE_DIR="${WORKDIR}/pkg_stage"
rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}/lib"
cp -a "${BUILD_OUT}/nginx_attachment/lib/"* "${STAGE_DIR}/lib/"
cp "${BUILD_OUT}/nginx_attachment/install-nginx-attachment.sh" "${STAGE_DIR}/install-nginx-attachment.sh"
cp /tmp/nginx.ver "${STAGE_DIR}/nginx-V.txt"

SAFE_PKG_VER=$(printf '%s' "${PKG_VERSION}" | sed 's/[^A-Za-z0-9._-]/./g')
TARBALL_NAME="openappsec-ngx-attachment_${SAFE_PKG_VER}_${DISTRO}-${CODENAME}-${CHANNEL}_${ARCH}.tar.gz"
BUILD_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

cat <<EOF > "${STAGE_DIR}/metadata.json"
{
  "key": "${TARGET_KEY}",
  "target_id": "${TARGET_ID}",
  "distro": "${DISTRO}",
  "codename": "${CODENAME}",
  "channel": "${CHANNEL}",
  "pkg_type": "${PKG_TYPE}",
  "arch": "${ARCH}",
  "nginx_version": "${NGINX_VERSION}",
  "nginx_pkg_version": "${PKG_VERSION}",
  "attachment_commit": "${ATTACHMENT_COMMIT}",
  "built_at": "${BUILD_TIME}",
  "tarball": "${TARBALL_NAME}"
}
EOF

tar -C "${STAGE_DIR}" -czf "${OUT_DIR}/${TARBALL_NAME}" .
cp "${STAGE_DIR}/metadata.json" "${OUT_DIR}/${TARBALL_NAME}.metadata.json"

(
    cd "${OUT_DIR}"
    sha256sum "${TARBALL_NAME}" > "${TARBALL_NAME}.sha256"
    sha256sum -c "${TARBALL_NAME}.sha256"
)

echo "Built and verified ${OUT_DIR}/${TARBALL_NAME}"
