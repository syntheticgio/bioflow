"""Metadata enrichment from external sources.

The governing rule: **enrichment never overwrites what a person entered.**
Public records contain mistakes, and a user correcting one must not have that
correction silently reverted every time the file is re-ingested. Fields that
are already set are left alone and any disagreement is reported as a conflict,
so the user can decide rather than being overruled.
"""

from dataclasses import dataclass, field

from app.logging import get_logger
from app.metadata import assembly, sra
from app.models import FormatKind

log = get_logger(__name__)

# Only sequence reads carry SRA accessions. Looking up a BAM or VCF whose name
# happens to contain SRR-like text would be noise.
SRA_ELIGIBLE_FORMATS = {FormatKind.FASTQ, FormatKind.FASTA}


@dataclass
class EnrichmentResult:
    """What enrichment found, and what it deliberately did not touch."""

    accession: str | None = None
    source: str | None = None  # "metadata" | "filename"
    # Fields safe to apply: not already set by the user.
    values: dict = field(default_factory=dict)
    # Where the archive disagrees with an existing value. Surfaced, not applied.
    conflicts: list[dict] = field(default_factory=list)
    # Fields left alone because they already had the same value.
    unchanged: list[str] = field(default_factory=list)
    # Measurements from the source, kept out of user-editable metadata.
    facts: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "accession": self.accession,
            "source": self.source,
            "values": self.values,
            "conflicts": self.conflicts,
            "unchanged": self.unchanged,
            "facts": self.facts,
            "error": self.error,
        }


def resolve_accession(
    existing_metadata: dict, filename: str
) -> tuple[str | None, str | None]:
    """Decide which accession to look up, and where it came from.

    Explicit metadata beats the filename. That ordering is the whole point of
    the manual field: when a name is missing or misparsed, typing the accession
    and re-ingesting must win.
    """
    for key in ("sra_run", "sra_experiment"):
        value = existing_metadata.get(key)
        if value and sra.is_valid_accession(str(value)):
            return str(value).strip().upper(), "metadata"

    from_name = sra.parse_accession(filename)
    if from_name:
        return from_name, "filename"
    return None, None


def enrich_from_sra(
    *,
    filename: str,
    existing_metadata: dict,
    format_kind: FormatKind | str | None,
    enabled: bool = True,
) -> EnrichmentResult:
    """Look up SRA metadata and return only the changes that are safe to apply.

    Never raises: enrichment is a convenience, and a network problem must not
    turn a good file into a failed ingest.
    """
    result = EnrichmentResult()
    if not enabled:
        return result

    if isinstance(format_kind, str):
        try:
            format_kind = FormatKind(format_kind)
        except ValueError:
            format_kind = None
    if format_kind not in SRA_ELIGIBLE_FORMATS:
        return result

    accession, source = resolve_accession(existing_metadata, filename)
    if not accession:
        return result

    result.accession = accession
    result.source = source

    try:
        meta = sra.lookup(accession)
    except Exception as e:  # noqa: BLE001 - enrichment must never break ingest
        log.warning("sra_lookup_failed", accession=accession, error=str(e))
        result.error = f"SRA lookup failed: {e}"
        return result

    if meta is None:
        result.error = f"No SRA record found for {accession}"
        return result

    candidate = meta.to_metadata()

    for key, value in candidate.items():
        if value in (None, ""):
            continue
        current = existing_metadata.get(key)

        if current in (None, ""):
            result.values[key] = value
        elif str(current).strip() == str(value).strip():
            result.unchanged.append(key)
        else:
            # The user's value stands. We only report the difference.
            result.conflicts.append(
                {"key": key, "yours": current, "sra": value}
            )

    if result.conflicts:
        log.info(
            "sra_conflicts",
            accession=accession,
            count=len(result.conflicts),
            keys=[c["key"] for c in result.conflicts],
        )
    return result


# Only an assembly carries an assembly accession. A FASTQ whose name happens to
# contain GCA-like text would be noise.
ASSEMBLY_ELIGIBLE_FORMATS = {FormatKind.FASTA}


def resolve_assembly_accession(
    existing_metadata: dict, filename: str
) -> tuple[str | None, str | None]:
    """Decide which assembly accession to look up, and where it came from.

    Explicit metadata beats the filename, so typing an accession and
    re-ingesting is the escape hatch when a name is missing or misparsed.
    """
    value = existing_metadata.get("assembly_accession")
    if value and assembly.is_valid_accession(str(value)):
        return str(value).strip().upper(), "metadata"

    from_name = assembly.parse_accession(filename)
    if from_name:
        return from_name, "filename"
    return None, None


def enrich_from_assembly(
    *,
    filename: str,
    existing_metadata: dict,
    format_kind: FormatKind | str | None,
    enabled: bool = True,
) -> EnrichmentResult:
    """Look up an NCBI assembly record and return only safe changes.

    Never raises, for the same reason enrich_from_sra never does: a network
    problem must not turn a good file into a failed ingest.
    """
    result = EnrichmentResult()
    if not enabled:
        return result

    if isinstance(format_kind, str):
        try:
            format_kind = FormatKind(format_kind)
        except ValueError:
            format_kind = None
    if format_kind not in ASSEMBLY_ELIGIBLE_FORMATS:
        return result

    accession, source = resolve_assembly_accession(existing_metadata, filename)
    if not accession:
        return result

    result.accession = accession
    result.source = source

    try:
        meta = assembly.lookup(accession)
    except Exception as e:  # noqa: BLE001 - enrichment must never break ingest
        log.warning("assembly_lookup_failed", accession=accession, error=str(e))
        result.error = f"Assembly lookup failed: {e}"
        return result

    if meta is None:
        result.error = f"No assembly record found for {accession}"
        return result

    result.facts = meta.to_facts()

    # Per-sequence chromosome names, so the strip can label a bar "IV" rather
    # than deriving "1136" from its accession. A second request, and a strictly
    # optional one: it must never cost the stats the lookup above already got.
    try:
        labels = assembly.lookup_sequence_names(meta.accession or accession)
    except Exception as e:  # noqa: BLE001 - enrichment must never break ingest
        log.warning("sequence_names_failed", accession=accession, error=str(e))
        labels = None
    if labels:
        result.facts["sequence_labels"] = labels

    for key, value in meta.to_metadata().items():
        if value in (None, ""):
            continue
        current = existing_metadata.get(key)
        if current in (None, ""):
            result.values[key] = value
        elif str(current).strip() == str(value).strip():
            result.unchanged.append(key)
        else:
            # The user's value stands. We only report the difference.
            result.conflicts.append({"key": key, "yours": current, "ncbi": value})

    if result.conflicts:
        log.info(
            "assembly_conflicts",
            accession=accession,
            count=len(result.conflicts),
            keys=[c["key"] for c in result.conflicts],
        )
    return result
