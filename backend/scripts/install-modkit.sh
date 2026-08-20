#!/bin/sh
# Install modkit, ONT's per-site base-modification (methylation) summarizer.
#
# Not in Debian. Bioconda ships linux-aarch64 builds of ont-modkit --
# verified against bioconda's linux-aarch64 repodata.json on 2026-08-20,
# where ont-modkit is present from 0.3.1 through the current 0.6.4 (e.g.
# ont-modkit-0.6.4-ha2fee11_0.conda). That is the difference between the
# tool working on Apple Silicon and an arm64 skip -- ONT's own GitHub
# release assets are routinely x86-64 only, and this repo does not take one
# without checking bioconda first (per CLAUDE.md).
#
# License: "Oxford Nanopore Technologies PLC. Public License, v1.0", read
# from https://github.com/nanoporetech/modkit/blob/master/LICENCE.txt on
# 2026-08-20. Non-commercial/research-only (Section 2.1: "solely for
# Research Purposes"). Recorded verbatim in TOOL_META["modkit"] rather than
# rounded to a more familiar license name -- this app is single-user and
# local-only per CLAUDE.md, which is an acceptable fit, but the field has to
# say what the license actually is.
#
# Same micromamba mechanics as install-mosdepth.sh/install-medaka.sh -- the
# micromamba binary is downloaded, used once, and deleted.

set -eu

MODKIT_VERSION="${MODKIT_VERSION:-0.6.4}"
INSTALL_DIR="/opt/modkit"

# This image ships WITHOUT curl by design -- install-meryl.sh and
# install-quast.sh purge it after their own downloads, and both run before
# this script, so no later layer may assume it exists. Install it, use it,
# and restore that end state at the finish, the same way install-mosdepth.sh
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

# micromamba publishes per-arch builds; picking the wrong one yields an
# "exec format error" that reads like a corrupt download.
case "$(uname -m)" in
    aarch64|arm64) MAMBA_ARCH="linux-aarch64" ;;
    x86_64|amd64)  MAMBA_ARCH="linux-64" ;;
    *)
        echo "ERROR: unsupported architecture $(uname -m) for modkit" >&2
        exit 1
        ;;
esac

# Download to a file and check it before unpacking -- piping the download
# into tar hides which half failed. Same helper and reasoning as
# install-mosdepth.sh/install-medaka.sh.
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

echo "Creating modkit ${MODKIT_VERSION} environment..."
/tmp/bin/micromamba create -y -p "${INSTALL_DIR}/env" \
    -c conda-forge -c bioconda \
    "ont-modkit=${MODKIT_VERSION}"

rm -rf /tmp/bin/micromamba
# The conda env carries package caches and static archives nothing needs.
rm -rf "${INSTALL_DIR}/env/pkgs" "${INSTALL_DIR}/env/conda-meta"

# Trim build-time-only content. `lib/` is left alone deliberately, matching
# install-mosdepth.sh's reasoning: modkit is a Rust binary linked against
# htslib, and `ldd` alone cannot be trusted to prove nothing there is needed
# at runtime, so nothing under lib/ is removed here.
rm -rf "${INSTALL_DIR}/env/include" "${INSTALL_DIR}/env/share" \
    "${INSTALL_DIR}/env/sbin" "${INSTALL_DIR}/env/libexec" \
    "${INSTALL_DIR}/env/ssl"
find "${INSTALL_DIR}/env/bin" -type f ! -name modkit -delete

# A wrapper rather than a symlink, matching install-mosdepth.sh: whether
# modkit's shared-library resolution survives a symlinked entry point is a
# question this sidesteps rather than answers.
cat > /usr/local/bin/modkit <<'WRAPPER'
#!/bin/sh
exec /opt/modkit/env/bin/modkit "$@"
WRAPPER
chmod +x /usr/local/bin/modkit

# Fail the build here rather than at first job, same as install-mosdepth.sh:
# a probe that finds nothing on PATH reads to the user as a broken install.
modkit --version

# `--version` is deliberately not the whole check -- a tool that dlopens its
# libraries can pass `--version` with those libraries missing. The guard
# that matters is a real `pileup` pass over a BAM carrying MM/ML tags, built
# here with samtools, which this image already installs.
echo "Verifying a real pileup run..."
MODKIT_SMOKE="$(mktemp -d)"
{
    printf '@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:1000\n'
    i=1
    while [ "$i" -le 5 ]; do
        # MM: one C->5mC call per read at the first base; ML: its probability
        # (255 = maximal confidence). This is the minimal tag pair modkit
        # needs to see any site at all in the pileup output.
        printf 'r%d\t0\tchr1\t%d\t60\t20M\t*\t0\t0\tACGTACGTACGTACGTACGT\tIIIIIIIIIIIIIIIIIIII\tMM:Z:C+m,0;\tML:B:C,255\n' \
            "$i" "$((i * 10))"
        i=$((i + 1))
    done
} > "${MODKIT_SMOKE}/smoke.sam"
samtools view -b "${MODKIT_SMOKE}/smoke.sam" > "${MODKIT_SMOKE}/smoke.bam"
samtools index "${MODKIT_SMOKE}/smoke.bam"
if ! modkit pileup "${MODKIT_SMOKE}/smoke.bam" "${MODKIT_SMOKE}/smoke.bed"; then
    echo "ERROR: modkit installed but cannot run pileup." >&2
    exit 1
fi
if [ ! -s "${MODKIT_SMOKE}/smoke.bed" ]; then
    echo "ERROR: modkit produced no bedMethyl output." >&2
    exit 1
fi
rm -rf "${MODKIT_SMOKE}"

# Leave the image as this script found it. See CURL_WAS_MISSING above.
restore_curl_state
