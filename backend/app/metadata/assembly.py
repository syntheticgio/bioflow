"""Assembly accession detection and NCBI Datasets lookup.

Reference genomes downloaded from NCBI carry the assembly accession in the
filename (`GCF_000002445.2_ASM244v1_genomic.fna`), and NCBI knows far more about
that assembly than anyone will retype: organism, strain, assembly name,
submitter, release date. Recognizing the accession and filling those in is most
of the value.

The NCBI Datasets API is used rather than E-utilities (which `sra.py` uses):
Datasets is the genome-oriented service and returns the whole assembly report in
one request, while E-utilities does not cover assemblies as directly.

Everything here is best-effort, exactly as in `sra.py`. A network failure, a
rate limit, or a retired accession must never fail an ingest -- the file is
still a perfectly good file.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.logging import get_logger
from app.metadata.sra import _get  # same throttling, retry and never-raise rules

log = get_logger(__name__)

DATASETS = "https://api.ncbi.nlm.nih.gov/datasets/v2alpha"

# GCA (GenBank) or GCF (RefSeq), nine digits, dot, version. Anchored at a word
# boundary so `MYGCA_000000001.1` does not match but a path separator or an
# underscore-joined filename does.
_ACCESSION_RE = re.compile(r"(?:^|[^A-Za-z0-9])(GC[AF]_\d{9}\.\d+)", re.IGNORECASE)


@dataclass
class AssemblyMetadata:
    """Normalized subset of an NCBI assembly report."""

    accession: str | None = None
    organism: str | None = None
    tax_id: int | None = None
    strain: str | None = None
    assembly_name: str | None = None
    assembly_level: str | None = None
    submitter: str | None = None
    release_date: str | None = None
    bioproject: str | None = None
    paired_accession: str | None = None
    # Statistics describe the *published* assembly, not the file on disk.
    total_length: int | None = None
    # Both counts are kept, but they answer different questions: a FASTA's
    # records are scaffolds, so scaffold_count is the one comparable to our
    # parser's sequence count. For GCF_000002445.2 they are 12 and 50.
    scaffold_count: int | None = None
    contig_count: int | None = None
    gc_percent: float | None = None
    scaffold_n50: int | None = None

    def to_metadata(self) -> dict:
        """Identity fields, mapped onto our schema's names.

        Only what a person might search on or correct. Statistics go to facts
        instead -- nobody hand-edits a contig count.
        """
        out: dict = {}
        if self.accession:
            out["assembly_accession"] = self.accession
        if self.organism:
            out["organism"] = self.organism
        if self.strain:
            out["strain"] = self.strain
        if self.assembly_name:
            out["reference_build"] = self.assembly_name
        if self.submitter:
            out["source"] = self.submitter
        if self.bioproject:
            out["bioproject"] = self.bioproject
        if self.tax_id:
            out["tax_id"] = self.tax_id
        if self.assembly_level:
            out["assembly_level"] = self.assembly_level
        if self.release_date:
            out["assembly_date"] = self.release_date
        if self.paired_accession:
            out["paired_accession"] = self.paired_accession
        return out

    def to_facts(self) -> dict:
        """Published-assembly statistics, namespaced so they never read as ours.

        The parser's own numbers describe the file on disk; these describe what
        NCBI published. They can legitimately differ, which is exactly why both
        are kept.
        """
        out: dict = {}
        if self.total_length is not None:
            out["ncbi_total_length"] = self.total_length
        # The comparable number: FASTA records are scaffolds.
        if self.scaffold_count is not None:
            out["ncbi_sequence_count"] = self.scaffold_count
        if self.contig_count is not None:
            out["ncbi_contig_count"] = self.contig_count
        if self.gc_percent is not None:
            out["ncbi_gc_percent"] = self.gc_percent
        if self.scaffold_n50 is not None:
            out["ncbi_scaffold_n50"] = self.scaffold_n50
        if self.assembly_name:
            out["ncbi_assembly_name"] = self.assembly_name
        # Also duplicated into facts (beyond to_metadata) so the QC tab's
        # divergence warning can link to the published assembly without
        # threading obj.metadata through AssemblyFacts.
        if self.accession:
            out["ncbi_assembly_accession"] = self.accession
        if out:
            out["ncbi_fetched_at"] = datetime.now(UTC).isoformat()
        return out


def parse_accession(filename: str) -> str | None:
    """Extract an assembly accession from a filename, or None."""
    if not filename:
        return None
    match = _ACCESSION_RE.search(filename)
    return match.group(1).upper() if match else None


def is_valid_accession(accession: str) -> bool:
    if not accession:
        return False
    return bool(re.fullmatch(r"GC[AF]_\d{9}\.\d+", accession.strip().upper()))


def _int(value) -> int | None:
    """NCBI returns some numeric stats as strings; coerce without raising."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _text(value) -> str | None:
    """A string, or None for anything else.

    The `_obj` guards keep a wrong-typed *container* from raising, but a leaf
    that arrives as a dict or list would otherwise be written straight into
    user-editable metadata -- a dict in a text field nobody can correct by hand.
    """
    return value if isinstance(value, str) and value.strip() else None


def _obj(value) -> dict:
    """A mapping, or an empty one.

    `x or {}` only substitutes for *falsy* values; a truthy value of the wrong
    type -- a non-empty list, a non-empty string -- passes straight through and
    raises on the next `.get()`. That is exactly what an NCBI schema change, a
    proxy's JSON error envelope, or a CDN interstitial looks like, and none of
    those may fail an ingest.
    """
    return value if isinstance(value, dict) else {}


def parse_report(payload: dict) -> AssemblyMetadata | None:
    """Normalize a Datasets `dataset_report` response.

    Every field is optional: NCBI omits plenty for older or sparse assemblies,
    and neither a missing field nor a wrong-typed one may raise.
    """
    reports = _obj(payload).get("reports")
    # A dict here would make `reports[0]` silently return a key, not a record.
    if not isinstance(reports, list) or not reports:
        return None
    r = _obj(reports[0])
    if not r:
        return None

    organism = _obj(r.get("organism"))
    info = _obj(r.get("assembly_info"))
    stats = _obj(r.get("assembly_stats"))
    infra = _obj(organism.get("infraspecific_names"))

    return AssemblyMetadata(
        # Every text field goes through _text: these land in user-editable
        # metadata, where a stray dict or list would be uncorrectable by hand.
        accession=_text(r.get("current_accession")) or _text(r.get("accession")),
        organism=_text(organism.get("organism_name")),
        tax_id=_int(organism.get("tax_id")),
        strain=_text(infra.get("strain")),
        assembly_name=_text(info.get("assembly_name")),
        assembly_level=_text(info.get("assembly_level")),
        submitter=_text(info.get("submitter")),
        release_date=_text(info.get("release_date")),
        bioproject=_text(info.get("bioproject_accession")),
        paired_accession=_text(r.get("paired_accession")),
        total_length=_int(stats.get("total_sequence_length")),
        scaffold_count=_int(stats.get("number_of_scaffolds")),
        contig_count=_int(stats.get("number_of_contigs")),
        gc_percent=_float(stats.get("gc_percent")),
        scaffold_n50=_int(stats.get("scaffold_n50")),
    )


def lookup(accession: str) -> AssemblyMetadata | None:
    """Fetch and normalize an assembly record, or None.

    Falls back to the unversioned accession when a specific version is not
    found: filenames frequently carry a superseded version, and the current
    record is far more useful than nothing.
    """
    if not is_valid_accession(accession):
        return None
    accession = accession.strip().upper()

    for candidate in (accession, accession.split(".")[0]):
        body = _get(f"{DATASETS}/genome/accession/{candidate}/dataset_report")
        if body is None:
            continue
        # parse_report guards the shapes it knows about, but it must not be the
        # single point where the never-raises promise can break.
        try:
            payload = json.loads(body)
            meta = parse_report(payload)
        except (ValueError, TypeError) as e:
            log.warning("assembly_parse_failed", accession=candidate, error=str(e))
            continue
        except Exception as e:  # noqa: BLE001 - a lookup must never fail an ingest
            log.warning("assembly_parse_error", accession=candidate, error=str(e))
            continue
        if meta is not None:
            if candidate != accession:
                log.info(
                    "assembly_version_fallback",
                    requested=accession,
                    resolved=meta.accession,
                )
            return meta
    return None
