#!/bin/bash
set -eo pipefail

NGINX_VERSION_CONF_INPUT_PATH=/tmp/nginx.sourcefile.ver
if [ "${1}" == "--docker" ]; then
    docker run --rm "${2}" nginx -V > "${NGINX_VERSION_CONF_INPUT_PATH}" 2>&1
elif [ "${1}" == "--conf" ]; then
    cp "${2}" "${NGINX_VERSION_CONF_INPUT_PATH}"
else
    echo "Usage: ${0} [--conf <input nginx compilation flags file> | --docker <input docker name>] <compilation artifacts directory>"
    exit 1
fi
NGINX_VERSION_CONF_OUTPUT_PATH=/tmp/nginx.sourcefile.conf
if command -v dos2unix >/dev/null 2>&1; then
    dos2unix "${NGINX_VERSION_CONF_INPUT_PATH}"
else
    tr -d '\r' < "${NGINX_VERSION_CONF_INPUT_PATH}" > "${NGINX_VERSION_CONF_INPUT_PATH}.tmp" && mv "${NGINX_VERSION_CONF_INPUT_PATH}.tmp" "${NGINX_VERSION_CONF_INPUT_PATH}"
fi
"$(dirname "$0")/nginx_version_extractor.sh" -i "${NGINX_VERSION_CONF_INPUT_PATH}" -o "${NGINX_VERSION_CONF_OUTPUT_PATH}"
BUILD_OUTPUT_DIR=${3}

if [[ ${BUILD_OUTPUT_DIR} != /* ]]; then
    BUILD_OUTPUT_DIR=$(pwd)/${BUILD_OUTPUT_DIR}
fi

source "${NGINX_VERSION_CONF_OUTPUT_PATH}"

if [[ ! "${NGINX_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Invalid NGINX_VERSION parsed: '${NGINX_VERSION}'"
    exit 1
fi

CURRENT_PWD=$(pwd)

mkdir -p "${BUILD_OUTPUT_DIR}"
cd "${BUILD_OUTPUT_DIR}"

TARBALL="nginx-${NGINX_VERSION}.tar.gz"
ASC_FILE="${TARBALL}.asc"
TARBALL_URL="https://nginx.org/download/${TARBALL}"
ASC_URL="https://nginx.org/download/${ASC_FILE}"

rm -rf "${TARBALL}" "${ASC_FILE}" nginx-src "nginx-${NGINX_VERSION}"

# Download using strict TLS certificate verification (never --no-check-certificate)
if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "${TARBALL}" "${TARBALL_URL}"
    curl -fsSL -o "${ASC_FILE}" "${ASC_URL}"
else
    wget -q -O "${TARBALL}" "${TARBALL_URL}"
    wget -q -O "${ASC_FILE}" "${ASC_URL}"
fi

ACTUAL_SHA256=$(sha256sum "${TARBALL}" | awk '{print $1}')
echo "Downloaded ${TARBALL} (sha256: ${ACTUAL_SHA256})"

if [ -n "${NGINX_TARBALL_SHA256:-}" ]; then
    if [ "${ACTUAL_SHA256}" != "${NGINX_TARBALL_SHA256}" ]; then
        echo "ERROR: SHA-256 mismatch for ${TARBALL}: expected ${NGINX_TARBALL_SHA256}, got ${ACTUAL_SHA256}"
        exit 1
    fi
    echo "Verified ${TARBALL} against expected SHA-256."
fi

if [ "${SKIP_NGINX_GPG_VERIFY:-false}" != "true" ]; then
    if ! command -v gpg >/dev/null 2>&1; then
        echo "ERROR: gpg is required to verify ${TARBALL}. Set SKIP_NGINX_GPG_VERIFY=true only if intentionally bypassing."
        exit 1
    fi
    GNUPGHOME_TMP=$(mktemp -d)
    trap 'rm -rf "${GNUPGHOME_TMP}"' EXIT
    export GNUPGHOME="${GNUPGHOME_TMP}"
    chmod 700 "${GNUPGHOME}"

    if [ -n "${NGINX_PGP_KEYS_DIR:-}" ] && [ -d "${NGINX_PGP_KEYS_DIR}" ]; then
        gpg --batch --import "${NGINX_PGP_KEYS_DIR}"/*.key
    else
        # Fetch official NGINX maintainer PGP keys over verified HTTPS
        for key in arut pluknet sb thresh; do
            if command -v curl >/dev/null 2>&1; then
                curl -fsSL -o "${GNUPGHOME}/${key}.key" "https://nginx.org/keys/${key}.key" || true
            else
                wget -q -O "${GNUPGHOME}/${key}.key" "https://nginx.org/keys/${key}.key" || true
            fi
            if [ -s "${GNUPGHOME}/${key}.key" ]; then
                gpg --batch --import "${GNUPGHOME}/${key}.key"
            fi
        done
    fi

    echo "Verifying PGP signature ${ASC_FILE} for ${TARBALL}..."
    gpg --batch --verify "${ASC_FILE}" "${TARBALL}"
    echo "PGP signature verification succeeded for ${TARBALL}."
fi

tar -xzf "${TARBALL}"
rm -f "${TARBALL}" "${ASC_FILE}"

mv "nginx-${NGINX_VERSION}" nginx-src
cd nginx-src

if test ! -f configured.ok; then
    echo "Configuring nginx compiler: ./configure ${CONFIGURE_OPT} --with-cc-opt=\"${EXTRA_CC_OPT} ${LOCAL_CC_OPT}\" && touch configured.ok"
    ./configure ${CONFIGURE_OPT} --with-cc-opt="${EXTRA_CC_OPT} ${LOCAL_CC_OPT}" && touch configured.ok
else 
    echo "Nginx compiler already Configured...\n"
fi

if test ! -f configured.ok; then
    echo "Failed to configure NGINX source code"
    exit 1
fi

make && echo "${EXTRA_CC_OPT}" > cc_flags.mk
if [[ $? != 0 ]]; then
    echo "Failed to build NGINX source code"
    exit 1
fi

OUTPUT_FILE_NAME=include_paths.mk

ALL_INCS=$(cat objs/Makefile|grep -v \$\(ALL_INCS\) | awk '/ALL_INCS/' RS="\n\n" ORS="\n\n" | tr '\\' ' ')
ALL_INCS=${ALL_INCS/ALL_INCS =/}
ALL_INCS=$(sed "s/-I//g" <<< "${ALL_INCS}")

echo > include_paths.mk
for include in ${ALL_INCS}; do
    echo $include >> include_paths.mk
done

cd ${CURRENT_PWD}

if [ "${1}" == "--docker" ]; then
    cp -f $(dirname $0)/../../../docker/Dockerfile ${BUILD_OUTPUT_DIR}/Dockerfile
    sed -i "s|<DOCKER BASE IMAGE>|${2}|g" ${BUILD_OUTPUT_DIR}/Dockerfile
    docker run -it ${2} whoami > /tmp/usertouse
    USER_NAME="$(cat /tmp/usertouse)"
    rm /tmp/usertouse
    sed -i "s|<DOCKER USER>|${USER_NAME}|g" ${BUILD_OUTPUT_DIR}/Dockerfile
fi
