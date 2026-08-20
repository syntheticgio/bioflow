"""Building a short-read structural variant calling run with Delly.

Kept separate from the job handler so the parts worth testing -- command
construction and parameter validation -- are pure functions over strings and
paths, with no queue or filesystem involved. Mirrors `sniffles_runner.py`,
which splits the same way for the same reason.

Which caller runs for a given chemistry is `sv_caller.py`'s question, not
this module's.
"""

from dataclasses import dataclass
from pathlib import Path

from app.errors import ValidationError
from app.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class DellyParams:
    """User-facing knobs for a Delly run.

    Deliberately not a mirror of `SnifflesParams`. Delly has no minimum
    call-size flag: its `-m` is `minrefsep` (minimum reference separation,
    default 25), which governs breakpoint clustering rather than the size of
    calls it reports, so mapping Sniffles' 50 bp `--minsvlen` onto it would
    be a wrong mapping that looks right. Length filtering happens in the SV
    table instead, where the user can see it. Verified against src/delly.h at
    v2.6.0 on 2026-08-20.
    """

    threads: int = 4
    # Delly's own default for min. paired-end mapping quality (-q).
    min_map_quality: int = 1

    def as_dict(self) -> dict:
        return {
            "threads": self.threads,
            "min_map_quality": self.min_map_quality,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "DellyParams":
        raw = dict(raw or {})

        threads = int(raw.get("threads", 4))
        if threads < 1:
            raise ValidationError("threads must be at least 1")

        min_map_quality = int(raw.get("min_map_quality", 1))
        if min_map_quality < 0:
            raise ValidationError("min_map_quality cannot be negative")

        return cls(threads=threads, min_map_quality=min_map_quality)


def build_delly_command(
    *,
    delly_path: str,
    bam: Path,
    reference: Path,
    output: Path,
    params: DellyParams,
) -> list[str]:
    """Assemble the Delly invocation.

    `sr` is the short-read subcommand. Delly 2.x replaced the single `call`
    subcommand with `sr` and `lr`; a `delly call` invocation targets a CLI
    that no longer exists.

    `lr` is never used here. Sniffles2 is this pipeline's long-read caller --
    see `sv_caller.py`'s `caller_for_chemistry`.

    Output is BCF (`-o`) rather than a VCF redirected from stdout. Delly
    supports both, and stdout is the worse choice: a crash mid-write leaves a
    truncated file that exists and is non-empty, which defeats the handler's
    "exited 0 but produced no VCF" check. `build_bcf_to_vcf_command` does the
    conversion.
    """
    return [
        delly_path,
        "sr",
        "-g",
        str(reference),
        "-o",
        str(output),
        "-q",
        str(params.min_map_quality),
        "-h",
        str(params.threads),
        str(bam),
    ]


def build_bcf_to_vcf_command(
    *,
    bcftools_path: str,
    bcf: Path,
    output: Path,
) -> list[str]:
    """Convert Delly's BCF output to the bgzipped VCF the rest of the SV
    pipeline expects.

    `-O z` is bgzipped VCF, which is what tabix indexes and what `sv_db`
    ingests. Teaching `sv_db` to read BCF directly was rejected: it would add
    a second ingest path into one table.
    """
    return [
        bcftools_path,
        "view",
        "-O",
        "z",
        "-o",
        str(output),
        str(bcf),
    ]
