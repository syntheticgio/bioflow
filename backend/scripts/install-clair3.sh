#!/bin/sh
# Install Clair3 and its models into the image.
#
# Bioconda is the only distribution of Clair3 that works on arm64: there is no
# Debian package, the upstream Docker image is linux/amd64 only, and building
# from source needs a pinned TensorFlow/PyTorch toolchain. micromamba gives us
# the conda package without dragging a full conda installation into the image --
# the binary is downloaded, used once, and deleted.
#
# Models are baked in at build time rather than fetched on first run. A variant
# calling job that has to download half a gigabyte before it starts is a job
# that fails when the network is down, and this application is meant to run on
# a laptop that may not have one.

set -eu

CLAIR3_VERSION="${CLAIR3_VERSION:-2.0.2}"
INSTALL_DIR="/opt/clair3"
MODELS_DIR="${INSTALL_DIR}/models"

# micromamba publishes per-arch builds; picking the wrong one yields an "exec
# format error" that reads like a corrupt download.
case "$(uname -m)" in
    aarch64|arm64) MAMBA_ARCH="linux-aarch64" ;;
    x86_64|amd64)  MAMBA_ARCH="linux-64" ;;
    *)
        echo "ERROR: unsupported architecture $(uname -m) for Clair3" >&2
        exit 1
        ;;
esac

mkdir -p "${INSTALL_DIR}" "${MODELS_DIR}/ont" "${MODELS_DIR}/hifi"

# Download to a file and check it before unpacking. Piping curl straight into
# tar hides *which* half failed: a 404, an expired certificate and a truncated
# transfer all surface identically as "tar: Child returned status 2", which
# sent one debugging session chasing a corrupt archive when the real problem
# was that ca-certificates had been purged from an earlier layer.
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

echo "Creating Clair3 ${CLAIR3_VERSION} environment..."
/tmp/bin/micromamba create -y -p "${INSTALL_DIR}/env" \
    -c conda-forge -c bioconda \
    "clair3=${CLAIR3_VERSION}"

# The wrapper resolves the env's own python and scripts, so it must run with
# the env's bin first on PATH rather than being symlinked into /usr/local/bin.
cat > /usr/local/bin/run_clair3.sh <<EOF
#!/bin/sh
export PATH="${INSTALL_DIR}/env/bin:\$PATH"
exec "${INSTALL_DIR}/env/bin/run_clair3.sh" "\$@"
EOF
chmod +x /usr/local/bin/run_clair3.sh

# Models.
#
# Clair3 2.x uses PyTorch checkpoints, served as individual files under
# clair3_models_pytorch/<model>/ -- not as tarballs, and not from the legacy
# clair3_models/ directory, whose newest ONT model is v420 and whose contents
# are the older TensorFlow format. A model directory is exactly two files.
#
# ONT model confirmed against the Clair3 v2.0.1 release notes; unchanged in
# 2.0.2.
MODEL_BASE="https://www.bio8.cs.hku.hk/clair3/clair3_models_pytorch"
ONT_MODEL="r1041_e82_400bps_sup_v520_with_mv"
HIFI_MODEL="hifi_revio"

# Downloaded straight into the platform directory that --model_path points at,
# so there is no layout to normalize afterwards.
echo "Downloading ONT model (${ONT_MODEL})..."
fetch "${MODEL_BASE}/${ONT_MODEL}/pileup.pt" "${MODELS_DIR}/ont/pileup.pt"
fetch "${MODEL_BASE}/${ONT_MODEL}/full_alignment.pt" "${MODELS_DIR}/ont/full_alignment.pt"

echo "Downloading HiFi model (${HIFI_MODEL})..."
fetch "${MODEL_BASE}/${HIFI_MODEL}/pileup.pt" "${MODELS_DIR}/hifi/pileup.pt"
fetch "${MODEL_BASE}/${HIFI_MODEL}/full_alignment.pt" "${MODELS_DIR}/hifi/full_alignment.pt"

# Both checkpoints must be present: Clair3 runs pileup first and full-alignment
# second, so a missing full_alignment.pt fails hours into a run rather than at
# startup. Checking here turns that into a failed build.
for platform in ont hifi; do
    for checkpoint in pileup.pt full_alignment.pt; do
        if [ ! -s "${MODELS_DIR}/${platform}/${checkpoint}" ]; then
            echo "ERROR: missing ${MODELS_DIR}/${platform}/${checkpoint}" >&2
            exit 1
        fi
    done
done

rm -rf /tmp/bin/micromamba
# The conda env carries package caches and static archives that nothing needs
# at runtime; dropping them is a meaningful fraction of the image size.
rm -rf "${INSTALL_DIR}/env/pkgs" "${INSTALL_DIR}/env/conda-meta"
find "${INSTALL_DIR}/env" -name '*.a' -delete 2>/dev/null || true

echo "Clair3 ${CLAIR3_VERSION} installed to ${INSTALL_DIR}"
ls -la "${MODELS_DIR}/ont" "${MODELS_DIR}/hifi"
