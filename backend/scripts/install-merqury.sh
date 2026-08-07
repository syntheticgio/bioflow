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
