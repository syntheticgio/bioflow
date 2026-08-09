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

from app.models import DataObject, ObjectRole, normalize_organism
from app.services import pipeline_service


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
    # Keyed to the strain, not the bare species. `normalize_organism`'s own
    # docstring gives the same reason: "Escherichia coli K-12 is not
    # Escherichia coli O157:H7" -- GC genuinely varies by E. coli strain, and
    # this table would otherwise attribute one strain's figure to any project
    # whose organism metadata just says "Escherichia coli". A bare-species
    # entry would be a plausible-looking but unearned hit.
    "escherichia coli k-12": GenomeGc(
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


def from_references(objects) -> ExpectedGc | None:
    """Tier 1: GC measured from a reference genome in the same project.

    Better than the table when it applies -- it is the user's actual assembly
    rather than a published figure for some assembly of the species.

    Two exclusions, both learned the same way (see CLAUDE.md on the Actions
    tab's suggestion rules, which shipped green tests while getting both
    wrong against a real project):

    * A protein or transcript FASTA is the same FormatKind as a genome and has
      a GC content that means something entirely different. `role` is what
      separates them; `COMPLETENESS_EXCLUDED_ROLES` is the same set the
      completeness card already excludes for the same reason.
    * Two references that disagree are not resolvable by picking one, so this
      answers with nothing. Two copies of one assembly agree on their value
      and are therefore one answer, not two.
    """
    usable = [
        obj
        for obj in objects
        if obj.role is not None
        and obj.role not in pipeline_service.COMPLETENESS_EXCLUDED_ROLES
        and isinstance(obj.facts.get("gc_content_percent"), (int, float))
    ]
    if not usable:
        return None

    values = {obj.facts["gc_content_percent"] for obj in usable}
    if len(values) > 1:
        return None

    chosen = usable[0]
    percent = float(chosen.facts["gc_content_percent"])
    return ExpectedGc(
        percent=percent,
        source="reference",
        attribution=f"expected {percent}%, measured from {chosen.name}",
    )


def resolve(*, references, organism: str | None) -> ExpectedGc | None:
    """The full cascade, in trust order. None means the chart draws no curve.

    `references` should already be narrowed to the project's reference-role
    objects by the caller; `from_references` re-checks the role rather than
    trusting that, because the check is one line and the failure it prevents
    is a confidently wrong curve.
    """
    return from_references(references) or from_organism(organism)


async def references_for_project(*, project_id, owner) -> list:
    """Reference-role objects in one project, for `resolve` to measure.

    Scoped by owner as well as project: this runs on a detail endpoint whose
    authorization is the object fetch, and a query that ignored owner would
    read another profile's files to answer a question about this one.
    """
    return await DataObject.find(
        DataObject.owner == owner,
        DataObject.project_id == project_id,
        DataObject.role == ObjectRole.REFERENCE,
    ).to_list()
