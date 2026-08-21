#!/bin/sh
# Install CheckM2, the metagenome-bin completeness/contamination scorer.
#
# Not in Debian. Checked against the running api container (trixie, arm64) on
# 2026-08-21 *with* a control package in the same run, per the false negative
# install-polypolish.sh documents: `apt-cache policy samtools` resolves while
# `apt-cache policy checkm2` reports no candidate at all.
#
# Bioconda, in its own env, the install-metabat2.sh micromamba pattern.
#
# arm64 is deliberately unsupported, and the reason is worth stating because
# "CheckM2 is pure Python" makes it look like it should work anywhere -- the
# bioconda package is even noarch. The blocker is a *dependency*:
#
#   CheckM2 1.1.0 has two builds: one requiring tensorflow ==2.17, one
#   requiring tensorflow >=2.1.0,<2.6.0. linux-aarch64 has NEITHER -- the only
#   tensorflow builds published for it, of any variant (tensorflow,
#   tensorflow-cpu, tensorflow-base, libtensorflow), are 2.18.0 and 2.19.1.
#   So both branches fail and the environment is unsolvable. (1.0.1 and 1.0.2
#   pin <2.6 only, and fail the same way.)
#
# Verified on 2026-08-21 with a real micromamba solve on linux/arm64, which
# fails on both branches ("tensorflow =2.17 ... does not exist" and
# "tensorflow >=2.1.0,<2.6.0 ... does not exist"), against a linux/amd64
# control run in the same session that installs and imports cleanly
# (tensorflow 2.17.0, scikit-learn 1.6.1, DIAMOND 2.1.11). DIAMOND -- the
# dependency the design doc expected to be the blocker -- is fine: bioconda
# publishes it for linux-aarch64.
#
# pip is not an escape hatch either: CheckM2 hard-pins scikit-learn==0.23.2,
# which has no aarch64 wheel and source-builds against numpy==1.17.3 (2019),
# which does not build under Python 3.12.
#
# Unpinning those dependencies WOULD install, and is deliberately not done:
# CheckM2 scores by loading a pickled scikit-learn/Keras model, and crossing a
# 0.23 -> 1.x scikit-learn and a 2.5 -> 2.21 tensorflow gap is the case that
# yields numbers rather than errors. A silently wrong completeness score is
# worse than an honest "not available on this architecture", which is what
# tools.checkm2() reports instead. See
# docs/superpowers/specs/2026-08-20-checkm2-bin-qc-design.md.
set -eu

CHECKM2_VERSION="${CHECKM2_VERSION:-1.1.0}"
TARGETARCH="${TARGETARCH:-amd64}"
INSTALL_DIR="/opt/checkm2"

if [ "$TARGETARCH" = "arm64" ]; then
    echo "Skipping CheckM2: no linux-aarch64 tensorflow it can use."
    exit 0
fi

# This image ships WITHOUT curl by design -- install-meryl.sh and
# install-quast.sh purge it after their own downloads, and both run before
# this script, so no later layer may assume it exists. Install it, use it,
# and restore that end state at the finish, exactly as install-metabat2.sh
# does.
CURL_WAS_MISSING=""
if ! command -v curl >/dev/null 2>&1; then
    CURL_WAS_MISSING=1
    apt-get update
    apt-get install -y --no-install-recommends curl ca-certificates
fi

restore_curl_state() {
    if [ -n "${CURL_WAS_MISSING}" ]; then
        apt-get purge -y curl
        apt-get autoremove -y
        apt-get clean
        rm -rf /var/lib/apt/lists/*
    fi
}

# Only ever reached on x86_64 -- the arm64 guard above returns first -- but
# resolved from uname rather than hardcoded, so a future arm64 build (once
# CheckM2 relaxes its tensorflow pin) needs only the guard removed.
case "$(uname -m)" in
    aarch64|arm64) MAMBA_ARCH="linux-aarch64" ;;
    x86_64|amd64)  MAMBA_ARCH="linux-64" ;;
    *)
        echo "ERROR: unsupported architecture $(uname -m) for checkm2" >&2
        exit 1
        ;;
esac

fetch() {
    url="$1"
    dest="$2"
    if ! curl -fsSL --retry 3 --retry-delay 2 -o "${dest}" "${url}"; then
        echo "ERROR: download failed: ${url}" >&2
        exit 1
    fi
    if [ ! -s "${dest}" ]; then
        echo "ERROR: downloaded an empty file: ${url}" >&2
        exit 1
    fi
}

echo "Installing micromamba (${MAMBA_ARCH})..."
fetch "https://micro.mamba.pm/api/micromamba/${MAMBA_ARCH}/latest" /tmp/micromamba.tar.bz2
tar -xj -C /tmp bin/micromamba < /tmp/micromamba.tar.bz2
rm -f /tmp/micromamba.tar.bz2

echo "Creating checkm2 ${CHECKM2_VERSION} environment..."
/tmp/bin/micromamba create -y -p "${INSTALL_DIR}/env" \
    -c conda-forge -c bioconda \
    "checkm2=${CHECKM2_VERSION}"

rm -rf /tmp/bin/micromamba
rm -rf "${INSTALL_DIR}/env/pkgs" "${INSTALL_DIR}/env/conda-meta"

# Deliberately conservative trimming. CheckM2 loads a Keras model at runtime
# and DIAMOND is a compiled binary linking out of this env, so lib/ and
# share/ both stay -- the dlopen trap install-mosdepth.sh documents applies
# here with more force, since a missing tensorflow shared object surfaces as
# a Python traceback in the middle of a job rather than at install time.
rm -rf "${INSTALL_DIR}/env/include"
rm -f "${INSTALL_DIR}"/env/lib/*.a

cat > /usr/local/bin/checkm2 <<'WRAPPER'
#!/bin/sh
exec /opt/checkm2/env/bin/checkm2 "$@"
WRAPPER
chmod +x /usr/local/bin/checkm2

# Fail the build here rather than at first job: a probe that finds nothing on
# PATH reads to the user as a broken install, and a build that never produced
# a working binary should say so while someone is watching.
checkm2 --version

# `--version` is deliberately not the whole check. It imports almost nothing,
# so it exercises neither tensorflow, nor scikit-learn's pickle load, nor
# DIAMOND -- the three things most likely to be broken. `checkm2 testrun`
# runs the real prediction path end to end on bundled genomes, which is the
# guard that matters.
#
# It needs the DIAMOND database, which is 9.3 GB and is NOT baked into the
# image (it is fetched at runtime by download_checkm2_db, so that the pin,
# checksum and disk cost live in the registry rather than in a layer). So the
# strongest check available at build time is that the whole import graph
# loads and the CLI resolves its subcommands -- which does exercise
# tensorflow and scikit-learn, the two dependencies this install is fragile
# in.
echo "Verifying the import graph loads..."
/opt/checkm2/env/bin/python -c "
import tensorflow, sklearn, lightgbm, h5py  # noqa: F401
from checkm2 import predictQuality  # noqa: F401
print('checkm2 imports OK:', tensorflow.__version__, sklearn.__version__)
"

# And that DIAMOND -- the compiled half -- actually runs, not merely exists.
"${INSTALL_DIR}/env/bin/diamond" version

checkm2 predict --help > /dev/null

# Leave the image as this script found it. See CURL_WAS_MISSING above.
restore_curl_state
