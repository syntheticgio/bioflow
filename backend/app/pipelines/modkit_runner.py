"""modkit command-building, MM/ML presence probing, and bedMethyl parsing.

A pure module, on the mosdepth_runner.py model: no subprocess calls live
here, so every function is unit-testable without the binary installed. The
handler in app/queue/methylation_handlers.py supplies the process.

`has_modification_tags` is the module's load-bearing function -- see
docs/superpowers/specs/2026-08-20-modkit-methylation-design.md, decision K1.
MM and ML are per-alignment-record tags, not header fields, so
`storage/parsers.py` -- headers and a small prefix only, by deliberate policy
-- cannot answer "does this BAM carry modification calls" without breaking
the rule that module is built around. This is a bounded prefix scan instead,
in its own place, that says what it found *and how far it looked*.

No `--ref`/`--cpg` in `build_pileup_command`: `modkit pileup --ref` is only
required to resolve CpG-context sites for `--cpg`, and this feature does not
use `--cpg`. Without it modkit still calls and tabulates every modification
the MM/ML tags carry, just without reference-context annotation -- so the
pileup works on any BAM with modification tags regardless of whether a
reference is resolvable for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# How many alignment records `has_modification_tags` reads before giving up.
# Cheap (kilobytes, not gigabytes) and correct for the realistic case where
# modification calling is a property of the basecalling run and therefore
# uniform across the file. A BAM whose first DEFAULT_PROBE_LIMIT reads lack
# MM but whose later reads carry it is a real, documented false negative --
# see ModTagProbe and the design's decision K1.
DEFAULT_PROBE_LIMIT = 1000

# Both spellings appear in the wild: `MM`/`ML` is the current SAM spec name,
# `Mm`/`Ml` is the legacy name some older basecaller/tooling output used
# before the spec settled. Checking only "MM" would silently treat a
# legacy-tagged BAM as having no modification calls.
_MM_TAG_NAMES = ("MM", "Mm")


@dataclass(frozen=True)
class ModTagProbe:
    """What a bounded prefix scan of a BAM found, and how far it looked.

    `records_scanned` is what lets a caller say "no modification tags in the
    first 1000 reads" -- the honest, bounded claim -- rather than the
    unqualified "no modification tags" the scan has not earned.
    """

    found: bool
    records_scanned: int
    limit: int


def has_modification_tags(
    bam_path: str | Path, *, limit: int = DEFAULT_PROBE_LIMIT
) -> ModTagProbe:
    """Scan at most `limit` alignment records for an MM/Mm tag.

    Stops as soon as one is found. A BAM with fewer than `limit` records is
    scanned in full, and `records_scanned` reflects the real count in that
    case rather than the requested limit.
    """
    import pysam

    records_scanned = 0
    found = False
    with pysam.AlignmentFile(str(bam_path), "rb", check_sq=False) as fh:
        for read in fh:
            records_scanned += 1
            if any(read.has_tag(name) for name in _MM_TAG_NAMES):
                found = True
                break
            if records_scanned >= limit:
                break
    return ModTagProbe(found=found, records_scanned=records_scanned, limit=limit)


def build_pileup_command(
    bam_path: str | Path, output_path: str | Path, *, threads: int = 2
) -> list[str]:
    """Build `modkit pileup <bam> <output_bed> --threads N`.

    Deliberately no `--ref`/`--cpg` -- see the module docstring. That keeps
    this invocation usable on any BAM carrying MM/ML tags, with no reference
    resolution step required before the job can run.
    """
    return [
        "modkit",
        "pileup",
        str(bam_path),
        str(output_path),
        "--threads",
        str(threads),
    ]


# bedMethyl's documented 18-column layout (modkit README, "Description of
# bedMethyl output", current master/0.6.x as of 2026-08-20). Columns 1-9 are
# tab-delimited BED9; columns 10-18 are the modkit-specific counts, space-
# delimited -- a documented modkit quirk. `line.split()` (whitespace-generic)
# handles both correctly here because no field in this fixed 18-column shape
# itself contains whitespace.
_BEDMETHYL_FIELDS = (
    "chrom",
    "start",
    "end",
    "modified_base_code",
    "score",
    "strand",
    "thick_start",
    "thick_end",
    "color",
    "n_valid_cov",
    "fraction_modified",
    "n_mod",
    "n_canonical",
    "n_other_mod",
    "n_delete",
    "n_fail",
    "n_diff",
    "n_nocall",
)

_INT_FIELDS = frozenset(
    {
        "start",
        "end",
        "score",
        "thick_start",
        "thick_end",
        "n_valid_cov",
        "n_mod",
        "n_canonical",
        "n_other_mod",
        "n_delete",
        "n_fail",
        "n_diff",
        "n_nocall",
    }
)


@dataclass(frozen=True)
class BedMethylRecord:
    chrom: str
    start: int
    end: int
    modified_base_code: str
    score: int
    strand: str
    thick_start: int
    thick_end: int
    color: str
    n_valid_cov: int
    fraction_modified: float
    n_mod: int
    n_canonical: int
    n_other_mod: int
    n_delete: int
    n_fail: int
    n_diff: int
    n_nocall: int


def parse_bedmethyl(path: str | Path) -> list[BedMethylRecord]:
    """Parse a modkit bedMethyl file into `BedMethylRecord`s.

    A missing or unreadable file returns an empty list rather than raising:
    the handler is what decides whether zero rows is a failure (K3), and
    raising here would replace that decision with a less specific one.
    """
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeDecodeError) as e:
        log.warning("bedmethyl_unreadable", path=str(path), error=str(e))
        return []

    records: list[BedMethylRecord] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) < len(_BEDMETHYL_FIELDS):
            log.warning("bedmethyl_bad_row", path=str(path), row=line)
            continue
        raw = dict(zip(_BEDMETHYL_FIELDS, fields, strict=False))
        try:
            values = {
                key: (int(val) if key in _INT_FIELDS else val)
                for key, val in raw.items()
                if key != "fraction_modified"
            }
            values["fraction_modified"] = float(raw["fraction_modified"])
        except ValueError:
            log.warning("bedmethyl_bad_row", path=str(path), row=line)
            continue
        records.append(BedMethylRecord(**values))
    return records


def summarize(records: list[BedMethylRecord]) -> dict:
    """Reduce parsed bedMethyl rows to the `methylation_*` facts merged onto
    the BAM.

    Mean methylation percentage is coverage-weighted -- sum(N_mod) /
    sum(N_valid_cov) across every row -- rather than an unweighted mean of
    each row's own fraction_modified, so a handful of deeply-covered sites
    are not swamped by many shallow ones (or vice versa). Per-modification-
    code breakdown groups by column 4 (e.g. "m"=5mC, "h"=5hmC, "a"=6mA --
    whichever the tags contained), each with its own site count and
    coverage-weighted mean.

    Returns `{}` for no records: the caller (K3) is what turns that into a
    job failure, and an empty dict here keeps that decision in one place
    rather than duplicating the "no rows" check.
    """
    if not records:
        return {}

    total_valid_cov = sum(r.n_valid_cov for r in records)
    total_mod = sum(r.n_mod for r in records)
    mean_pct = (
        round(100.0 * total_mod / total_valid_cov, 2) if total_valid_cov else 0.0
    )

    by_code: dict[str, dict] = {}
    for record in records:
        bucket = by_code.setdefault(
            record.modified_base_code, {"sites": 0, "valid_cov": 0, "mod": 0}
        )
        bucket["sites"] += 1
        bucket["valid_cov"] += record.n_valid_cov
        bucket["mod"] += record.n_mod

    codes: dict[str, dict] = {}
    for code, bucket in by_code.items():
        codes[code] = {
            "sites": bucket["sites"],
            "mean_pct": (
                round(100.0 * bucket["mod"] / bucket["valid_cov"], 2)
                if bucket["valid_cov"]
                else 0.0
            ),
        }

    return {
        "methylation_site_count": len(records),
        "methylation_mean_pct": mean_pct,
        "methylation_by_code": codes,
    }
