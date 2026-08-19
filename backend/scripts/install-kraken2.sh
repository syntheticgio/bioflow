#!/bin/sh
# Build Kraken2 and Bracken from their pinned release tarballs.
#
# Neither is packaged for Debian trixie, and both are plain C++/shell with no
# architecture-specific assembly or SIMD requirements -- so, like compleasm,
# a source build is the route that works on both amd64 and arm64 without a
# separate arch branch.
#
# Kraken2's own install_kraken2.sh does the `make -C src install` and then
# copies scripts/* (the kraken2/kraken2-build/kraken2-inspect/k2 wrapper
# scripts plus the taxonomy-download helpers) into the target directory --
# this script just runs that installer and symlinks the two binaries this
# application calls onto PATH.
#
# Bracken's own install_bracken.sh builds src/ via `make` and chmods the
# bracken/bracken-build scripts in place; it takes no destination argument,
# so this script runs it from within the checked-out release tree and
# symlinks the two scripts it produces onto PATH from there.
#
# The database for either tool is not installed here -- both are probed as
# binaries only (see tools.kraken2()/tools.bracken()), with the multi-GB
# reference database delivered on demand at launch time instead.

set -eu

KRAKEN2_VERSION="${KRAKEN2_VERSION:-2.1.3}"
BRACKEN_VERSION="${BRACKEN_VERSION:-2.9}"

BUILD_DEPS="git make g++ zlib1g-dev rsync"

apt-get update
apt-get install -y --no-install-recommends ${BUILD_DEPS}

echo "Building Kraken2 ${KRAKEN2_VERSION}..."
git clone --depth 1 --branch "v${KRAKEN2_VERSION}" \
    https://github.com/DerrickWood/kraken2.git /tmp/kraken2
(cd /tmp/kraken2 && ./install_kraken2.sh /usr/local/kraken2)
for bin in kraken2 kraken2-build kraken2-inspect k2; do
    ln -sf "/usr/local/kraken2/${bin}" "/usr/local/bin/${bin}"
done
rm -rf /tmp/kraken2

echo "Building Bracken ${BRACKEN_VERSION}..."
git clone --depth 1 --branch "v${BRACKEN_VERSION}" \
    https://github.com/jenniferlu717/Bracken.git /tmp/bracken
(cd /tmp/bracken && ./install_bracken.sh)
install -m 0755 /tmp/bracken/bracken /usr/local/bin/bracken
install -m 0755 /tmp/bracken/bracken-build /usr/local/bin/bracken-build
rm -rf /tmp/bracken

apt-get purge -y ${BUILD_DEPS}
apt-get autoremove -y
apt-get clean
rm -rf /var/lib/apt/lists/*

kraken2 --version
bracken -v
