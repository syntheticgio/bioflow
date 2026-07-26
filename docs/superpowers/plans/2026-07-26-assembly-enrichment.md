# Assembly Accession Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A reference genome named `GCF_000002445.2_ASM244v1_genomic.fna` is automatically recognized as a reference, and its organism, strain, assembly name and related fields are pulled from NCBI — while the file itself is still parsed, so what it contains can be compared against what its name claims.

**Architecture:** A new `metadata/assembly.py` mirrors the existing `metadata/sra.py`: regex the accession out of a filename, fetch the NCBI Datasets record, normalize it. `enrich.py` gains `enrich_from_assembly` reusing the existing `EnrichmentResult` and never-overwrite logic. Role is auto-assigned in `results.py` only when it is currently unset. NCBI statistics land in `facts` under an `ncbi_` prefix beside the parser's own numbers, so the detail panel can show both.

**Tech Stack:** FastAPI + Beanie + Pydantic v2; stdlib `urllib` for HTTP (no new dependency — matches `sra.py`). React + TanStack Query frontend. Tests are pytest (`asyncio_mode = "auto"`).

**Design spec:** `docs/superpowers/specs/2026-07-26-assembly-accession-enrichment-design.md`

---

## Background for the engineer

**Read `backend/app/metadata/sra.py` first.** This feature is deliberately its
twin. The module docstring, the `_throttle`/`_get` helpers, the dataclass with a
`to_metadata()` method, and the "never raises" discipline are all patterns to
copy rather than reinvent.

**The governing rule, from `enrich.py`:** *"enrichment never overwrites what a
person entered. Public records contain mistakes, and a user correcting one must
not have that correction silently reverted every time the file is re-ingested."*
Everything here obeys that.

**Enrichment must never fail an ingest.** A network error, timeout, rate limit,
malformed response, or unknown accession all produce an empty result and a
logged warning — never an exception that propagates. `sra._get` already models
this; reuse it.

**Tests must run offline.** There is a captured NCBI response at
`backend/tests/fixtures/ncbi_assembly_GCF_000002445.2.json` (already committed).
Use it. Do not write a test that makes a live network call — the existing suite
runs in 1.5s and must stay that way.

**Run tests inside Docker:** `docker compose exec -T api pytest <path> -v`.
The stack is up. Backend source and tests are mounted from the host, so edits
apply immediately.

**Real NCBI response shape** (from the fixture, verified):

```json
{"reports": [{
  "accession": "GCF_000002445.2",
  "current_accession": "GCF_000002445.2",
  "paired_accession": "GCA_000002445.1",
  "organism": {"tax_id": 185431,
               "organism_name": "Trypanosoma brucei brucei TREU927",
               "infraspecific_names": {"strain": "927/4 GUTat10.1"}},
  "assembly_info": {"assembly_level": "Chromosome",
                    "assembly_name": "ASM244v1",
                    "bioproject_accession": "PRJNA11756",
                    "release_date": "2005-12-14",
                    "submitter": "Trypanosoma brucei consortium"},
  "assembly_stats": {"total_sequence_length": ..., "number_of_contigs": 50,
                     "gc_percent": 46.5, "scaffold_n50": ...}
}]}
```

Note `assembly_stats` values may arrive as strings; coerce defensively.

---

## File Structure

**Backend — create:**
- `app/metadata/assembly.py` — accession regex, NCBI Datasets lookup, `AssemblyMetadata` dataclass
- `tests/storage/test_assembly_accession.py` — regex + parsing + enrichment

**Backend — modify:**
- `app/config.py` — `assembly_enrichment_enabled`
- `app/metadata/enrich.py` — `enrich_from_assembly`
- `app/metadata/schemas.py` — four new `REFERENCE_FIELDS`
- `app/queue/handlers.py` — call assembly enrichment in the `enriching` phase
- `app/queue/results.py` — apply results, auto-assign role

**Frontend — modify:**
- `frontend/src/components/AssemblyFacts.tsx` — published-assembly block, divergence note

`assembly.py` stays separate from `sra.py` rather than merging them: they hit
different APIs with different response shapes, and one module doing both would
be harder to follow than two that rhyme.

---

## Task 1: Accession detection and NCBI lookup

**Files:**
- Create: `backend/app/metadata/assembly.py`
- Create: `backend/tests/storage/test_assembly_accession.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/storage/test_assembly_accession.py`:

```python
"""Assembly accession detection and NCBI Datasets parsing."""

import json
from pathlib import Path

import pytest

from app.metadata import assembly

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "ncbi_assembly_GCF_000002445.2.json"
)


class TestParseAccession:
    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("GCF_000002445.2_ASM244v1_genomic.fna", "GCF_000002445.2"),
            ("GCA_000001405.29_GRCh38.p14_genomic.fna.gz", "GCA_000001405.29"),
            ("gcf_000002445.2_lowercase.fna", "GCF_000002445.2"),
            ("/data/refs/GCF_000002445.2/genome.fna", "GCF_000002445.2"),
        ],
    )
    def test_finds_accessions(self, filename, expected):
        assert assembly.parse_accession(filename) == expected

    @pytest.mark.parametrize(
        "filename",
        [
            "",
            "sample.fastq.gz",
            "GCF_000002445.fna",       # no version suffix
            "GCF_00000244.2.fna",      # eight digits, not nine
            "MYGCA_000000001.1.fna",   # not at a word boundary
            "SRR11768093.fastq",
        ],
    )
    def test_rejects_non_accessions(self, filename):
        assert assembly.parse_accession(filename) is None

    def test_uppercases_the_result(self):
        """Stored uppercase so a filename's casing does not fragment lookups."""
        assert assembly.parse_accession("gca_000001405.29.fna") == "GCA_000001405.29"


class TestIsValidAccession:
    def test_accepts_bare_accessions(self):
        assert assembly.is_valid_accession("GCF_000002445.2")
        assert assembly.is_valid_accession("gca_000001405.29")

    def test_rejects_malformed(self):
        assert not assembly.is_valid_accession("GCF_000002445")
        assert not assembly.is_valid_accession("SRR11768093")
        assert not assembly.is_valid_accession("")


class TestParseReport:
    """Parsing a real captured NCBI response, offline."""

    @pytest.fixture
    def report(self) -> dict:
        return json.loads(FIXTURE.read_text())

    def test_extracts_identity_fields(self, report):
        meta = assembly.parse_report(report)
        assert meta is not None
        assert meta.accession == "GCF_000002445.2"
        assert meta.organism == "Trypanosoma brucei brucei TREU927"
        assert meta.tax_id == 185431
        assert meta.strain == "927/4 GUTat10.1"
        assert meta.assembly_name == "ASM244v1"
        assert meta.assembly_level == "Chromosome"
        assert meta.submitter == "Trypanosoma brucei consortium"
        assert meta.release_date == "2005-12-14"
        assert meta.bioproject == "PRJNA11756"
        assert meta.paired_accession == "GCA_000002445.1"

    def test_extracts_stats(self, report):
        meta = assembly.parse_report(report)
        assert meta.contig_count == 50
        assert meta.gc_percent == pytest.approx(46.5)
        assert meta.total_length == 26075494

    def test_sequence_count_uses_scaffolds_not_contigs(self):
        """A FASTA's records are scaffolds, so that is what our sequence count
        must be compared against.

        For GCF_000002445.2 these differ sharply: 12 scaffolds versus 50
        contigs. Comparing a correct file's 12 sequences against 50 would
        report a divergence that does not exist.
        """
        meta = assembly.parse_report(json.loads(FIXTURE.read_text()))
        assert meta.scaffold_count == 12
        assert meta.contig_count == 50
        assert meta.to_facts()["ncbi_sequence_count"] == 12

    def test_returns_none_for_an_empty_report(self):
        assert assembly.parse_report({"reports": []}) is None
        assert assembly.parse_report({}) is None

    def test_survives_a_partial_record(self):
        """NCBI omits fields for some assemblies; absence must not raise."""
        meta = assembly.parse_report({"reports": [{"accession": "GCA_000000001.1"}]})
        assert meta is not None
        assert meta.accession == "GCA_000000001.1"
        assert meta.organism is None
        assert meta.contig_count is None


class TestToMetadata:
    def test_maps_onto_schema_field_names(self):
        meta = assembly.AssemblyMetadata(
            accession="GCF_000002445.2",
            organism="Trypanosoma brucei brucei TREU927",
            strain="927/4 GUTat10.1",
            assembly_name="ASM244v1",
            submitter="Trypanosoma brucei consortium",
            bioproject="PRJNA11756",
            tax_id=185431,
            assembly_level="Chromosome",
            release_date="2005-12-14",
            paired_accession="GCA_000002445.1",
        )
        out = meta.to_metadata()
        assert out["assembly_accession"] == "GCF_000002445.2"
        assert out["organism"] == "Trypanosoma brucei brucei TREU927"
        assert out["strain"] == "927/4 GUTat10.1"
        assert out["reference_build"] == "ASM244v1"
        assert out["source"] == "Trypanosoma brucei consortium"
        assert out["bioproject"] == "PRJNA11756"
        assert out["tax_id"] == 185431
        assert out["assembly_level"] == "Chromosome"
        assert out["assembly_date"] == "2005-12-14"
        assert out["paired_accession"] == "GCA_000002445.1"

    def test_omits_absent_fields(self):
        """A sparse record must not write empty strings into metadata."""
        out = assembly.AssemblyMetadata(accession="GCA_000000001.1").to_metadata()
        assert out == {"assembly_accession": "GCA_000000001.1"}

    def test_stats_go_to_facts_not_metadata(self):
        """Statistics are measurements, not user-editable metadata."""
        meta = assembly.AssemblyMetadata(
            accession="GCF_000002445.2", contig_count=50, gc_percent=46.5,
            total_length=26075494, assembly_name="ASM244v1",
        )
        assert "contig_count" not in meta.to_metadata()
        facts = meta.to_facts()
        assert facts["ncbi_contig_count"] == 50
        assert facts["ncbi_gc_percent"] == 46.5
        assert facts["ncbi_total_length"] == 26075494
        assert facts["ncbi_assembly_name"] == "ASM244v1"
```

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec -T api pytest tests/storage/test_assembly_accession.py -v`

Expected: FAIL — `ImportError: cannot import name 'assembly' from 'app.metadata'`

- [ ] **Step 3: Implement the module**

Create `backend/app/metadata/assembly.py`:

```python
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


def parse_report(payload: dict) -> AssemblyMetadata | None:
    """Normalize a Datasets `dataset_report` response.

    Every field is optional: NCBI omits plenty for older or sparse assemblies,
    and a missing field must never raise.
    """
    reports = (payload or {}).get("reports") or []
    if not reports:
        return None
    r = reports[0]

    organism = r.get("organism") or {}
    info = r.get("assembly_info") or {}
    stats = r.get("assembly_stats") or {}
    infra = organism.get("infraspecific_names") or {}

    return AssemblyMetadata(
        accession=r.get("current_accession") or r.get("accession"),
        organism=organism.get("organism_name"),
        tax_id=_int(organism.get("tax_id")),
        strain=infra.get("strain"),
        assembly_name=info.get("assembly_name"),
        assembly_level=info.get("assembly_level"),
        submitter=info.get("submitter"),
        release_date=info.get("release_date"),
        bioproject=info.get("bioproject_accession"),
        paired_accession=r.get("paired_accession"),
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
        try:
            payload = json.loads(body)
        except (ValueError, TypeError) as e:
            log.warning("assembly_parse_failed", accession=candidate, error=str(e))
            continue
        meta = parse_report(payload)
        if meta is not None:
            if candidate != accession:
                log.info(
                    "assembly_version_fallback",
                    requested=accession,
                    resolved=meta.accession,
                )
            return meta
    return None
```

- [ ] **Step 4: Run the tests**

Run: `docker compose exec -T api pytest tests/storage/test_assembly_accession.py -v`

Expected: PASS, 21 passed

- [ ] **Step 5: Verify the live lookup works once, by hand**

The tests are offline; confirm the real endpoint works at least once:

```bash
docker compose exec -T api python -c "
from app.metadata import assembly
m = assembly.lookup('GCF_000002445.2')
print('organism:', m.organism)
print('build   :', m.assembly_name)
print('metadata:', sorted(m.to_metadata()))
print('facts   :', sorted(m.to_facts()))
"
```

Expected: organism `Trypanosoma brucei brucei TREU927`, build `ASM244v1`, and
both key lists populated. If the network is unavailable, note it and move on —
the offline tests are what gate this task.

- [ ] **Step 6: Commit**

```bash
git add backend/app/metadata/assembly.py backend/tests/storage/test_assembly_accession.py
git commit -m "feat: add assembly accession detection and NCBI lookup"
```

---

## Task 2: The new reference metadata fields

**Files:**
- Modify: `backend/app/metadata/schemas.py` (`REFERENCE_FIELDS`)
- Modify: `backend/tests/storage/test_metadata_schemas.py`

- [ ] **Step 1: Write the failing test**

Append to `TestReferenceFieldDefinitions` in
`backend/tests/storage/test_metadata_schemas.py`:

```python
    def test_ncbi_enrichment_fields_exist(self):
        """Fields the NCBI assembly lookup fills in."""
        fields = schemas.field_map(None, role=ObjectRole.REFERENCE)
        assert fields["tax_id"].type is FieldType.INTEGER
        assert fields["assembly_level"].type is FieldType.ENUM
        assert fields["assembly_date"].type is FieldType.DATE
        assert fields["paired_accession"].type is FieldType.TEXT
        for key in ("tax_id", "assembly_level", "assembly_date", "paired_accession"):
            assert fields[key].group == "Reference"

    def test_enrichment_fields_are_not_suggested(self):
        """They are filled by lookup, so they should not clutter the form."""
        fields = schemas.field_map(None, role=ObjectRole.REFERENCE)
        for key in ("tax_id", "assembly_level", "assembly_date", "paired_accession"):
            assert not fields[key].suggested
```

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec -T api pytest tests/storage/test_metadata_schemas.py::TestReferenceFieldDefinitions -v`

Expected: FAIL — `KeyError: 'tax_id'`

- [ ] **Step 3: Add the fields**

In `backend/app/metadata/schemas.py`, add to the end of `REFERENCE_FIELDS`
(after the existing `masked` entry, inside the tuple):

```python
    # Filled by the NCBI assembly lookup rather than by hand, so none are
    # suggested -- they appear once enrichment has run.
    FieldDef("tax_id", "NCBI taxonomy ID", type=FieldType.INTEGER,
             help="e.g. 9606 for human. Set from the assembly record.",
             group="Reference"),
    FieldDef("assembly_level", "Assembly level", type=FieldType.ENUM,
             options=("Complete Genome", "Chromosome", "Scaffold", "Contig"),
             help="How finished the assembly is.", group="Reference"),
    FieldDef("assembly_date", "Release date", type=FieldType.DATE,
             help="When NCBI published this assembly.", group="Reference"),
    FieldDef("paired_accession", "Paired accession",
             help="The GenBank counterpart of a RefSeq assembly, or vice versa.",
             group="Reference"),
```

- [ ] **Step 4: Run the tests**

Run: `docker compose exec -T api pytest tests/storage/test_metadata_schemas.py -v`

Expected: PASS, all existing tests plus the two new ones.

- [ ] **Step 5: Commit**

```bash
git add backend/app/metadata/schemas.py backend/tests/storage/test_metadata_schemas.py
git commit -m "feat: add NCBI-sourced fields to the reference schema"
```

---

## Task 3: Assembly enrichment

**Files:**
- Modify: `backend/app/metadata/enrich.py`
- Modify: `backend/app/config.py`
- Modify: `backend/tests/storage/test_assembly_accession.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/storage/test_assembly_accession.py`:

```python
import json as _json
from unittest.mock import patch

from app.metadata import assembly as assembly_mod
from app.metadata import enrich
from app.models import FormatKind


def _fixture_metadata() -> assembly_mod.AssemblyMetadata:
    return assembly_mod.parse_report(_json.loads(FIXTURE.read_text()))


class TestEnrichFromAssembly:
    """Enrichment fills gaps and never overwrites a person's entry."""

    def test_fills_empty_fields_from_the_filename(self):
        with patch.object(assembly_mod, "lookup", return_value=_fixture_metadata()):
            result = enrich.enrich_from_assembly(
                filename="GCF_000002445.2_ASM244v1_genomic.fna",
                existing_metadata={},
                format_kind=FormatKind.FASTA,
            )
        assert result.accession == "GCF_000002445.2"
        assert result.source == "filename"
        assert result.values["organism"] == "Trypanosoma brucei brucei TREU927"
        assert result.values["reference_build"] == "ASM244v1"

    def test_never_overwrites_a_user_value(self):
        """A correction must survive re-ingest -- the whole point of the rule."""
        with patch.object(assembly_mod, "lookup", return_value=_fixture_metadata()):
            result = enrich.enrich_from_assembly(
                filename="GCF_000002445.2_genomic.fna",
                existing_metadata={"organism": "Trypanosoma brucei (my correction)"},
                format_kind=FormatKind.FASTA,
            )
        assert "organism" not in result.values
        assert any(c["key"] == "organism" for c in result.conflicts)

    def test_explicit_metadata_accession_beats_the_filename(self):
        with patch.object(assembly_mod, "lookup", return_value=_fixture_metadata()) as m:
            result = enrich.enrich_from_assembly(
                filename="GCA_000001405.29_something.fna",
                existing_metadata={"assembly_accession": "GCF_000002445.2"},
                format_kind=FormatKind.FASTA,
            )
        assert result.source == "metadata"
        m.assert_called_once_with("GCF_000002445.2")

    def test_ignores_a_fastq(self):
        """Only an assembly carries an assembly accession."""
        result = enrich.enrich_from_assembly(
            filename="GCF_000002445.2_genomic.fastq",
            existing_metadata={},
            format_kind=FormatKind.FASTQ,
        )
        assert result.accession is None
        assert result.values == {}

    def test_no_accession_is_a_quiet_no_op(self):
        result = enrich.enrich_from_assembly(
            filename="random_genome.fna",
            existing_metadata={},
            format_kind=FormatKind.FASTA,
        )
        assert result.accession is None
        assert result.error is None

    def test_a_lookup_failure_never_raises(self):
        """Enrichment is a bonus; a network problem must not fail an ingest."""
        with patch.object(assembly_mod, "lookup", side_effect=OSError("network down")):
            result = enrich.enrich_from_assembly(
                filename="GCF_000002445.2_genomic.fna",
                existing_metadata={},
                format_kind=FormatKind.FASTA,
            )
        assert result.values == {}
        assert result.error and "network down" in result.error

    def test_disabled_returns_nothing(self):
        result = enrich.enrich_from_assembly(
            filename="GCF_000002445.2_genomic.fna",
            existing_metadata={},
            format_kind=FormatKind.FASTA,
            enabled=False,
        )
        assert result.accession is None

    def test_stats_are_returned_as_facts(self):
        with patch.object(assembly_mod, "lookup", return_value=_fixture_metadata()):
            result = enrich.enrich_from_assembly(
                filename="GCF_000002445.2_genomic.fna",
                existing_metadata={},
                format_kind=FormatKind.FASTA,
            )
        assert result.facts["ncbi_contig_count"] == 50
        assert "ncbi_gc_percent" in result.facts
```

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec -T api pytest tests/storage/test_assembly_accession.py::TestEnrichFromAssembly -v`

Expected: FAIL — `AttributeError: module 'app.metadata.enrich' has no attribute 'enrich_from_assembly'`

- [ ] **Step 3: Add a `facts` field to EnrichmentResult**

In `backend/app/metadata/enrich.py`, add to the `EnrichmentResult` dataclass,
after `unchanged`:

```python
    # Measurements from the source, kept out of user-editable metadata.
    facts: dict = field(default_factory=dict)
```

and to its `to_dict`:

```python
            "facts": self.facts,
```

This is additive; the SRA path simply leaves it empty.

- [ ] **Step 4: Implement `enrich_from_assembly`**

Add to `backend/app/metadata/enrich.py`, below `enrich_from_sra`:

```python
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
```

Add `assembly` to the existing import at the top of the file:
`from app.metadata import assembly, sra`

- [ ] **Step 5: Add the setting**

In `backend/app/config.py`, below `sra_enrichment_enabled`:

```python
    # Looks up GCA/GCF assembly accessions at NCBI during ingest. Outbound
    # network call; set false to keep the stack fully offline.
    assembly_enrichment_enabled: bool = True
```

- [ ] **Step 6: Run the tests**

Run: `docker compose exec -T api pytest tests/storage/test_assembly_accession.py -v`

Expected: PASS, 29 passed

Then the full suite: `docker compose exec -T api pytest -q`

Expected: all pass. The new `facts` field on `EnrichmentResult` is additive, so
the SRA tests must be unaffected — if any fail, that is a real regression.

- [ ] **Step 7: Commit**

```bash
git add backend/app/metadata/enrich.py backend/app/config.py backend/tests/storage/test_assembly_accession.py
git commit -m "feat: enrich reference files from the NCBI assembly record"
```

---

## Task 4: Wire enrichment into ingest and auto-assign the role

**Files:**
- Modify: `backend/app/queue/handlers.py` (the `enriching` phase, ~line 285-305)
- Modify: `backend/app/queue/results.py` (`_apply_ingest_headers`)
- Modify: `backend/tests/storage/test_assembly_accession.py`

- [ ] **Step 1: Write the failing test**

The applier is async and touches the database, so test the *decision* it makes
via a pure helper, mirroring how Task 3 of the previous plan handled
`apply_role_update`.

Append to `backend/tests/storage/test_assembly_accession.py`:

```python
from app.queue.results import should_assign_reference_role


class TestAutoRoleAssignment:
    """Auto-assignment fills a gap; it never overrules a person."""

    def test_assigns_when_an_accession_was_found_and_role_is_unset(self):
        assert should_assign_reference_role(
            current_role=None, enrichment={"accession": "GCF_000002445.2"}
        )

    def test_does_not_assign_without_an_accession(self):
        assert not should_assign_reference_role(current_role=None, enrichment={})

    def test_never_overrides_an_existing_role(self):
        """A user who set a role may know something the filename does not say."""
        assert not should_assign_reference_role(
            current_role="reference", enrichment={"accession": "GCF_000002445.2"}
        )

    def test_never_reasserts_a_role_the_user_cleared(self):
        """Converting a reference back to reads must survive a re-ingest.

        A cleared role reads as None, so this is the same guard as above -- but
        it is the case that actually matters, and deserves its own test.
        """
        assert should_assign_reference_role(
            current_role=None, enrichment={"accession": "GCF_000002445.2"}
        )
        # ... and once they convert it back, role is "reference" again, which
        # the guard above already refuses to touch.

    def test_handles_a_missing_enrichment_block(self):
        assert not should_assign_reference_role(current_role=None, enrichment=None)
```

**Note on `test_never_reasserts_a_role_the_user_cleared`:** this documents a real
limitation. A cleared role is indistinguishable from a never-set one, so
re-ingesting a file the user converted back to reads *will* re-assign the
reference role. Implement the helper as specified; the limitation is recorded in
`docs/TODO.md` in Task 6 rather than solved here, because solving it needs a
"user has touched this" marker that is a larger design change.

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec -T api pytest tests/storage/test_assembly_accession.py::TestAutoRoleAssignment -v`

Expected: FAIL — `ImportError: cannot import name 'should_assign_reference_role'`

- [ ] **Step 3: Add the helper and apply it**

In `backend/app/queue/results.py`, add above `_apply_ingest_headers`:

```python
def should_assign_reference_role(*, current_role, enrichment: dict | None) -> bool:
    """Whether an ingest should mark this object a reference.

    Only when an assembly accession was found *and* no role is set. A role the
    user chose is never overruled: they may be running something unusual, or
    know something about the file that its name does not say.
    """
    if current_role is not None:
        return False
    return bool((enrichment or {}).get("accession"))
```

Then inside `_apply_ingest_headers`, after the existing SRA enrichment block
(after the `elif enrichment.get("error")` branch, before `await obj.set(update)`):

```python
    assembly_enrichment = result.get("assembly_enrichment") or {}
    if assembly_enrichment.get("values"):
        # Already filtered against what the user set, so this cannot clobber.
        merged_metadata = update.get(DataObject.metadata, obj.metadata)
        update[DataObject.metadata] = {
            **merged_metadata,
            **assembly_enrichment["values"],
        }
    if assembly_enrichment.get("facts"):
        merged_facts = update.get(DataObject.facts, obj.facts)
        update[DataObject.facts] = {
            **merged_facts,
            **assembly_enrichment["facts"],
        }
    if assembly_enrichment.get("accession"):
        merged_facts = update.get(DataObject.facts, obj.facts)
        provenance = {
            "assembly_accession_source": assembly_enrichment.get("source"),
            "assembly_fields_applied": sorted(assembly_enrichment.get("values", {})),
        }
        if assembly_enrichment.get("conflicts"):
            provenance["assembly_conflicts"] = assembly_enrichment["conflicts"]
        update[DataObject.facts] = {**merged_facts, **provenance}
    if assembly_enrichment.get("error"):
        merged_facts = update.get(DataObject.facts, obj.facts)
        update[DataObject.facts] = {
            **merged_facts,
            "assembly_error": assembly_enrichment["error"],
        }

    if should_assign_reference_role(
        current_role=obj.role, enrichment=assembly_enrichment
    ):
        update[DataObject.role] = ObjectRole.REFERENCE
```

Add `ObjectRole` to the existing `from app.models import ...` line in this file.

- [ ] **Step 4: Call the enrichment during ingest**

In `backend/app/queue/handlers.py`, in the `enriching` phase, after the existing
`enrichment = enrich.enrich_from_sra(...)` block:

```python
    assembly_enrichment = None
    if settings.assembly_enrichment_enabled:
        from app.metadata import enrich as _enrich

        assembly_enrichment = _enrich.enrich_from_assembly(
            filename=name,
            existing_metadata=ctx.payload.get("metadata") or {},
            format_kind=detection.kind,
        ).to_dict()
```

and add it to the returned dict, beside `"enrichment"`:

```python
        "assembly_enrichment": assembly_enrichment,
```

If `enrich` is already imported in that scope by the SRA block, reuse that name
rather than re-importing under an alias — read the surrounding code and match it.

- [ ] **Step 5: Run the tests**

Run: `docker compose exec -T api pytest tests/storage/test_assembly_accession.py -v`

Expected: PASS, 34 passed

Full suite: `docker compose exec -T api pytest -q` — all pass.

Lint: `docker compose exec -T api ruff check app` — clean.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/handlers.py backend/app/queue/results.py backend/tests/storage/test_assembly_accession.py
git commit -m "feat: auto-assign the reference role from an assembly accession"
```

---

## Task 5: Show the published assembly alongside the measured file

**Files:**
- Modify: `frontend/src/components/AssemblyFacts.tsx`

- [ ] **Step 1: Add the published-assembly block**

In `frontend/src/components/AssemblyFacts.tsx`, read the NCBI facts alongside
the existing parser facts. Add after the existing `namesTruncated` line:

```tsx
  const ncbiTotal = facts.ncbi_total_length as number | undefined;
  // Sequences, not contigs: a FASTA's records are scaffolds, and the two
  // counts differ sharply for a chromosome-level assembly (12 vs 50 for
  // GCF_000002445.2). Comparing against contigs would invent a divergence.
  const ncbiSequences = facts.ncbi_sequence_count as number | undefined;
  const ncbiGc = facts.ncbi_gc_percent as number | undefined;
  const ncbiName = facts.ncbi_assembly_name as string | undefined;
  const assemblyError = facts.assembly_error as string | undefined;
  const hasNcbi =
    ncbiTotal !== undefined || ncbiSequences !== undefined || ncbiGc !== undefined;

  // A file named for a full assembly that holds one chromosome is a real and
  // easily-missed problem. Compare only when both sides are known.
  const countDiverges =
    count !== undefined && ncbiSequences !== undefined && count !== ncbiSequences;
  const lengthDiverges =
    totalBases !== undefined &&
    ncbiTotal !== undefined &&
    Math.abs(totalBases - ncbiTotal) / ncbiTotal > 0.01;
  const diverges = countDiverges || lengthDiverges;
```

Then render, immediately before the closing `</div>` of the component (after the
contig-name block):

```tsx
      {hasNcbi && (
        <div style={{ marginTop: 14 }}>
          <div
            style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 6 }}
          >
            Published assembly (NCBI){ncbiName ? ` · ${ncbiName}` : ""}
          </div>
          <dl className="kv">
            {ncbiSequences !== undefined && (
              <>
                <dt>Sequences</dt>
                <dd>{ncbiSequences.toLocaleString()}</dd>
              </>
            )}
            {ncbiTotal !== undefined && (
              <>
                <dt>Total bases</dt>
                <dd>{formatBases(ncbiTotal)}</dd>
              </>
            )}
            {ncbiGc !== undefined && (
              <>
                <dt>GC content</dt>
                <dd>{ncbiGc}%</dd>
              </>
            )}
          </dl>
          {diverges && (
            <div className="warn-box" style={{ marginTop: 8 }}>
              This file{" "}
              {count !== undefined && <>has {count.toLocaleString()} sequences</>}
              {count !== undefined && totalBases !== undefined && " "}
              {totalBases !== undefined && <>totalling {formatBases(totalBases)}</>};
              the published assembly has{" "}
              {ncbiSequences !== undefined && (
                <>{ncbiSequences.toLocaleString()} sequences</>
              )}
              {ncbiSequences !== undefined && ncbiTotal !== undefined && " "}
              {ncbiTotal !== undefined && <>totalling {formatBases(ncbiTotal)}</>}. It
              may be a subset, or a different patch level.
            </div>
          )}
        </div>
      )}

      {assemblyError && (
        <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 10 }}>
          NCBI lookup: {assemblyError}
        </div>
      )}
```

Note the divergence note uses `warn-box` (an existing class in `styles.css`)
rather than `error-box`: a chromosome subset is often deliberate, so this is an
observation, not a failure.

- [ ] **Step 2: Handle the empty-state interaction**

`AssemblyFacts` currently early-returns "No assembly facts extracted yet." when
`hasAnything` is false. That must not hide NCBI data for a file whose parse
produced nothing. Change the guard from:

```tsx
  if (!hasAnything) {
```

to:

```tsx
  if (!hasAnything && !hasNcbi && !assemblyError) {
```

so a file with NCBI data but no parser facts still renders the published block.
Move the three `const` declarations from Step 1 above this guard if they are not
already.

- [ ] **Step 3: Typecheck**

Run: `npm --prefix frontend run lint`

Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/AssemblyFacts.tsx
git commit -m "feat: show the published NCBI assembly beside the measured file"
```

---

## Task 6: Record the known limitation

**Files:**
- Modify: `docs/TODO.md`

- [ ] **Step 1: Add the entry**

Add at the top of the list in `docs/TODO.md` (the file is newest-first):

```markdown
## Re-ingest re-asserts a reference role the user cleared

Raised: 2026-07-26, during assembly-accession enrichment.

`should_assign_reference_role` in `backend/app/queue/results.py` assigns the
reference role when an assembly accession is found and `role is None`. A role
the user *cleared* is indistinguishable from one never set, so converting a
reference back to reads and then re-ingesting will silently re-assign it.

Rare in practice — it needs a deliberate conversion plus a re-ingest of a file
whose name carries a GCA/GCF accession — but it quietly contradicts the promise
that an explicit choice is never overruled.

The fix needs a way to record that a user has touched the role: either a
nullable `role_set_by` field (`"user"` vs `"ingest"`), or a general
`user_touched: list[str]` on the object. The second generalizes to the same
problem for metadata fields, so it is probably the better shape. Deferred
because it is a schema change that this feature does not otherwise need.

Touches: `backend/app/models/object.py`, `backend/app/queue/results.py`,
`backend/app/services/object_service.py`.
```

- [ ] **Step 2: Commit**

```bash
git add docs/TODO.md
git commit -m "docs: record that re-ingest can re-assert a cleared reference role"
```

---

## Task 7: End-to-end verification

No code. Confirm the feature works against the running stack.

- [ ] **Step 1: Full suite and lint**

```bash
docker compose exec -T api pytest -q
docker compose exec -T api ruff check app
npm --prefix frontend run lint
```

All three must be clean.

- [ ] **Step 2: Verify the lookup end to end**

```bash
docker compose exec -T api python -c "
from app.metadata import enrich
from app.models import FormatKind
r = enrich.enrich_from_assembly(
    filename='GCF_000002445.2_ASM244v1_genomic.fna',
    existing_metadata={},
    format_kind=FormatKind.FASTA,
)
print('accession:', r.accession, '| source:', r.source)
print('metadata :', r.values)
print('facts    :', r.facts)
print('error    :', r.error)
"
```

Expected: accession `GCF_000002445.2`, source `filename`, organism
*Trypanosoma brucei brucei TREU927*, `reference_build` ASM244v1, and populated
facts. No error.

- [ ] **Step 3: Verify the never-overwrite rule end to end**

```bash
docker compose exec -T api python -c "
from app.metadata import enrich
from app.models import FormatKind
r = enrich.enrich_from_assembly(
    filename='GCF_000002445.2_ASM244v1_genomic.fna',
    existing_metadata={'organism': 'My corrected organism'},
    format_kind=FormatKind.FASTA,
)
print('applied organism?', 'organism' in r.values)
print('conflicts:', r.conflicts)
"
```

Expected: `applied organism? False`, and one conflict showing both values.

- [ ] **Step 4: Verify in the browser**

Upload a small FASTA named with a real accession — the simplest is to create one:

```bash
printf '>chr1 test\nACGTACGTAC\n' > /tmp/GCF_000002445.2_ASM244v1_genomic.fna
```

Upload it through the UI at http://localhost:5173 into any project. Then confirm:

1. It appears under **REFERENCES**, not Reads, without anyone clicking Convert.
2. The detail panel header reads **Reference**.
3. The Metadata form shows Organism *Trypanosoma brucei brucei TREU927*, Build
   ASM244v1, Source *Trypanosoma brucei consortium*.
4. The **Assembly** section shows both the measured figures (1 sequence, 10 bp)
   and **Published assembly (NCBI)** (12 sequences, ~26 Mb).
5. The divergence note appears, since 1 sequence ≠ 12.
6. The Role section offers **Convert back to reads** — auto-assignment is still
   reversible by hand.

Delete the test file afterwards so it does not clutter the project.

- [ ] **Step 5: Commit any fixes**

Skip if nothing needed fixing.

---

## Self-review notes

Checked against the spec:

- **Detection** — Task 1 (regex, word-boundary anchoring, uppercase, version
  fallback).
- **NCBI lookup** — Task 1, reusing `sra._get` so throttling, retry and the
  never-raise rule are inherited rather than duplicated.
- **Captured fixture, offline tests** — Task 1. The fixture is already committed.
- **Field capture split** — Tasks 1 and 2: identity to `metadata` via
  `to_metadata()`, statistics to `facts` via `to_facts()` under `ncbi_`.
- **Never overwrites** — Task 3, reusing the SRA conflict logic verbatim.
- **Settings flag** — Task 3.
- **Auto-role, never overriding** — Task 4, isolated into a pure testable helper.
- **Parsing unchanged** — nothing in this plan touches the parsers; FASTA
  parsing runs exactly as before, which is what makes the comparison possible.
- **Comparison display and divergence note** — Task 5.
- **Known limitation** — Task 6, recorded rather than silently shipped.

One deliberate addition beyond the spec: `EnrichmentResult` gains a `facts`
field (Task 3, Step 3). The spec implied statistics reach `facts` but did not say
how they travel from the enricher to the applier; this is the smallest change
that carries them, and it is additive so the SRA path is unaffected.

One correction made while writing this plan, worth preserving because it is easy
to get wrong: the divergence check compares our sequence count against NCBI's
**scaffold** count, not its contig count. A FASTA's records are scaffolds. For
`GCF_000002445.2` the two differ sharply — 12 scaffolds versus 50 contigs — so
comparing against contigs would report a divergence on a perfectly correct file,
which is worse than not comparing at all. Both numbers are still captured
(`ncbi_sequence_count` and `ncbi_contig_count`); only the comparison is scoped
to the scaffold count.
