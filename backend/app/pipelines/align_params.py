"""Per-aligner parameter sets.

Split from `align_runner` because parameter validation and command
construction are separate responsibilities, and because a single flat class
covering every aligner would be a union of about thirty fields, most of them
inapplicable to whichever tool is actually running. A field that exists but
does nothing is worse than one that is absent: it reaches the run's recorded
parameters and implies the tool honored it.

`from_dict` dispatches on the `aligner` key. Every subclass validates only
its own knobs, so an unknown key for the wrong tool is rejected at launch
rather than silently dropped.
"""

from dataclasses import dataclass

from app.errors import ValidationError
from app.pipelines.aligners import Aligner

# samtools spills to disk below this, which is slower than the memory saved.
MIN_SORT_MEMORY_MB = 64


@dataclass
class BaseAlignParams:
    """The knobs every aligner in this application shares.

    samtools does the sorting for all of them, which is why sort memory is
    here rather than per-tool. Threads is shared because every tool takes a
    thread count, even though the flag differs (-t, -p).
    """

    aligner: Aligner
    threads: int = 4
    sort_memory_mb: int = 1024
    mark_duplicates: bool = False

    def as_dict(self) -> dict:
        return {
            "aligner": self.aligner.value,
            "threads": self.threads,
            "sort_memory_mb": self.sort_memory_mb,
            "mark_duplicates": self.mark_duplicates,
        }

    @staticmethod
    def _shared(data: dict) -> dict:
        threads = int(data.get("threads", 4))
        if threads < 1:
            raise ValidationError("threads must be at least 1")

        sort_memory_mb = int(data.get("sort_memory_mb", 1024))
        if sort_memory_mb < MIN_SORT_MEMORY_MB:
            raise ValidationError(
                f"sort_memory_mb must be at least {MIN_SORT_MEMORY_MB}"
            )

        return {
            "threads": threads,
            "sort_memory_mb": sort_memory_mb,
            "mark_duplicates": bool(data.get("mark_duplicates", False)),
        }


@dataclass
class Bwa2Params(BaseAlignParams):
    """bwa-mem2 tuning parameters.

    Most of these map directly to bwa-mem2 CLI flags. The defaults match the
    "Human / other Eukaryote" preset behavior; organism-type presets override
    specific values via the preset system in aligner_registry.py.

    Fields with type "text" accept comma-separated pairs (e.g. "5,5" for
    clip_penalty) and are stored as strings -- the validation ensures they
    match the expected pattern.
    """

    aligner: Aligner = Aligner.BWA_MEM2
    # -T: minimum alignment score threshold
    min_score: int = 30
    # -M: mark shorter split hits as secondary (Picard/GATK compat)
    mark_split: bool = False
    # -c: max seed occurrences before discarding
    max_seed_occ: int = 500
    # -r: re-seeding trigger factor
    reseed_factor: float = 1.5
    # -a: output all alignments for unpaired reads
    all_alignments: bool = False
    # -m: max mate-rescue attempts
    max_mate_rescue: int = 100
    # -Y: soft-clip supplementary alignments instead of hard-clipping
    soft_clip_supp: bool = False
    # -L: clipping penalty (5' and 3'), comma-separated pair
    clip_penalty: str = "5,5"
    # -h: multi-mapping hits reported as XA tags, comma-separated pair
    multimap_xa: str = "5,200"
    # -K: fixed read batch size (0 = bwa-mem2's default)
    batch_size: int = 0
    # Which preset is active, if any. "" means no preset / advanced mode.
    preset: str = ""

    def as_dict(self) -> dict:
        return {
            **super().as_dict(),
            "min_score": self.min_score,
            "mark_split": self.mark_split,
            "max_seed_occ": self.max_seed_occ,
            "reseed_factor": self.reseed_factor,
            "all_alignments": self.all_alignments,
            "max_mate_rescue": self.max_mate_rescue,
            "soft_clip_supp": self.soft_clip_supp,
            "clip_penalty": self.clip_penalty,
            "multimap_xa": self.multimap_xa,
            "batch_size": self.batch_size,
            "preset": self.preset,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Bwa2Params":
        min_score = int(data.get("min_score", 30))
        if min_score < 0:
            raise ValidationError("min_score cannot be negative")

        max_seed_occ = int(data.get("max_seed_occ", 500))
        if max_seed_occ < 1:
            raise ValidationError("max_seed_occ must be at least 1")

        reseed_factor = float(data.get("reseed_factor", 1.5))
        if reseed_factor < 1:
            raise ValidationError("reseed_factor must be at least 1")

        max_mate_rescue = int(data.get("max_mate_rescue", 100))
        if max_mate_rescue < 0:
            raise ValidationError("max_mate_rescue cannot be negative")

        clip_penalty = str(data.get("clip_penalty", "5,5"))
        _validate_comma_pair(clip_penalty, "clip_penalty")

        multimap_xa = str(data.get("multimap_xa", "5,200"))
        _validate_comma_pair(multimap_xa, "multimap_xa")

        batch_size = int(data.get("batch_size", 0))
        if batch_size < 0:
            raise ValidationError("batch_size cannot be negative")

        preset = str(data.get("preset", ""))

        return cls(
            aligner=Aligner.BWA_MEM2,
            min_score=min_score,
            mark_split=bool(data.get("mark_split", False)),
            max_seed_occ=max_seed_occ,
            reseed_factor=reseed_factor,
            all_alignments=bool(data.get("all_alignments", False)),
            max_mate_rescue=max_mate_rescue,
            soft_clip_supp=bool(data.get("soft_clip_supp", False)),
            clip_penalty=clip_penalty,
            multimap_xa=multimap_xa,
            batch_size=batch_size,
            preset=preset,
            **cls._shared(data),
        )


def _validate_comma_pair(value: str, name: str) -> None:
    """Validate a comma-separated pair of integers (e.g. "5,5" or "5,200")."""
    parts = value.split(",")
    if len(parts) != 2:
        raise ValidationError(
            f"{name} must be a comma-separated pair of integers, got {value!r}"
        )
    try:
        int(parts[0].strip())
        int(parts[1].strip())
    except ValueError:
        raise ValidationError(
            f"{name} must be a comma-separated pair of integers, got {value!r}"
        )


# minimap2 presets. Not cosmetic: the wrong preset for long reads produces
# silently poor alignments rather than an error.
MINIMAP2_PRESETS: tuple[str, ...] = ("map-ont", "map-pb", "map-hifi", "lr:hq", "sr")


@dataclass
class Minimap2Params(BaseAlignParams):
    aligner: Aligner = Aligner.MINIMAP2
    preset: str = "sr"

    def as_dict(self) -> dict:
        return {**super().as_dict(), "preset": self.preset}

    @classmethod
    def from_dict(cls, data: dict) -> "Minimap2Params":
        preset = data.get("preset") or "sr"
        if preset not in MINIMAP2_PRESETS:
            raise ValidationError(
                f"Unknown minimap2 preset {preset!r}",
                details={"valid": list(MINIMAP2_PRESETS)},
            )
        return cls(aligner=Aligner.MINIMAP2, preset=preset, **cls._shared(data))


BOWTIE2_SENSITIVITIES: tuple[str, ...] = (
    "--very-fast", "--fast", "--sensitive", "--very-sensitive",
)


@dataclass
class Bowtie2Params(BaseAlignParams):
    aligner: Aligner = Aligner.BOWTIE2
    sensitivity: str = "--sensitive"
    # End-to-end requires the whole read to align; --local soft-clips the
    # ends. Local is the right choice when reads carry adapter remnants or
    # when the reference is a partial assembly.
    local: bool = False
    # The insert-size ceiling. A pair whose implied fragment exceeds this is
    # not called properly-paired, which is why it matters for ChIP-seq, where
    # fragment length is the experimental variable.
    maxins: int = 500
    no_mixed: bool = False
    no_discordant: bool = False
    # Report up to N alignments per read rather than the single best. 0 means
    # "leave the flag off", which is bowtie2's default behavior.
    report_k: int = 0

    def as_dict(self) -> dict:
        return {
            **super().as_dict(),
            "sensitivity": self.sensitivity,
            "local": self.local,
            "maxins": self.maxins,
            "no_mixed": self.no_mixed,
            "no_discordant": self.no_discordant,
            "report_k": self.report_k,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Bowtie2Params":
        sensitivity = data.get("sensitivity") or "--sensitive"
        if sensitivity not in BOWTIE2_SENSITIVITIES:
            raise ValidationError(
                f"Unknown bowtie2 sensitivity {sensitivity!r}",
                details={"valid": list(BOWTIE2_SENSITIVITIES)},
            )

        maxins = int(data.get("maxins", 500))
        if maxins < 1:
            raise ValidationError("maxins must be at least 1")

        report_k = int(data.get("report_k", 0))
        if report_k < 0:
            raise ValidationError("report_k cannot be negative")

        return cls(
            aligner=Aligner.BOWTIE2,
            sensitivity=sensitivity,
            local=bool(data.get("local", False)),
            maxins=maxins,
            no_mixed=bool(data.get("no_mixed", False)),
            no_discordant=bool(data.get("no_discordant", False)),
            report_k=report_k,
            **cls._shared(data),
        )


# "" means unstranded, which is HISAT2's default (the flag is omitted).
HISAT2_STRANDNESS: tuple[str, ...] = ("", "FR", "RF", "F", "R")


@dataclass
class Hisat2Params(BaseAlignParams):
    aligner: Aligner = Aligner.HISAT2
    # FR/RF for paired libraries, F/R for single. The wrong value does not
    # fail -- it silently reverses which strand a read is attributed to, and
    # the error only surfaces as nonsense in downstream counting.
    rna_strandness: str = ""
    max_intronlen: int = 500000
    # For DNA input: HISAT2 is splice-aware by default, and spliced alignment
    # over genomic DNA invents junctions that are not there.
    no_spliced_alignment: bool = False
    # Formats output for downstream transcript assembly (StringTie et al).
    dta: bool = False
    report_k: int = 0

    def as_dict(self) -> dict:
        return {
            **super().as_dict(),
            "rna_strandness": self.rna_strandness,
            "max_intronlen": self.max_intronlen,
            "no_spliced_alignment": self.no_spliced_alignment,
            "dta": self.dta,
            "report_k": self.report_k,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Hisat2Params":
        strandness = data.get("rna_strandness") or ""
        if strandness not in HISAT2_STRANDNESS:
            raise ValidationError(
                f"Unknown rna_strandness {strandness!r}",
                details={"valid": list(HISAT2_STRANDNESS)},
            )

        max_intronlen = int(data.get("max_intronlen", 500000))
        if max_intronlen < 1:
            raise ValidationError("max_intronlen must be at least 1")

        report_k = int(data.get("report_k", 0))
        if report_k < 0:
            raise ValidationError("report_k cannot be negative")

        return cls(
            aligner=Aligner.HISAT2,
            rna_strandness=strandness,
            max_intronlen=max_intronlen,
            no_spliced_alignment=bool(data.get("no_spliced_alignment", False)),
            dta=bool(data.get("dta", False)),
            report_k=report_k,
            **cls._shared(data),
        )


@dataclass
class StarParams(BaseAlignParams):
    aligner: Aligner = Aligner.STAR
    # Re-align against the junctions found in a first pass. Roughly doubles
    # runtime and is the standard recommendation when novel junctions matter.
    two_pass: bool = False
    # Reads aligning to more loci than this are reported as unmapped rather
    # than as multi-mappers. STAR's own default.
    out_filter_multimap_nmax: int = 20
    # 0 means "leave the flag off", which lets STAR derive its own ceiling
    # (~590 kb) from the window parameters. Set it low -- 1 -- to forbid
    # spliced alignment entirely, which is what DNA input wants.
    align_intron_max: int = 0
    # Keep unmapped reads in the output, which STAR does *not* do by default.
    # Defaulted True to match the other four aligners: every downstream number
    # this application shows comes from `samtools flagstat`, and flagstat over
    # a BAM with the unmapped reads discarded reports 100% mapped whatever the
    # truth was. A silently meaningless headline statistic is worse than a
    # departure from STAR's default.
    out_sam_unmapped: bool = True

    def as_dict(self) -> dict:
        return {
            **super().as_dict(),
            "two_pass": self.two_pass,
            "out_filter_multimap_nmax": self.out_filter_multimap_nmax,
            "align_intron_max": self.align_intron_max,
            "out_sam_unmapped": self.out_sam_unmapped,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StarParams":
        multimap = int(data.get("out_filter_multimap_nmax", 20))
        if multimap < 1:
            raise ValidationError("out_filter_multimap_nmax must be at least 1")

        intron_max = int(data.get("align_intron_max", 0))
        if intron_max < 0:
            raise ValidationError("align_intron_max cannot be negative")

        return cls(
            aligner=Aligner.STAR,
            two_pass=bool(data.get("two_pass", False)),
            out_filter_multimap_nmax=multimap,
            align_intron_max=intron_max,
            out_sam_unmapped=bool(data.get("out_sam_unmapped", True)),
            **cls._shared(data),
        )


# winnowmap has no short-read mode -- it exists to cross-check minimap2 on
# long reads for GCI, so offering "sr" here would be offering a run that
# cannot work. Kept as its own tuple rather than reusing MINIMAP2_PRESETS,
# which includes "sr".
WINNOWMAP_PRESETS: tuple[str, ...] = ("map-pb", "map-ont", "map-hifi")


@dataclass
class WinnowmapParams(BaseAlignParams):
    """winnowmap's knobs, plus the meryl parameters that build its `-W` file.

    `k` and `distinct` govern the meryl index (`meryl count k=... output
    ...` then `meryl print greater-than distinct=... ...`), not winnowmap
    itself -- they are here rather than on a separate params class because
    the meryl step has no independent existence in this application: it is
    always in service of one winnowmap alignment, the same reasoning that
    keeps sort_memory_mb on BaseAlignParams even though samtools, not the
    aligner, reads it.
    """

    aligner: Aligner = Aligner.WINNOWMAP
    preset: str = "map-pb"
    # GCI's own README example: `meryl count k=15`.
    k: int = 15
    # GCI's own README example: `meryl print greater-than distinct=0.9998`.
    distinct: float = 0.9998

    def as_dict(self) -> dict:
        return {
            **super().as_dict(),
            "preset": self.preset,
            "k": self.k,
            "distinct": self.distinct,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WinnowmapParams":
        preset = data.get("preset") or "map-pb"
        if preset not in WINNOWMAP_PRESETS:
            raise ValidationError(
                f"Unknown winnowmap preset {preset!r}",
                details={"valid": list(WINNOWMAP_PRESETS)},
            )

        k = int(data.get("k", 15))
        if k < 1 or k > 28:
            # winnowmap's own -k help: "k-mer size (no larger than 28)".
            raise ValidationError("k must be between 1 and 28")

        distinct = float(data.get("distinct", 0.9998))
        if not (0.0 < distinct <= 1.0):
            raise ValidationError("distinct must be between 0 and 1")

        return cls(
            aligner=Aligner.WINNOWMAP,
            preset=preset,
            k=k,
            distinct=distinct,
            **cls._shared(data),
        )


PARAMS_CLASSES: dict[Aligner, type[BaseAlignParams]] = {
    Aligner.BWA_MEM2: Bwa2Params,
    Aligner.MINIMAP2: Minimap2Params,
    Aligner.BOWTIE2: Bowtie2Params,
    Aligner.HISAT2: Hisat2Params,
    Aligner.STAR: StarParams,
    Aligner.WINNOWMAP: WinnowmapParams,
}


def from_dict(data: dict | None) -> BaseAlignParams:
    """Build the parameter set for whichever aligner the payload names.

    `Aligner(...)` raises ValueError on an unknown name, which is the right
    failure: an aligner this application has no spec for has no command
    builder either, and defaulting would run a tool the user did not choose.
    """
    data = dict(data or {})
    aligner = Aligner(data.get("aligner", Aligner.MINIMAP2))
    return PARAMS_CLASSES[aligner].from_dict(data)
