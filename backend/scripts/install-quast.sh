#!/bin/sh
# Install QUAST from its GitHub release tarball, patched and trimmed.
#
# Not packaged for Debian trixie -- confirmed against a clean debian:trixie
# container on 2026-08-05 (`apt-cache policy quast` reports no candidate at
# all), and PyPI stops at 5.2.0 (2022): 5.3.0 (2024-11-10) exists only as a
# GitHub release. The gap matters because 5.2.0 is dead on this image --
# both versions import `distutils`, which Python 3.12 removed -- so the
# version question and the patch question are independent; picking 5.3.0
# from PyPI was never an option.
#
# There is no compilation here, unlike compleasm/miniprot above. QUAST
# prefers an installed minimap2 over its own bundled copy (see the deletion
# of quast_libs/minimap2 below for why that also matters for arm64), and its
# own C parts are otherwise unused in reference-based mode. Verified: a real
# run against yeast used this image's Debian minimap2 2.27 and logged a
# version-mismatch warning about the bundled 2.28, never touching it.

set -eu

QUAST_VERSION="${QUAST_VERSION:-5.3.0}"
INSTALL_DIR="/opt/quast"

apt-get update
apt-get install -y --no-install-recommends curl

echo "Fetching QUAST ${QUAST_VERSION}..."
mkdir -p "${INSTALL_DIR}"
curl -sL "https://github.com/ablab/quast/archive/refs/tags/quast_${QUAST_VERSION}.tar.gz" \
    | tar xz --strip-components=1 -C "${INSTALL_DIR}"

# Python 3.12 removed `distutils`. Both call sites reached by a reference run
# have a stdlib/packaging equivalent; patched rather than worked around with
# a `setuptools<81` pin, which also shims `distutils` but ties an installed
# tool's importability to a global build-system pin that any future
# `pip install` can silently break. Verified with setuptools uninstalled and
# `import distutils` raising.
sed -i \
    "s/^from distutils.version import LooseVersion\$/from packaging.version import Version as LooseVersion/" \
    "${INSTALL_DIR}/quast_libs/qconfig.py"
sed -i \
    "s/from distutils.dir_util import copy_tree/from shutil import copytree as _ct\n    copy_tree = lambda s, d: _ct(s, d, dirs_exist_ok=True)/" \
    "${INSTALL_DIR}/quast_libs/ra_utils/misc.py"

# Everything below is unreachable in the reference-based mode this
# application runs (no --gene-finding, --rna-finding,
# --conserved-genes-finding, or reads-alignment mode), and dropping it turns
# a 400 MB tarball into 8.6 MB with no change in output -- verified against
# the misassembly test below both before and after.
#
# quast_libs/minimap2 is on this list for a second reason, not just bulk:
# QUAST falls back to compiling its own bundled minimap2 only when nothing
# on PATH meets its `min_version='2.19'` floor (quast_libs/ca_utils/misc.py).
# That bundled copy's arm64 compile fix is on QUAST's master branch only,
# merged 2026-06-10 -- two years after this release -- so leaving the tree in
# place would mean a silent, architecture-dependent fallback: fine on amd64,
# a build failure on arm64, and nothing in a QC job's log would say a
# compile was even attempted. Deleting it converts that into an immediate,
# legible "no minimap2" error instead.
rm -rf \
    "${INSTALL_DIR}/external_tools" \
    "${INSTALL_DIR}/tc_tests" \
    "${INSTALL_DIR}/test_data" \
    "${INSTALL_DIR}/manual.html" \
    "${INSTALL_DIR}/quast_libs/genemark" \
    "${INSTALL_DIR}/quast_libs/genemark-es" \
    "${INSTALL_DIR}/quast_libs/barrnap" \
    "${INSTALL_DIR}/quast_libs/glimmer" \
    "${INSTALL_DIR}/quast_libs/sambamba" \
    "${INSTALL_DIR}/quast_libs/busco" \
    "${INSTALL_DIR}/quast_libs/minimap2"

# A wrapper, not a symlink: QUAST locates quast_libs relative to its own
# module path (__file__), and whether that resolution survives a symlinked
# entry point is a question this sidesteps rather than answers. Named
# quast.py to match how the tool is invoked everywhere else in this
# codebase and how ragtag.py's own wrapper-free binary is named.
cat > /usr/local/bin/quast.py <<'WRAPPER'
#!/bin/sh
exec python3 /opt/quast/quast.py "$@"
WRAPPER
chmod +x /usr/local/bin/quast.py

apt-get purge -y curl
apt-get autoremove -y
apt-get clean
rm -rf /var/lib/apt/lists/*

quast.py --version
