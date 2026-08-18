"""Real tools, real (tiny) NCBI data: the seams the unit suite mocks away.

Opt-in via BIOFLOW_TEST_LIVE_DATA=1, following the same pattern as
test_node_ssh_live.py: these tests download a genome from NCBI and run the
actual aligner binaries, so they need network and a few extra seconds the
other ~1900 tests should not pay for.

    BIOFLOW_TEST_LIVE_DATA=1 ./backend/run-worktree-tests.sh \
        tests/integration/test_pipeline_live_data.py -v

Why these exist: every unit test of `align_runner` asserts the *shape* of a
command, never that the binary accepts it. That gap has bitten for real --
`builder_accepts_gzip` was hand-measured after hisat2-build died partway
through a gzipped reference (#560), and nothing since verifies the claim
against the binaries the image actually ships. These tests re-measure it, and
run each aligner's full index-then-align path end to end.

The genome is phiX174 (GCF_000819615.1), 5,386 bp -- the smallest assembly
NCBI hosts that every aligner here still accepts. The download is a few KB.
Reads are exact substrings of the genome, so near-100% of them must map; the
assertion uses 90% to stay away from aligner-specific edge behaviour at the
sequence ends.
"""

import gzip
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from app.pipelines import align_params, align_runner
from app.pipelines.aligner_registry import spec_for
from app.pipelines.aligners import Aligner
from app.pipelines.tools import samtools as samtools_tool

pytestmark = pytest.mark.skipif(
    not os.environ.get("BIOFLOW_TEST_LIVE_DATA"),
    reason="Set BIOFLOW_TEST_LIVE_DATA=1 to run live NCBI-data tests",
)

PHIX_ACCESSION = "GCF_000819615.1"
READ_LENGTH = 150
READ_STRIDE = 25

# The aligners whose index+align path runs through build_index_command /
# build_align_command. STAR and Winnowmap are deliberately absent: STAR
# indexes through build_star_index_command with its own sizing rules, and
# Winnowmap needs meryl preprocessing -- each deserves its own test when the
# plumbing is worth carrying here.
SIMPLE_ALIGNERS = (Aligner.BWA_MEM2, Aligner.MINIMAP2, Aligner.BOWTIE2, Aligner.HISAT2)


def _require(tool):
    if not tool.available:
        pytest.skip(f"{tool.name} is not installed in this image")
    return tool


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=600, **kwargs)


@pytest.fixture(scope="session")
def phix_fasta(tmp_path_factory) -> Path:
    """The phiX174 genome, downloaded once per session via `datasets`."""
    datasets = shutil.which("datasets")
    if not datasets:
        pytest.skip("the NCBI `datasets` CLI is not installed in this image")

    workdir = tmp_path_factory.mktemp("phix")
    archive = workdir / "phix.zip"
    result = _run(
        [
            datasets,
            "download",
            "genome",
            "accession",
            PHIX_ACCESSION,
            "--include",
            "genome",
            "--filename",
            str(archive),
        ]
    )
    if result.returncode != 0:
        pytest.fail(
            f"datasets download failed (network?): {result.stderr.strip()[:500]}"
        )

    with zipfile.ZipFile(archive) as zf:
        zf.extractall(workdir)
    fastas = list(workdir.rglob("*.fna"))
    assert fastas, "the datasets archive contained no .fna"
    return fastas[0]


@pytest.fixture(scope="session")
def phix_reads(phix_fasta, tmp_path_factory) -> Path:
    """Synthetic single-end reads: exact substrings of the genome."""
    sequence = "".join(
        line.strip()
        for line in phix_fasta.read_text().splitlines()
        if not line.startswith(">")
    )
    assert len(sequence) > 5000, "phiX should be ~5.4 kb"

    reads_path = tmp_path_factory.mktemp("reads") / "phix_reads.fastq"
    with reads_path.open("w") as out:
        for i, start in enumerate(range(0, len(sequence) - READ_LENGTH, READ_STRIDE)):
            read = sequence[start : start + READ_LENGTH]
            out.write(f"@read{i}\n{read}\n+\n{'I' * len(read)}\n")
    return reads_path


def _index(aligner: Aligner, reference: Path) -> Path:
    """Build the index for `reference` in place; return the path to align against."""
    spec = spec_for(aligner)
    builder = _require((spec.builder_tool or spec.tool)())

    if aligner is Aligner.MINIMAP2:
        output = reference.with_suffix(".mmi")
        argv = align_runner.build_index_command(
            aligner=aligner, tool_path=builder.path, reference=reference, output=output
        )
        result = _run(argv)
        assert result.returncode == 0, result.stderr[-800:]
        return output

    argv = align_runner.build_index_command(
        aligner=aligner, tool_path=builder.path, reference=reference
    )
    result = _run(argv)
    assert result.returncode == 0, result.stderr[-800:]
    return reference


@pytest.mark.parametrize("aligner", SIMPLE_ALIGNERS, ids=lambda a: a.value)
def test_index_then_align_produces_a_bam_that_maps(aligner, phix_fasta, phix_reads, tmp_path):
    """The full path our unit tests only shape-check: build the real index,
    run the real align|sort pipeline, and read the mapping rate back out of
    flagstat through our own parser."""
    spec = spec_for(aligner)
    tool = _require(spec.tool())
    samtools = _require(samtools_tool())

    reference = tmp_path / phix_fasta.name
    shutil.copy(phix_fasta, reference)
    align_target = _index(aligner, reference)

    params = align_params.from_dict(
        {"aligner": aligner.value, "threads": 2, "sort_memory_mb": 128}
    )
    bam = tmp_path / "out.bam"
    argv = align_runner.build_align_command(
        aligner=aligner,
        aligner_path=tool.path,
        samtools_path=samtools.path,
        reference=align_target,
        r1=phix_reads,
        r2=None,
        output=bam,
        read_group=align_runner.ReadGroup(
            sample="phix", library="L1", platform="ILLUMINA"
        ),
        params=params,
    )
    result = _run(argv)
    assert result.returncode == 0, result.stderr[-1500:]
    assert bam.exists() and bam.stat().st_size > 0

    flagstat = _run(
        align_runner.build_flagstat_command(samtools_path=samtools.path, bam=bam)
    )
    assert flagstat.returncode == 0, flagstat.stderr[-800:]
    facts = align_runner.parse_flagstat(flagstat.stdout)
    assert facts["total_reads"] > 100
    assert facts["mapped_pct"] > 90, (
        f"{aligner.value} mapped only {facts['mapped_pct']}% of exact-substring "
        f"reads -- the command builder and the binary disagree about something"
    )


@pytest.mark.parametrize("aligner", SIMPLE_ALIGNERS, ids=lambda a: a.value)
def test_builder_accepts_gzip_matches_the_binary(aligner, phix_fasta, tmp_path):
    """Re-measure the registry's `builder_accepts_gzip` claim against the
    binary itself. The flag was hand-measured once (#560) after hisat2-build
    died partway through a gzipped reference; if an image upgrade changes a
    builder's behaviour, this is the only thing that will say so."""
    spec = spec_for(aligner)
    builder = _require((spec.builder_tool or spec.tool)())

    gzipped = tmp_path / (phix_fasta.name + ".gz")
    with phix_fasta.open("rb") as src, gzip.open(gzipped, "wb") as dst:
        shutil.copyfileobj(src, dst)

    output = gzipped.with_suffix(".mmi") if aligner is Aligner.MINIMAP2 else None
    argv = align_runner.build_index_command(
        aligner=aligner, tool_path=builder.path, reference=gzipped, output=output
    )
    result = _run(argv)
    succeeded = result.returncode == 0

    assert succeeded == spec.builder_accepts_gzip, (
        f"{aligner.value}: builder_accepts_gzip={spec.builder_accepts_gzip} but the "
        f"binary {'accepted' if succeeded else 'rejected'} a gzipped reference -- "
        f"update the registry flag (stderr: {result.stderr.strip()[-300:]})"
    )
