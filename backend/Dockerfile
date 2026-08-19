# meryl 1.4.2's Linux-arm64 release binary links against OpenSSL 1.1
# (libssl.so.1.1 / libcrypto.so.1.1), which Debian trixie's own archive no
# longer carries (trixie ships OpenSSL 3.x only, no libssl1.1 apt candidate).
# bullseye-slim still has it -- Debian's oldstable-of-oldstable, still
# resolvable via debian-security as of 2026-08-07 (libssl1.1 1.1.1w-0+deb11u8)
# -- so this stage exists only to donate those two .so files via a
# multi-stage COPY. No RUN needed: bullseye-slim has them installed by
# default. See install-meryl.sh's own header for the full story and
# tools.meryl()'s probe.
FROM debian:bullseye-slim AS legacy-ssl

# Winnowmap ships NO binary releases (v2.03's GitHub release asset list is
# empty), so it has to be compiled -- and that toolchain (build-essential,
# zlib1g-dev, git) has no reason to live in the shipped image once the one
# ~1.5 MB binary it produces exists. This stage builds it and stage two
# COPYs only the binary out. See install-winnowmap.sh for the two
# aarch64-specific build gotchas (the top-level Makefile's `-e` discarding
# the aarch64 `-fsigned-char` flag, and the bundled ext/meryl that must not
# be built) this script's own header documents in full.
FROM python:3.12-slim AS winnowmap-build
WORKDIR /srv
ARG WINNOWMAP_VERSION=2.03
COPY scripts/install-winnowmap.sh /srv/scripts/install-winnowmap.sh
RUN chmod +x /srv/scripts/install-winnowmap.sh \
    && WINNOWMAP_VERSION="${WINNOWMAP_VERSION}" \
       /srv/scripts/install-winnowmap.sh

# edlib is a required Sniffles dependency and publishes no linux-aarch64
# wheel for any Python version -- only x86-64 wheels and an sdist (verified
# 2026-08-18 against PyPI). The final image carries no compiler, so pip would
# fall back to that sdist and die with `command 'gcc' failed`, exactly as
# recorded at line 336 for another package and at line 59 for cutadapt.
#
# Building the wheel here keeps the toolchain in a stage that is discarded
# and leaves both architectures running identical code. This fails *only* on
# arm64, so a bare `pip install sniffles` passes x86-64 CI and breaks on an
# Apple Silicon machine.
FROM python:3.12-slim AS edlib-build
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN pip wheel --no-deps --wheel-dir /wheels edlib==1.3.9.post1

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
        abyss \
        hmmer \
        ivar \
        bedtools \
        r-cran-argparse \
        r-cran-ggplot2 \
        r-cran-scales \
        libcurl4t64 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*
# libcurl4t64 and libgomp1 are meryl's release binary's remaining runtime
# deps beyond OpenSSL -- both present normally in trixie's own archive, no
# vendoring needed. See the legacy-ssl build stage above and
# install-meryl.sh for the two that are.
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
# a source build with the same arm64 SIMD problem bwa-mem2 has.
# abyss is the short-read assembler (2.3.10, paired-end de Bruijn) and stays
# the default for short reads. SPAdes is the better choice on isolates and is
# now installed too -- not from apt (trixie has no candidate, verified
# 2026-08-17) and not from a single vendored tarball either, since upstream
# publishes no Linux-arm64 binary. See the SPAdes layer below.
# See docs/superpowers/specs/2026-08-01-de-novo-assembly-design.md.
# bcftools and tabix are separate packages from samtools -- installing samtools
# does not bring htslib's command-line tools along. bcftools does the
# short-read calling and writes every .tbi index; the tabix package is here for
# bgzip, which object_service.py's ingest path calls directly to compress
# FASTQ/FASTA/VCF/etc at ingest time -- see docs/superpowers/specs/
# 2026-08-05-object-compression-design.md. A missing bgzip degrades to
# Python's stdlib gzip rather than failing an ingest, so this package is a
# performance and seekability dependency, not a hard one.

# --- flye-samtools shim -----------------------------------------------------
#
# Flye 2.9.5 upstream vendors its own samtools build and invokes it as
# `flye-samtools`. Debian's flye 2.9.5+dfsg-1 unbundles that binary -- the
# +dfsg suffix says so -- and declares `Depends: minimap2, samtools`,
# expecting the packaged /usr/bin/samtools to serve instead.
#
# The unbundling patched one of the two call sites and missed the other:
# flye/polishing/alignment.py:27 was rewritten to "/usr/bin/samtools", but
# flye/utils/sam_parser.py:43 still reads SAMTOOLS_BIN = "flye-samtools" and
# reaches it from three places (two `depth` calls and a `view`). `dpkg -L
# flye` ships no such binary, so every assembly died at the consensus stage
# with "flye-samtools: not found" and an empty consensus.fasta. See
# https://github.com/syntheticgio/bioflow/issues/67.
#
# A wrapper rather than a symlink. Either works -- unlike bwa-mem2 below,
# samtools has no argv[0]-relative dispatch to break -- but the wrapper
# matches that precedent and states the indirection outright.
#
# The trailing --version is a build-time assertion, in this same RUN so it
# cannot drift from what it checks: the build fails if samtools ever moves off
# /usr/bin/samtools. Same shape as the `datasets --version` and
# `docker --version` checks further down this file.
RUN printf '#!/bin/sh\nexec /usr/bin/samtools "$@"\n' > /usr/local/bin/flye-samtools \
    && chmod +x /usr/local/bin/flye-samtools \
    && flye-samtools --version

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

# --- SPAdes ----------------------------------------------------------------
#
# The better short-read assembler on bacterial isolates, and the reason this
# is its own late layer is size: ~193MB built, ~196MB vendored. An edit above
# should not reinstall it.
#
# Not packaged for trixie, and -- unlike every other vendored tool here --
# with no Linux-arm64 release asset either, so the install script branches on
# architecture: prebuilt binary on amd64, source build on arm64. See
# scripts/install-spades.sh's header and
# docs/superpowers/specs/2026-08-18-spades-short-read-assembly-design.md.
ARG SPADES_VERSION=4.3.0
COPY scripts/install-spades.sh /srv/scripts/install-spades.sh
RUN chmod +x /srv/scripts/install-spades.sh \
    && SPADES_VERSION="${SPADES_VERSION}" \
       /srv/scripts/install-spades.sh

# --- Salmon ----------------------------------------------------------------
#
# Alignment-free transcript quantification. Prebuilt binaries per architecture from COMBINE-lab.
# Debian trixie's packaged salmon (1.10.2+ds1-1+b5) arm64 build crashes with SIGILL (exit 132)
# on Apple Silicon / ARM VMs due to CPU instruction set mismatches (#667).
ARG SALMON_VERSION=1.12.1
COPY scripts/install-salmon.sh /srv/scripts/install-salmon.sh
RUN chmod +x /srv/scripts/install-salmon.sh \
    && SALMON_VERSION="${SALMON_VERSION}" \
       /srv/scripts/install-salmon.sh

# --- Medaka ----------------------------------------------------------------
#
# ONT's neural-network consensus tool, and the only polishing path for
# long-read assemblies -- Polypolish above is short-read-only. Its own late
# layer for the same reason Clair3 and SPAdes are late: it is large (medaka
# plus a CPU torch) and slow, and an edit anywhere above should not
# reinstall it.
#
# Installed on both architectures, unlike Polypolish: bioconda ships
# linux-aarch64 builds, which makes this the only polisher available on
# Apple Silicon. See scripts/install-medaka.sh for why pytorch-cpu is
# pinned there, and
# docs/superpowers/specs/2026-08-18-medaka-long-read-polishing-design.md.
ARG MEDAKA_VERSION=2.2.2
COPY scripts/install-medaka.sh /srv/scripts/install-medaka.sh
RUN chmod +x /srv/scripts/install-medaka.sh \
    && MEDAKA_VERSION="${MEDAKA_VERSION}" \
       /srv/scripts/install-medaka.sh

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

# --- seqkit -------------------------------------------------------------
#
# Single static Go binary, same shape as the NCBI datasets CLI above --
# only the URL and archive layout differ. Used by the planned region/
# sequence extraction feature (#632). Version pinned; seqkit does not
# publish sha256 checksums, only MD5 sidecar files per release asset, so
# this verifies with md5sum rather than the sha256sum this file uses
# elsewhere.
ARG SEQKIT_VERSION=2.13.0
RUN set -e \
    && case "$TARGETARCH" in \
         arm64) SEQKIT_ARCH=linux_arm64; SEQKIT_MD5=77aaa9f03792553e993e96157421e87f ;; \
         *)     SEQKIT_ARCH=linux_amd64; SEQKIT_MD5=872368c1e24706dbd1f931d26b38d7d1 ;; \
       esac \
    && curl -fsSL \
         "https://github.com/shenwei356/seqkit/releases/download/v${SEQKIT_VERSION}/seqkit_${SEQKIT_ARCH}.tar.gz" \
         -o /tmp/seqkit.tar.gz \
    && echo "${SEQKIT_MD5}  /tmp/seqkit.tar.gz" | md5sum -c - \
    && tar -xzf /tmp/seqkit.tar.gz -C /usr/local/bin seqkit \
    && chmod +x /usr/local/bin/seqkit \
    && rm /tmp/seqkit.tar.gz \
    && seqkit version

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

# Sniffles2, the long-read structural variant caller. Its edlib dependency
# comes from the wheel built in the edlib-build stage above -- see there for
# why. --no-index is deliberately absent: the other dependencies (pysam,
# pyspoa, numpy, psutil) all publish arm64 wheels and come from PyPI.
COPY --from=edlib-build /wheels /tmp/wheels
RUN pip install --no-cache-dir --find-links /tmp/wheels sniffles==2.8.0 \
    && rm -rf /tmp/wheels

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

# --- Kraken2 / Bracken -------------------------------------------------------
# Taxonomic classification and abundance re-estimation. Neither is packaged
# for trixie; both are plain C++/shell with no arch-specific code, so a
# source build (each tool's own install_*.sh from its pinned release tag) is
# the same route compleasm above takes, and works on both amd64 and arm64.
# The reference database is not installed here -- it is several GB and is
# delivered on demand at launch time, per tools.kraken2()/tools.bracken().
ARG KRAKEN2_VERSION=2.1.3
ARG BRACKEN_VERSION=2.9
COPY scripts/install-kraken2.sh /srv/scripts/install-kraken2.sh
RUN chmod +x /srv/scripts/install-kraken2.sh \
    && KRAKEN2_VERSION="${KRAKEN2_VERSION}" \
       BRACKEN_VERSION="${BRACKEN_VERSION}" \
       /srv/scripts/install-kraken2.sh

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

# --- CRAQ --------------------------------------------------------------
#
# Reference-free assembly error detection. Not packaged for Debian trixie;
# installed straight from GitHub since CRAQ publishes no release tags -- see
# the install script's comments for why the whole tree, not just bin/craq,
# has to come along, and for how CRAQ_COMMIT pins a specific commit despite
# there being no tag to name.
ARG CRAQ_COMMIT=63509381fe85c4bd5832f1c67d0279c823ce9592
COPY scripts/install-craq.sh /srv/scripts/install-craq.sh
RUN chmod +x /srv/scripts/install-craq.sh \
    && CRAQ_COMMIT="${CRAQ_COMMIT}" \
       /srv/scripts/install-craq.sh

# --- meryl / Merqury -----------------------------------------------------
#
# Merqury needs meryl, and specifically Marbl's meryl -- Debian's `meryl`
# package is the unrelated Celera Assembler k-mer suite. Both installs are
# binary/source extracts with no compilation; see each script's header for
# why the meryl version pin is load-bearing.
COPY scripts/install-meryl.sh /srv/scripts/install-meryl.sh
RUN chmod +x /srv/scripts/install-meryl.sh \
    && /srv/scripts/install-meryl.sh

# meryl's release binary needs OpenSSL 1.1, gone from trixie's own archive on
# both architectures (verified: neither libssl1.1 nor libssl3 has an apt
# candidate on trixie amd64 either -- trixie ships OpenSSL 3.x via libssl3t64
# instead, and nothing here links against that). Vendored from bullseye-slim
# via the legacy-ssl stage above rather than downloaded at install-script run
# time, since apt has no candidate for it in this image at all. Copied
# unconditionally for both arches rather than gated on arm64 -- on amd64
# these two files are a few hundred KB of unused dead weight if the amd64
# release binary turns out to link against system OpenSSL fine, which is
# cheaper than adding arch-conditional COPY logic to find out.
# Globbed rather than hardcoded to aarch64-linux-gnu: legacy-ssl builds
# natively for whatever platform this build targets, so the multiarch
# triplet directory it populates is amd64's x86_64-linux-gnu on an amd64
# build and arm64's aarch64-linux-gnu on an arm64 build. Verified both
# resolve on debian:bullseye-slim via `docker run --platform linux/amd64|
# linux/arm64 debian:bullseye-slim sh -c 'ls /usr/lib/*-linux-gnu/libssl.so.1.1'`
# on 2026-08-07. Exactly one file matches per build since only one triplet
# directory exists in a single-arch stage.
COPY --from=legacy-ssl /usr/lib/*-linux-gnu/libssl.so.1.1 /opt/meryl/lib/
COPY --from=legacy-ssl /usr/lib/*-linux-gnu/libcrypto.so.1.1 /opt/meryl/lib/

COPY scripts/install-merqury.sh /srv/scripts/install-merqury.sh
RUN chmod +x /srv/scripts/install-merqury.sh \
    && /srv/scripts/install-merqury.sh

# Every Merqury script begins `source $MERQURY/util/util.sh` and fails
# immediately without it, so this belongs in the image ENV rather than in
# the install script's shell.
ENV MERQURY=/opt/merqury
ENV PATH="/opt/meryl/bin:${PATH}"
# See the vendored libssl.so.1.1/libcrypto.so.1.1 COPY above -- meryl's
# release binary needs them and trixie's archive no longer carries them.
ENV LD_LIBRARY_PATH="/opt/meryl/lib:${LD_LIBRARY_PATH:-}"

# --- GCI ---------------------------------------------------------------
#
# GCI consumes finished long-read alignments; it never runs an aligner
# itself, so there is no winnowmap dependency and nothing to compile.
ARG GCI_COMMIT=543cd4136187ff3ddd3ba4d1585626dbcdef6af6
COPY scripts/install-gci.sh /srv/scripts/install-gci.sh
RUN chmod +x /srv/scripts/install-gci.sh \
    && GCI_COMMIT="${GCI_COMMIT}" \
       /srv/scripts/install-gci.sh

# --- winnowmap -----------------------------------------------------------
#
# GCI's own FAQ recommends pairing winnowmap with minimap2 for higher
# sensitivity in repetitive regions -- see gci_runner.py's module docstring.
# The binary itself was built in the winnowmap-build stage above; only the
# ~1.5 MB result is copied in here, not the compiler toolchain that built
# it. `ldd` against a real build showed no dependency beyond libz,
# libstdc++, libm, libgomp, libgcc_s, libc -- all already present in this
# image, so no vendoring is needed the way meryl needed OpenSSL 1.1.
COPY --from=winnowmap-build /opt/winnowmap/bin/winnowmap /opt/winnowmap/bin/winnowmap
ENV PATH="/opt/winnowmap/bin:${PATH}"

# --- Pi coding agent -----------------------------------------------------
#
# The in-app AI agent harness (epic #30) spawns `pi --mode rpc` as a
# subprocess per project; see docs/superpowers/specs/
# 2026-08-09-ai-agent-harness-design.md.
#
# Pinned, same rationale as every other version pin in this file: the RPC
# event stream is part of the backend's SSE translation contract, and a
# silent pi upgrade could change its shape without any error anywhere.
# Local-only tool: no install telemetry.
ENV PI_TELEMETRY=0
ARG NODE_VERSION=22.23.2
ARG PI_VERSION=0.84.1
ARG PI_MCP_ADAPTER_VERSION=2.21.1
ARG PI_SUBAGENTS_VERSION=0.45.2
ARG PI_HERMES_MEMORY_VERSION=0.9.4
ARG MCP_FETCH_VERSION=2026.7.10
ARG NCBI_DATASETS_MCP_VERSION=0.1.4
# The external MCP servers must run against mcp<2 (see the venv RUN below),
# while the backend pins mcp>=2.0,<3 for its own Streamable HTTP server.
ARG AGENT_MCP_MCP_VERSION=1.29.0
#
# pi requires Node >= 22.19 (its package.json engines field). python:3.12-slim
# ships no Node at all, and trixie's apt nodejs is 20.x, so Node comes from
# NodeSource. Three traps, two of which already cost a failed build once:
#
# - This image ships WITHOUT curl by design: install-meryl.sh and
#   install-quast.sh purge it after their own downloads, so no later layer
#   can assume it exists. Install it here, use it, and purge it again so the
#   end state matches what those scripts established.
# - The setup script is fetched through a pipe in some docs (`curl | bash -`);
#   in `sh` the pipeline's exit status is the script's, not curl's, so a
#   failed fetch silently leaves no repo configured and apt falls back to
#   trixie's nodejs 20 -- a green build with the wrong Node. Download to a
#   file so curl's failure aborts the build.
# - The script's own `apt-get update` can flake, leaving the lists without
#   the nodistro repo. Re-running update after the script, pinning the exact
#   package version, and asserting `node --version` make every failure mode
#   loud instead of silently degrading.
RUN set -e \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x -o /tmp/setup-node.sh \
    && bash /tmp/setup-node.sh \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs=${NODE_VERSION}-1nodesource1 \
    && node --version \
    # Restore the no-curl end state the purge scripts above established.
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/setup-node.sh

# pi itself, via npm (the package that ships the `pi` binary and the RPC CLI).
RUN set -e \
    && npm install -g @earendil-works/pi-coding-agent@${PI_VERSION} \
    && pi --version

# pi-mcp-adapter is what lets pi talk to any MCP server: it registers the
# `--mcp-config` flag the backend passes per spawn, and exposes each server
# through a single lazy-connecting proxy tool. Installed with pi's own
# package manager, which must run inside this image (it writes
# ~/.pi/agent/settings.json). Headless -- no prompts, safe at build time.
# `pi list` proves the adapter registered; a build that fails here is
# caught before it can produce an image whose agent has no tools.
RUN set -e \
    && pi install npm:pi-mcp-adapter@${PI_MCP_ADAPTER_VERSION} \
    && pi install npm:pi-subagents@${PI_SUBAGENTS_VERSION} \
    && pi install npm:pi-hermes-memory@${PI_HERMES_MEMORY_VERSION} \
    && pi list

# The two external stdio MCP servers, installed in an isolated venv.
# They cannot share the main environment: the backend pins mcp>=2.0,<3 for
# its own Streamable HTTP server, but mcp-server-fetch (every PyPI release)
# and its dependency graph import the pre-2.0 name `McpError`, which mcp
# 2.0.0 renamed to `MCPError` -- so in the main env either the backend or
# the fetch server is broken. This venv pins mcp<2, so the servers run
# against the API they were written for, and the backend's mcp 2.x stays
# untouched. The venv bin paths are what the agent's --mcp-config references.
ENV AGENT_MCP_VENV=/opt/agent-mcp/venv
RUN set -e \
    && python -m venv ${AGENT_MCP_VENV} \
    && ${AGENT_MCP_VENV}/bin/pip install --no-cache-dir \
        mcp==${AGENT_MCP_MCP_VERSION} \
        mcp-server-fetch==${MCP_FETCH_VERSION} \
        ncbi-datasets-mcp==${NCBI_DATASETS_MCP_VERSION} \
    && ${AGENT_MCP_VENV}/bin/python -c "import mcp_server_fetch, ncbi_datasets_mcp"

# Verify the console scripts exist (they are what the agent's config invokes).
RUN set -e \
    && command -v ${AGENT_MCP_VENV}/bin/mcp-server-fetch \
    && command -v ${AGENT_MCP_VENV}/bin/ncbi-datasets-mcp

# Skills that teach the agent how to use BioFlow (written against the mcp
# proxy tool and the real bioflow_* tool names; see pi-skills/README.md).
COPY pi-skills /root/.pi/agent/skills

COPY pyproject.toml ./
RUN mkdir -p app && touch app/__init__.py \
    && pip install --no-cache-dir '.[dev]' \
    && pip uninstall -y biopipe-backend

# Medaka's env goes on PATH only now, after every bare `pip install` in this
# image that targets the system Python -- including this file's own
# dependency install immediately above. A bare `pip install` resolves `pip`
# via PATH: placed before this line it lands in the system site-packages as
# intended; placed after it would silently land inside
# /opt/medaka/env/lib/python3.12/site-packages instead, the same way this bug
# happened the first time -- NanoPlot's `pip install` (and, it turned out,
# this project's own `pip install '.[dev]'`) picked up Medaka's env because
# this ENV line used to sit right after the Medaka layer near the top of the
# file, which pulled pandas/scipy/pyarrow and eventually the entire backend
# dependency tree into Medaka's isolated env and inflated /opt/medaka from
# tens of MB to multiple GB with no error anywhere. This line is also not
# just a build-ordering convenience that later becomes safe to delete once
# config.py resolves medaka_path/medaka_binary_path by absolute path:
# medaka_consensus itself calls `medaka` and `mini_align` by bare name
# internally, so removing this from PATH would break every real polish job
# the moment it reaches those internal calls, with nothing catching it short
# of a real run failing.
ENV PATH="/opt/medaka/env/bin:${PATH}"

COPY app ./app
COPY tests ./tests

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
