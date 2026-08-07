#!/bin/sh
# Install Merqury from its GitHub source tag.
#
# Merqury publishes NO release assets -- verified 2026-08-06,
# `gh api repos/marbl/merqury/releases` returns `assets: []` on every tag,
# so there is nothing to download but the source archive.
#
# Merqury is shell + R + Java: no compilation. Its scripts locate each
# other through $MERQURY, and every one of them begins
# `source $MERQURY/util/util.sh` -- so MERQURY must be set in the image
# ENV, not merely in this script's shell. See the Dockerfile.
#
# Runtime dependencies, all installed via apt in the Dockerfile rather
# than here: bedtools, r-cran-argparse, r-cran-ggplot2, r-cran-scales,
# default-jre-headless (already present for other tools), samtools
# (already present).
#
# Note the split of what needs what: eval/qv.sh -- the QV number alone --
# needs only meryl, bedtools and awk. The R packages and the bundled
# .jar files are for spectra-cn plotting. Dropping the plots later would
# recover every r-cran-* package and lose nothing from the fact table.

set -eu

MERQURY_VERSION="${MERQURY_VERSION:-1.4.1}"
INSTALL_DIR="/opt/merqury"

apt-get update
apt-get install -y --no-install-recommends curl ca-certificates

mkdir -p "${INSTALL_DIR}"
cd /tmp
curl -fsSL -o merqury.tar.gz \
    "https://github.com/marbl/merqury/archive/refs/tags/v${MERQURY_VERSION}.tar.gz"
tar -xzf merqury.tar.gz -C "${INSTALL_DIR}" --strip-components=1
rm -f merqury.tar.gz

chmod +x "${INSTALL_DIR}"/*.sh "${INSTALL_DIR}"/eval/*.sh "${INSTALL_DIR}"/util/*.sh

# eval/spectra-cn.sh detects k by piping `meryl print` through
# `head -n 2 | tail -n 1`, expecting line 2 to be the first k-mer row. That
# was true against the meryl this script was written for, but Marbl meryl
# 1.4.2 (see install-meryl.sh -- the version this image pins, for the arm64
# binary) prints an 11-line banner ("Found 1 command tree.", "PROCESSING
# TREE #1...", etc.) before any k-mer data, so line 2 is always blank. `k`
# ends up empty, `meryl count k=` fails with "Kmer size not supplied", and
# every downstream step in the script silently produces empty output --
# confirmed against a real run: an empty spectra-cn.hist, no .qv file, and
# `merqury.sh` still exits 0 because it never checks spectra-cn.sh's result.
# Patched here rather than left to fail at runtime: filter for a line that
# actually looks like `<kmer><whitespace>...` instead of trusting a fixed
# line number, which works against both the old and new banner shapes.
python3 - "${INSTALL_DIR}/eval/spectra-cn.sh" <<'PATCH'
import re
import sys

path = sys.argv[1]
text = open(path).read()
old = "k=`meryl print $read | head -n 2 | tail -n 1 | awk '{print length($1)}'`"
new = (
    "k=`meryl print $read | grep -m1 -E '^[ACGT]+[[:space:]]' "
    "| awk '{print length($1)}'`"
)
if old not in text:
    raise SystemExit(
        f"expected k-detection line not found in {path} -- "
        "spectra-cn.sh's source changed; re-check this patch against the "
        "new content before dropping it"
    )
open(path, "w").write(text.replace(old, new))
print(f"patched k-detection in {path}")
PATCH

# A wrapper, not a symlink: merqury.sh resolves its siblings through
# $MERQURY and must run with it set even if the caller's environment
# lacks it.
cat > /usr/local/bin/merqury <<'WRAPPER'
#!/bin/sh
export MERQURY=/opt/merqury
exec bash /opt/merqury/merqury.sh "$@"
WRAPPER
chmod +x /usr/local/bin/merqury

apt-get purge -y curl
apt-get autoremove -y
rm -rf /var/lib/apt/lists/*

echo "Merqury ${MERQURY_VERSION} installed:"
du -sh "${INSTALL_DIR}"
