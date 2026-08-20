#!/bin/sh
# Install MetaBAT2, the metagenome contig binner, and its depth summarizer.
#
# Not in Debian. Checked against the running api container (trixie, arm64) on
# 2026-08-20 *with* a control package in the same run, per the false negative
# install-polypolish.sh documents: `apt-cache policy samtools` resolves
# 1.21-1 while `apt-cache policy metabat` and `metabat2` both report no
# candidate at all, and `apt-cache search metabat` returns nothing.
#
# Bioconda rather than a release binary, and that choice is what makes this
# tool work on Apple Silicon: bioconda publishes 2.18 for BOTH linux-64 and
# linux-aarch64 (verified against the anaconda.org API on 2026-08-20), so
# there is no arm64 skip and tools.metabat2() needs no architecture branch.
#
# Two binaries matter here, and they ship in the SAME package -- verified by
# listing bin/ inside the downloaded linux-aarch64 artifact on 2026-08-20:
#
#   metabat2                        the binner
#   jgi_summarize_bam_contig_depths the depth summarizer MetaBAT2 bins from
#
# The depth summarizer is not optional and not substitutable. It emits mean
# depth *and* per-contig depth variance, and MetaBAT2 bins on coverage
# co-variance alongside tetranucleotide composition. A depth file built from
# some other tool's means is a file MetaBAT2 accepts and bins from -- worse
# bins, no error, nothing to say so. See the design doc's decision B1.
#
# Same micromamba mechanics as install-mosdepth.sh -- the binary is
# downloaded, used once, and deleted.
set -eu

METABAT2_VERSION="${METABAT2_VERSION:-2.18}"
INSTALL_DIR="/opt/metabat2"

# This image ships WITHOUT curl by design -- install-meryl.sh and
# install-quast.sh purge it after their own downloads, and both run before
# this script, so no later layer may assume it exists. Install it, use it,
# and restore that end state at the finish, exactly as install-mosdepth.sh
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
        echo "ERROR: unsupported architecture $(uname -m) for metabat2" >&2
        exit 1
        ;;
esac

# Download to a file and check it before unpacking -- piping the download
# into tar hides which half failed. Same helper as install-mosdepth.sh.
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

echo "Creating metabat2 ${METABAT2_VERSION} environment..."
/tmp/bin/micromamba create -y -p "${INSTALL_DIR}/env" \
    -c conda-forge -c bioconda \
    "metabat2=${METABAT2_VERSION}"

rm -rf /tmp/bin/micromamba
# The conda env carries package caches and static archives nothing needs.
rm -rf "${INSTALL_DIR}/env/pkgs" "${INSTALL_DIR}/env/conda-meta"

# Trim what no binning run reads. Deliberately conservative compared with
# install-mosdepth.sh's aggressive pass: MetaBAT2 links boost and htslib as
# shared libraries, and `perl` is a real runtime dependency of the package's
# aggregate*.pl helpers, so `lib/` and the perl interpreter stay. Only the
# build-time surface goes.
#
# `lib/` must NOT be removed. The same dlopen trap install-mosdepth.sh
# documents applies with more force here: metabat2 links libboost_*.so and
# libhts.so out of its own env, and deleting lib/ leaves `metabat2 --help`
# printing usage while every real run dies on a missing shared object.
rm -rf "${INSTALL_DIR}/env/include" "${INSTALL_DIR}/env/share" \
    "${INSTALL_DIR}/env/ssl"
rm -f "${INSTALL_DIR}"/env/lib/*.a

# Wrappers rather than symlinks, matching install-mosdepth.sh: both binaries
# resolve their shared libraries relative to the env, and whether that
# survives a symlinked entry point is a question this sidesteps rather than
# answers.
cat > /usr/local/bin/metabat2 <<'WRAPPER'
#!/bin/sh
exec /opt/metabat2/env/bin/metabat2 "$@"
WRAPPER
chmod +x /usr/local/bin/metabat2

cat > /usr/local/bin/jgi_summarize_bam_contig_depths <<'WRAPPER'
#!/bin/sh
exec /opt/metabat2/env/bin/jgi_summarize_bam_contig_depths "$@"
WRAPPER
chmod +x /usr/local/bin/jgi_summarize_bam_contig_depths

# Fail the build here rather than at first job: a probe that finds nothing on
# PATH reads to the user as a broken install, and a build that never produced
# a working binary should say so while someone is watching.
#
# Note metabat2 has no `--version` flag at all -- it prints its version in the
# banner of `--help` and exits 0. tools.metabat2() probes `--help` for the
# same reason.
metabat2 --help > /dev/null

# `--help` is deliberately not the whole check, and here that is not a
# theoretical worry: usage printing exercises none of boost, htslib, or the
# OpenMP runtime this binary actually bins with. So the guard that matters is
# a real binning pass over a real BAM, end to end through both binaries.
#
# The fixture is a two-organism community: one AT-rich set of contigs at high
# depth, one GC-rich set at low depth. Those are exactly the two signals
# MetaBAT2 separates on (tetranucleotide composition and coverage), so a
# working install bins them apart and a broken one does not. Built with
# samtools, which this image already installs.
echo "Verifying a real binning run..."
METABAT_SMOKE="$(mktemp -d)"
python3 - "${METABAT_SMOKE}" <<'FIXTURE'
import random
import sys

out = sys.argv[1]
random.seed(7)


def seq(n, gc):
    return "".join(
        random.choice("GC") if random.random() < gc else random.choice("AT")
        for _ in range(n)
    )


# Six contigs per organism, comfortably over MetaBAT2's 2500bp minContig and
# its 200kb minClsSize floor for an emitted bin.
contigs = [(f"orgA_ctg{i}", seq(120_000, 0.30), 30) for i in range(6)]
contigs += [(f"orgB_ctg{i}", seq(120_000, 0.68), 8) for i in range(6)]

with open(f"{out}/contigs.fa", "w") as fh:
    for name, s, _ in contigs:
        fh.write(f">{name}\n")
        for j in range(0, len(s), 80):
            fh.write(s[j:j + 80] + "\n")

RL = 150
sam = ["@HD\tVN:1.6\tSO:coordinate"]
for name, s, _ in contigs:
    sam.append(f"@SQ\tSN:{name}\tLN:{len(s)}")
recs = []
rid = 0
for name, s, depth in contigs:
    for _ in range(max(1, (len(s) * depth) // RL)):
        pos = random.randint(1, len(s) - RL)
        recs.append((name, pos, f"r{rid}", s[pos - 1:pos - 1 + RL]))
        rid += 1
order = {name: k for k, (name, _, _) in enumerate(contigs)}
recs.sort(key=lambda r: (order[r[0]], r[1]))
for name, pos, qn, read in recs:
    sam.append(f"{qn}\t0\t{name}\t{pos}\t60\t{RL}M\t*\t0\t0\t{read}\t{'I' * RL}")

with open(f"{out}/aln.sam", "w") as fh:
    fh.write("\n".join(sam) + "\n")
FIXTURE

samtools view -b -o "${METABAT_SMOKE}/aln.bam" "${METABAT_SMOKE}/aln.sam"
samtools index "${METABAT_SMOKE}/aln.bam"

if ! jgi_summarize_bam_contig_depths \
        --outputDepth "${METABAT_SMOKE}/depth.txt" \
        "${METABAT_SMOKE}/aln.bam" > /dev/null 2>&1; then
    echo "ERROR: jgi_summarize_bam_contig_depths installed but cannot run." >&2
    exit 1
fi
# The variance column is the whole reason this binary is used rather than a
# generic depth tool, so assert the header carries one.
if ! head -1 "${METABAT_SMOKE}/depth.txt" | grep -q -- '-var'; then
    echo "ERROR: depth file has no variance column; MetaBAT2 would bin blind." >&2
    exit 1
fi

if ! metabat2 -i "${METABAT_SMOKE}/contigs.fa" \
        -a "${METABAT_SMOKE}/depth.txt" \
        -o "${METABAT_SMOKE}/bins/bin" \
        --seed 42 -t 2 > /dev/null 2>&1; then
    echo "ERROR: metabat2 installed but cannot bin." >&2
    exit 1
fi
if [ ! -s "${METABAT_SMOKE}/bins/bin.1.fa" ]; then
    echo "ERROR: metabat2 produced no bins from the smoke community." >&2
    exit 1
fi

rm -rf "${METABAT_SMOKE}"

# Leave the image as this script found it. See CURL_WAS_MISSING above.
restore_curl_state
