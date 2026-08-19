"""Building and observing a structural variant calling run.

Kept separate from the job handler so the parts worth testing -- command
construction and the chemistry gate -- are pure functions over strings and
paths, with no queue or filesystem involved. Mirrors `variant_runner.py` and
`align_runner.py`, which split the same way for the same reason.
"""

from dataclasses import dataclass
from pathlib import Path

from app.errors import ValidationError
from app.logging import get_logger
from app.pipelines.align_runner import ReadChemistry

log = get_logger(__name__)

# Chemistries whose reads are long enough for breakpoint resolution.
_LONG_READ = frozenset(
    {
        ReadChemistry.HIFI,
        ReadChemistry.CLR,
        ReadChemistry.ONT_SIMPLEX,
        ReadChemistry.ONT_DUPLEX,
    }
)


@dataclass
class SnifflesParams:
    """User-facing knobs for a structural variant run."""

    threads: int = 4
    # None means Sniffles' own automatic mode, which derives the threshold
    # from coverage. Deliberately not a fixed integer: a hardcoded default is
    # wrong in both directions -- too high on a 10x callset, too low on a
    # 100x one -- so this exists to *override* the automatic value, and unset
    # must reach Sniffles as "decide for me" rather than as a number this
    # application chose.
    min_support: int | None = None
    # 50 bp, the conventional floor for what counts as structural rather
    # than an indel.
    min_sv_length: int = 50
    tandem_repeats: str | None = None

    def as_dict(self) -> dict:
        return {
            "threads": self.threads,
            "min_support": self.min_support,
            "min_sv_length": self.min_sv_length,
            "tandem_repeats": self.tandem_repeats,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "SnifflesParams":
        raw = dict(raw or {})

        threads = int(raw.get("threads", 4))
        if threads < 1:
            raise ValidationError("threads must be at least 1")

        min_support = raw.get("min_support")
        if min_support is not None:
            min_support = int(min_support)
            if min_support < 1:
                raise ValidationError("min_support must be at least 1")

        min_sv_length = int(raw.get("min_sv_length", 50))
        if min_sv_length < 1:
            raise ValidationError("min_sv_length must be at least 1")

        tandem_repeats = raw.get("tandem_repeats")
        return cls(
            threads=threads,
            min_support=min_support,
            min_sv_length=min_sv_length,
            tandem_repeats=str(tandem_repeats) if tandem_repeats else None,
        )


def sv_calling_allowed_for(chemistry: ReadChemistry) -> bool:
    """Whether this chemistry's reads can support SV calling.

    CLR is allowed here and *refused* by
    `variant_runner.caller_for_chemistry`. That asymmetry is deliberate and
    should not be harmonised away: small-variant calling reads per-base
    accuracy, which CLR does not have, while Sniffles resolves breakpoints
    from alignment structure -- split reads and within-read gaps -- which
    tolerates a high per-base error rate. CLR reads are long, and length is
    the property SV detection needs.

    SHORT is refused because Sniffles is a long-read caller; UNKNOWN because
    it means QC has not run, and an unrecognised BAM that turns out to be
    Illumina would produce junk quietly.
    """
    return chemistry in _LONG_READ


def build_sniffles_command(
    *,
    sniffles_path: str,
    bam: Path,
    reference: Path,
    output: Path,
    params: SnifflesParams,
) -> list[str]:
    """Assemble the Sniffles invocation.

    `--reference` is passed so insertion sequences are reported rather than
    left symbolic; without it an INS record carries no inserted bases, which
    is most of what makes an insertion call useful.
    """
    argv = [
        sniffles_path,
        "--input",
        str(bam),
        "--reference",
        str(reference),
        "--vcf",
        str(output),
        "--threads",
        str(params.threads),
        "--minsvlen",
        str(params.min_sv_length),
    ]
    if params.min_support is not None:
        argv += ["--minsupport", str(params.min_support)]
    if params.tandem_repeats:
        argv += ["--tandem-repeats", params.tandem_repeats]
    return argv
