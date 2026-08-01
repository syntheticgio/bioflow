"""Metadata enrichment from external sources.

The governing rule: **enrichment never overwrites what a person entered.**
Public records contain mistakes, and a user correcting one must not have that
correction silently reverted every time the file is re-ingested. Fields that
are already set are left alone and any disagreement is reported as a conflict,
so the user can decide rather than being overruled.
"""

import re
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


# --- Sequence type ----------------------------------------------------------

# Only a FASTA name carries a sequence type by convention. The `sequence_type`
# field itself is in COMMON_FIELDS and can be set by hand on anything -- it is
# only the *guessing* that is scoped here, because a BAM or FASTQ name almost
# never says, and a guess from one would be noise dressed as knowledge.
SEQUENCE_TYPE_ELIGIBLE_FORMATS = {FormatKind.FASTA}

# NCBI Datasets' compound forms, tested against the whole name before the
# single-token table below. The order is load-bearing: `cds_from_genomic.fna`
# and `rna_from_genomic.fna` both contain the token `genomic`, so matching the
# bare token first would claim them both and label a CDS file a genome -- the
# same confusion that once let a CDS FASTA be offered as an alignable
# reference.
_COMPOUND_SEQUENCE_TYPES: tuple[tuple[str, str], ...] = (
    ("cds_from_genomic", "CDS"),
    ("rna_from_genomic", "RNA"),
)

# Matched against whole `[._-]`-separated tokens, never as substrings.
# Substring matching looks simpler and is wrong: "alternative" contains "rna",
# so `alternative_contigs.fna` would come back as an RNA file.
_TOKEN_SEQUENCE_TYPES: dict[str, str] = {
    "genomic": "Genomic",
    "genome": "Genomic",
    "dna": "Genomic",
    "cds": "CDS",
    "protein": "Protein",
    "proteins": "Protein",
    "pep": "Protein",
    "aa": "Protein",
    "rna": "RNA",
    "mrna": "RNA",
    "rrna": "RNA",
    "trna": "RNA",
    "ncrna": "RNA",
    "transcript": "RNA",
    "transcripts": "RNA",
}

# Suffixes that say nothing about sequence type and would otherwise be read as
# tokens. Stripped so the last *meaningful* token is reachable in a name like
# `..._protein.faa.gz`.
_UNINFORMATIVE_SUFFIXES = frozenset(
    {"gz", "bgz", "bz2", "xz", "zip", "fa", "fna", "fasta", "faa", "frn",
     "ffn", "fas"}
)

# Extension conventions: the weakest signal, so consulted last. `.faa` is amino
# acid FASTA, `.ffn` nucleotide coding regions and `.frn` non-coding RNA by
# long-standing convention, so a name that says nothing else still resolves.
_EXTENSION_SEQUENCE_TYPES: dict[str, str] = {
    "faa": "Protein",
    "ffn": "CDS",
    "frn": "RNA",
}


def detect_sequence_type(
    *,
    filename: str,
    existing_metadata: dict | None = None,
    format_kind: FormatKind | str | None = None,
) -> str | None:
    """Guess Genomic/CDS/Protein/RNA from a reference's filename.

    Returns None rather than a guess whenever the name does not say. An absent
    tag is a question the user can answer at leisure; a wrong one is a claim
    they have to notice before they can correct it.

    Obeys the same never-overwrite rule as the enrichers above: a value the
    user has already set ends this before the name is even read.
    """
    if (existing_metadata or {}).get("sequence_type") not in (None, ""):
        return None

    if isinstance(format_kind, str):
        try:
            format_kind = FormatKind(format_kind)
        except ValueError:
            format_kind = None
    if format_kind not in SEQUENCE_TYPE_ELIGIBLE_FORMATS:
        return None

    name = (filename or "").strip().lower()
    if not name:
        return None

    for needle, value in _COMPOUND_SEQUENCE_TYPES:
        if needle in name:
            return value

    parts = [p for p in re.split(r"[._\-\s]+", name) if p]

    # Last token first: NCBI puts the component name at the end
    # (`GCF_000002445.2_ASM244v1_genomic.fna`), so a directory or assembly name
    # earlier in the string must not outrank it.
    for token in reversed(parts):
        if token in _UNINFORMATIVE_SUFFIXES:
            continue
        value = _TOKEN_SEQUENCE_TYPES.get(token)
        if value:
            return value

    for token in reversed(parts):
        value = _EXTENSION_SEQUENCE_TYPES.get(token)
        if value:
            return value

    return None
