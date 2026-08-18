#!/bin/sh
# Install SPAdes 4.3.0, per architecture.
#
# THE ARCHITECTURE SPLIT IS LOAD-BEARING. Read this before simplifying it.
#
# SPAdes ships exactly one Linux release asset, `SPAdes-4.3.0-Linux.tar.gz`,
# and it is x86-64 ONLY -- verified 2026-08-18 by reading its ELF header:
#   spades-core: ELF 64-bit LSB executable, x86-64
# The macOS assets are arch-qualified (Darwin-arm64 / Darwin-x86_64); the
# Linux one is not, and upstream's install docs list compatible *distributions*
# with no architecture caveat, which is why this needs checking rather than
# reading. Vendoring that tarball alone leaves arm64 -- half of what
# release.yml publishes -- with no SPAdes at all.
#
# SPAdes does support ARM from source: upstream issue #1062 ("Support Apple
# m1") is closed, and 4.3.0's release notes fix an ARM/Linux GFA bug. The
# arm64 branch below builds it, verified 2026-08-18 at 124s on 24 cores with
# no patches -- unlike bwa-mem2, no sse2neon translation is needed.
#
# THE VERSION PIN IS LOAD-BEARING: 4.3.0 is the release carrying that
# ARM/Linux fix. Relaxing it to a floor reintroduces a fixed bug.
#
# CHECKSUMS ARE PINNED HERE, NOT FETCHED. Unlike meryl, SPAdes publishes no
# SHA256SUMS asset -- verified 2026-08-18, the release has four assets, none
# checksum-shaped. A hash committed in a reviewed script is stronger than one
# downloaded from beside the tarball anyway. NOTE: a version bump means
# updating THREE constants below, not one. A stale hash fails this script
# loudly, which is the intended failure.

set -eu

SPADES_VERSION="${SPADES_VERSION:-4.3.0}"
INSTALL_DIR="/opt/spades"
BASE="https://github.com/ablab/spades/releases/download/v${SPADES_VERSION}"

BINARY_SHA256="e88a8c533c8614dd4b7c5788cfcd46427848a0575267f97c690a75fd2a343034"
SOURCE_SHA256="09671ca39f9c6d2479d9fc168100bfd089b4a24002d51b815386d2b24d424456"

apt-get update
apt-get install -y --no-install-recommends curl ca-certificates

cd /tmp

case "$(uname -m)" in
    x86_64)
        TARBALL="SPAdes-${SPADES_VERSION}-Linux.tar.gz"
        echo "Fetching ${TARBALL} (prebuilt, amd64)..."
        curl -fsSL -O "${BASE}/${TARBALL}"
        echo "${BINARY_SHA256}  ${TARBALL}" > "${TARBALL}.sha256"
        sha256sum -c "${TARBALL}.sha256"
        mkdir -p "${INSTALL_DIR}"
        tar -xzf "${TARBALL}" -C "${INSTALL_DIR}" --strip-components=1
        rm -f "${TARBALL}" "${TARBALL}.sha256"
        BUILD_PACKAGES=""
        ;;
    aarch64|arm64)
        TARBALL="SPAdes-${SPADES_VERSION}.tar.gz"
        echo "Fetching ${TARBALL} (source, arm64 has no published binary)..."
        curl -fsSL -O "${BASE}/${TARBALL}"
        echo "${SOURCE_SHA256}  ${TARBALL}" > "${TARBALL}.sha256"
        sha256sum -c "${TARBALL}.sha256"
        BUILD_PACKAGES="g++ cmake make zlib1g-dev libbz2-dev"
        apt-get install -y --no-install-recommends ${BUILD_PACKAGES}
        tar -xzf "${TARBALL}"
        cd "SPAdes-${SPADES_VERSION}"
        PREFIX="${INSTALL_DIR}" ./spades_compile.sh
        cd /tmp
        rm -rf "SPAdes-${SPADES_VERSION}" "${TARBALL}" "${TARBALL}.sha256"
        ;;
    *)
        echo "unsupported arch: $(uname -m)" >&2
        exit 1
        ;;
esac

# Neither ca-certificates nor curl is purged here: both are installed
# persistently in the base tool layer near the top of the Dockerfile and
# stay in place until Node setup purges curl much later. Purging either one
# here broke a downstream layer needing HTTPS silently once already -- see
# the bwa-mem2 block in the Dockerfile, which established not purging them.
apt-get purge -y ${BUILD_PACKAGES}
apt-get autoremove -y
apt-get clean
rm -rf /var/lib/apt/lists/*

# A wrapper, not a symlink. spades.py locates its sibling binaries
# (spades-core, spades-hammer) relative to its own path, so a symlink into
# /usr/local/bin sends it looking for them there -- the same trap the
# bwa-mem2 block documents.
printf '#!/bin/sh\nexec "%s/bin/spades.py" "$@"\n' "${INSTALL_DIR}" \
    > /usr/local/bin/spades.py
chmod +x /usr/local/bin/spades.py

# Assert rather than announce: a version mismatch here means the pin and the
# installed tree disagree, and that must fail the build, not the first run.
INSTALLED="$(/usr/local/bin/spades.py --version 2>&1 | head -1)"
echo "${INSTALLED}"
case "${INSTALLED}" in
    *"${SPADES_VERSION}"*) ;;
    *) echo "expected SPAdes ${SPADES_VERSION}, got: ${INSTALLED}" >&2; exit 1 ;;
esac
du -sh "${INSTALL_DIR}"
