#!/bin/sh
# Install MultiQC into its own virtualenv.
#
# The venv is not tidiness, it is a hard requirement. MultiQC pins
# `kaleido==0.2.1`; this image already carries kaleido 1.3.0 for NanoPlot
# (`pip show kaleido` reports `Required-by: NanoPlot`). A plain
# `pip install multiqc` into the shared environment resolves that conflict
# by downgrading kaleido, which breaks NanoPlot -- silently, because nothing
# imports kaleido at startup and the failure only surfaces the next time a
# long-read QC job runs. Verified on 2026-08-20 against the running image.
#
# Same isolation reasoning as install-medaka.sh, but a plain venv rather
# than micromamba: MultiQC 1.35 is a pure `py3-none-any` wheel with no
# compiled component, so there is nothing conda buys here. Architecture is
# a non-issue for the same reason -- verified installing cleanly on
# aarch64, where every dependency that ships native code (polars, pyarrow,
# numpy) publishes a manylinux aarch64 wheel.
#
# kaleido is removed after install. MultiQC declares it for *static image
# export* (PNG/PDF of each plot); BioFlow serves the interactive HTML
# report and never exports images. It is 238 MB of bundled Chromium for a
# code path this application does not reach. Verified by generating a full
# report with FastQC + fastp input after removing it: both modules parsed,
# the plots rendered, the HTML came out at 2.3 MB.
#
# What is deliberately NOT stripped, despite looking like an easy win:
# `polars[rtcompat]` ships two runtimes, `_polars_runtime_32` and
# `_polars_runtime_compat`, at ~196 MB each. Polars selects between them at
# import time by CPU feature detection (see polars/_cpu_check.py). Deleting
# the one this build machine does not use would work here and fail on a
# host with a different CPU, with no error until a report job ran. That is
# the same class of silent, architecture-dependent trap install-quast.sh
# deletes its bundled minimap2 to avoid, so both stay.

set -eu

MULTIQC_VERSION="${MULTIQC_VERSION:-1.35}"
INSTALL_DIR="/opt/multiqc"

# Absolute path, never a bare `python3`. /opt/medaka/env/bin is ahead of the
# app interpreter on PATH, so `python3` in a script run inside this image
# resolves to Medaka's environment -- building the venv from it would base
# MultiQC on an unrelated tool's interpreter and silently couple the two.
# Caught by this script's own guard on 2026-08-20 when it was written with a
# bare `python3`. See AGENTS.md on the shadowed interpreter.
APP_PYTHON="${APP_PYTHON:-/usr/local/bin/python3.12}"

echo "Creating MultiQC ${MULTIQC_VERSION} environment..."
"${APP_PYTHON}" -m venv "${INSTALL_DIR}/env"
"${INSTALL_DIR}/env/bin/pip" install --no-cache-dir "multiqc==${MULTIQC_VERSION}"

# See the header: dead weight for this application, and large.
echo "Removing kaleido (static image export, unused here)..."
"${INSTALL_DIR}/env/bin/pip" uninstall -y kaleido || true

# A wrapper rather than a symlink, matching install-quast.sh's reasoning:
# it keeps the entry point's own module resolution inside the venv rather
# than depending on how a symlinked console script resolves sys.path. It
# also names the venv's interpreter by absolute path, which is what keeps
# this working despite /opt/medaka/env/bin sitting ahead of the app
# interpreter on PATH (see AGENTS.md on the shadowed interpreter).
cat > /usr/local/bin/multiqc <<'WRAPPER'
#!/bin/sh
exec /opt/multiqc/env/bin/multiqc "$@"
WRAPPER
chmod +x /usr/local/bin/multiqc

# Fail the build here rather than at first job, same as install-medaka.sh:
# a probe that finds nothing on PATH reads to the user as a broken install,
# and a build that never produced a working binary should say so while
# someone is watching.
multiqc --version

# Guard the reason this venv exists. If a future change installs MultiQC
# into the shared environment instead, kaleido gets downgraded and NanoPlot
# breaks with nothing failing until a long-read job runs -- so assert here,
# while the build is watching, that the shared environment still has the
# version NanoPlot needs.
# Read from package metadata rather than `kaleido.__version__`: kaleido 1.x
# does not define that attribute, so importing and reading it reports the
# healthy case as broken. Asked of the app interpreter by absolute path, for
# the same shadowing reason as APP_PYTHON above.
SHARED_KALEIDO=$("${APP_PYTHON}" -c \
    "from importlib.metadata import version; print(version('kaleido'))" \
    2>/dev/null || echo "missing")
case "${SHARED_KALEIDO}" in
    1.*) ;;
    *)
        echo "ERROR: shared-env kaleido is '${SHARED_KALEIDO}'; NanoPlot needs 1.x." >&2
        echo "       MultiQC must install into ${INSTALL_DIR}/env, never the shared env." >&2
        exit 1
        ;;
esac
