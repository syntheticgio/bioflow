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

What the tier covers, each slice standing in for a seam the unit suite mocks:

- the four simple aligners' index-then-align path, and `builder_accepts_gzip`
  re-measured against each builder;
- STAR, whose index goes through `build_star_index_command` with its own
  sizing rules -- including `star_index_sizing` checked against STAR's own
  recommendation, and `STAR_ANNOTATED_MEMBERS` re-measured from a real
  `--sjdbGTFfile` build;
- the gzipped-*reference* align path -- the stored-compressed case that is
  normal for anything from NCBI, where the handler's decompress-first
  decision (#560) had only ever been unit-tested against mocks -- plus
  gzipped reads;
- fastp/cutadapt/Trimmomatic trimming adapter-contaminated reads, asserting
  each runner's parser still reads the report format its tool writes;
- winnowmap's meryl preprocessing chain and the alignment that consumes it.

The genome is phiX174 (GCF_000819615.1), 5,386 bp -- the smallest assembly
NCBI hosts that every aligner here still accepts. The download is a few KB,
genome and GTF together. Reads are exact substrings of the genome, so
near-100% of them must map; the assertion uses 90% to stay away from
aligner-specific edge behaviour at the sequence ends. Keep anything added
here at that scale (phiX, or a single small viral/plasmid accession).
"""

import gzip
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from app.config import settings
from app.pipelines import (
    align_params,
    align_runner,
    aligners,
    cutadapt_runner,
    fastp_runner,
    trimmomatic_runner,
    winnowmap_runner,
)
from app.pipelines.aligner_registry import spec_for
from app.pipelines.aligners import Aligner
from app.pipelines.tools import cutadapt as cutadapt_tool
from app.pipelines.tools import fastp as fastp_tool
from app.pipelines.tools import meryl as meryl_tool
from app.pipelines.tools import samtools as samtools_tool
from app.pipelines.tools import star as star_tool
from app.pipelines.tools import trimmomatic as trimmomatic_tool
from app.pipelines.tools import winnowmap as winnowmap_tool
from app.queue import align_handlers

pytestmark = pytest.mark.skipif(
    not os.environ.get("BIOFLOW_TEST_LIVE_DATA"),
    reason="Set BIOFLOW_TEST_LIVE_DATA=1 to run live NCBI-data tests",
)

PHIX_ACCESSION = "GCF_000819615.1"
READ_LENGTH = 150
READ_STRIDE = 25

# The aligners whose index+align path runs through build_index_command /
# build_align_command. STAR and Winnowmap are absent because neither indexes
# through it: STAR goes through build_star_index_command with its own sizing
# rules, and Winnowmap needs meryl preprocessing. Both are covered below by
# tests that carry their own plumbing.
SIMPLE_ALIGNERS = (Aligner.BWA_MEM2, Aligner.MINIMAP2, Aligner.BOWTIE2, Aligner.HISAT2)

# The Illumina stem both TruSeq3 adapters start with, and the sequence fastp
# and cutadapt name as the canonical adapter. One contaminant all three
# trimmers can find, so the same reads exercise every runner's parser.
ADAPTER = "AGATCGGAAGAGCACACGTCTGAACTCCAGTCAC"


def _require(tool):
    if not tool.available:
        pytest.skip(f"{tool.name} is not installed in this image")
    return tool


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=600, **kwargs)


@pytest.fixture(scope="session")
def phix_download(tmp_path_factory) -> Path:
    """The unpacked phiX174 dataset: genome and annotation, one download.

    `gtf` rides along in the same `--include` rather than a second call --
    STAR's annotated-index path needs it, and NCBI ships one for phiX at a
    few KB.
    """
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
            "genome,gtf",
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
    return workdir


@pytest.fixture(scope="session")
def phix_fasta(phix_download) -> Path:
    """The phiX174 genome FASTA."""
    fastas = list(phix_download.rglob("*.fna"))
    assert fastas, "the datasets archive contained no .fna"
    return fastas[0]


@pytest.fixture(scope="session")
def phix_gtf(phix_download) -> Path:
    """phiX's annotation, for STAR's `--sjdbGTFfile` index."""
    gtfs = list(phix_download.rglob("*.gtf"))
    if not gtfs:
        pytest.skip("NCBI shipped no GTF for this accession")
    return gtfs[0]


@pytest.fixture(scope="session")
def phix_sequence(phix_fasta) -> str:
    """The genome as one uppercase string, headers and line breaks removed."""
    sequence = "".join(
        line.strip()
        for line in phix_fasta.read_text().splitlines()
        if not line.startswith(">")
    )
    assert len(sequence) > 5000, "phiX should be ~5.4 kb"
    return sequence


@pytest.fixture(scope="session")
def phix_reads(phix_sequence, tmp_path_factory) -> Path:
    """Synthetic single-end reads: exact substrings of the genome."""
    reads_path = tmp_path_factory.mktemp("reads") / "phix_reads.fastq"
    with reads_path.open("w") as out:
        for i, start in enumerate(
            range(0, len(phix_sequence) - READ_LENGTH, READ_STRIDE)
        ):
            read = phix_sequence[start : start + READ_LENGTH]
            out.write(f"@read{i}\n{read}\n+\n{'I' * len(read)}\n")
    return reads_path


@pytest.fixture(scope="session")
def phix_reads_gz(phix_reads, tmp_path_factory) -> Path:
    """The same reads, gzipped."""
    out = tmp_path_factory.mktemp("reads-gz") / (phix_reads.name + ".gz")
    with phix_reads.open("rb") as src, gzip.open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return out


@pytest.fixture(scope="session")
def adapter_contaminated_reads(phix_sequence, tmp_path_factory) -> Path:
    """Genome substrings with the Illumina adapter appended to every read.

    Every read is genome + adapter, so a trimmer that works removes exactly
    the adapter and leaves a read of `READ_LENGTH`. Deliberately every read
    rather than a fraction: the assertions below then have an exact expected
    count instead of a threshold, so a runner that reads its report's fields
    off by one is caught rather than averaged away.
    """
    reads_path = tmp_path_factory.mktemp("trim-reads") / "phix_adapters.fastq"
    count = 0
    with reads_path.open("w") as out:
        for i, start in enumerate(
            range(0, len(phix_sequence) - READ_LENGTH, READ_STRIDE)
        ):
            read = phix_sequence[start : start + READ_LENGTH] + ADAPTER
            out.write(f"@read{i}\n{read}\n+\n{'I' * len(read)}\n")
            count += 1
    assert count > 100, "expected a couple hundred reads from a 5.4 kb genome"
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


# ---------------------------------------------------------------------------
# STAR: its own index builder, its own sizing rules, its own align convention
# ---------------------------------------------------------------------------


def _star_geometry(fasta: Path) -> tuple[int, int]:
    """Genome length and contig count, the two numbers STAR's sizing needs.

    The handler reads these from a `.fai`; here they come from the FASTA
    directly, so this test does not also depend on faidx having run.
    """
    length = 0
    contigs = 0
    for line in fasta.read_text().splitlines():
        if line.startswith(">"):
            contigs += 1
        else:
            length += len(line.strip())
    return length, contigs


def _build_star_index(
    reference: Path, genome_dir: Path, scratch: Path, gtf: Path | None = None
) -> None:
    star = _require(star_tool())
    genome_length, contigs = _star_geometry(reference)
    genome_dir.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    argv = align_runner.build_star_index_command(
        tool_path=star.path,
        reference=reference,
        genome_dir=genome_dir,
        threads=2,
        genome_length=genome_length,
        contigs=contigs,
        scratch=scratch,
        gtf=gtf,
    )
    result = _run(argv)
    assert result.returncode == 0, result.stderr[-1500:]


def test_star_index_sizing_matches_what_star_recommends(phix_fasta, tmp_path):
    """`star_index_sizing`'s whole reason to exist is the small-genome branch.

    STAR does not fail on an oversized `--genomeSAindexNbases`; it prints a
    recommendation and builds an index that maps almost nothing, so the only
    way to check the formula is to build with it and read STAR's own opinion
    back out of the log. If our value were STAR's default 14, this is where
    phiX would say so.
    """
    star = _require(star_tool())
    genome_length, contigs = _star_geometry(phix_fasta)
    sa_index_nbases, chr_bin_nbits = align_runner.star_index_sizing(
        genome_length=genome_length, contigs=contigs
    )
    assert sa_index_nbases < 14, (
        "phiX is 5.4 kb -- if the formula returns STAR's mammalian default, "
        "the small-genome branch is not being taken"
    )

    # STAR prints its own recommendation to stderr when the value it was
    # given is wrong for the genome, and carries on succeeding. Its absence
    # is the assertion: the formula agrees with the binary.
    genome_dir = tmp_path / "sizing-check-index"
    scratch = tmp_path / "sizing-check-scratch"
    genome_dir.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    result = _run(
        align_runner.build_star_index_command(
            tool_path=star.path,
            reference=phix_fasta,
            genome_dir=genome_dir,
            threads=2,
            genome_length=genome_length,
            contigs=contigs,
            scratch=scratch,
        )
    )
    assert result.returncode == 0, result.stderr[-1500:]

    # The warning reads "!!!!! WARNING: --genomeSAindexNbases 14 is too large
    # for the genome size=5386 ... Re-run genome generation with recommended
    # --genomeSAindexNbases 5". Matching the phrase rather than the whole
    # output because STAR echoes its own command line, which always contains
    # the flag name.
    combined = result.stdout + result.stderr
    assert "is too large for the genome size" not in combined, (
        f"STAR rejected our --genomeSAindexNbases {sa_index_nbases} for a "
        f"{genome_length} bp genome -- star_index_sizing disagrees with the "
        f"binary: {combined[-400:]}"
    )


def test_star_index_then_align_produces_a_bam_that_maps(phix_fasta, phix_reads, tmp_path):
    """STAR end to end: genomeGenerate with our sizing, then the SAM-to-stdout
    align convention `_star_argv` builds, through samtools sort."""
    star = _require(star_tool())
    samtools = _require(samtools_tool())

    reference = tmp_path / phix_fasta.name
    shutil.copy(phix_fasta, reference)
    layout = aligners.layout_for(Aligner.STAR)
    genome_dir = reference.parent / layout.directory_name(reference.name)
    _build_star_index(reference, genome_dir, tmp_path / "index-scratch")

    # Every file the sidecar model expects to store must actually be there --
    # this is the registry-skip failure #11 describes, in its original form:
    # a build that succeeds while writing a different set of files than the
    # layout will later look for.
    for member in aligners.STAR_MEMBERS:
        assert (genome_dir / member).exists(), f"STAR wrote no {member}"

    params = align_params.from_dict(
        {"aligner": Aligner.STAR.value, "threads": 2, "sort_memory_mb": 128}
    )
    bam = tmp_path / "star.bam"
    argv = align_runner.build_align_command(
        aligner=Aligner.STAR,
        aligner_path=star.path,
        samtools_path=samtools.path,
        reference=reference,
        r1=phix_reads,
        r2=None,
        output=bam,
        read_group=align_runner.ReadGroup(
            sample="phix", library="L1", platform="ILLUMINA"
        ),
        params=params,
        scratch=tmp_path / "align-scratch",
    )
    (tmp_path / "align-scratch").mkdir(parents=True, exist_ok=True)
    result = _run(argv)
    assert result.returncode == 0, result.stderr[-1500:]

    flagstat = _run(
        align_runner.build_flagstat_command(samtools_path=samtools.path, bam=bam)
    )
    assert flagstat.returncode == 0, flagstat.stderr[-800:]
    facts = align_runner.parse_flagstat(flagstat.stdout)
    assert facts["total_reads"] > 100
    assert facts["mapped_pct"] > 90, (
        f"STAR mapped only {facts['mapped_pct']}% of exact-substring reads -- "
        "the sizing formula or the align convention is wrong"
    )


def test_star_annotated_index_writes_every_member_the_layout_expects(
    phix_fasta, phix_gtf, tmp_path
):
    """An annotated build writes a *different, larger* file set than a plain
    one. `STAR_ANNOTATED_MEMBERS` was measured rather than predicted, after a
    guess from the docs missed three files and left build_index silently
    dropping sidecars. This re-measures it against the binary."""
    _require(star_tool())

    reference = tmp_path / phix_fasta.name
    shutil.copy(phix_fasta, reference)
    layout = aligners.layout_for(Aligner.STAR, annotated=True)
    genome_dir = reference.parent / layout.directory_name(reference.name)
    _build_star_index(
        reference, genome_dir, tmp_path / "index-scratch", gtf=phix_gtf
    )

    missing = [
        member
        for member in aligners.STAR_ANNOTATED_MEMBERS
        if not (genome_dir / member).exists()
    ]
    assert not missing, (
        f"an annotated STAR build wrote none of {missing} -- STAR_ANNOTATED_MEMBERS "
        "claims files this STAR does not produce, so build_index would report "
        "success while storing fewer sidecars than the layout expects"
    )


# ---------------------------------------------------------------------------
# The gzipped-reference align path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("aligner", SIMPLE_ALIGNERS, ids=lambda a: a.value)
def test_gzipped_reference_indexes_and_aligns(aligner, phix_fasta, phix_reads, tmp_path):
    """The stored-gzipped reference path, end to end.

    NCBI assemblies are stored compressed, so this is the *normal* case, not
    an edge one -- and the handler-level "decompress first iff
    builder_accepts_gzip is false" logic that #560 and the HISAT2 fix patched
    has until now only been unit-tested against mocks. Here the reference is
    genuinely gzipped, the same decision is taken from the same registry
    flag, and the resulting index has to actually align reads.

    The index basename stays the *stored* (gzipped) name even when the
    builder reads a decompressed copy, exactly as the handler does it: that
    is what makes the index files land where the layout later looks for them.
    """
    spec = spec_for(aligner)
    tool = _require(spec.tool())
    builder = _require((spec.builder_tool or spec.tool)())
    samtools = _require(samtools_tool())

    stored = tmp_path / (phix_fasta.name + ".gz")
    with phix_fasta.open("rb") as src, gzip.open(stored, "wb") as dst:
        shutil.copyfileobj(src, dst)

    # The handler's own helper and the handler's own condition, not a copy of
    # either: what is under test is the decision `build_index` takes, so a
    # reimplementation here could agree with itself while disagreeing with
    # the code that runs in production.
    build_reference = stored
    if not spec.builder_accepts_gzip:
        build_reference = align_handlers._ensure_uncompressed(
            stored, tmp_path / "build-input"
        )

    if aligner is Aligner.MINIMAP2:
        align_target = stored.parent / f"{stored.name}{aligners.MINIMAP2_SUFFIX}"
        argv = align_runner.build_index_command(
            aligner=aligner,
            tool_path=builder.path,
            reference=build_reference,
            output=align_target,
        )
    else:
        align_target = stored
        argv = align_runner.build_index_command(
            aligner=aligner,
            tool_path=builder.path,
            reference=build_reference,
            output=stored,
        )
    result = _run(argv)
    assert result.returncode == 0, result.stderr[-1500:]

    # The layout's own filenames, not a hand-written list: this is what the
    # handler stores as sidecars, so a build that writes elsewhere would show
    # up later as a missing index rather than here.
    for name in aligners.index_filenames(stored.name, aligner):
        assert (stored.parent / name).exists(), (
            f"{aligner.value} index build wrote no {name} beside the gzipped "
            "reference -- the basename did not stay the stored name"
        )

    params = align_params.from_dict(
        {"aligner": aligner.value, "threads": 2, "sort_memory_mb": 128}
    )
    bam = tmp_path / "gz.bam"
    align_argv = align_runner.build_align_command(
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
    aligned = _run(align_argv)
    assert aligned.returncode == 0, aligned.stderr[-1500:]

    flagstat = _run(
        align_runner.build_flagstat_command(samtools_path=samtools.path, bam=bam)
    )
    assert flagstat.returncode == 0, flagstat.stderr[-800:]
    facts = align_runner.parse_flagstat(flagstat.stdout)
    assert facts["mapped_pct"] > 90, (
        f"{aligner.value} mapped only {facts['mapped_pct']}% against an index "
        "built from a gzipped reference -- the decompress-first path produced "
        "an index that builds but does not work"
    )


def test_gzipped_reads_align(phix_fasta, phix_reads_gz, tmp_path):
    """Gzipped *reads*, the other half of the compression story.

    Aligners infer read compression from the filename, which is why the
    handler links reads under names carrying `.gz`. minimap2 stands in for
    the four here: the read-side path is shared, unlike the reference side
    whose builders differ.
    """
    aligner = Aligner.MINIMAP2
    tool = _require(spec_for(aligner).tool())
    samtools = _require(samtools_tool())

    reference = tmp_path / phix_fasta.name
    shutil.copy(phix_fasta, reference)
    align_target = _index(aligner, reference)

    params = align_params.from_dict(
        {"aligner": aligner.value, "threads": 2, "sort_memory_mb": 128}
    )
    bam = tmp_path / "gzreads.bam"
    argv = align_runner.build_align_command(
        aligner=aligner,
        aligner_path=tool.path,
        samtools_path=samtools.path,
        reference=align_target,
        r1=phix_reads_gz,
        r2=None,
        output=bam,
        read_group=align_runner.ReadGroup(
            sample="phix", library="L1", platform="ILLUMINA"
        ),
        params=params,
    )
    result = _run(argv)
    assert result.returncode == 0, result.stderr[-1500:]

    flagstat = _run(
        align_runner.build_flagstat_command(samtools_path=samtools.path, bam=bam)
    )
    facts = align_runner.parse_flagstat(flagstat.stdout)
    assert facts["total_reads"] > 100, "gzipped reads were read as zero reads"
    assert facts["mapped_pct"] > 90


# ---------------------------------------------------------------------------
# Trimming: three tools, three report formats, three parsers
# ---------------------------------------------------------------------------


def _fastq_read_lengths(path: Path) -> list[int]:
    """Sequence lengths from a FASTQ, for checking what a trimmer produced."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as handle:
        return [
            len(line.strip())
            for i, line in enumerate(handle)
            if i % 4 == 1
        ]


def test_fastp_trims_the_adapter_and_the_parser_reads_the_real_report(
    adapter_contaminated_reads, tmp_path
):
    """fastp's real JSON through `parse_report`.

    The parser is unit-tested against a captured fixture; this checks the
    fixture still describes what the installed fastp writes -- a renamed or
    restructured field would leave the parser returning None for counts that
    look optional but are the whole report.
    """
    fastp = _require(fastp_tool())

    r1_out = tmp_path / "trimmed.fastq"
    json_out = tmp_path / "fastp.json"
    params = fastp_runner.TrimParams(adapter_r1=ADAPTER, threads=2)
    argv = fastp_runner.build_command(
        fastp_path=fastp.path,
        r1_in=adapter_contaminated_reads,
        r1_out=r1_out,
        json_out=json_out,
        html_out=tmp_path / "fastp.html",
        params=params,
    )
    result = _run(argv)
    assert result.returncode == 0, result.stderr[-1500:]

    report = fastp_runner.parse_report(json_out)
    assert report, "parse_report returned {} for a report fastp just wrote"
    assert report["tool"] == "fastp"
    assert report["tool_version"], "fastp's version field is no longer where the parser looks"

    expected = len(_fastq_read_lengths(adapter_contaminated_reads))
    assert report["before"]["total_reads"] == expected
    assert report["after"]["total_reads"] == expected, (
        "every read was genome+adapter and none should be dropped entirely"
    )
    assert report["adapters"]["trimmed_reads"] > 0, (
        "fastp reported no adapter-trimmed reads, but every read carried one"
    )
    assert report["before"]["total_bases"] > report["after"]["total_bases"], (
        "trimming removed no bases"
    )

    # The adapter is gone from the output, not merely reported as gone.
    assert max(_fastq_read_lengths(r1_out)) <= READ_LENGTH, (
        "a trimmed read is still longer than the genome fragment it came from"
    )


def test_cutadapt_trims_the_adapter_and_the_parser_reads_the_real_report(
    adapter_contaminated_reads, tmp_path
):
    """cutadapt's real JSON through `parse_report`. Unlike fastp, cutadapt
    searches for nothing unless given an adapter explicitly -- which is why
    the params below name one rather than relying on detection."""
    cutadapt = _require(cutadapt_tool())

    r1_out = tmp_path / "trimmed.fastq"
    json_out = tmp_path / "cutadapt.json"
    params = cutadapt_runner.CutadaptParams(adapter_r1=ADAPTER, threads=2)
    argv = cutadapt_runner.build_command(
        cutadapt_path=cutadapt.path,
        r1_in=adapter_contaminated_reads,
        r1_out=r1_out,
        json_out=json_out,
        params=params,
    )
    result = _run(argv)
    assert result.returncode == 0, result.stderr[-1500:]

    report = cutadapt_runner.parse_report(json_out)
    assert report, "parse_report returned {} for a report cutadapt just wrote"
    assert report["tool"] == "cutadapt"
    assert report["tool_version"], "cutadapt_version is no longer where the parser looks"

    expected = len(_fastq_read_lengths(adapter_contaminated_reads))
    assert report["before"]["total_reads"] == expected
    assert report["after"]["total_reads"] == expected
    assert report["adapters"]["trimmed_reads_r1"] == expected, (
        "every read carried the adapter, so cutadapt should have trimmed all of them"
    )
    assert report["before"]["total_bases"] > report["after"]["total_bases"]
    assert max(_fastq_read_lengths(r1_out)) <= READ_LENGTH


def test_trimmomatic_trims_the_adapter_and_the_parser_reads_the_real_summary(
    adapter_contaminated_reads, tmp_path
):
    """Trimmomatic's `-summary` file through `parse_summary`.

    The one trimmer here with no JSON: the report is `Key: Value` text, and
    the SE/PE key names differ for the same concept. `parse_summary` returns
    {} when the keys it wants are absent, so an assertion on a non-empty
    result is exactly the check that the real file still uses those names.
    """
    _require(trimmomatic_tool())
    adapters_dir = Path(settings.trimmomatic_adapters_dir)
    params = trimmomatic_runner.TrimmomaticParams(threads=2)
    if not (adapters_dir / (params.adapter_file or "")).exists():
        pytest.skip(f"no {params.adapter_file} under {adapters_dir}")

    r1_out = tmp_path / "trimmed.fastq"
    summary_out = tmp_path / "summary.txt"
    argv = trimmomatic_runner.build_command(
        trimmomatic_pe_path=settings.trimmomatic_pe_path,
        trimmomatic_se_path=settings.trimmomatic_se_path,
        adapters_dir=settings.trimmomatic_adapters_dir,
        r1_in=adapter_contaminated_reads,
        r1_out=r1_out,
        summary_out=summary_out,
        params=params,
    )
    result = _run(argv)
    assert result.returncode == 0, result.stderr[-1500:]
    assert summary_out.exists(), "-summary wrote no file"

    report = trimmomatic_runner.parse_summary(summary_out, paired=False)
    assert report, (
        "parse_summary returned {} -- the SE key names it looks for are not the "
        f"ones this Trimmomatic writes: {summary_out.read_text()[:400]}"
    )
    assert report["tool"] == "trimmomatic"
    expected = len(_fastq_read_lengths(adapter_contaminated_reads))
    assert report["before"]["total_reads"] == expected
    assert report["after"]["total_reads"] > 0, "Trimmomatic dropped every read"
    assert max(_fastq_read_lengths(r1_out)) <= READ_LENGTH, (
        "ILLUMINACLIP did not remove the adapter -- the adapter file or the "
        "clip parameters are not matching"
    )


# ---------------------------------------------------------------------------
# Winnowmap: meryl preprocessing, then an aligner that needs its output
# ---------------------------------------------------------------------------


def test_winnowmap_meryl_chain_then_align(phix_fasta, phix_reads, tmp_path):
    """The two meryl commands `build_index` runs for winnowmap, then the
    alignment that consumes their output.

    Winnowmap is the one aligner here whose index is built by a different
    tool entirely, and whose align step takes an extra input (`-W`) that
    fails the run if missing. Both halves are shape-checked in the unit
    suite; this runs them.
    """
    meryl = _require(meryl_tool())
    winnowmap = _require(winnowmap_tool())
    samtools = _require(samtools_tool())

    reference = tmp_path / phix_fasta.name
    shutil.copy(phix_fasta, reference)

    database = tmp_path / "winnowmap.meryl"
    count = _run(
        winnowmap_runner.build_meryl_count_command(
            meryl_path=meryl.path,
            k=15,
            reference=reference,
            output=database,
            threads=2,
        )
    )
    assert count.returncode == 0, count.stderr[-1500:]
    assert database.exists(), "meryl count produced no database"

    repetitive = reference.parent / (
        f"{reference.name}{aligners.WINNOWMAP_REPETITIVE_KMER_SUFFIX}"
    )
    printed = _run(
        winnowmap_runner.build_meryl_print_repetitive_shell_command(
            meryl_path=meryl.path,
            distinct=0.9998,
            database=database,
            output=repetitive,
        )
    )
    assert printed.returncode == 0, printed.stderr[-1500:]
    assert repetitive.exists(), (
        "the shell redirect in build_meryl_print_repetitive_shell_command wrote "
        "no file -- meryl exited 0 but its stdout went nowhere"
    )

    params = align_params.from_dict(
        {"aligner": Aligner.WINNOWMAP.value, "threads": 2, "sort_memory_mb": 128}
    )
    bam = tmp_path / "winnowmap.bam"
    argv = align_runner.build_align_command(
        aligner=Aligner.WINNOWMAP,
        aligner_path=winnowmap.path,
        samtools_path=samtools.path,
        reference=reference,
        r1=phix_reads,
        r2=None,
        output=bam,
        read_group=align_runner.ReadGroup(
            sample="phix", library="L1", platform="ILLUMINA"
        ),
        params=params,
        winnowmap_repetitive_kmers=repetitive,
    )
    result = _run(argv)
    assert result.returncode == 0, result.stderr[-1500:]

    flagstat = _run(
        align_runner.build_flagstat_command(samtools_path=samtools.path, bam=bam)
    )
    facts = align_runner.parse_flagstat(flagstat.stdout)
    assert facts["total_reads"] > 100
    assert facts["mapped_pct"] > 90, (
        f"winnowmap mapped only {facts['mapped_pct']}% -- the -W file or the "
        "preset is wrong for these reads"
    )
