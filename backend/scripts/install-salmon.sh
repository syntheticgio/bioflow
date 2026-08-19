#!/bin/sh
# Install Salmon 1.12.1 from upstream COMBINE-lab release tarballs per architecture.
#
# THE VENDORED UPSTREAM BINARY PATTERN IS LOAD-BEARING FOR ARM64.
#
# Debian trixie's packaged `salmon` (1.10.2+ds1-1+b5) on arm64 crashes with SIGILL
# ("Illegal instruction", exit code 132) during `salmon quant` immediately after
# initializing decoys, because the Debian package daemon compiled with CPU extension
# flags not exposed by Docker Desktop VM hypervisors or generic ARM cloud instances.
# See https://github.com/syntheticgio/bioflow/issues/667.
#
# COMBINE-lab publishes prebuilt `aarch64-unknown-linux-gnu` and `x86_64-unknown-linux-gnu`
# binaries starting with v1.11.0+. We vendor v1.12.1 for both architectures.
#
# CHECKSUMS ARE PINNED HERE, NOT FETCHED.

set -eu

SALMON_VERSION="${SALMON_VERSION:-1.12.1}"
INSTALL_DIR="/opt/salmon"
BASE="https://github.com/COMBINE-lab/salmon/releases/download/v${SALMON_VERSION}"

X86_64_SHA256="00900135ecca10b45e3d78a6ab64463f957d0b2b0069eaa078c10784f1e2f8d6"
AARCH64_SHA256="fccee6d68d72ad3f5afdc2d9c54b84c5b66b175005e9a97763d75d25722a3017"

cd /tmp

case "$(uname -m)" in
    x86_64)
        TARBALL="salmon-linux-x86_64.tar.gz"
        EXPECTED_SHA256="${X86_64_SHA256}"
        ;;
    aarch64|arm64)
        TARBALL="salmon-linux-aarch64.tar.gz"
        EXPECTED_SHA256="${AARCH64_SHA256}"
        ;;
    *)
        echo "unsupported arch: $(uname -m)" >&2
        exit 1
        ;;
esac

apt-get update
apt-get install -y --no-install-recommends locales
if [ -f /etc/locale.gen ]; then
    echo "en_US.UTF-8 UTF-8" >> /etc/locale.gen
    locale-gen
fi

echo "Fetching ${TARBALL} (Salmon ${SALMON_VERSION})..."
curl -fsSL -O "${BASE}/${TARBALL}"
echo "${EXPECTED_SHA256}  ${TARBALL}" > "${TARBALL}.sha256"
sha256sum -c "${TARBALL}.sha256"

mkdir -p "${INSTALL_DIR}"
tar -xzf "${TARBALL}" -C "${INSTALL_DIR}" --strip-components=1
rm -f "${TARBALL}" "${TARBALL}.sha256"

# Create a wrapper script at /usr/local/bin/salmon
printf '#!/bin/sh\nexec "%s/bin/salmon" "$@"\n' "${INSTALL_DIR}" > /usr/local/bin/salmon
chmod +x /usr/local/bin/salmon

# Verify binary execution and version output at build time
INSTALLED="$(/usr/local/bin/salmon --version 2>&1 | head -1)"
echo "Installed: ${INSTALLED}"
case "${INSTALLED}" in
    *"${SALMON_VERSION}"*) ;;
    *) echo "expected Salmon ${SALMON_VERSION}, got: ${INSTALLED}" >&2; exit 1 ;;
esac
