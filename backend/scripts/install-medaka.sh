#!/bin/sh
# Install Medaka, ONT's neural-network consensus tool, into the image.
#
# Bioconda is the right distribution here for the same reasons it is for
# Clair3: there is no Debian package, and building from source needs a
# pinned PyTorch toolchain. micromamba gives us the conda package without
# dragging a full conda installation into the image -- the binary is
# downloaded, used once, and deleted.
#
# Unlike Polypolish, this installs on arm64 too. bioconda publishes
# linux-aarch64 builds of medaka (2.0.1 through 2.2.2, checked 2026-08-18),
# and since Polypolish is x86-64-only, Medaka is the *only* polishing path
# available on Apple Silicon.
#
# pytorch-cpu is pinned deliberately and must stay pinned. conda-forge's
# bare `pytorch` resolves preferentially to CUDA builds, which pull libtorch
# in at roughly 885MB compressed against roughly 61MB for the CPU build
# (both measured from the conda-forge index on 2026-08-18). Nothing errors
# if this pin is dropped -- the image simply grows by about a gigabyte to
# ship CUDA kernels into a container that has no GPU and never asks for one.
# This is the same shape as the flye-samtools shim: a one-line install
# detail whose omission produces no error and a badly wrong result.

set -eu

MEDAKA_VERSION="${MEDAKA_VERSION:-2.2.2}"
INSTALL_DIR="/opt/medaka"

# micromamba publishes per-arch builds; picking the wrong one yields an
# "exec format error" that reads like a corrupt download.
case "$(uname -m)" in
    aarch64|arm64) MAMBA_ARCH="linux-aarch64" ;;
    x86_64|amd64)  MAMBA_ARCH="linux-64" ;;
    *)
        echo "ERROR: unsupported architecture $(uname -m) for Medaka" >&2
        exit 1
        ;;
esac

mkdir -p "${INSTALL_DIR}"

# Download to a file and check it before unpacking -- piping curl into tar
# hides which half failed. Same helper and same reasoning as
# install-clair3.sh.
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

echo "Creating Medaka ${MEDAKA_VERSION} environment..."
/tmp/bin/micromamba create -y -p "${INSTALL_DIR}/env" \
    -c conda-forge -c bioconda \
    "medaka=${MEDAKA_VERSION}" \
    "pytorch-cpu=2.9.*"

rm -rf /tmp/bin/micromamba
# The conda env carries package caches and static archives nothing needs.
rm -rf "${INSTALL_DIR}/env/pkgs" "${INSTALL_DIR}/env/conda-meta"

# medaka's own dependency, tensordict, has no linux-aarch64 build at any
# version, and every linux-64 build >=0.8.3 requires pytorch<2.8 -- which
# conflicts with the pytorch-cpu=2.9.* pin above. So on both architectures
# the solver falls back to the old noarch build tensordict 0.1.2, whose own
# unpinned dependencies drag in pandas, pyarrow, scipy, scikit-learn,
# plotly, and matplotlib. None of those appear in medaka's own bioconda
# dependency list, and grepping medaka's site-packages shows only
# medaka/torch_ext.py imports tensordict -- neither `import medaka` nor
# `import tensordict` touches any of them. Confirmed by running
# medaka_consensus -h and medaka inference --help after removal: both still
# work. Strip them with pip so the metadata stays consistent; anything pip
# can't remove cleanly falls back to rm -rf on its site-packages dir.
echo "Removing tensordict's unused heavy dependencies..."
"${INSTALL_DIR}/env/bin/pip" uninstall -y \
    pandas pyarrow scipy scikit-learn plotly matplotlib \
    matplotlib-inline fonttools numcodecs contourpy kiwisolver \
    cycler || true
SITE_PACKAGES=$(echo "${INSTALL_DIR}"/env/lib/python3.*/site-packages)
rm -rf "${SITE_PACKAGES}"/pandas* "${SITE_PACKAGES}"/pyarrow* \
    "${SITE_PACKAGES}"/scipy* "${SITE_PACKAGES}"/sklearn* \
    "${SITE_PACKAGES}"/scikit_learn* "${SITE_PACKAGES}"/plotly* \
    "${SITE_PACKAGES}"/matplotlib* "${SITE_PACKAGES}"/mpl_toolkits* \
    "${SITE_PACKAGES}"/fontTools* "${SITE_PACKAGES}"/numcodecs*

# Fail the build here rather than at first job. A probe that finds nothing
# on PATH reads as a broken install to the user; a build that never
# produced the binary should say so while someone is watching.
"${INSTALL_DIR}/env/bin/medaka" --version

# Guard the pin. If a future dependency bump reintroduces a CUDA torch, the
# build fails here rather than silently shipping ~1GB of unusable kernels.
if ls "${INSTALL_DIR}/env/lib/python3."*/site-packages/torch/lib/libtorch_cuda* >/dev/null 2>&1; then
    echo "ERROR: a CUDA build of torch was installed; pytorch-cpu pin failed" >&2
    exit 1
fi
