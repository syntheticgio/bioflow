#!/bin/sh
# Build miniprot and install compleasm from source.
#
# Neither is packaged for Debian trixie -- confirmed against the running image
# on 2026-08-02, not assumed. Their obvious alternatives are both x86-only:
# compleasm's only release asset is compleasm-<ver>_x64-linux.tar.bz2, and its
# biocontainer (quay.io/biocontainers/compleasm) reports is_manifest_list:
# false, i.e. a single amd64 image rather than a multi-arch manifest. Building
# from source is the one route that works on both architectures.
#
# miniprot needs no arm64 special-casing at all, unlike bwa-mem2: its own
# documentation states it "requires SSE2 or NEON instructions and only works
# on x86_64 or ARM CPUs" -- NEON is native upstream support, so there is
# nothing to patch and no sse2neon to vendor.
#
# compleasm is pure Python (bioconda packages it `noarch`, confirming this),
# so pip installing straight from its source tree is correct on any
# architecture; the x86-64 in the release tarball's name refers only to the
# miniprot binary bundled alongside it, which this script supplies itself.
#
# hmmer comes from apt in the main tool layer, not here -- it is a real
# Debian package and does not need a source build.
#
# No sepp. It is compleasm's dependency for --autolineage only (grep
# compleasm.py: AutoLineager is the sole caller, constructed only when
# autolineage=True), and BioFlow chooses a lineage from organism metadata
# instead of auto-detecting it -- so sepp would be installed and never
# invoked. Autolineage also downloads several lineage datasets to decide
# between them, which is the expensive way to answer a question this
# application mostly already knows the answer to.

set -eu

MINIPROT_VERSION="${MINIPROT_VERSION:-0.18}"
COMPLEASM_VERSION="${COMPLEASM_VERSION:-0.2.9}"

BUILD_DEPS="git make gcc g++ zlib1g-dev"

apt-get update
apt-get install -y --no-install-recommends ${BUILD_DEPS}

echo "Building miniprot ${MINIPROT_VERSION}..."
git clone --depth 1 --branch "v${MINIPROT_VERSION}" \
    https://github.com/lh3/miniprot.git /tmp/miniprot
(cd /tmp/miniprot && make -j "$(nproc)")
install -m 0755 /tmp/miniprot/miniprot /usr/local/bin/miniprot
rm -rf /tmp/miniprot

echo "Installing compleasm ${COMPLEASM_VERSION}..."
git clone --depth 1 --branch "v${COMPLEASM_VERSION}" \
    https://github.com/huangnengCSU/compleasm.git /tmp/compleasm
# From the source tree, not `pip install compleasm` -- PyPI is not confirmed
# to carry it (there was no package found there when this was written) and
# installing from a pinned tag is the same provenance guarantee every other
# source-built tool in this image gets.
#
# pandas is a real runtime dependency (compleasm.py imports it directly) but
# setup.py declares no install_requires at all, so it must be installed
# explicitly rather than trusted to arrive as a side effect of pip resolving
# compleasm's own metadata. Not pinned here: NanoPlot and PyDESeq2, installed
# elsewhere in this image, already pin a pandas-compatible stack, and pinning
# a third, independent version here is how two tools end up silently unable
# to share one.
pip install --no-cache-dir pandas
pip install --no-cache-dir /tmp/compleasm
rm -rf /tmp/compleasm

apt-get purge -y ${BUILD_DEPS}
apt-get autoremove -y
apt-get clean
rm -rf /var/lib/apt/lists/*

miniprot --version
compleasm --version
