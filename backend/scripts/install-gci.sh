#!/bin/sh
# Install GCI (Genome Continuity Inspector) from GitHub.
#
# There is NO BUILD. This is worth stating because issue #65 was filed
# believing otherwise -- it said "the real prerequisite is a second
# aligner", reading GCI's Requirements list as a dependency list.
#
# It is not. GCI never invokes an aligner: it consumes finished BAM/PAF
# files through --hifi and --nano. Its README marks winnowmap
# "(optional, but wanted for mapping)" -- the SAME parenthetical it gives
# minimap2, which this image has had since the alignment slice. Every
# aligner in that list is a suggestion for producing GCI's input.
#
# So GCI itself is: python3, pysam, biopython, numpy, matplotlib. MIT
# licensed. No arm64 asset check applies, because there is no asset.
#
# Pinned to a commit, not the v1.0 tag: commits have landed since that tag
# and the README documents behaviour that postdates it (the -mq guidance
# citing upstream issue #21). A "pin" that tracks main is not a pin.
#
# Resolved via `gh api repos/yeeus/GCI/commits/main --jq '.sha'` on
# 2026-08-06; that was GCI's main HEAD, committed 2026-02-28. Re-checked
# 2026-08-07 (task 1 implementation): main had not moved, same SHA.
#
# Bioconda ships GCI too, but this image carries no conda and adding one
# for a pure-Python tool would cost far more than a pinned clone.

set -eu

GCI_COMMIT="${GCI_COMMIT:-543cd4136187ff3ddd3ba4d1585626dbcdef6af6}"
INSTALL_DIR="/opt/gci"

apt-get update
apt-get install -y --no-install-recommends git

echo "Fetching GCI ${GCI_COMMIT}..."
mkdir -p "${INSTALL_DIR}"
git -C "${INSTALL_DIR}" init -q
git -C "${INSTALL_DIR}" remote add origin https://github.com/yeeus/GCI.git
git -C "${INSTALL_DIR}" fetch --depth 1 origin "${GCI_COMMIT}"
git -C "${INSTALL_DIR}" checkout -q FETCH_HEAD

chmod +x "${INSTALL_DIR}/GCI.py"

# A wrapper rather than a symlink: GCI.py resolves its utility siblings
# from its own location.
cat > /usr/local/bin/gci <<'WRAPPER'
#!/bin/sh
exec python3 /opt/gci/GCI.py "$@"
WRAPPER
chmod +x /usr/local/bin/gci

INSTALLED_COMMIT="$(git -C "${INSTALL_DIR}" rev-parse HEAD)"

apt-get purge -y git
apt-get autoremove -y
rm -rf /var/lib/apt/lists/*

echo "GCI installed at commit ${INSTALLED_COMMIT}:"
du -sh "${INSTALL_DIR}"
