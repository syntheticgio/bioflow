"""Building and observing an alignment run.

Kept separate from the job handler so the parts worth testing -- command
construction, progress parsing, flagstat extraction -- are pure functions over
strings and dicts, with no queue or filesystem involved.
"""

import math
import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.errors import ValidationError
from app.logging import get_logger
from app.pipelines import aligners

# Parameter classes moved to align_params.py when the second and third
# aligners arrived: one flat class covering four tools would be a union of
# mostly-inapplicable fields. Re-exported here because this is where every
# existing call site imports them from.
from app.pipelines.align_params import (
    Minimap2Params,
)
from app.pipelines.aligners import Aligner

log = get_logger(__name__)

# AlignParams aliases Minimap2Params rather than BaseAlignParams: every
# existing direct-construction call site (AlignParams(), AlignParams(preset=...))
# relies on defaults and fields that only Minimap2Params has, and dataclass
# `__init__` performs no validation -- that only lives in `from_dict` -- so
# constructing a Minimap2Params with aligner=Aligner.BWA_MEM2 works exactly
# like the old flat dataclass did.
AlignParams = Minimap2Params

# bwa-mem2 reports throughput on stderr as it goes:
#   [M::mem_process_seqs] Processed 80000 reads in 12.345 CPU sec
_PROCESSED_RE = re.compile(r"Processed\s+(\d+)\s+reads", re.IGNORECASE)

# minimap2 reports the same shape under a different name and message, one
# line per internal batch:
#   [M::worker_pipeline::13.642*3.91] mapped 166776 sequences
# Confirmed against a real 2.27 run (backend/tests/fixtures/tool_logs/
# minimap2-2.27.log) rather than assumed from the format alone -- an earlier
# version of this comment claimed minimap2 "says nothing comparable
# per-batch", which turned out to be wrong: it does, just at a batch size
# minimap2 decides internally (observed here as three batches for 400K
# reads, not the fixed 80K bwa-mem2 uses), so the count is still a per-batch
# figure to sum rather than a running total, exactly like _PROCESSED_RE.
_MAPPED_RE = re.compile(r"worker_pipeline.*mapped\s+(\d+)\s+sequences", re.IGNORECASE)

# samtools sort announces its merge phase, which is the tail of the run.
_MERGING_RE = re.compile(r"merging from \d+ files", re.IGNORECASE)

# As in trimming: the read total is an estimate extrapolated at ingest, so a
# bar driven by it must never claim completion.
MAX_MEASURED_PCT = 0.95

# aligning always precedes sorting; the run has no other phases. Assembly's
# Flye stage list is similarly closed and known ahead of time -- see
# assembly_runner.flye_stage_order -- it just varies per run instead of
# being a flat constant like this one.
PHASE_ORDER: tuple[str, ...] = ("aligning", "sorting")


class Preset:
    """minimap2 presets. Not cosmetic: the wrong preset for long reads produces
    silently poor alignments rather than an error."""

    MAP_ONT = "map-ont"
    MAP_PB = "map-pb"
    MAP_HIFI = "map-hifi"  # PacBio HiFi/CCS: ~Q30, since minimap2 2.19
    LR_HQ = "lr:hq"  # ONT duplex / Q20+ chemistry
    SHORT_READ = "sr"

    ALL = (MAP_ONT, MAP_PB, MAP_HIFI, LR_HQ, SHORT_READ)


class ReadChemistry(StrEnum):
    """How accurate a long-read file actually is, independent of who made it.

    `sam_platform` answers "who made this" (ONT/PACBIO/ILLUMINA); it cannot
    answer "how accurate is it", which is the question the minimap2 preset
    actually needs. HiFi and CLR are both PACBIO_SMRT in SRA and both PACBIO
    in SAM, so this has to be a separate axis rather than folded into platform.
    """

    HIFI = "hifi"
    CLR = "clr"
    ONT_SIMPLEX = "ont_simplex"
    ONT_DUPLEX = "ont_duplex"
    SHORT = "short"
    UNKNOWN = "unknown"


_CHEMISTRY_PRESETS: dict[ReadChemistry, str] = {
    ReadChemistry.HIFI: Preset.MAP_HIFI,
    ReadChemistry.CLR: Preset.MAP_PB,
    ReadChemistry.ONT_SIMPLEX: Preset.MAP_ONT,
    ReadChemistry.ONT_DUPLEX: Preset.LR_HQ,
    ReadChemistry.SHORT: Preset.SHORT_READ,
}


def preset_for_chemistry(chemistry: ReadChemistry) -> str:
    """The minimap2 preset for a read chemistry, defaulting to short-read.

    UNKNOWN falls back to short-read here; callers that have a platform to
    fall back on (see `suggested_preset`) should prefer that over this
    default, since "unknown chemistry on a PacBio file" should stay map-pb,
    not become sr.
    """
    return _CHEMISTRY_PRESETS.get(chemistry, Preset.SHORT_READ)


@dataclass(frozen=True)
class ReadGroup:
    """`@RG` header fields.

    Required rather than optional: GATK and most variant callers refuse to run
    without a read group, and adding one afterwards means rewriting the whole
    BAM. Defaulted from the reads' existing metadata at launch, so this is
    usually a confirmation rather than data entry.
    """

    sample: str
    library: str
    platform: str | None = None
    identifier: str | None = None

    def as_sam_header(self) -> str:
        """The `@RG` line, tab-separated as the SAM spec requires.

        Emitted with literal backslash-t rather than real tabs: this string is
        passed as a single argv element to `-R`, and the aligners parse the
        two-character escape themselves.

        PL is omitted when the platform is unknown, which the SAM spec
        prescribes -- see `pipeline_service.sam_platform`.
        """
        rg_id = self.identifier or self.sample
        fields = [
            "@RG",
            f"ID:{rg_id}",
            f"SM:{self.sample}",
            f"LB:{self.library}",
        ]
        if self.platform:
            fields.append(f"PL:{self.platform}")
        return "\\t".join(fields)

    def as_rg_args(self) -> list[str]:
        """`--rg-id` plus one `--rg` per remaining field.

        bowtie2 and HISAT2 have no single -R taking a whole @RG line. Handing
        them `as_sam_header()` would embed a literal backslash-t in the BAM
        header, which reads as a corrupt read group to every downstream tool
        rather than failing at alignment time.

        PL is omitted when the platform is unknown, as in `as_sam_header`.
        """
        rg_id = self.identifier or self.sample
        args = ["--rg-id", rg_id]
        field_values = [f"SM:{self.sample}", f"LB:{self.library}"]
        if self.platform:
            field_values.append(f"PL:{self.platform}")
        for field_value in field_values:
            args += ["--rg", field_value]
        return args

    def as_star_rg_fields(self) -> list[str]:
        """The fields for STAR's `--outSAMattrRGline`, one argument each.

        A third shape, because STAR accepts neither of the first two: it takes
        every field as a separate argv element after a single flag, and reads
        `as_sam_header()`'s tab-escaped string as one malformed ID. Verified
        against STAR 2.7.11b, whose output carries the resulting `@RG` line
        intact.

        PL is omitted when the platform is unknown, as in `as_sam_header`.
        """
        rg_id = self.identifier or self.sample
        fields = [
            f"ID:{rg_id}",
            f"SM:{self.sample}",
            f"LB:{self.library}",
        ]
        if self.platform:
            fields.append(f"PL:{self.platform}")
        return fields

    def as_dict(self) -> dict:
        return {
            "sample": self.sample,
            "library": self.library,
            "platform": self.platform,
            "identifier": self.identifier,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "ReadGroup":
        data = data or {}
        missing = [k for k in ("sample", "library") if not data.get(k)]
        if missing:
            raise ValidationError(
                f"Read group requires {', '.join(missing)}",
                details={"missing": missing},
            )
        return cls(
            sample=str(data["sample"]),
            library=str(data["library"]),
            platform=str(data["platform"]) if data.get("platform") else None,
            identifier=str(data["identifier"]) if data.get("identifier") else None,
        )


def default_preset(aligner: Aligner) -> str:
    """bwa-mem2 has a single short-read mode; minimap2 must be told."""
    return "" if aligner is Aligner.BWA_MEM2 else Preset.SHORT_READ


def star_index_sizing(*, genome_length: int, contigs: int) -> tuple[int, int]:
    """`--genomeSAindexNbases` and `--genomeChrBinNbits` for one reference.

    STAR's defaults are tuned for a mammalian genome and misbehave in both
    directions on anything else, quietly rather than loudly:

    - `genomeSAindexNbases` defaults to 14. On a small genome -- a virus, a
      bacterium, a single plasmid -- that builds a suffix-array index far
      larger than the genome and produces an index that maps almost nothing.
      STAR prints a recommendation to stderr and carries on succeeding, which
      is the worst combination: a green job and an empty BAM. The manual's
      formula is min(14, log2(genomeLength)/2 - 1).
    - `genomeChrBinNbits` defaults to 18, sized for a genome with tens of
      chromosomes. A draft assembly with tens of thousands of scaffolds
      allocates a bin per scaffold and exhausts memory during the build. The
      manual's formula is min(18, log2(genomeLength/contigs)).

    Both are computed from the `.fai` this application has already built for
    the reference, so they are exact rather than estimated from the file size.
    """
    # A degenerate reference (empty, or a .fai that failed to parse) would send
    # log2 to a negative or undefined value. Clamped rather than raised: STAR
    # will produce its own honest error about an unusable genome, and that is
    # a better message than anything invented here.
    length = max(int(genome_length), 1)
    n_contigs = max(int(contigs), 1)

    sa_index_nbases = min(14, max(int(math.log2(length) / 2) - 1, 1))
    chr_bin_nbits = min(18, max(int(math.log2(length / n_contigs)), 1))
    return sa_index_nbases, chr_bin_nbits


# STAR's own default, and the value the TODO that asked for annotation-aware
# indexing settled on rather than threading read length into index caching:
# indexes here are cached per reference with no read-length dimension, and
# 100 is correct for the common case (100-150bp Illumina). `--sjdbOverhang`
# only tunes the splice-junction database's sensitivity at read ends; it does
# not change the number or names of files written, so a build that turns out
# to be tuned for the wrong read length is a resolution loss, not a broken
# index.
STAR_SJDB_OVERHANG = 100


def build_star_index_command(
    *,
    tool_path: str,
    reference: Path,
    genome_dir: Path,
    threads: int,
    genome_length: int,
    contigs: int,
    scratch: Path,
    gtf: Path | None = None,
    sjdb_overhang: int = STAR_SJDB_OVERHANG,
) -> list[str]:
    """`STAR --runMode genomeGenerate`.

    Separate from `build_index_command` rather than a fifth branch in it:
    STAR's index build takes seven arguments the other four have no analogue
    for, and folding it in would mean every caller passes a genome length and
    a thread count that four of five aligners ignore.

    `--outFileNamePrefix` is not cosmetic. STAR writes `Log.out` wherever it
    is pointed, and pointed at nothing it writes into the process's working
    directory -- which for a job here is not a directory anyone reaps.

    `gtf` is optional: STAR finds junctions de novo without one (9,818 on a
    real yeast alignment with none supplied), so an index without an
    annotation is a real, useful thing to build, not a degraded version of
    this one. Supplying it changes the file set the build produces -- see
    `aligners.STAR_ANNOTATED_MEMBERS` -- so the caller must build the genome
    directory under the matching layout (`aligners.layout_for(..., annotated=
    gtf is not None)`), not just add the flag.
    """
    sa_index_nbases, chr_bin_nbits = star_index_sizing(
        genome_length=genome_length, contigs=contigs
    )
    cmd = [
        tool_path,
        "--runMode", "genomeGenerate",
        "--genomeDir", str(genome_dir),
        "--genomeFastaFiles", str(reference),
        "--runThreadN", str(threads),
        "--genomeSAindexNbases", str(sa_index_nbases),
        "--genomeChrBinNbits", str(chr_bin_nbits),
        # Trailing separator: STAR concatenates this with its own filenames
        # rather than treating it as a directory, so without the slash the
        # logs land beside the directory as `star-buildLog.out`.
        "--outFileNamePrefix", f"{scratch}/",
    ]
    if gtf is not None:
        cmd += ["--sjdbGTFfile", str(gtf), "--sjdbOverhang", str(sjdb_overhang)]
    return cmd


def build_index_command(
    *, aligner: Aligner, tool_path: str, reference: Path, output: Path | None = None
) -> list[str]:
    """The command that builds an aligner's index for a reference.

    Three shapes: bwa-mem2 writes its five files beside the reference and
    takes no output path, minimap2 writes one file wherever it is told, and
    bowtie2/HISAT2 take a reference and a basename as two positional
    arguments. `tool_path` for the latter two is the *builder* binary
    (bowtie2-build, hisat2-build), not the aligner -- see
    `aligners.layout_for(...).builder`.

    STAR is deliberately not here; see `build_star_index_command`.
    """
    if aligner is Aligner.STAR:
        raise ValidationError(
            "STAR's index is built by build_star_index_command, not this"
        )

    if aligner is Aligner.BWA_MEM2:
        return [tool_path, "index", str(reference)]

    if aligner is Aligner.MINIMAP2:
        if output is None:
            raise ValidationError("minimap2 index requires an output path")
        return [tool_path, "-d", str(output), str(reference)]

    # bowtie2-build / hisat2-build: <reference> <basename>. The basename is
    # the reference path itself, so the index files land beside it as
    # `genome.fna.1.bt2` and materialize back under names the layout knows.
    return [tool_path, str(reference), str(reference)]


def build_faidx_command(*, samtools_path: str, reference: Path) -> list[str]:
    return [samtools_path, "faidx", str(reference)]


def build_align_command(
    *,
    aligner: Aligner,
    aligner_path: str,
    samtools_path: str,
    reference: Path,
    r1: Path,
    r2: Path | None,
    output: Path,
    read_group: ReadGroup,
    params: AlignParams,
    tmp_prefix: Path | None = None,
    scratch: Path | None = None,
    winnowmap_repetitive_kmers: Path | None = None,
) -> list[str]:
    """Align and coordinate-sort in one pipeline, as a `/bin/sh` invocation.

    Piping straight into `samtools sort` never materializes the intermediate
    SAM, which is several times the size of the resulting BAM and pure waste to
    write.

    Returned as an explicit `sh -o pipefail -c` argv rather than a bare command
    list, because the exit status of a shell pipe is the *last* command's:
    `bwa-mem2 | samtools sort` reports samtools' success even when bwa died
    halfway, and the result is a truncated BAM that looks fine. `pipefail` is
    what makes a failing first stage fail the job.

    `sh` rather than `bash`: the base image has no bash, and `-o pipefail` is
    supported by Debian's dash as of the version trixie ships.

    `winnowmap_repetitive_kmers` is required exactly when `aligner is
    Aligner.WINNOWMAP` -- checked in `_aligner_argv`, not here, so the one
    place that raises on a missing extra input is the same place that
    consumes it.
    """
    align_argv = _aligner_argv(
        aligner=aligner,
        aligner_path=aligner_path,
        reference=reference,
        r1=r1,
        r2=r2,
        read_group=read_group,
        params=params,
        scratch=scratch,
        winnowmap_repetitive_kmers=winnowmap_repetitive_kmers,
    )

    sort_argv = [
        samtools_path,
        "sort",
        "-@",
        str(max(params.threads - 1, 1)),
        "-m",
        f"{params.sort_memory_mb}M",
        "-o",
        str(output),
    ]
    if tmp_prefix is not None:
        # Keeps samtools' spill files inside the job's scratch directory, so a
        # crashed run leaves nothing in the system temp dir for nobody to reap.
        sort_argv += ["-T", str(tmp_prefix)]

    pipeline = f"{_quote(align_argv)} | {_quote(sort_argv)}"
    return ["/bin/sh", "-o", "pipefail", "-c", pipeline]


def _aligner_argv(
    *,
    aligner: Aligner,
    aligner_path: str,
    reference: Path,
    r1: Path,
    r2: Path | None,
    read_group: ReadGroup,
    params,
    scratch: Path | None = None,
    winnowmap_repetitive_kmers: Path | None = None,
) -> list[str]:
    """The aligner half of the pipeline, before samtools.

    Six tools, five calling conventions. bwa-mem2 and minimap2 take reads
    positionally and the reference as a path; bowtie2 and HISAT2 take the
    index basename via -x and the reads via -U or -1/-2; STAR takes a genome
    *directory* via --genomeDir and has to be told to write SAM to stdout at
    all; winnowmap is minimap2's own convention plus one extra flag. Getting
    that wrong does not fail cleanly -- bowtie2 reads a stray positional
    argument as its index basename and reports a missing index.
    """
    if aligner is Aligner.WINNOWMAP:
        if winnowmap_repetitive_kmers is None:
            raise ValidationError(
                "winnowmap needs the repetitive-k-mer file meryl produces"
            )
        # `-ax <preset>` and `-R` are minimap2's own flags, verified against
        # a real build of winnowmap (it shares minimap2's argument parser);
        # `-W` is the one addition, the file that makes this a cross-check
        # aligner rather than a second minimap2 run.
        argv = [
            aligner_path,
            "-W",
            str(winnowmap_repetitive_kmers),
            "-a",
            "-x",
            params.preset,
            "-t",
            str(params.threads),
        ]
        argv += ["-R", read_group.as_sam_header(), str(reference), str(r1)]
        if r2 is not None:
            argv.append(str(r2))
        return argv

    if aligner is Aligner.STAR:
        return _star_argv(
            aligner_path=aligner_path,
            reference=reference,
            r1=r1,
            r2=r2,
            read_group=read_group,
            params=params,
            scratch=scratch,
        )

    if aligner is Aligner.BWA_MEM2:
        argv = [aligner_path, "mem", "-t", str(params.threads)]
        argv += ["-R", read_group.as_sam_header()]

        # Biology-tuning flags from Bwa2Params
        if params.min_score > 0:
            argv += ["-T", str(params.min_score)]
        if params.mark_split:
            argv.append("-M")
        argv += ["-c", str(params.max_seed_occ)]
        argv += ["-r", str(params.reseed_factor)]
        if params.all_alignments:
            argv.append("-a")
        argv += ["-m", str(params.max_mate_rescue)]
        if params.soft_clip_supp:
            argv.append("-Y")
        argv += ["-L", params.clip_penalty]
        argv += ["-h", params.multimap_xa]
        if params.batch_size > 0:
            argv += ["-K", str(params.batch_size)]

        argv += [str(reference), str(r1)]
        if r2 is not None:
            argv.append(str(r2))
        return argv

    if aligner is Aligner.MINIMAP2:
        # -a emits SAM rather than PAF, which samtools sort requires.
        argv = [aligner_path, "-a", "-x", params.preset, "-t", str(params.threads)]
        argv += ["-R", read_group.as_sam_header(), str(reference), str(r1)]
        if r2 is not None:
            argv.append(str(r2))
        return argv

    return _prefix_aligner_argv(
        aligner=aligner,
        aligner_path=aligner_path,
        reference=reference,
        r1=r1,
        r2=r2,
        read_group=read_group,
        params=params,
    )


def _star_argv(
    *,
    aligner_path: str,
    reference: Path,
    r1: Path,
    r2: Path | None,
    read_group: ReadGroup,
    params,
    scratch: Path | None,
) -> list[str]:
    """STAR, streaming SAM to stdout for samtools to sort.

    `--outStd SAM --outSAMtype SAM` is what keeps STAR inside the same
    align-and-sort pipe as the other four. STAR can write a sorted BAM itself
    (`--outSAMtype BAM SortedByCoordinate`), but taking that path would mean a
    second command shape, a second place sort memory is configured, and an
    output file rather than a stream -- for no gain, since samtools is already
    doing the sort for four other aligners.

    Two things here fail silently rather than loudly if omitted, which is why
    each is unconditional rather than a knob:

    `--readFilesCommand zcat` for gzipped input. STAR does not sniff its
    input and does not infer compression from the extension -- it reads a
    gzipped FASTQ as text and reports that every read is too short, which
    looks like bad data rather than a wrong flag. This is `aligners`' opening
    warning about fastp exactly, one tool further along, except that STAR
    cannot even be rescued by naming the file correctly.

    `--outSAMattrRGline` rather than the `@RG` line the other aligners take:
    STAR wants the fields as separate arguments and rejects the tab-escaped
    single string, so this is the third read-group convention among five
    tools.
    """
    argv = [
        aligner_path,
        "--genomeDir", aligners.layout_for(Aligner.STAR).reference_argument(reference),
        "--runThreadN", str(params.threads),
        "--outStd", "SAM",
        "--outSAMtype", "SAM",
    ]

    argv += ["--readFilesIn", str(r1)]
    if r2 is not None:
        argv.append(str(r2))

    if _is_gzipped(r1) or (r2 is not None and _is_gzipped(r2)):
        argv += ["--readFilesCommand", "zcat"]

    argv += ["--outSAMattrRGline", *read_group.as_star_rg_fields()]

    if scratch is not None:
        # Trailing slash: STAR concatenates rather than joins. Everything it
        # writes that is not the SAM stream -- Log.final.out, SJ.out.tab, its
        # own scratch directory -- lands under here, inside the job's workdir.
        argv += ["--outFileNamePrefix", f"{scratch}/"]

    if params.two_pass:
        argv += ["--twopassMode", "Basic"]

    argv += ["--outFilterMultimapNmax", str(params.out_filter_multimap_nmax)]

    if params.align_intron_max > 0:
        # 0 means "leave the flag off": STAR reads an explicit 0 as "derive
        # the ceiling from the window parameters", which is the same thing,
        # but passing the flag makes the recorded parameters claim a decision
        # that was not made.
        argv += ["--alignIntronMax", str(params.align_intron_max)]

    if params.out_sam_unmapped:
        argv += ["--outSAMunmapped", "Within"]

    return argv


def _is_gzipped(path: Path) -> bool:
    """Whether a read file is gzipped, by name.

    By name and not by magic bytes, deliberately: this is a pure command
    builder with no filesystem access, and the handler has already linked each
    read under its user-facing name for precisely this reason. A read file
    that reaches here without its extension is a bug upstream in
    `_named_read_link`, and one that would mislead every other aligner too.
    """
    return path.name.endswith(".gz")


def _prefix_aligner_argv(
    *,
    aligner: Aligner,
    aligner_path: str,
    reference: Path,
    r1: Path,
    r2: Path | None,
    read_group: ReadGroup,
    params,
) -> list[str]:
    """bowtie2 and HISAT2, which share a calling convention."""
    layout = aligners.layout_for(aligner)
    argv = [aligner_path, "-x", layout.reference_argument(reference)]

    if r2 is not None:
        argv += ["-1", str(r1), "-2", str(r2)]
    else:
        argv += ["-U", str(r1)]

    argv += ["-p", str(params.threads)]
    argv += read_group.as_rg_args()

    if params.report_k > 0:
        # 0 means "leave the flag off". `-k 0` tells the tool to report zero
        # alignments, which produces an empty BAM rather than an error.
        argv += ["-k", str(params.report_k)]

    if aligner is Aligner.BOWTIE2:
        argv.append(params.sensitivity)
        if params.local:
            argv.append("--local")
        argv += ["-X", str(params.maxins)]
        if params.no_mixed:
            argv.append("--no-mixed")
        if params.no_discordant:
            argv.append("--no-discordant")
    else:
        if params.rna_strandness:
            # The flag has no "unstranded" value -- omitting it is how that is
            # expressed, and an empty string would be rejected as an argument.
            argv += ["--rna-strandness", params.rna_strandness]
        argv += ["--max-intronlen", str(params.max_intronlen)]
        if params.no_spliced_alignment:
            argv.append("--no-spliced-alignment")
        if params.dta:
            argv.append("--dta")

    return argv


def build_index_bam_command(*, samtools_path: str, bam: Path) -> list[str]:
    return [samtools_path, "index", str(bam)]


def build_flagstat_command(*, samtools_path: str, bam: Path) -> list[str]:
    return [samtools_path, "flagstat", str(bam)]


def build_markdup_command(
    *,
    samtools_path: str,
    source: Path,
    output: Path,
    threads: int,
    paired: bool,
    tmp_prefix: Path | None = None,
) -> list[str]:
    """Mark duplicates on a coordinate-sorted BAM.

    Standard for DNA-seq variant calling and wrong for RNA-seq and amplicon
    data, where duplicate reads are the expected consequence of the protocol
    rather than a PCR artifact -- so this is a real choice, not a default.

    `markdup` picks which read of a pair to flag using the `ms` (mate score)
    tag, which only `fixmate -m` writes -- and fixmate needs mates adjacent,
    i.e. name-sorted input, while markdup needs the usual coordinate order.
    So for paired data this is name-sort -> fixmate -> coordinate-sort ->
    markdup, piped together to avoid materializing three intermediate BAMs.
    Single-end reads have no mate to score, and `markdup` runs directly on
    the (already coordinate-sorted) input.
    """
    worker_threads = str(max(threads - 1, 1))
    markdup_argv = [samtools_path, "markdup", "-@", str(threads), str(source), str(output)]
    if not paired:
        return markdup_argv

    name_sort_argv = [samtools_path, "sort", "-@", worker_threads, "-n", "-o", "-", str(source)]
    fixmate_argv = [samtools_path, "fixmate", "-@", worker_threads, "-m", "-", "-"]
    coord_sort_argv = [samtools_path, "sort", "-@", worker_threads, "-o", "-"]
    if tmp_prefix is not None:
        coord_sort_argv += ["-T", str(tmp_prefix)]
    coord_sort_argv.append("-")
    markdup_argv[-2] = "-"  # read the coordinate-sorted stream, not `source`

    pipeline = " | ".join(
        _quote(argv) for argv in (name_sort_argv, fixmate_argv, coord_sort_argv, markdup_argv)
    )
    return ["/bin/sh", "-o", "pipefail", "-c", pipeline]


def _quote(argv: list[str]) -> str:
    """Shell-quote an argv for embedding in a `sh -c` string.

    Every element goes through shlex.quote: filenames come from user-facing
    object names, and one with a space in it would otherwise split into two
    arguments -- turning a valid run into a confusing "file not found".
    """
    return " ".join(shlex.quote(a) for a in argv)


def parse_flagstat(text: str) -> dict:
    """Extract the summary numbers from `samtools flagstat` output.

    Read as part of index_bam because the file is already being traversed, and
    these four numbers are what a person actually checks before trusting an
    alignment.
    """
    facts: dict = {}

    total = re.search(r"^(\d+)\s+\+\s+(\d+)\s+in total", text, re.MULTILINE)
    if total:
        facts["total_reads"] = int(total.group(1)) + int(total.group(2))

    mapped = re.search(r"^(\d+)\s+\+\s+(\d+)\s+mapped\s*\(", text, re.MULTILINE)
    if mapped:
        facts["mapped_reads"] = int(mapped.group(1)) + int(mapped.group(2))

    paired = re.search(
        r"^(\d+)\s+\+\s+(\d+)\s+properly paired\s*\(", text, re.MULTILINE
    )
    if paired:
        facts["properly_paired_reads"] = int(paired.group(1)) + int(paired.group(2))

    dups = re.search(r"^(\d+)\s+\+\s+(\d+)\s+duplicates", text, re.MULTILINE)
    if dups:
        facts["duplicate_reads"] = int(dups.group(1)) + int(dups.group(2))

    # Rates are derived here rather than parsed from flagstat's own percentages,
    # which it prints as "N/A" when the denominator is zero.
    total_reads = facts.get("total_reads")
    if total_reads:
        if "mapped_reads" in facts:
            facts["mapped_pct"] = round(100 * facts["mapped_reads"] / total_reads, 2)
        if "properly_paired_reads" in facts:
            facts["properly_paired_pct"] = round(
                100 * facts["properly_paired_reads"] / total_reads, 2
            )
        if "duplicate_reads" in facts:
            facts["duplicate_pct"] = round(100 * facts["duplicate_reads"] / total_reads, 2)

    return facts


@dataclass
class SamtoolsProgress:
    """Turns samtools sort/merge/index stderr lines into progress updates.

    samtools reports its merge phase during sort and merge:
      [bam_sort_core] merging from 2 files...

    These are phase-only signals with no countable unit, so pct stays None
    and the bar stays indeterminate — but the phase label tells the user
    what the tool is doing rather than showing a generic "sorting" for
    the entire run.
    """

    name: str = "samtools"
    phase: str = "starting"
    _merge_seen: bool = False

    def feed(self, line: str) -> bool:
        """Consume a line. True if the caller should publish an update."""
        if _MERGING_RE.search(line):
            if not self._merge_seen:
                self._merge_seen = True
                self.phase = "merging"
                return True
            return False
        return False

    @property
    def pct(self) -> float | None:
        return None

    def message(self) -> str:
        return self.phase

    def snapshot(self) -> dict:
        return {
            "pct": self.pct,
            "phase": self.phase,
            "message": self.message(),
        }


@dataclass
class AlignProgress:
    """Turns an aligner's own output into a progress fraction.

    Same honesty constraint as trimming: the read total is an estimate
    extrapolated at ingest, so the bar caps below complete rather than claiming
    a completion it cannot verify.
    """

    name: str = "aligner"
    expected_reads: int | None = None
    processed: int = 0
    phase: str = "aligning"

    def feed(self, line: str) -> bool:
        """Consume a line. True if the caller should publish an update."""
        if _MERGING_RE.search(line):
            self.phase = "sorting"
            return True

        match = _PROCESSED_RE.search(line) or _MAPPED_RE.search(line)
        if not match:
            return False

        # Cumulative, not a running total: both bwa-mem2 and minimap2 report
        # a per-batch count, not a running total.
        self.processed += int(match.group(1))
        return True

    @property
    def pct(self) -> float | None:
        if not self.expected_reads:
            return None
        return min(self.processed / self.expected_reads, MAX_MEASURED_PCT)

    @property
    def phase_index(self) -> int | None:
        """Position in PHASE_ORDER, 1-based for "step N of M" display."""
        if self.phase not in PHASE_ORDER:
            return None
        return PHASE_ORDER.index(self.phase) + 1

    def message(self) -> str:
        if self.phase == "sorting":
            return "sorting alignments"
        if self.processed:
            return f"aligned {self.processed:,} reads"
        return "aligning"

    def snapshot(self) -> dict:
        return {
            "pct": self.pct,
            "phase": self.phase,
            "message": self.message(),
            "phase_index": self.phase_index,
            "phase_total": len(PHASE_ORDER),
        }
