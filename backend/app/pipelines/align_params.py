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
    aligner: Aligner = Aligner.BWA_MEM2

    @classmethod
    def from_dict(cls, data: dict) -> "Bwa2Params":
        return cls(aligner=Aligner.BWA_MEM2, **cls._shared(data))


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


PARAMS_CLASSES: dict[Aligner, type[BaseAlignParams]] = {
    Aligner.BWA_MEM2: Bwa2Params,
    Aligner.MINIMAP2: Minimap2Params,
    Aligner.BOWTIE2: Bowtie2Params,
    Aligner.HISAT2: Hisat2Params,
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
