#!/bin/sh
# Install Marbl meryl from its GitHub release binaries.
#
# THE VERSION PIN IS LOAD-BEARING. Read this before changing it.
#
# meryl v1.4.2 (2026-07-21) is the FIRST release ever to ship a
# Linux-arm64 binary. v1.4.1 and v1.4 are amd64-only. Merqury's own
# README still points at v1.4.1, because it was written before v1.4.2
# existed -- following the README, or "relaxing" this pin to a floor
# like >=1.4.1, silently reintroduces an arm64 C++ source build. That is
# the exact trap that bit bwa-mem2, compleasm's release asset, and
# compleasm's biocontainer in this repo.
#
# Verified 2026-08-06 via `gh api repos/marbl/meryl/releases`:
#   v1.4.2  meryl-1.4.2.Linux-arm64.tar.xz  <- exists
#   v1.4.1  (no arm64 asset)
#   v1.4    (no arm64 asset)
#
# This is why the slice is a tarball extract rather than compleasm-priced.
#
# NOT Debian's `meryl` package. That is 0~20150903+r2013-9+b1, the Celera
# Assembler k-mer suite -- a different program with the same name. See
# tools.meryl()'s probe, which rejects it explicitly.
#
# RUNTIME LINKING: this release binary needs OpenSSL 1.1 (libssl.so.1.1,
# libcrypto.so.1.1), which Debian trixie's own archive no longer carries --
# verified 2026-08-07, neither libssl1.1 nor libssl3 has an apt candidate on
# trixie (trixie ships OpenSSL 3.x through libssl3t64 instead, which this
# binary is not linked against). `meryl --version` fails with "error while
# loading shared libraries: libssl.so.1.1: cannot open shared object file"
# without them. This script cannot fix that itself -- apt has nothing to
# install here -- so the two .so files are vendored from a `debian:
# bullseye-slim` image (still carries libssl1.1 1.1.1w-0+deb11u8 via
# debian-security) via a Dockerfile multi-stage COPY into /opt/meryl/lib,
# with LD_LIBRARY_PATH pointed at it. See the `legacy-ssl` build stage and
# surrounding COPY/ENV lines in backend/Dockerfile. The binary's two other
# runtime deps beyond libc, libcurl.so.4 and libgomp.so.1, ARE present in
# trixie's own archive (as libcurl4t64 and libgomp1) and are installed
# through the normal apt block instead, no vendoring needed for those two.

set -eu

MERYL_VERSION="${MERYL_VERSION:-1.4.2}"
INSTALL_DIR="/opt/meryl"

case "$(uname -m)" in
    x86_64)          MERYL_ARCH="amd64" ;;
    aarch64|arm64)   MERYL_ARCH="arm64" ;;
    *)               echo "unsupported arch: $(uname -m)" >&2; exit 1 ;;
esac

TARBALL="meryl-${MERYL_VERSION}.Linux-${MERYL_ARCH}.tar.xz"
BASE="https://github.com/marbl/meryl/releases/download/v${MERYL_VERSION}"

apt-get update
apt-get install -y --no-install-recommends curl ca-certificates xz-utils

cd /tmp
echo "Fetching ${TARBALL}..."
curl -fsSL -O "${BASE}/${TARBALL}"
curl -fsSL -O "${BASE}/SHA256SUMS"

# Verify rather than trust the download. SHA256SUMS covers every asset in
# the release, so filter to ours before checking -- `sha256sum -c` fails on
# lines naming files that are not present.
grep "${TARBALL}" SHA256SUMS > "${TARBALL}.sha256"
sha256sum -c "${TARBALL}.sha256"

mkdir -p "${INSTALL_DIR}"
tar -xJf "${TARBALL}" -C "${INSTALL_DIR}" --strip-components=1
rm -f "${TARBALL}" "${TARBALL}.sha256" SHA256SUMS

apt-get purge -y curl xz-utils
apt-get autoremove -y
rm -rf /var/lib/apt/lists/*

echo "meryl ${MERYL_VERSION} (${MERYL_ARCH}) installed:"
"${INSTALL_DIR}/bin/meryl" --version 2>&1 | head -1 || true
du -sh "${INSTALL_DIR}"
