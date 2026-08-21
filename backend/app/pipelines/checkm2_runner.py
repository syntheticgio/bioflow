"""CheckM2 command-building and quality-report parsing.

A pure module, on the metabat_runner.py model: no subprocess calls live here,
so every function is unit-testable without the binary installed -- which
matters more than usual here, because CheckM2 cannot be installed on arm64 at
all (see tools.checkm2()) and these functions must still be covered on the
machine this is developed on.

Output shapes are pinned to CheckM2 1.1.0, read from `predictQuality.py` in a
real bioconda install on 2026-08-21. Three things that inspection revealed,
none of them safe to guess:

  * The score columns are **mode-dependent**. The default run writes
    `Completeness` and `Contamination`; `--allmodels` writes
    `Completeness_General` and `Completeness_Specific` and NO plain
    `Completeness` column at all. A parser keyed only on `Completeness` reads
    an --allmodels table as having no scores rather than failing loudly, so
    `parse_quality_report` accepts either shape.
  * `Name` is the bin's file stem, without the extension -- that is the only
    join back to the bin object, so the handler maps stems to object ids.
  * CheckM2 rounds to 2dp itself, so the numbers in the table are already the
    numbers to store; this module does no further rounding.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# The file CheckM2 writes its table to, inside --output-directory.
QUALITY_REPORT = "quality_report.tsv"


@dataclass(frozen=True)
class BinQuality:
    """One row of `quality_report.tsv` -- one bin's scores."""

    name: str  # the bin file's stem, e.g. "bin.3"
    completeness: float
    contamination: float
    completeness_model: str | None = None
    coding_density: float | None = None
    contig_n50: int | None = None
    genome_size: int | None = None
    gc_content: float | None = None
    total_contigs: int | None = None
    additional_notes: str | None = None

    @property
    def quality_score(self) -> float:
        """The conventional aggregate: completeness - 5 x contamination.

        Reported alongside the two raw numbers rather than instead of them.
        It is a convention (Parks et al.), not a measurement, and it can go
        negative for a badly contaminated bin -- which is meaningful and is
        deliberately not floored at zero.
        """
        return round(self.completeness - 5.0 * self.contamination, 2)


def build_predict_command(
    *,
    checkm2_path: str,
    bins_dir: Path,
    output_dir: Path,
    database_path: Path,
    extension: str = "fa",
    threads: int = 4,
    lowmem: bool = False,
) -> list[str]:
    """`checkm2 predict` over a directory of bins.

    One invocation over the whole directory, never one per bin (spec Q3):
    CheckM2's fixed cost is loading the DIAMOND database, and it already
    takes a directory and emits one table.

    `--extension` is passed explicitly and defaults to `fa` here, NOT to
    CheckM2's own default of `.fna`. MetaBAT2 writes `<prefix>.<N>.fa`, so
    leaving CheckM2's default in place makes it find zero bins and exit
    successfully with an empty table -- a silent no-op, not an error.

    `--force` is passed so a retried job overwrites its own previous output
    directory rather than exiting on "output directory exists".
    """
    cmd = [
        checkm2_path,
        "predict",
        "--input",
        str(bins_dir),
        "--output-directory",
        str(output_dir),
        # Explicit per-run path beats the CHECKM2DB environment variable:
        # it is testable and cannot be shadowed by ambient state (spec S-5).
        "--database_path",
        str(database_path),
        "--extension",
        extension,
        "--threads",
        str(max(1, int(threads))),
        "--force",
    ]
    if lowmem:
        # Halves DIAMOND's block size. Slower, but the documented escape
        # hatch when the database does not fit.
        cmd.append("--lowmem")
    return cmd


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text or text.upper() in {"NA", "N/A", "NAN", "NONE"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: str | None) -> int | None:
    number = _to_float(value)
    return None if number is None else int(number)


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def parse_quality_report(content: str) -> list[BinQuality]:
    """Parse `quality_report.tsv` into one `BinQuality` per bin.

    Rows without a usable completeness/contamination pair are dropped with a
    warning rather than raising: one unscored bin in a set of forty must not
    cost the other thirty-nine their scores, the same posture #728 takes when
    ingesting bins.

    **Contamination is never clamped** (spec R6). Values above 100% are real
    and mean several organisms merged into one bin; clamping to 100 would hide
    the worst bins by making them look merely mediocre, which is the exact
    opposite of what this number is for.
    """
    if not content.strip():
        return []

    reader = csv.DictReader(io.StringIO(content), delimiter="\t")
    rows: list[BinQuality] = []
    for row in reader:
        name = _text(row.get("Name"))
        if not name:
            continue

        # Mode-dependent columns -- see the module docstring. Prefer the
        # plain column the default run writes; fall back to the specific
        # model, then the general one, so an --allmodels table still parses.
        completeness = _to_float(row.get("Completeness"))
        if completeness is None:
            completeness = _to_float(row.get("Completeness_Specific"))
        if completeness is None:
            completeness = _to_float(row.get("Completeness_General"))
        contamination = _to_float(row.get("Contamination"))

        if completeness is None or contamination is None:
            log.warning("checkm2_row_unscored", name=name)
            continue

        rows.append(
            BinQuality(
                name=name,
                completeness=completeness,
                contamination=contamination,
                completeness_model=_text(row.get("Completeness_Model_Used")),
                coding_density=_to_float(row.get("Coding_Density")),
                contig_n50=_to_int(row.get("Contig_N50")),
                genome_size=_to_int(row.get("Genome_Size")),
                gc_content=_to_float(row.get("GC_Content")),
                total_contigs=_to_int(row.get("Total_Contigs")),
                additional_notes=_text(row.get("Additional_Notes")),
            )
        )
    return rows


# The community conventions, as a label beside the numbers -- never used to
# filter, hide or discard a bin (spec Q4/R5). A 40%-complete bin is a
# legitimate result for a low-abundance organism, and dropping it would
# destroy the finding that the organism is present at all.
#
# Thresholds from the MIMAG standard (Bowers et al. 2017).
def quality_tier(completeness: float, contamination: float) -> str:
    """The conventional tier for one bin, as a descriptive label."""
    if completeness >= 90.0 and contamination <= 5.0:
        return "high"
    if completeness >= 50.0 and contamination <= 10.0:
        return "medium"
    return "low"


def bin_quality_facts(quality: BinQuality) -> dict:
    """What one bin records about having been scored.

    Per-key facts, merged by `facts.<key>` path rather than as a whole dict
    (#606). The tier is stored alongside the raw numbers so the UI need not
    re-derive a convention, but the numbers remain the source of truth.
    """
    facts: dict = {
        "checkm2_completeness": quality.completeness,
        "checkm2_contamination": quality.contamination,
        "checkm2_quality_score": quality.quality_score,
        "checkm2_quality_tier": quality_tier(
            quality.completeness, quality.contamination
        ),
    }
    if quality.completeness_model:
        facts["checkm2_completeness_model"] = quality.completeness_model
    if quality.coding_density is not None:
        facts["checkm2_coding_density"] = quality.coding_density
    if quality.contig_n50 is not None:
        facts["checkm2_contig_n50"] = quality.contig_n50
    if quality.genome_size is not None:
        facts["checkm2_genome_size"] = quality.genome_size
    if quality.gc_content is not None:
        facts["checkm2_gc_content"] = quality.gc_content
    if quality.total_contigs is not None:
        facts["checkm2_total_contigs"] = quality.total_contigs
    if quality.additional_notes:
        facts["checkm2_notes"] = quality.additional_notes
    return facts
