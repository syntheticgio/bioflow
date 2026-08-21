#!/bin/sh
# Install MEGAHIT, the short-read metagenome assembler.
#
# Not in Debian. Checked against the running api container (trixie, arm64) on
# 2026-08-21 *with* a control package in the same run, per the false negative
# install-polypolish.sh documents: `apt-cache policy samtools` resolves
# 1.21-1 while `apt-cache policy megahit` reports no candidate at all, and
# `apt-cache search megahit` returns nothing.
#
# BIOCONDA RATHER THAN A RELEASE BINARY, AND THAT CHOICE IS WHAT MAKES THIS
# WORK ON APPLE SILICON. Upstream's releases page ships x86-64 Linux tarballs
# only -- there is no Linux arm64 asset at all. Bioconda publishes 1.2.9 for
# BOTH linux-64 and linux-aarch64 (verified against the anaconda.org API on
# 2026-08-21), so there is no arm64 skip and tools.megahit() needs no
# architecture branch, unlike install-spades.sh and install-checkm2.sh.
#
# THE VERSION PIN: 1.2.9 is the only version bioconda publishes for
# linux-aarch64. Relaxing it to a floor would resolve fine on x86-64 and fail
# to solve on arm64 -- an asymmetry that would show up only on one half of
# what release.yml publishes.
#
# Same micromamba mechanics as install-metabat2.sh -- the binary is
# downloaded, used once, and deleted.
set -eu

MEGAHIT_VERSION="${MEGAHIT_VERSION:-1.2.9}"
INSTALL_DIR="/opt/megahit"

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

# micromamba publishes per-arch builds; picking the wrong one yields an
# "exec format error" that reads like a corrupt download.
case "$(uname -m)" in
    aarch64|arm64) MAMBA_ARCH="linux-aarch64" ;;
    x86_64|amd64)  MAMBA_ARCH="linux-64" ;;
    *)
        echo "ERROR: unsupported architecture $(uname -m) for megahit" >&2
        exit 1
        ;;
esac

# Download to a file and check it before unpacking -- piping the download
# into tar hides which half failed.
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

echo "Creating megahit ${MEGAHIT_VERSION} environment..."
/tmp/bin/micromamba create -y -p "${INSTALL_DIR}/env" \
    -c conda-forge -c bioconda \
    "megahit=${MEGAHIT_VERSION}"

rm -rf /tmp/bin/micromamba
# The conda env carries package caches and static archives nothing needs.
rm -rf "${INSTALL_DIR}/env/pkgs" "${INSTALL_DIR}/env/conda-meta"

# Trim the build-time surface only. `lib/` MUST NOT be removed: the same
# dlopen trap install-mosdepth.sh documents applies here -- megahit_core links
# its shared libraries out of this env, and deleting lib/ leaves
# `megahit --version` printing a version while every real assembly dies on a
# missing shared object. That is exactly why the check at the bottom of this
# script is a real assembly rather than `--version`.
#
# `bin/` keeps more than the wrapper: `megahit` is a Python script that
# execs `megahit_core` (and `megahit_core_popcnt` / `megahit_core_no_hw_accel`,
# chosen at runtime by a CPUID probe). Removing any of them turns a working
# install into one that fails on some hosts and not others.
rm -rf "${INSTALL_DIR}/env/include" "${INSTALL_DIR}/env/share" \
    "${INSTALL_DIR}/env/ssl"
rm -f "${INSTALL_DIR}"/env/lib/*.a

# A wrapper rather than a symlink, matching install-metabat2.sh: the megahit
# script resolves its sibling megahit_core binaries relative to its own
# location, and whether that survives a symlinked entry point is a question
# this sidesteps rather than answers.
#
# THE `PATH=` PREFIX IS LOAD-BEARING, and is the one thing this wrapper has
# that install-metabat2.sh's does not. `megahit` is not a binary: it is a
# Python script whose shebang is `#!/usr/bin/env python3`, and `python` is a
# declared dependency of the bioconda package precisely because of that. Ship
# the wrapper without this prefix and `env` resolves python3 against the
# *caller's* PATH -- which is the image's own /usr/bin, not this env -- so
# every invocation dies with:
#
#     env: 'python3': No such file or directory
#
# Verified by building the env exactly as this script does and running the
# unprefixed wrapper on 2026-08-21. Putting the env's bin first also keeps
# the megahit_core binaries resolvable by name, which is how the wrapper
# execs them after its CPUID probe.
cat > /usr/local/bin/megahit <<'WRAPPER'
#!/bin/sh
PATH="/opt/megahit/env/bin:${PATH}"
export PATH
exec /opt/megahit/env/bin/megahit "$@"
WRAPPER
chmod +x /usr/local/bin/megahit

# Fail the build here rather than at first job: a probe that finds nothing on
# PATH reads to the user as a broken install, and a build that never produced
# a working binary should say so while someone is watching.
megahit --version > /dev/null

# `--version` is deliberately not the whole check. It exercises none of the
# SdBG construction this binary actually assembles with, and per the note on
# lib/ above it would keep passing with the shared libraries deleted. So the
# guard that matters is a real assembly, end to end.
#
# The fixture is a two-organism community at different abundances -- the shape
# MEGAHIT exists to assemble. Reads are tiled with overlap so a de Bruijn
# graph can actually traverse them; k=21 (MEGAHIT's k-min) needs far less than
# the 150bp reads written here.
echo "Verifying a real assembly..."
MEGAHIT_SMOKE="$(mktemp -d)"
python3 - "${MEGAHIT_SMOKE}" <<'FIXTURE'
import random
import sys

out = sys.argv[1]
random.seed(11)


def seq(n, gc):
    return "".join(
        random.choice("GC") if random.random() < gc else random.choice("AT")
        for _ in range(n)
    )


# Two "organisms", one AT-rich and abundant, one GC-rich and rarer.
genomes = [("orgA", seq(30_000, 0.32), 25), ("orgB", seq(30_000, 0.65), 8)]

RL = 150
r1, r2 = [], []
rid = 0
for name, g, depth in genomes:
    # Step chosen so consecutive reads overlap heavily -- a sparse tiling
    # assembles into fragments and makes this check flaky for reasons that
    # have nothing to do with the install being broken.
    step = max(1, (RL * 2) // depth)
    for pos in range(0, len(g) - 2 * RL - 300, step):
        frag = g[pos:pos + 2 * RL + 300]
        fwd = frag[:RL]
        # Reverse complement of the fragment's tail, as a real mate would be.
        rev = frag[-RL:][::-1].translate(str.maketrans("ACGT", "TGCA"))
        r1.append(f"@r{rid}/1\n{fwd}\n+\n{'I' * RL}\n")
        r2.append(f"@r{rid}/2\n{rev}\n+\n{'I' * RL}\n")
        rid += 1

with open(f"{out}/r1.fq", "w") as fh:
    fh.write("".join(r1))
with open(f"{out}/r2.fq", "w") as fh:
    fh.write("".join(r2))
FIXTURE

# Note -o must NOT already exist unless --force is given: MEGAHIT refuses an
# existing output directory outright. That is the same constraint
# assembly_runner._megahit_command handles with --force, and exercising the
# no-force path here keeps this script honest about the tool's real default.
if ! megahit -1 "${MEGAHIT_SMOKE}/r1.fq" -2 "${MEGAHIT_SMOKE}/r2.fq" \
        -o "${MEGAHIT_SMOKE}/asm" -t 2 -m 2000000000 \
        --min-contig-len 500 > /dev/null 2>&1; then
    echo "ERROR: megahit installed but cannot assemble." >&2
    exit 1
fi

# The filename assembler_registry.MEGAHIT_SPEC declares. Confirmed here
# against a real run rather than read from documentation, per the standing
# rule in SPADES_SPEC's own comment.
if [ ! -s "${MEGAHIT_SMOKE}/asm/final.contigs.fa" ]; then
    echo "ERROR: megahit produced no final.contigs.fa from the smoke community." >&2
    exit 1
fi
if ! grep -q '^>' "${MEGAHIT_SMOKE}/asm/final.contigs.fa"; then
    echo "ERROR: final.contigs.fa contains no FASTA records." >&2
    exit 1
fi

rm -rf "${MEGAHIT_SMOKE}"

# Leave the image as this script found it. See CURL_WAS_MISSING above.
restore_curl_state
