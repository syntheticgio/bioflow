FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Bioinformatics tools come from Debian rather than bioconda: on trixie the
# versions are current (fastp 0.24.0, FastQC 0.12.1, minimap2 2.27,
# samtools 1.21) and it avoids carrying a conda installation for a handful of
# binaries. bwa-mem2 is the one exception, installed from upstream below.
#
# bzip2 is there for that install: the slim image has tar but not the bzip2
# binary tar shells out to for `-j`, so `tar --help` advertises support that
# fails at runtime.
#
# fastqc needs a JRE, which is ~200MB of the image on its own. It earns that
# only because its HTML report is the artifact people expect to hand to a
# collaborator -- the before/after numbers the UI shows come from fastp's JSON
# in a single pass, so nothing on the default path depends on it.
#
# Versions are recorded at build time and surfaced through the API: a set of
# trimming parameters means nothing without the version of the tool that ran
# them, and that ends up in a methods section.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        fastp \
        fastqc \
        default-jre-headless \
        minimap2 \
        samtools \
        bzip2 \
    && rm -rf /var/lib/apt/lists/*

# bwa-mem2 is the exception: it is not packaged for Debian, so it comes from the
# project's own release. The upstream binary is the recommended way to install
# it -- it is built with the Intel compiler and dispatches at runtime to the
# SSE/AVX2/AVX512 variant the CPU supports, which is most of the ~2x speedup
# over bwa that makes it worth carrying at all.
#
# Pinned to an exact version rather than "latest": the aligner version is half
# of a run's provenance, and an image that silently changed aligners between
# builds would make two runs of the same pipeline incomparable.
#
# x86-64 only, and genuinely so: on an arm64 host these binaries fail under
# Rosetta with "failed to open elf". The `bwa_mem2()` probe reports that as an
# unavailable tool in the launch dialog rather than letting it become a job
# that dies minutes in -- build for linux/amd64 to use bwa-mem2 on Apple
# Silicon. minimap2 is packaged for both and works either way.
#
# Note the release tag and the reported version disagree: the v2.3 tarball
# ships binaries that self-report 2.2.1. The probe records what the binary
# says, since that is what actually ran.
ARG BWA_MEM2_VERSION=2.3
RUN curl -fsSL \
      "https://github.com/bwa-mem2/bwa-mem2/releases/download/v${BWA_MEM2_VERSION}/bwa-mem2-${BWA_MEM2_VERSION}_x64-linux.tar.bz2" \
      -o /tmp/bwa-mem2.tar.bz2 \
    && tar -xjf /tmp/bwa-mem2.tar.bz2 -C /opt \
    && rm /tmp/bwa-mem2.tar.bz2 \
# A wrapper, not a symlink. The `bwa-mem2` entrypoint is a dispatcher that
# picks a SIMD variant (.avx512bw/.avx2/.avx/.sse42/.sse41) and looks for it
# beside *its own argv[0]* -- so a symlink into /usr/local/bin sends it hunting
# for /usr/local/bin/bwa-mem2.avx2, which does not exist. It then walks the
# whole list, fails every one, and still exits 0. Execing the real path keeps
# the variants beside it where the dispatcher expects them.
    && printf '#!/bin/sh\nexec "/opt/bwa-mem2-%s_x64-linux/bwa-mem2" "$@"\n' "${BWA_MEM2_VERSION}" \
       > /usr/local/bin/bwa-mem2 \
    && chmod +x /usr/local/bin/bwa-mem2

# Dependencies first so the layer caches across source edits.
# Installed from pyproject.toml rather than a duplicated list: two copies of the
# dependency set drift, and the version that matters is the one in the image.
COPY pyproject.toml ./
RUN mkdir -p app && touch app/__init__.py \
    && pip install --no-cache-dir '.[dev]' \
    && pip uninstall -y biopipe-backend

COPY app ./app
COPY tests ./tests

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
