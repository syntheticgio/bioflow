#!/bin/sh
# Install CRAQ from GitHub.
#
# Not packaged for Debian trixie (verified: no apt candidate). Pure
# Perl/shell over samtools + minimap2, both already in this image -- there
# is nothing to compile, unlike compleasm.
#
# The whole tree is installed, not just bin/craq: that script is a thin
# driver that shells out to ../src/runLR.sh, ../src/runAQI.sh and a dozen
# sibling .pl files, resolved relative to its own location.
#
# pycircos is deliberately NOT installed. It is needed only for -pl
# plotting, which BioFlow never passes, and it would add a Python
# dependency for output this application does not serve.
#
# CRAQ_COMMIT defaults to a specific commit SHA, not a branch name -- a
# "pin" that tracks `main` isn't a pin. Resolved via
# `gh api repos/JiaoLaboratory/CRAQ/commits/main --jq '.sha'` on 2026-08-06;
# that was CRAQ's main HEAD as of 2025-12-03.
#
# Pinned with a shallow fetch of just that commit (`git fetch --depth 1
# origin "${CRAQ_COMMIT}"` + `git checkout FETCH_HEAD`) rather than a full
# clone + checkout, to keep the same minimal-image-size tradeoff
# install-quast.sh makes -- a full history clone would pull far more than
# this small tree needs just to land on one commit.

set -eu

CRAQ_COMMIT="${CRAQ_COMMIT:-63509381fe85c4bd5832f1c67d0279c823ce9592}"
INSTALL_DIR="/opt/craq"

apt-get update
apt-get install -y --no-install-recommends git

echo "Fetching CRAQ ${CRAQ_COMMIT}..."
mkdir -p "${INSTALL_DIR}"
git -C "${INSTALL_DIR}" init -q
git -C "${INSTALL_DIR}" remote add origin https://github.com/JiaoLaboratory/CRAQ.git
git -C "${INSTALL_DIR}" fetch --depth 1 origin "${CRAQ_COMMIT}"
git -C "${INSTALL_DIR}" checkout -q FETCH_HEAD

chmod +x "${INSTALL_DIR}/bin/craq" "${INSTALL_DIR}"/src/*.sh

# A wrapper rather than a symlink: bin/craq resolves its src/ siblings from
# its own path, so it must be invoked at its real location.
cat > /usr/local/bin/craq <<'WRAPPER'
#!/bin/sh
exec perl /opt/craq/bin/craq "$@"
WRAPPER
chmod +x /usr/local/bin/craq

# Captured before `git` is purged below -- git itself isn't kept in the
# final image, only this resolved commit recorded in the build log.
INSTALLED_COMMIT="$(git -C "${INSTALL_DIR}" rev-parse HEAD)"

apt-get purge -y git
apt-get autoremove -y
rm -rf /var/lib/apt/lists/*

echo "CRAQ installed at commit ${INSTALLED_COMMIT}:"
du -sh "${INSTALL_DIR}"
