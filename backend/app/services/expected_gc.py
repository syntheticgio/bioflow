"""What GC content a read file should be expected to show, and who says so.

Read files are not graded on GC and this does not change that -- see
`readQuality.ts`, which has always refused to demote on GC because expected GC
is a property of the organism and an unusual value is not evidence of a
problem without knowing the source. This module supplies the missing half of
that sentence: when the source *is* known, the QC chart can draw what to
expect, attributed to whatever said so.

Three tiers, in order of how much they can be trusted:

1. Measured from a reference genome in the same project (`from_project`).
   A real measurement of a real file the user has.
2. A small table of well-established genomes, each with a citation.
3. Nothing. The chart omits the curve and offers a fitted normal instead,
   labelled as a fit to the observed data rather than an expectation of it.

`OrganismBlurb` is deliberately not a source here. It is AI-generated prose --
"Nothing here is authoritative... it is page colour", by its own docstring --
and a recalled GC percentage rendered as an authoritative reference line is
exactly the fabricated value that TOOL_META's required `license`/`citation`
fields exist to prevent.
"""

from dataclasses import dataclass

from app.models import normalize_organism


@dataclass(frozen=True)
class GenomeGc:
    """One published genome's GC content, and where the figure came from."""

    percent: float
    # The assembly or database the figure is quoted from, e.g. "GRCh38".
    source_name: str
    # Enough to check the number without guessing which release was meant.
    citation: str


@dataclass(frozen=True)
class ExpectedGc:
    """A resolved expectation, ready to draw and to attribute."""

    percent: float
    # "reference" (measured from a project file) or "table" (published value).
    source: str
    # Human-readable, shown next to the curve. Always says where it came from.
    attribution: str


# Hand-maintained, and deliberately short. This is the third of the three
# registry shapes CLAUDE.md describes: its keys belong to an open vocabulary
# (any organism a user might type), so it cannot be derived from an enum and
# must not try to be exhaustive. A miss falls to tier 3, which is a correct
# outcome rather than a gap -- the wrong instinct here is to pad the table with
# half-remembered values to raise the hit rate.
#
# Keys must be `normalize_organism` output; a test enforces it, because an
# unnormalized key is silently unreachable.
#
# Every percent below was checked against the NCBI Datasets API's own
# `assembly_stats.gc_percent` field for the cited RefSeq accession
# (`GET /datasets/v2/genome/accession/{accession}/dataset_report`) on
# 2026-08-09, not recalled from memory:
#   Homo sapiens          GCF_000001405.40 (GRCh38)   -> 41
#   Mus musculus           GCF_000001635.27 (GRCm39)   -> 42
#   Escherichia coli K-12  GCF_000005845.2 (ASM584v2)  -> 51 (50.79% raw)
#   Saccharomyces cerevisiae GCF_000146045.2 (R64)      -> 38.5
#   Drosophila melanogaster GCF_000001215.4 (Release 6) -> 42
#   Caenorhabditis elegans  GCF_000002985.6 (WBcel235)  -> 35.5
#   Arabidopsis thaliana    GCF_000001735.4 (TAIR10.1)  -> 36
#   Plasmodium falciparum 3D7 GCF_000002765.6           -> 19.5
GENOME_GC: dict[str, GenomeGc] = {
    "homo sapiens": GenomeGc(
        percent=41.0,
        source_name="GRCh38",
        citation="NCBI RefSeq GCF_000001405.40 (GRCh38) assembly statistics",
    ),
    "mus musculus": GenomeGc(
        percent=42.0,
        source_name="GRCm39",
        citation="NCBI RefSeq GCF_000001635.27 (GRCm39) assembly statistics",
    ),
    "escherichia coli": GenomeGc(
        percent=50.8,
        source_name="K-12 MG1655 (ASM584v2)",
        citation="NCBI RefSeq GCF_000005845.2 (ASM584v2) assembly statistics",
    ),
    "saccharomyces cerevisiae": GenomeGc(
        percent=38.5,
        source_name="R64",
        citation="NCBI RefSeq GCF_000146045.2 (R64) assembly statistics",
    ),
    "drosophila melanogaster": GenomeGc(
        percent=42.0,
        source_name="Release 6 (BDGP6-derived)",
        citation="NCBI RefSeq GCF_000001215.4 (Release 6 plus ISO1 MT) assembly statistics",
    ),
    "caenorhabditis elegans": GenomeGc(
        percent=35.5,
        source_name="WBcel235",
        citation="NCBI RefSeq GCF_000002985.6 (WBcel235) assembly statistics",
    ),
    "arabidopsis thaliana": GenomeGc(
        percent=36.0,
        source_name="TAIR10.1",
        citation="NCBI RefSeq GCF_000001735.4 (TAIR10.1) assembly statistics",
    ),
    "plasmodium falciparum": GenomeGc(
        percent=19.5,
        source_name="3D7",
        citation="NCBI RefSeq GCF_000002765.6 (Plasmodium falciparum 3D7) assembly statistics",
    ),
}


def from_organism(organism: str | None) -> ExpectedGc | None:
    """Tier 2: a published figure for a named organism.

    Never infers the organism -- from a filename, from read content, or from
    anything else. An organism guessed and then drawn as an authoritative
    reference curve is the same fabrication risk the module docstring rejects
    `OrganismBlurb` for.
    """
    if not organism or not organism.strip():
        return None
    entry = GENOME_GC.get(normalize_organism(organism))
    if entry is None:
        return None
    return ExpectedGc(
        percent=entry.percent,
        source="table",
        attribution=(
            f"expected {entry.percent}% for {organism.strip()} "
            f"({entry.source_name})"
        ),
    )
