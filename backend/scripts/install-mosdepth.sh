#!/bin/sh
# Install mosdepth, the per-base/per-window BAM depth calculator.
#
# Not in Debian. Checked against a clean python:3.12-slim (trixie) container
# on 2026-08-20 *with* an `apt-get update` first -- the stale-cache false
# negative install-polypolish.sh documents already produced one wrong claim
# in this repo, so the check was run with a control: in the same container
# `apt-cache policy bedtools` resolves 2.31.1+dfsg-2+b1 while
# `apt-cache policy mosdepth` reports no candidate at all. Nor is there a
# package under another name (`covtobed` and `megadepth` are the only
# depth-adjacent hits, and neither is mosdepth).
#
# Bioconda rather than the GitHub release binary, and that choice is what
# makes this tool available on Apple Silicon. The v0.3.14 release ships a
# single unsuffixed `mosdepth` asset which is ELF x86-64 (verified with
# `file` on the downloaded artifact, 2026-08-20) -- there is no
# linux-aarch64 build, so the release-binary route would have meant an
# arm64 skip and a Polypolish-shaped "not available on this architecture".
# bioconda publishes 0.3.14 for both linux-64 and linux-aarch64 (checked
# against the anaconda.org API on 2026-08-20), so both architectures get
# the same version and no probe needs an arch special case.
#
# Same micromamba mechanics as install-medaka.sh -- the binary is
# downloaded, used once, and deleted -- but none of medaka's dependency
# surgery: mosdepth is a compiled Nim binary whose only conda dependencies
# are htslib's shared libraries, so there is no Python environment to trim.
set -eu

MOSDEPTH_VERSION="${MOSDEPTH_VERSION:-0.3.14}"
INSTALL_DIR="/opt/mosdepth"

# This image ships WITHOUT curl by design -- install-meryl.sh and
# install-quast.sh purge it after their own downloads, and both run before
# this script, so no later layer may assume it exists. (Caught by a real
# build failing here with "curl: not found", exactly the trap the Dockerfile
# documents at its Node block.) Install it, use it, and restore that end
# state at the finish, the same way the Node layer does.
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
        echo "ERROR: unsupported architecture $(uname -m) for mosdepth" >&2
        exit 1
        ;;
esac

# Download to a file and check it before unpacking -- piping the download
# into tar hides which half failed. Same helper and reasoning as
# install-medaka.sh.
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

echo "Creating mosdepth ${MOSDEPTH_VERSION} environment..."
/tmp/bin/micromamba create -y -p "${INSTALL_DIR}/env" \
    -c conda-forge -c bioconda \
    "mosdepth=${MOSDEPTH_VERSION}"

rm -rf /tmp/bin/micromamba
# The conda env carries package caches and static archives nothing needs.
rm -rf "${INSTALL_DIR}/env/pkgs" "${INSTALL_DIR}/env/conda-meta"

# Trim the env from 126 MB to 48 MB. Everything removed here was verified
# unnecessary by running mosdepth against a real indexed BAM after removal
# -- in windowed (`--by 100`), BED-region (`--by regions.bed`) and per-base
# modes -- and byte-comparing the `.regions.bed.gz` against the same run
# before the trim (identical, 2026-08-20).
#
# `lib/` itself must NOT be removed, despite `ldd` on the binary listing
# nothing but libc: mosdepth is Nim and loads libhts.so through dlopen at
# runtime, which ldd cannot see. Deleting lib/ leaves `mosdepth --version`
# working and every actual run failing with "could not load: libhts.so" --
# tested, and precisely the silent, look-fine-until-a-job-runs trap
# install-quast.sh deletes its bundled minimap2 to avoid.
#
# ICU (~40 MB of the total) arrives transitively and is untouched by any
# depth code path; `.a` archives are build-time only.
rm -rf "${INSTALL_DIR}/env/include" "${INSTALL_DIR}/env/share" \
    "${INSTALL_DIR}/env/sbin" "${INSTALL_DIR}/env/libexec" \
    "${INSTALL_DIR}/env/ssl"
find "${INSTALL_DIR}/env/bin" -type f ! -name mosdepth -delete
rm -f "${INSTALL_DIR}"/env/lib/libicu*.so* "${INSTALL_DIR}"/env/lib/*.a

# A wrapper rather than a symlink, matching install-quast.sh and
# install-multiqc.sh: mosdepth loads htslib from its own env's lib
# directory, and whether that resolution survives a symlinked entry point
# is a question this sidesteps rather than answers.
cat > /usr/local/bin/mosdepth <<'WRAPPER'
#!/bin/sh
exec /opt/mosdepth/env/bin/mosdepth "$@"
WRAPPER
chmod +x /usr/local/bin/mosdepth

# Fail the build here rather than at first job, same as install-medaka.sh:
# a probe that finds nothing on PATH reads to the user as a broken install,
# and a build that never produced a working binary should say so while
# someone is watching.
mosdepth --version

# `--version` is deliberately not the whole check. It returns zero even when
# lib/ has been emptied and every real run dies on "could not load:
# libhts.so" (observed while sizing the trim above), so the guard that
# matters is a depth pass over an actual BAM. Built here with samtools,
# which this image already installs.
echo "Verifying a real depth run..."
MOSDEPTH_SMOKE="$(mktemp -d)"
{
    printf '@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:1000\n'
    i=1
    while [ "$i" -le 40 ]; do
        printf 'r%d\t0\tchr1\t%d\t60\t20M\t*\t0\t0\tACGTACGTACGTACGTACGT\tIIIIIIIIIIIIIIIIIIII\n' \
            "$i" "$((i * 10))"
        i=$((i + 1))
    done
} > "${MOSDEPTH_SMOKE}/smoke.sam"
samtools view -b "${MOSDEPTH_SMOKE}/smoke.sam" > "${MOSDEPTH_SMOKE}/smoke.bam"
samtools index "${MOSDEPTH_SMOKE}/smoke.bam"
if ! mosdepth -n --by 100 "${MOSDEPTH_SMOKE}/smoke" "${MOSDEPTH_SMOKE}/smoke.bam"; then
    echo "ERROR: mosdepth installed but cannot compute depth." >&2
    exit 1
fi
if [ ! -s "${MOSDEPTH_SMOKE}/smoke.regions.bed.gz" ]; then
    echo "ERROR: mosdepth produced no windowed output." >&2
    exit 1
fi
rm -rf "${MOSDEPTH_SMOKE}"

# Leave the image as this script found it. See CURL_WAS_MISSING above.
restore_curl_state
