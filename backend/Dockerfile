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
# sra-toolkit provides fasterq-dump and prefetch, which is how sequencing runs
# are pulled from NCBI. Deliberately not pinned to an exact Debian revision:
# `=3.2.1+dfsg-4` breaks the build the moment the archive supersedes that
# revision, which it does routinely. Pin the base image digest instead if
# reproducibility matters -- that pins every package at once and does not rot.
#
# Its config directory is handled at *runtime*, not here. `vdb-config` writes
# to the invoking user's home, which at build time is not necessarily the
# runtime user's, so doing it in this file would not prevent the first-run
# failure it appears to. NCBI_SETTINGS points at a path under BIOINFO_HOME and
# the download handler ensures it exists before shelling out.
#
# cutadapt comes from apt rather than pip despite being a Python package, and
# that is not a style preference: it ships Cython extension modules with no
# prebuilt aarch64 wheels, so `pip install cutadapt` compiles from source and
# fails in this image for want of gcc. Adding a toolchain to carry one trimmer
# is not worth ~200MB, and Debian's build is the same code. trimmomatic is
# beside it because both are trim alternatives to fastp -- see TOOL_META in
# pipelines/tools.py for what each is actually good at.
#
# fastqc needs a JRE, which is ~200MB of the image on its own. It earns that
# only because its HTML report is the artifact people expect to hand to a
# collaborator -- the before/after numbers the UI shows come from fastp's JSON
# in a single pass, so nothing on the default path depends on it.
#
# STAR is `rna-star` here -- the package name and the binary name disagree,
# and `apt-get install star` installs an unrelated package. Debian's build
# also installs /usr/bin/STAR as a dispatcher that execs a CPU-specific
# binary (STAR-avx2 or a plainer one), the same trick bwa-mem2's upstream
# release uses.
#
# Versions are recorded at build time and surfaced through the API: a set of
# trimming parameters means nothing without the version of the tool that ran
# them, and that ends up in a methods section.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        fastp \
        fastqc \
        default-jre-headless \
        minimap2 \
        bowtie2 \
        hisat2 \
        rna-star \
        samtools \
        cutadapt \
        trimmomatic \
        sra-toolkit \
        bzip2 \
        bcftools \
        tabix \
        subread \
        flye \
        hmmer \
        ivar \
    && rm -rf /var/lib/apt/lists/*
# hmmer is compleasm's dependency, not a standalone BioFlow tool -- see the
# compleasm install below and its own comment for why miniprot and compleasm
# itself are not on this list.
# subread is the package; featureCounts is the binary inside it, and the only
# part of it anything here calls. Named for the package rather than the tool
# because that is what apt understands.
#
# flye is the de novo assembler, and the cheapest of the three worth having:
# Debian packages it (2.9.5, ~37MB) and its dependencies are minimap2 and
# samtools, both already above. hifiasm is not packaged at all and would need
# a source build with the same arm64 SIMD problem bwa-mem2 has, and SPAdes is
# packaged but only assembles short reads, which is not a workflow here yet.
# See docs/superpowers/specs/2026-08-01-de-novo-assembly-design.md.
# bcftools and tabix are separate packages from samtools -- installing samtools
# does not bring htslib's command-line tools along. bcftools does the
# short-read calling and writes every .tbi index; the tabix package is here for
# bgzip, which object_service.py's ingest path calls directly to compress
# FASTQ/FASTA/VCF/etc at ingest time -- see docs/superpowers/specs/
# 2026-08-05-object-compression-design.md. A missing bgzip degrades to
# Python's stdlib gzip rather than failing an ingest, so this package is a
# performance and seekability dependency, not a hard one.

# bwa-mem2 is the exception: it is not packaged for Debian, so it comes from
# the project's own release. On x86-64 the upstream prebuilt binary is the
# recommended way to install it -- it is built with the Intel compiler and
# dispatches at runtime to the SSE/AVX2/AVX512 variant the CPU supports, which
# is most of the ~2x speedup over bwa that makes it worth carrying at all.
#
# On arm64 (Apple Silicon, linux/arm64 Docker), the upstream binaries are
# x86-64 only and fail under Rosetta with "failed to open elf". Instead of
# leaving bwa-mem2 unavailable on arm64, we build from source using sse2neon
# (https://github.com/DLTcollab/sse2neon), which translates Intel SSE/AVX
# intrinsics to ARM NEON at compile time. This is the same technique the
# Homebrew formula (brewsci/bio/bwa-mem2) uses for macOS ARM, adapted here
# for Linux arm64 containers. The build logic is in scripts/build-bwa-mem2-arm64.sh
# to keep this file readable.
#
# Pinned to an exact version rather than "latest": the aligner version is half
# of a run's provenance, and an image that silently changed aligners between
# builds would make two runs of the same pipeline incomparable.
#
# Note the release tag and the reported version disagree: the v2.3 tarball
# ships binaries that self-report 2.2.1. The probe records what the binary
# says, since that is what actually ran. The source build patches the version
# string to 2.3 to match the tag.
ARG TARGETARCH
ARG BWA_MEM2_VERSION=2.3
COPY scripts/build-bwa-mem2-arm64.sh /srv/scripts/build-bwa-mem2-arm64.sh
RUN if [ "$TARGETARCH" = "arm64" ]; then \
      set -e \
      && apt-get update \
      && apt-get install -y --no-install-recommends \
          git cmake dos2unix build-essential ca-certificates zlib1g-dev \
      # Download sources and patches
      && git clone https://github.com/bwa-mem2/bwa-mem2.git /tmp/bwa-mem2 \
      && cd /tmp/bwa-mem2 \
      && git checkout "v${BWA_MEM2_VERSION}" \
      && curl -fsSL \
           "https://raw.githubusercontent.com/DLTcollab/sse2neon/v1.8.0/sse2neon.h" \
           -o src/sse2neon.h \
      && curl -fsSL \
           "https://github.com/intel/safestringlib/archive/refs/tags/v1.2.0.tar.gz" \
           -o /tmp/safestringlib.tar.gz \
      && tar -xzf /tmp/safestringlib.tar.gz -C /tmp \
      && rm /tmp/safestringlib.tar.gz \
      && curl -fsSL \
           "https://gist.githubusercontent.com/YoshitakaMo/eb6e6df7a621a9c9737bcc9363cf9bfc/raw/5936f4884daac3c961c6c9a62d9a0c676f578bbb/fastmap.patch" \
           -o /tmp/fastmap.patch \
      && curl -fsSL \
           "https://gist.githubusercontent.com/YoshitakaMo/c4cabc8e1e4b618047507bc354dbb51e/raw/1265ecf70a976476bd3e55d06804b94f9969310e/bandedSWA.cpp.patch" \
           -o /tmp/bandedSWA.patch \
      # Run the build script (patches source, builds safestringlib, compiles)
      && chmod +x /srv/scripts/build-bwa-mem2-arm64.sh \
      && BWA_MEM2_VERSION="${BWA_MEM2_VERSION}" /srv/scripts/build-bwa-mem2-arm64.sh \
      # Clean up build dependencies and sources
      && rm -rf /tmp/bwa-mem2 /tmp/fastmap.patch /tmp/bandedSWA.patch /tmp/safestringlib-1.2.0 \
      # ca-certificates is deliberately NOT purged here: it is installed in the
      # base tool layer above and later layers (Clair3) need working HTTPS.
      # Purging it broke that silently -- curl failed, and the failure only
      # surfaced as tar choking on an empty stream.
      && apt-get purge -y \
             git cmake dos2unix build-essential zlib1g-dev \
      && apt-get autoremove -y \
      && apt-get clean \
      && rm -rf /var/lib/apt/lists/*; \
    else \
      curl -fsSL \
        "https://github.com/bwa-mem2/bwa-mem2/releases/download/v${BWA_MEM2_VERSION}/bwa-mem2-${BWA_MEM2_VERSION}_x64-linux.tar.bz2" \
        -o /tmp/bwa-mem2.tar.bz2 \
      && tar -xjf /tmp/bwa-mem2.tar.bz2 -C /opt \
      && rm /tmp/bwa-mem2.tar.bz2 \
      # A wrapper, not a symlink. The `bwa-mem2` entrypoint is a dispatcher that
      # picks a SIMD variant (.avx512bw/.avx2/.avx/.sse42/.sse41) and looks for
      # it beside *its own argv[0]* -- so a symlink into /usr/local/bin sends
      # it hunting for /usr/local/bin/bwa-mem2.avx2, which does not exist. It
      # then walks the whole list, fails every one, and still exits 0. Execing
      # the real path keeps the variants beside it where the dispatcher
      # expects them.
      && printf '#!/bin/sh\nexec "/opt/bwa-mem2-%s_x64-linux/bwa-mem2" "$@"\n' "${BWA_MEM2_VERSION}" \
         > /usr/local/bin/bwa-mem2 \
      && chmod +x /usr/local/bin/bwa-mem2; \
    fi

# Clair3, the long-read variant caller. Its own layer and near the end of the
# tool section because it is the largest single addition here (~600MB with
# models) and the slowest to build -- an edit anywhere above should not
# reinstall it, and an edit below must not either.
#
# Bioconda is the only arm64-capable distribution; the install script explains
# why and normalizes the model directory layout. Models are baked in rather
# than fetched at job time so a run does not depend on the network.
ARG CLAIR3_VERSION=2.0.2
COPY scripts/install-clair3.sh /srv/scripts/install-clair3.sh
RUN chmod +x /srv/scripts/install-clair3.sh \
    && CLAIR3_VERSION="${CLAIR3_VERSION}" /srv/scripts/install-clair3.sh \
    && rm -rf /tmp/bin

# --- NCBI Datasets CLI ------------------------------------------------------
#
# How assemblies are downloaded. A single static Go binary, so unlike
# bwa-mem2 there is nothing to compile for arm64 -- only a different URL.
# TARGETARCH is already in scope from the bwa-mem2 ARG above (this is a
# single-stage build, so it need not be redeclared).
RUN set -e \
    && case "$TARGETARCH" in \
         arm64) DATASETS_ARCH=linux-arm64 ;; \
         *)     DATASETS_ARCH=linux-amd64 ;; \
       esac \
    && curl -fsSL \
         "https://ftp.ncbi.nlm.nih.gov/pub/datasets/command-line/v2/${DATASETS_ARCH}/datasets" \
         -o /usr/local/bin/datasets \
    && chmod +x /usr/local/bin/datasets \
    && datasets --version

# --- Docker client ----------------------------------------------------------
#
# For starting *sibling* containers, not for running a daemon here. DeepVariant
# is distributed as an 8.83GB image -- 2.3GB of TensorFlow on a Python 3.10 this
# image does not otherwise ship -- so vendoring it in would roughly double this
# image to gain one variant caller. It runs from its own image instead, started
# through the host's daemon via a socket mounted in docker-compose.override.yml.
#
# The static client tarball rather than the `docker.io` package: that package
# pulls in the daemon, containerd and runc (~400MB) to obtain a binary we use
# only as a client. This is ~50MB, and this image is already large.
ARG DOCKER_CLI_VERSION=27.3.1
RUN set -e \
    && case "$TARGETARCH" in \
         arm64) DOCKER_ARCH=aarch64 ;; \
         *)     DOCKER_ARCH=x86_64 ;; \
       esac \
    && curl -fsSL \
         "https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-${DOCKER_CLI_VERSION}.tgz" \
         -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz \
    # --version only: `docker version` would try to reach a daemon, which does
    # not exist at build time and would fail the build for the wrong reason.
    && docker --version

# Dependencies first so the layer caches across source edits.
# Installed from pyproject.toml rather than a duplicated list: two copies of the
# dependency set drift, and the version that matters is the one in the image.
# NanoPlot for long-read QC. FastQC's per-base model is meaningless for reads
# that vary from 200 bp to 100 kb within one file, so Nanopore and PacBio runs
# get this instead -- see the platform dispatch in run_qc.
#
# Its own layer, and pinned, because it is expensive: it pulls numpy, pandas,
# scipy, pyarrow and plotly, which is the largest single addition to this
# image. Keeping it separate means an application source edit does not
# reinstall it. Installed globally rather than in a venv because the worker
# already runs in the system Python.
# biopython is pinned here even though nothing in this project imports it: it
# is NanoPlot's transitive dependency, and 1.88 ships no aarch64 wheel, so pip
# falls back to the sdist and the build dies with `command 'gcc' failed` --
# the same trap the cutadapt note at the top of this file describes. Pinning
# NanoPlot alone does not prevent it, because the break arrives through a
# dependency NanoPlot leaves unpinned.
#
# It broke arm64 only, which is how it reached main unnoticed: amd64 still
# resolves a wheel and builds clean. Raise this pin when biopython publishes
# aarch64 wheels again, not before -- check PyPI rather than assuming a newer
# release has them.
#
# Unlike cutadapt this cannot come from apt: NanoPlot imports biopython, so it
# has to be installed into this image's Python 3.12, whereas Debian's
# python3-biopython targets the system python3 that nothing here runs.
RUN pip install --no-cache-dir biopython==1.87 NanoPlot==1.45.0

# PyDESeq2 rather than Bioconductor's DESeq2, which is also packaged for
# trixie (r-bioc-deseq2). Both implement the same negative-binomial GLM; the
# difference is what they drag in. Measured on this image: `apt-get install -s
# --no-install-recommends r-bioc-deseq2` is 110 packages plus a full R
# runtime, in an image that already carries arm64 special-casing for
# bwa-mem2. PyDESeq2 is 28 pip packages and stays inside the Python the worker
# already runs, so results come back as a DataFrame instead of a TSV written
# by another language.
#
# The honest cost, recorded so nobody rediscovers it as a bug: R DESeq2 is
# what a reviewer expects to see cited, and PyDESeq2 implements a narrower
# slice of `lfcShrink`.
#
# Its own layer for the same reason as NanoPlot: it pulls anndata, zarr, h5py,
# scikit-learn and matplotlib, and an application source edit should not
# reinstall them. Pinned, because a DE result is only comparable to another
# one from the same version.
RUN pip install --no-cache-dir pydeseq2==0.5.4

# RagTag, reference-guided scaffolding. Not in Debian (checked with an
# apt-get update first: no candidate for ragtag, ragtag-python, or
# python3-ragtag). Pure Python and PyPI carries it, so pip is correct here
# rather than a source build or a script -- unlike Polypolish this has no
# arm64 problem, since its only external binary dependency is minimap2,
# already installed above for both architectures. Pinned because a
# scaffolding result is only comparable to another one from the same
# version, the same reasoning pydeseq2 above gives. Of its four dependencies
# (intervaltree, networkx, numpy, pysam) only intervaltree is new to this
# image -- networkx, numpy and pysam already arrived via NanoPlot/PyDESeq2
# and pysam's own top-level dependency.
RUN pip install --no-cache-dir ragtag==2.1.0

# compleasm, the assembly completeness tool -- BUSCO's job, ~10-20x faster,
# and it recovers BUSCOs the metaeuk step in Debian's own busco package
# misses. Placed after the pip installs above rather than beside Clair3:
# compleasm imports pandas directly and setup.py declares no dependency on
# it, so this relies on NanoPlot/PyDESeq2 having already installed a
# pandas-compatible stack for it to resolve against. See
# docs/superpowers/specs/2026-08-02-post-assembly-qc-design.md and the
# install script's own comments for why this is a source build rather than
# the release tarball (x86-only) or the biocontainer (also x86-only, despite
# looking multi-arch).
ARG MINIPROT_VERSION=0.18
ARG COMPLEASM_VERSION=0.2.9
COPY scripts/install-compleasm.sh /srv/scripts/install-compleasm.sh
RUN chmod +x /srv/scripts/install-compleasm.sh \
    && MINIPROT_VERSION="${MINIPROT_VERSION}" \
       COMPLEASM_VERSION="${COMPLEASM_VERSION}" \
       /srv/scripts/install-compleasm.sh

# --- Polypolish -------------------------------------------------------------
#
# Short-read polishing of long-read assemblies. Not in Debian; installed from
# upstream's musl-static release binary, and deliberately skipped on arm64 --
# see the install script's comments for both decisions.
ARG POLYPOLISH_VERSION=0.7.1
COPY scripts/install-polypolish.sh /srv/scripts/install-polypolish.sh
RUN chmod +x /srv/scripts/install-polypolish.sh \
    && POLYPOLISH_VERSION="${POLYPOLISH_VERSION}" \
       TARGETARCH="${TARGETARCH}" \
       /srv/scripts/install-polypolish.sh

# QUAST, reference-based misassembly QC. Not in Debian; PyPI stops at 5.2.0,
# which is dead on this image's Python 3.12 (`distutils` was removed), so
# this takes the 5.3.0 GitHub release tarball instead and patches it -- see
# the install script's comments, including why this is not a source build
# despite not being packaged.
ARG QUAST_VERSION=5.3.0
COPY scripts/install-quast.sh /srv/scripts/install-quast.sh
RUN chmod +x /srv/scripts/install-quast.sh \
    && QUAST_VERSION="${QUAST_VERSION}" \
       /srv/scripts/install-quast.sh

COPY pyproject.toml ./
RUN mkdir -p app && touch app/__init__.py \
    && pip install --no-cache-dir '.[dev]' \
    && pip uninstall -y biopipe-backend

COPY app ./app
COPY tests ./tests

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
