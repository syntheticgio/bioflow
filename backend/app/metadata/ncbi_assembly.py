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

# Bounded like the parser's MAX_STORED_CONTIGS: the strip draws the assembly's
# chromosomes and lists the remainder from the stored lengths, so labels past
# this window would have nothing to label.
#
# Counted in *records*, not map entries -- each record contributes two keys
# (RefSeq and GenBank), so a cap on entries labels only half as many sequences
# as it appears to. Sized for a large assembled-molecule set: wheat has 21
# chromosomes plus organelles, and this leaves room for the unplaced scaffolds
# that follow them.
MAX_STORED_LABELS = 128

# The role NCBI gives a sequence that is a chromosome (or an organelle) of the
# assembly proper, as opposed to a patch, an alt locus, or an unplaced piece.
# This is the curated "what a textbook shows" set: 25 records for GRCh38, 22
# for wheat's 91,589, 1 for E. coli.
ASSEMBLED_MOLECULE = "assembled-molecule"

# Rank among the records that are *not* chromosomes, for the overflow list.
# Sequence that is genuinely part of the assembly outranks the patch and alt
# records, which are alternate representations of sequence already drawn: with
# a bounded budget, "and N more" is more useful listing real unplaced sequence
# than 24 fix-patches of chromosome 1.
_NON_CORE_RANK = {
    "unlocalized-scaffold": 0,
    "unplaced-scaffold": 0,
    "alt-scaffold": 1,
    "fix-patch": 2,
    "novel-patch": 2,
}
_NON_CORE_RANK_DEFAULT = 3

# How much of the label budget an assembled molecule is allowed to displace.
# No real assembly has more than a few dozen -- 26 is the largest observed
# (zebrafish) -- so this is a guard against a pathological report, not a
# design limit.
MAX_ASSEMBLED_MOLECULES = 64


def _order_key(record: dict) -> tuple:
    """Sort records so an assembly's chromosomes come first, then in its order.

    `sort_order` is *the chromosome number*, not a global rank. Every scaffold,
    patch and alt locus on chromosome 1 also reports `sort_order: 1` -- GRCh38
    has 44 such records. Sorting on it alone and then truncating spends the
    whole budget inside chromosome 1, so chromosomes 2-22, X and Y get no label
    at all (issue #836).

    Role first therefore, and only then the assembly's own ordering. The
    sequence name breaks remaining ties so the result does not depend on the
    order NCBI happened to serialize the records in.
    """
    role = _text(record.get("role"))
    order = _int(record.get("sort_order"))
    core = role == ASSEMBLED_MOLECULE
    return (
        0 if core else 1,
        # Chromosomes keep the assembly's ordering; everything else is ranked
        # by how much it is worth listing (see _NON_CORE_RANK).
        0 if core else _NON_CORE_RANK.get(role or "", _NON_CORE_RANK_DEFAULT),
        # Organelles carry no sort_order. A missing one sorts last, not first:
        # a karyotype reads 1..22, X, Y, MT, and `or 0` would open with MT.
        (1, 0) if order is None else (0, order),
        _text(record.get("sequence_name")) or "",
    )


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
    # NCBI's own pick of "the" assembly for this organism -- "reference
    # genome" (at most one per species) or "representative genome" (one per
    # strain/lineage when there's no single species-wide reference). None for
    # every other assembly of the same organism.
    refseq_category: str | None = None
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


def _parse_one_report(r: dict) -> AssemblyMetadata | None:
    """Normalize one `reports[]` record from a Datasets response.

    Split out from `parse_report` so a multi-record response (search by
    taxon) can reuse the exact same per-record field extraction that a
    single-accession lookup uses -- a missing or wrong-typed field must be
    handled identically in both places.
    """
    r = _obj(r)
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
        refseq_category=_text(info.get("refseq_category")),
        total_length=_int(stats.get("total_sequence_length")),
        scaffold_count=_int(stats.get("number_of_scaffolds")),
        contig_count=_int(stats.get("number_of_contigs")),
        gc_percent=_float(stats.get("gc_percent")),
        scaffold_n50=_int(stats.get("scaffold_n50")),
    )


def parse_report(payload: dict) -> AssemblyMetadata | None:
    """Normalize a Datasets `dataset_report` response.

    Every field is optional: NCBI omits plenty for older or sparse assemblies,
    and neither a missing field nor a wrong-typed one may raise.
    """
    reports = _obj(payload).get("reports")
    # A dict here would make `reports[0]` silently return a key, not a record.
    if not isinstance(reports, list) or not reports:
        return None
    return _parse_one_report(reports[0])


def parse_report_list(payload: dict) -> list[AssemblyMetadata]:
    """Normalize every record in a multi-assembly `dataset_report` response.

    Used for a search-by-taxon response, which can hold many records where
    `parse_report` only ever looks at the first. A record that fails to parse
    is dropped rather than failing the whole page.
    """
    reports = _obj(payload).get("reports")
    if not isinstance(reports, list):
        return []
    out: list[AssemblyMetadata] = []
    for r in reports:
        meta = _parse_one_report(r)
        if meta is not None:
            out.append(meta)
    return out


def parse_sequence_reports(payload: dict) -> dict[str, str]:
    """Map every sequence accession in a report to a human-readable label.

    Both namespaces are keyed to the same label: a record carries
    `refseq_accession` *and* `genbank_accession`, so one lookup labels a GCF
    file (`NC_001133.9`) and the GCA file beside it (`BK006935.2`).

    The label depends on the record's role, and this is not cosmetic.
    Unplaced and unlocalized scaffolds inherit their parent chromosome's
    `chr_name`: the real Aspergillus assembly has two records both reporting
    `chr_name: "11"`, and the larger of them is the longest sequence in the
    file. Labelling those by `chr_name` would draw two bars reading "11".
    Only an assembled molecule may use `chr_name`; everything else uses its own
    `sequence_name`.

    Never raises. A schema change or a wrong-typed field yields a smaller map,
    or an empty one -- never a failed ingest.
    """
    reports = _obj(payload).get("reports")
    if not isinstance(reports, list):
        return {}

    labels: dict[str, str] = {}
    # Chromosomes first, then the assembly's own ordering, so a truncated map
    # keeps every chromosome rather than one chromosome's worth of scaffolds.
    # `_order_key` is shared with `parse_sequence_roles`, so the two cannot
    # disagree about what comes first.
    records = [r for r in reports if isinstance(r, dict)]
    records.sort(key=_order_key)

    # Counted in labelled records, not len(labels): each record writes both its
    # RefSeq and GenBank accession, so counting entries would halve the real
    # budget. A record that yields no label or no accession is not charged
    # against it either -- it labels nothing, so it costs nothing.
    kept = 0
    for record in records:
        if kept >= MAX_STORED_LABELS:
            break
        role = _text(record.get("role"))
        if role == "assembled-molecule":
            label = _text(record.get("chr_name")) or _text(record.get("sequence_name"))
        else:
            label = _text(record.get("sequence_name")) or _text(record.get("chr_name"))
        if not label:
            continue
        wrote = False
        for key in ("refseq_accession", "genbank_accession"):
            accession = _text(record.get(key))
            if accession:
                labels[accession] = label
                wrote = True
        if wrote:
            kept += 1

    return labels


def parse_sequence_roles(payload: dict) -> dict[str, dict]:
    """Map every sequence accession to its label, whether it is core, and its rank.

    `parse_sequence_reports` answers "what is this sequence called". This
    answers the question the chromosome strip actually asks: *which of these
    sequences are the chromosomes* -- the set a textbook figure of the species
    would show.

    That judgement is not inferred here. NCBI curates it per assembly as
    `role == "assembled-molecule"`, and it holds across the tree with no
    tuning: 25 records for GRCh38's 705, 22 for wheat's 91,589, 17 for yeast,
    8 for Drosophila, 1 for E. coli. Organelles carry the same role, so a
    mitochondrion and a chloroplast are kept -- which is right, a textbook
    shows those too.

    Non-core records are still returned. The strip lists them behind "and N
    more", and dropping them here would lose the only length-independent
    ordering they have.

    `order` is the assembly's own ordering, so the strip can draw 1..22, X, Y,
    MT rather than sorting by length -- chr11 is longer than chr10 and would
    otherwise swap places.

    Never raises, exactly like `parse_sequence_reports`: a schema change yields
    a smaller map, never a failed ingest.
    """
    reports = _obj(payload).get("reports")
    if not isinstance(reports, list):
        return {}

    records = [r for r in reports if isinstance(r, dict)]
    records.sort(key=_order_key)

    out: dict[str, dict] = {}
    core_seen = 0
    stored = 0
    for position, record in enumerate(records):
        # Records, not keys: each contributes both its RefSeq and its GenBank
        # accession, so a key-counted budget would store half as many.
        if stored >= MAX_STORED_LABELS:
            break
        role = _text(record.get("role"))
        core = role == ASSEMBLED_MOLECULE
        if core:
            # A guard against a pathological report, not a design limit; past
            # it a record is kept but no longer drawn as a chromosome.
            if core_seen >= MAX_ASSEMBLED_MOLECULES:
                core = False
            else:
                core_seen += 1
        if core:
            label = _text(record.get("chr_name")) or _text(record.get("sequence_name"))
        else:
            label = _text(record.get("sequence_name")) or _text(record.get("chr_name"))
        if not label:
            continue
        entry = {"label": label, "core": core, "order": position}
        added = False
        for key in ("refseq_accession", "genbank_accession"):
            accession = _text(record.get(key))
            if accession:
                out[accession] = entry
                added = True
        if added:
            stored += 1

    return out


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


def lookup_sequence_names(accession: str) -> dict[str, str] | None:
    """Fetch per-sequence chromosome names for an assembly, or None.

    A second Datasets call: the `dataset_report` the rest of this module uses
    carries only `total_number_of_chromosomes`, not per-sequence names.

    Best-effort in every direction, exactly like `lookup`. Returns None rather
    than an empty map so a failed lookup and a report with nothing usable in it
    look the same to the caller, and neither writes an empty fact.
    """
    if not is_valid_accession(accession):
        return None
    accession = accession.strip().upper()

    try:
        body = _get(f"{DATASETS}/genome/accession/{accession}/sequence_reports")
        if body is None:
            return None
        labels = parse_sequence_reports(json.loads(body))
    except (ValueError, TypeError) as e:
        log.warning("sequence_reports_parse_failed", accession=accession, error=str(e))
        return None
    except Exception as e:  # noqa: BLE001 - a lookup must never fail an ingest
        log.warning("sequence_reports_error", accession=accession, error=str(e))
        return None

    return labels or None


def lookup_sequence_roles(accession: str) -> dict[str, dict] | None:
    """Fetch per-sequence roles for an assembly, or None.

    Same endpoint and same never-raises contract as `lookup_sequence_names`;
    the richer parse is what the chromosome strip needs to draw only the core
    chromosomes.

    None means the lookup did not happen or produced nothing usable. That is
    deliberately distinct from an empty-but-successful answer: a draft assembly
    with no sequence promoted to chromosome is a legitimate result, and the
    strip must fall back to length ranking rather than drawing no bars.
    """
    if not is_valid_accession(accession):
        return None
    accession = accession.strip().upper()

    try:
        body = _get(f"{DATASETS}/genome/accession/{accession}/sequence_reports")
        if body is None:
            return None
        roles = parse_sequence_roles(json.loads(body))
    except (ValueError, TypeError) as e:
        log.warning("sequence_roles_parse_failed", accession=accession, error=str(e))
        return None
    except Exception as e:  # noqa: BLE001 - a lookup must never fail an ingest
        log.warning("sequence_roles_error", accession=accession, error=str(e))
        return None

    return roles or None


def component_availability(accession: str) -> list | None:
    """What this assembly offers, preferring the CLI's own preview.

    Best-effort in the same way `lookup` is: a failure here costs accurate
    checkboxes, not the ability to download a genome.
    """
    import subprocess

    from app.config import settings
    from app.metadata import ncbi_assembly_components

    if not is_valid_accession(accession):
        return None
    accession = accession.strip().upper()

    try:
        completed = subprocess.run(
            [
                settings.datasets_path,
                "download",
                "genome",
                "accession",
                accession,
                "--include",
                "genome,gff3,protein,cds",
                "--preview",
                # Without this the CLI writes an ANSI progress bar that buries
                # the JSON.
                "--no-progressbar",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("assembly_preview_failed", accession=accession, error=str(e))
        return None

    if completed.returncode == 0:
        parsed = ncbi_assembly_components.parse_preview(completed.stdout)
        if parsed is not None:
            return parsed

    log.info("assembly_preview_unusable", accession=accession, code=completed.returncode)
    return None
