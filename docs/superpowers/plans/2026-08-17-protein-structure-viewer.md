# Protein Structure Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user open a protein FASTA in BioFlow, browse the proteins inside it, and see the 3D structure of the one they pick.

**Architecture:** Four layers, bottom-up. A pure header parser turns a FASTA header into a UniProt or RefSeq accession; the ingest handler records every record of a protein FASTA into a new collection; a resolver turns an accession into PDB IDs via UniProt with its own accession-keyed cache; a new Structure tab lists records and renders the pick through the iCn3D iframe the variants viewer already uses.

**Tech Stack:** Python 3.12 / FastAPI / Beanie (Mongo) on the backend; React 19 / TypeScript / Vite on the frontend. No new dependencies on either side — the 3D rendering is NCBI's iCn3D in a cross-origin iframe.

**Spec:** `docs/superpowers/specs/2026-08-17-protein-structure-viewer-design.md`

## Global Constraints

- **Never reach the network in a test.** Every UniProt test patches the transport seam (`_get`). A suite that depends on UniProt being up fails for reasons unrelated to this code. This mirrors `backend/tests/services/test_structure_lookup.py`, which states the same rule in its docstring.
- **`backend/app/storage/parsers.py` stays pure.** Its docstring commits to "All functions are synchronous and cancellable; callers run them off the loop." No task in this plan adds a database write to it.
- **`ingest_headers` is `HandlerMode.THREAD`.** Any Mongo access from it goes through `app.db.client.run_from_thread`. Never `asyncio.run()` — the Mongo client is bound to the loop `connect_to_mongo()` ran on, and a second loop makes Motor raise "attached to a different loop".
- **Record cap is 150,000** (spec R5).
- **UniProt failure is never an exception.** Every resolution failure path returns `None` and logs, matching the contract stated in `structure_lookup.py` and `uniprot.py`.
- **Run tests from the worktree with `./backend/run-worktree-tests.sh`**, never `docker compose exec api` — that command tests main's code, not this worktree's.
- **Conventional Commits**, lowercase after the colon, no trailing period. Install the hook once: `git config core.hooksPath ops/hooks`.

---

### Task 1: Parse protein FASTA headers into accessions

**Files:**
- Create: `backend/app/metadata/protein_headers.py`
- Test: `backend/tests/metadata/test_protein_headers.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ProteinRef` (frozen dataclass with `kind: RefKind` and `accession: str`), `RefKind` (StrEnum with `UNIPROT` and `REFSEQ`), and `parse_header(header: str) -> ProteinRef | None`. Task 2 stores the result; Task 5 resolves it.

This is the TDD core of the feature: pure functions, no I/O, every real header shape a test case. Implements spec R10–R13.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/metadata/test_protein_headers.py`:

```python
"""Reading a protein FASTA header.

No network, no database: this module is string parsing, and the tests are
the specification of which header shapes the viewer can resolve.

The regexes are deliberately strict, following the reasoning recorded in
`metadata/uniprot.py`: a loose accession pattern classifies the gene symbol
EGFR as an accession and returns nothing useful.
"""

import pytest
from app.metadata.protein_headers import RefKind, parse_header


@pytest.mark.parametrize(
    "header,accession",
    [
        # UniProt's own FASTA format, the shape a proteome download has.
        (">sp|P00924|ENO1_YEAST Enolase 1 OS=Saccharomyces cerevisiae", "P00924"),
        # TrEMBL uses tr| rather than sp|; both are UniProtKB accessions.
        (">tr|A0A0B7P3V8|A0A0B7P3V8_YEAST Uncharacterized protein", "A0A0B7P3V8"),
        # The leading '>' is optional -- callers may have stripped it.
        ("sp|P00924|ENO1_YEAST Enolase 1", "P00924"),
    ],
)
def test_uniprot_headers(header, accession):
    ref = parse_header(header)
    assert ref is not None
    assert ref.kind is RefKind.UNIPROT
    assert ref.accession == accession


@pytest.mark.parametrize(
    "header,accession",
    [
        # NCBI RefSeq protein, the shape `protein.faa` from a genome download has.
        (">NP_009342.1 Cdc19p [Saccharomyces cerevisiae S288C]", "NP_009342"),
        (">XP_011542244.1 pyruvate kinase isoform X1 [Homo sapiens]", "XP_011542244"),
        # WP_ is the non-redundant bacterial protein prefix.
        (">WP_000177921.1 chaperonin GroEL [Escherichia coli]", "WP_000177921"),
        # An unversioned accession is still an accession.
        (">NP_009342 Cdc19p", "NP_009342"),
    ],
)
def test_refseq_headers(header, accession):
    """The version suffix is stripped.

    UniProt's cross-reference index is keyed on the unversioned accession:
    `xref:refseq-NP_009342` matches where `xref:refseq-NP_009342.1` does not.
    """
    ref = parse_header(header)
    assert ref is not None
    assert ref.kind is RefKind.REFSEQ
    assert ref.accession == accession


@pytest.mark.parametrize(
    "header",
    [
        # Prokka/Bakta annotation output: a locus tag, not a database ID.
        ">KLLIPMDF_00023 hypothetical protein",
        # A bare gene symbol is not an accession. This is the case the strict
        # regex exists for: a loose pattern would classify EGFR as one.
        ">EGFR",
        # An assembly accession is not a protein accession.
        ">GCF_000002445.2 something",
        # Degenerate inputs are ordinary misses, not errors.
        ">",
        "",
        "   ",
    ],
)
def test_unrecognized_headers_return_none(header):
    """Naming no identifier is an ordinary outcome, not an error (R12)."""
    assert parse_header(header) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/metadata/test_protein_headers.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.metadata.protein_headers'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/metadata/protein_headers.py`:

```python
"""What a protein FASTA header names, if anything.

Pure string parsing -- no network, no database. Separate from `uniprot.py`,
which asks UniProt questions; this module only reads the text in the file.

Two header shapes resolve, because they are the two that actually appear in
files this app handles:

- UniProt's own FASTA format (`>sp|P00924|ENO1_YEAST ...`), which a proteome
  download from `uniprot_service` produces.
- NCBI RefSeq protein (`>NP_009342.1 Cdc19p [...]`), which the `protein.faa`
  component of an NCBI assembly download produces.

Everything else -- most importantly annotation-tool output such as
`>KLLIPMDF_00023 hypothetical protein` -- names no identifier. That is an
ordinary answer rather than a failure: it is where the structure-prediction
path belongs, and prediction is the follow-up this design defers.

The patterns are deliberately strict, for the reason `uniprot.is_valid_accession`
records: a loose accession pattern classifies the gene symbol EGFR as an
accession, which resolves to nothing and looks like a lookup failure rather
than a parse error.
"""

import re
from dataclasses import dataclass
from enum import StrEnum


class RefKind(StrEnum):
    UNIPROT = "uniprot"
    REFSEQ = "refseq"


@dataclass(frozen=True)
class ProteinRef:
    """One header's identifier, and which database it belongs to.

    The kind matters to the resolver: a UniProt accession is looked up
    directly, while a RefSeq one goes through UniProt's cross-reference index.
    """

    kind: RefKind
    accession: str


# UniProt's own documented accession pattern, copied from `uniprot._ACCESSION`
# rather than imported: that one anchors a whole token (`^...$`) because it
# validates a user-typed box, while this one matches inside a pipe-delimited
# header. Two anchorings of one pattern; the shared part is the pattern text.
_UNIPROT_ACCESSION = (
    r"[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2}"
)

# `>sp|P00924|ENO1_YEAST ...` and its TrEMBL `tr|` counterpart.
_UNIPROT_HEADER = re.compile(rf"^(?:sp|tr)\|({_UNIPROT_ACCESSION})\|", re.IGNORECASE)

# NP_/XP_/WP_ plus digits, with an optional version suffix that is dropped --
# UniProt's cross-reference index is keyed on the unversioned form.
_REFSEQ_HEADER = re.compile(r"^((?:NP|XP|WP)_\d+)(?:\.\d+)?\b", re.IGNORECASE)


def parse_header(header: str) -> ProteinRef | None:
    """The accession a FASTA header names, or None.

    Accepts the header with or without its leading `>`, since callers differ
    on whether they have stripped it.

    None means "this header names nothing we can resolve" and is the expected
    answer for annotation-tool output. It is not an error and must not be
    logged as one -- for a de-novo annotated proteome it is every record.
    """
    text = (header or "").strip()
    if text.startswith(">"):
        text = text[1:].strip()
    if not text:
        return None

    # Only the first whitespace-delimited token can hold the identifier; the
    # rest is free-text description, and searching it would match an accession
    # mentioned in prose.
    token = text.split()[0]

    match = _UNIPROT_HEADER.match(token)
    if match:
        return ProteinRef(kind=RefKind.UNIPROT, accession=match.group(1).upper())

    match = _REFSEQ_HEADER.match(token)
    if match:
        return ProteinRef(kind=RefKind.REFSEQ, accession=match.group(1).upper())

    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/metadata/test_protein_headers.py -q`
Expected: PASS, 13 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/metadata/protein_headers.py backend/tests/metadata/test_protein_headers.py
git commit -m "feat(metadata): read UniProt and RefSeq accessions out of protein FASTA headers"
```

---

### Task 2: The ProteinRecord model

**Files:**
- Create: `backend/app/models/protein_record.py`
- Modify: `backend/app/models/__init__.py` (add import, `ALL_MODELS` entry, and `__all__` entry)
- Test: `backend/tests/models/test_protein_record.py`

**Interfaces:**
- Consumes: `RefKind` from Task 1.
- Produces: `ProteinRecord` document with fields `object_id: PydanticObjectId`, `ordinal: int`, `identifier: str`, `description: str`, `length: int`, `byte_offset: int`, `ref_kind: RefKind | None`, `ref_accession: str | None`. Task 3 writes it; Task 4 reads it.

Implements spec R2 (fields) and the index design in the spec's Layer 1.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/models/test_protein_record.py`:

```python
"""The protein record collection's shape and its indexes.

`ALL_MODELS` is what `init_beanie` registers, so a model missing from it has
no collection and no indexes -- the failure is silent at write time, which is
why registration is asserted here rather than assumed.
"""

import pytest
from app.models import ALL_MODELS, ProteinRecord
from app.metadata.protein_headers import RefKind


def test_registered_in_all_models():
    """A model absent from ALL_MODELS never gets its indexes created."""
    assert ProteinRecord in ALL_MODELS


def test_collection_name():
    assert ProteinRecord.Settings.name == "protein_records"


def test_declares_object_ordinal_and_identifier_indexes():
    """Two indexes, because paging and search are different queries.

    `(object_id, ordinal)` orders the list and enforces uniqueness;
    `(object_id, identifier)` serves identifier search without a collection
    scan at the 150,000-record cap.
    """
    names = {ix.document["name"] for ix in ProteinRecord.Settings.indexes}
    assert "uniq_object_ordinal" in names
    assert "object_identifier" in names

    uniq = next(
        ix for ix in ProteinRecord.Settings.indexes
        if ix.document["name"] == "uniq_object_ordinal"
    )
    assert uniq.document.get("unique") is True


def test_ref_fields_are_optional():
    """A record whose header named no identifier is still a record (R12).

    This is every record of a de-novo annotated proteome, so it must be
    constructible without a reference rather than being a validation error.
    """
    record = ProteinRecord(
        object_id="507f1f77bcf86cd799439011",
        ordinal=0,
        identifier="KLLIPMDF_00023",
        description="hypothetical protein",
        length=143,
        byte_offset=0,
    )
    assert record.ref_kind is None
    assert record.ref_accession is None


def test_carries_a_parsed_reference_when_there_is_one():
    record = ProteinRecord(
        object_id="507f1f77bcf86cd799439011",
        ordinal=1,
        identifier="NP_009342.1",
        description="Cdc19p [Saccharomyces cerevisiae S288C]",
        length=500,
        byte_offset=4096,
        ref_kind=RefKind.REFSEQ,
        ref_accession="NP_009342",
    )
    assert record.ref_kind is RefKind.REFSEQ
    assert record.ref_accession == "NP_009342"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/models/test_protein_record.py -q`
Expected: FAIL — `ImportError: cannot import name 'ProteinRecord' from 'app.models'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/models/protein_record.py`:

```python
"""One record of a protein FASTA, indexed at ingest.

Its own collection rather than a field on the object, because the population
is unbounded relative to the object: a human RefSeq protein set is roughly
120,000 records, and a fact document is not where 120,000 of anything belongs.

`facts["sequence_names"]` is deliberately not replaced by this. That list is
capped at 50 (`parsers.MAX_STORED_CONTIGS`), stores only the first whitespace
token, and feeds the Quality tab, which works. This collection answers a
different question -- "which proteins are in this file, and where is each one"
-- and the description and byte offset are the parts that make it able to.

The byte offset is stored for work this design defers rather than for anything
it does today: both follow-ups (sequence-similarity resolution, structure
prediction) need the bytes of one record, and an offset recorded during a pass
the ingest already makes is what keeps that cheap later.

Nothing here is authoritative. It is derived from a file that is itself the
source of truth, so the collection can be dropped and rebuilt by re-ingesting.
"""

from beanie import PydanticObjectId
from pymongo import ASCENDING, IndexModel

from app.metadata.protein_headers import RefKind
from app.models.base import TimestampedDocument


class ProteinRecord(TimestampedDocument):
    """One `>` record: what it is called, how long it is, and where it starts."""

    object_id: PydanticObjectId
    # Position in the file, 0-based. The list's stable sort key -- a name is
    # not unique within a FASTA and cannot order the list.
    ordinal: int
    # The header's first whitespace-delimited token, matching what
    # `parsers._parse_fasta` stores in `sequence_names`.
    identifier: str
    # Everything after that token. This is the part a person picks a protein
    # by, and the part the facts document drops.
    description: str = ""
    length: int
    byte_offset: int
    # What `protein_headers.parse_header` made of the header. None is the
    # ordinary outcome for annotation-tool output, not a parse failure.
    ref_kind: RefKind | None = None
    ref_accession: str | None = None

    class Settings:
        name = "protein_records"
        indexes = [
            # Orders the list and makes re-ingest collisions impossible.
            IndexModel(
                [("object_id", ASCENDING), ("ordinal", ASCENDING)],
                unique=True,
                name="uniq_object_ordinal",
            ),
            # Serves identifier search. Description search is a scoped
            # substring match rather than a text index: the corpus is at most
            # 150,000 short strings under a key already in hand, and a text
            # index would be a third index maintained at ingest for a query
            # that never spans objects.
            IndexModel(
                [("object_id", ASCENDING), ("identifier", ASCENDING)],
                name="object_identifier",
            ),
        ]
```

Then wire it into `backend/app/models/__init__.py`. Add the import alongside the
other model imports (they are alphabetical by module — place it after
`organism`):

```python
from app.models.protein_record import ProteinRecord
```

Add `ProteinRecord` to the `ALL_MODELS` list and to `__all__`, matching the
existing alphabetical placement of `StructureLookup` and `OrganismBlurb` in
each.

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/models/test_protein_record.py -q`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/protein_record.py backend/app/models/__init__.py backend/tests/models/test_protein_record.py
git commit -m "feat(models): add a protein record collection for FASTA record listings"
```

---

### Task 3: Index records at ingest

**Files:**
- Create: `backend/app/services/protein_record_index.py`
- Modify: `backend/app/queue/handlers.py` (inside `ingest_headers`, after the assembly-enrichment block)
- Test: `backend/tests/services/test_protein_record_index.py`

**Interfaces:**
- Consumes: `ProteinRecord` (Task 2), `parse_header` / `ProteinRef` (Task 1).
- Produces: `MAX_INDEXED_RECORDS = 150_000`, `scan_records(path, compression) -> Iterator[ScannedRecord]`, and `async def index_protein_records(*, object_id, path, compression) -> IndexResult`. `IndexResult` carries `indexed: int` and `truncated: bool`. Task 4 reads the resulting documents.

Implements spec R1–R9. The scan is a generator so the 150,000 cap bounds memory, not just row count.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_protein_record_index.py`:

```python
"""Indexing a protein FASTA's records.

The scan is tested against real files on disk rather than mocked readers: the
thing most likely to be wrong is byte-offset arithmetic, and a mock that
returns lines cannot get that wrong.
"""

import pytest
import pytest_asyncio
from app.models import Compression, ProteinRecord
from app.metadata.protein_headers import RefKind
from app.services import protein_record_index as index_mod

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

FASTA = (
    ">sp|P00924|ENO1_YEAST Enolase 1 OS=Saccharomyces cerevisiae\n"
    "MAVSKVYARS\nVYDSRGNPTV\n"
    ">NP_009342.1 Cdc19p [Saccharomyces cerevisiae S288C]\n"
    "MSRLERLTSL\n"
    ">KLLIPMDF_00023 hypothetical protein\n"
    "MKKLLA\n"
)


@pytest.fixture
def fasta_file(tmp_path):
    path = tmp_path / "proteins.faa"
    path.write_text(FASTA)
    return path


def test_scan_reads_identifier_description_and_length(fasta_file):
    records = list(index_mod.scan_records(fasta_file, Compression.NONE))

    assert [r.identifier for r in records] == [
        "sp|P00924|ENO1_YEAST",
        "NP_009342.1",
        "KLLIPMDF_00023",
    ]
    # The description is everything after the first token (R3) -- the part a
    # person picks a protein by, and the part the facts document drops.
    assert records[1].description == "Cdc19p [Saccharomyces cerevisiae S288C]"
    # Length is residues, not bytes: newlines inside the sequence do not count.
    assert records[0].length == 20
    assert records[2].length == 6


def test_scan_records_byte_offsets_that_point_at_the_header(fasta_file):
    """The offset must land on the '>' so a later reader can seek to it."""
    raw = fasta_file.read_bytes()
    for record in index_mod.scan_records(fasta_file, Compression.NONE):
        assert raw[record.byte_offset : record.byte_offset + 1] == b">"


def test_scan_attaches_the_parsed_reference(fasta_file):
    records = list(index_mod.scan_records(fasta_file, Compression.NONE))

    assert records[0].ref_kind is RefKind.UNIPROT
    assert records[0].ref_accession == "P00924"
    assert records[1].ref_kind is RefKind.REFSEQ
    assert records[1].ref_accession == "NP_009342"
    # Annotation-tool output names nothing, which is an ordinary outcome.
    assert records[2].ref_kind is None


def test_scan_stops_at_the_cap(fasta_file, monkeypatch):
    """Above the cap the scan stops rather than reading on (R5)."""
    monkeypatch.setattr(index_mod, "MAX_INDEXED_RECORDS", 2)
    records = list(index_mod.scan_records(fasta_file, Compression.NONE))
    assert len(records) == 2


def test_scan_honours_an_explicit_limit(fasta_file):
    records = list(index_mod.scan_records(fasta_file, Compression.NONE, limit=1))
    assert len(records) == 1


async def test_index_writes_one_document_per_record(fasta_file):
    object_id = "507f1f77bcf86cd799439011"
    result = await index_mod.index_protein_records(
        object_id=object_id, path=fasta_file, compression=Compression.NONE
    )

    assert result.indexed == 3
    assert result.truncated is False
    stored = await ProteinRecord.find(ProteinRecord.object_id == object_id).to_list()
    assert len(stored) == 3
    assert {r.ordinal for r in stored} == {0, 1, 2}


async def test_reindexing_replaces_rather_than_appends(fasta_file):
    """R7. The handler promises idempotency; appending would break it.

    Delete-then-insert rather than upsert-per-record: a re-ingest of a file
    that has *changed* must not leave orphaned records from the old one.
    """
    object_id = "507f1f77bcf86cd799439011"
    for _ in range(2):
        await index_mod.index_protein_records(
            object_id=object_id, path=fasta_file, compression=Compression.NONE
        )

    stored = await ProteinRecord.find(ProteinRecord.object_id == object_id).to_list()
    assert len(stored) == 3


async def test_index_reports_truncation_above_the_cap(fasta_file, monkeypatch):
    monkeypatch.setattr(index_mod, "MAX_INDEXED_RECORDS", 2)
    result = await index_mod.index_protein_records(
        object_id="507f1f77bcf86cd799439011",
        path=fasta_file,
        compression=Compression.NONE,
    )
    assert result.indexed == 2
    assert result.truncated is True


async def test_a_file_exactly_at_the_cap_is_not_truncated(fasta_file, monkeypatch):
    """The boundary. A file of exactly N records is complete, and flagging it
    as truncated would warn the user that a complete list is partial.

    This is why the scan runs one past the cap: stopping at exactly the cap
    cannot distinguish the two cases.
    """
    monkeypatch.setattr(index_mod, "MAX_INDEXED_RECORDS", 3)
    result = await index_mod.index_protein_records(
        object_id="507f1f77bcf86cd799439011",
        path=fasta_file,
        compression=Compression.NONE,
    )
    assert result.indexed == 3
    assert result.truncated is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_protein_record_index.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.protein_record_index'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/protein_record_index.py`:

```python
"""Recording every record of a protein FASTA so the file can be browsed.

Lives here rather than in `storage/parsers.py` on purpose. That module's
docstring commits to being pure -- "All functions are synchronous and
cancellable; callers run them off the loop" -- and every function in it returns
a facts dict and writes nothing. A database write inside it would break a
contract the whole ingest path depends on, so the write happens in the caller
instead.

The scan is a generator. The cap bounds rows, but a list comprehension over a
120,000-record proteome would still hold every record in memory at once before
the cap could apply.

`parsers._open_text` is imported despite the underscore. It is four lines of
compression dispatch, and the alternative -- a second copy here -- is a pair
that drifts the day a compression format is added. Reading is the only thing
borrowed; nothing in this module writes through it.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from beanie import PydanticObjectId

from app.logging import get_logger
from app.metadata.protein_headers import ProteinRef, parse_header
from app.models import Compression, ProteinRecord
from app.storage.parsers import _open_text

log = get_logger(__name__)

# Above the largest realistic input -- human RefSeq is roughly 120,000 records
# -- so an ordinary proteome never trips it, while a pathological file stays
# bounded. Module level so a test can patch it down rather than writing a
# 150,000-record fixture.
MAX_INDEXED_RECORDS = 150_000

# How many documents go to Mongo in one round trip. A record is a small
# document and 150,000 single inserts would dominate the ingest.
_BATCH = 1_000


@dataclass(frozen=True)
class ScannedRecord:
    ordinal: int
    identifier: str
    description: str
    length: int
    byte_offset: int
    ref: ProteinRef | None


@dataclass(frozen=True)
class IndexResult:
    indexed: int
    truncated: bool


def scan_records(
    path: Path, compression: Compression, *, limit: int | None = None
) -> Iterator[ScannedRecord]:
    """Every record in the file, up to `limit` (default `MAX_INDEXED_RECORDS`).

    The caller passes `limit=MAX_INDEXED_RECORDS + 1` when it needs to
    distinguish "exactly at the cap" from "over it": a file with exactly
    150,000 records is complete, and reporting it as truncated would put a
    "this list is incomplete" warning on a list that is not.

    Byte offsets are counted in the *decompressed* stream, which is what a
    later reader seeking into the same stream needs. For an uncompressed file
    they are also file offsets; for a gzipped one they are not, and a caller
    that wants random access into a `.gz` has to decompress to reach them.
    That limitation is acceptable here because nothing in this ticket seeks --
    the offsets are recorded for the follow-ups.
    """
    cap = MAX_INDEXED_RECORDS if limit is None else limit
    ordinal = 0
    offset = 0
    pending: dict | None = None

    def finish(record: dict) -> ScannedRecord:
        return ScannedRecord(
            ordinal=record["ordinal"],
            identifier=record["identifier"],
            description=record["description"],
            length=record["length"],
            byte_offset=record["offset"],
            ref=record["ref"],
        )

    with _open_text(path, compression) as fh:
        for line in fh:
            line_bytes = len(line.encode("utf-8"))
            if line.startswith(">"):
                if pending is not None:
                    yield finish(pending)
                    ordinal += 1
                    if ordinal >= cap:
                        return
                text = line[1:].strip()
                parts = text.split(maxsplit=1)
                pending = {
                    "ordinal": ordinal,
                    "identifier": parts[0] if parts else "",
                    "description": parts[1] if len(parts) > 1 else "",
                    "length": 0,
                    "offset": offset,
                    "ref": parse_header(text),
                }
            elif pending is not None:
                # Residues, not bytes: the newline is not part of the sequence.
                pending["length"] += len(line.strip())
            offset += line_bytes

    if pending is not None:
        yield finish(pending)


async def index_protein_records(
    *, object_id: PydanticObjectId | str, path: Path, compression: Compression
) -> IndexResult:
    """Replace this object's records with a fresh scan of the file.

    Delete-then-insert rather than upsert-per-record. `ingest_headers` promises
    idempotency, and an append would break it -- but more than that, a
    re-ingest of a *changed* file must not leave records from the previous
    version behind, which an upsert keyed on ordinal would do whenever the new
    file has fewer records than the old one.
    """
    await ProteinRecord.find(ProteinRecord.object_id == object_id).delete()

    indexed = 0
    truncated = False
    batch: list[ProteinRecord] = []
    # One past the cap, so that reaching record 150,001 proves the file has
    # more than the cap holds. Scanning to exactly the cap cannot tell a file
    # of exactly 150,000 records -- which is complete -- from a larger one.
    for scanned in scan_records(path, compression, limit=MAX_INDEXED_RECORDS + 1):
        if scanned.ordinal >= MAX_INDEXED_RECORDS:
            truncated = True
            break
        batch.append(
            ProteinRecord(
                object_id=object_id,
                ordinal=scanned.ordinal,
                identifier=scanned.identifier,
                description=scanned.description,
                length=scanned.length,
                byte_offset=scanned.byte_offset,
                ref_kind=scanned.ref.kind if scanned.ref else None,
                ref_accession=scanned.ref.accession if scanned.ref else None,
            )
        )
        if len(batch) >= _BATCH:
            await ProteinRecord.insert_many(batch)
            indexed += len(batch)
            batch = []

    if batch:
        await ProteinRecord.insert_many(batch)
        indexed += len(batch)

    return IndexResult(indexed=indexed, truncated=truncated)
```

Then wire it into `backend/app/queue/handlers.py`. Inside `ingest_headers`,
after the assembly-enrichment block and before the function returns, add:

```python
    # Protein FASTA records: what proteins are in this file, so the Structure
    # tab can list them. Gated on format and role (R4) -- no other object
    # gains records.
    #
    # Wrapped so a failure here cannot fail the ingest (R9): facts are this
    # handler's primary product and the record list is additive. A protein
    # file that ingests today must still ingest after this change (R34).
    #
    # `run_from_thread`, not `asyncio.run`: this handler is HandlerMode.THREAD
    # and has no loop of its own, while the Mongo client is bound to the loop
    # `connect_to_mongo()` ran on. A fresh loop makes Motor raise "attached to
    # a different loop" the moment a query touches it.
    if detection.kind is FormatKind.FASTA and role is ObjectRole.PROTEIN:
        from app.db.client import run_from_thread
        from app.services import protein_record_index

        try:
            ctx.progress(phase="indexing proteins", pct=0.9)
            result = run_from_thread(
                protein_record_index.index_protein_records(
                    object_id=object_id,
                    path=path,
                    compression=detection.compression,
                )
            )
            facts["protein_records_indexed"] = result.indexed
            if result.truncated:
                facts["protein_records_truncated"] = True
        except Exception as exc:
            log.warning(
                "protein_record_index_failed", object_id=str(object_id), error=str(exc)
            )
```

Read the surrounding code before inserting: `role` may need to be read from
`ctx.payload` and `FormatKind` / `ObjectRole` imported. Match whatever the
existing enrichment blocks do rather than introducing a new way of reading the
payload.

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_protein_record_index.py -q`
Expected: PASS, 9 passed

Then confirm nothing in the ingest path regressed (R34):

Run: `./backend/run-worktree-tests.sh tests/queue/ tests/storage/ -q`
Expected: PASS, no new failures

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/protein_record_index.py backend/app/queue/handlers.py backend/tests/services/test_protein_record_index.py
git commit -m "feat(api): index a protein FASTA's records at ingest so its proteins can be listed"
```

---

### Task 4: Serve the record list

**Files:**
- Modify: `backend/app/api/v1/objects.py` (add endpoint after the `computations` route)
- Modify: `backend/app/api/v1/schemas.py` (add response models)
- Test: `backend/tests/api/test_protein_records_api.py`

**Interfaces:**
- Consumes: `ProteinRecord` (Task 2).
- Produces: `GET /objects/{object_id}/protein-records?offset=&limit=&q=` returning `{"total": int, "truncated": bool, "rows": [{"ordinal", "identifier", "description", "length", "has_reference"}]}`. Task 7 calls it.

Implements spec R6, R25, R26, R33.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_protein_records_api.py`:

```python
"""The protein record listing endpoint.

Paging and search are one endpoint because they are one question -- "which of
this file's proteins am I looking at" -- and splitting them would mean the
client picks between two routes on whether a search box is empty.
"""

import pytest
import pytest_asyncio
from app.models import ProteinRecord
from app.metadata.protein_headers import RefKind

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture
async def seeded(protein_object):
    """Twelve records, two of which are findable by description."""
    records = [
        ProteinRecord(
            object_id=protein_object.id,
            ordinal=i,
            identifier=f"NP_{100000 + i}",
            description="pyruvate kinase" if i in (3, 7) else "hypothetical protein",
            length=200 + i,
            byte_offset=i * 100,
            ref_kind=RefKind.REFSEQ,
            ref_accession=f"NP_{100000 + i}",
        )
        for i in range(12)
    ]
    await ProteinRecord.insert_many(records)
    return protein_object


async def test_lists_records_in_file_order(client, seeded):
    resp = await client.get(f"/api/v1/objects/{seeded.id}/protein-records?limit=5")
    assert resp.status_code == 200
    body = resp.json()

    assert body["total"] == 12
    assert [r["ordinal"] for r in body["rows"]] == [0, 1, 2, 3, 4]


async def test_paging_returns_the_requested_window(client, seeded):
    resp = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?offset=10&limit=5"
    )
    body = resp.json()

    # Total is the whole population, not the window -- the UI shows "11-12 of 12".
    assert body["total"] == 12
    assert [r["ordinal"] for r in body["rows"]] == [10, 11]


async def test_search_matches_identifier_and_description(client, seeded):
    """R26. One query covers both fields; a user does not know which they typed."""
    by_description = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?q=pyruvate"
    )
    assert [r["ordinal"] for r in by_description.json()["rows"]] == [3, 7]

    by_identifier = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?q=NP_100007"
    )
    assert [r["ordinal"] for r in by_identifier.json()["rows"]] == [7]


async def test_search_total_reflects_the_match_not_the_file(client, seeded):
    resp = await client.get(f"/api/v1/objects/{seeded.id}/protein-records?q=pyruvate")
    assert resp.json()["total"] == 2


async def test_search_is_case_insensitive(client, seeded):
    resp = await client.get(f"/api/v1/objects/{seeded.id}/protein-records?q=PYRUVATE")
    assert resp.json()["total"] == 2


async def test_search_treats_regex_metacharacters_literally(client, seeded):
    """A user typing `NP_100003.1` must not have the dot read as a wildcard.

    The search is implemented as a Mongo regex, so an unescaped input is both
    a wrong-results bug and a way to hand the database a pathological pattern.
    """
    resp = await client.get(
        f"/api/v1/objects/{seeded.id}/protein-records?q=hypothetical.protein"
    )
    assert resp.json()["total"] == 0


async def test_reports_no_records_for_a_file_that_has_none(client, non_protein_object):
    resp = await client.get(
        f"/api/v1/objects/{non_protein_object.id}/protein-records"
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
    assert resp.json()["rows"] == []
```

Read `backend/tests/api/` for the existing `client` fixture and the pattern
other tests use to create an object; add `protein_object` and
`non_protein_object` fixtures following whatever that pattern is rather than
inventing a new one.

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/api/test_protein_records_api.py -q`
Expected: FAIL — 404, the route does not exist

- [ ] **Step 3: Write minimal implementation**

Add to `backend/app/api/v1/schemas.py`:

```python
class ProteinRecordOut(BaseModel):
    """One row of the protein record list.

    Deliberately does not carry the byte offset: it is stored for work this
    design defers, and an offset in a response is an invitation to build a
    client that seeks with it.
    """

    ordinal: int
    identifier: str
    description: str
    length: int
    # Whether the header named an accession at all. The client shows a
    # different empty state for "names nothing we can resolve" than for
    # "resolved but has no structure", and this is what tells them apart
    # without a resolution round trip per row.
    has_reference: bool


class ProteinRecordsOut(BaseModel):
    total: int
    # The file held more records than the cap, so this list is incomplete (R6).
    truncated: bool
    rows: list[ProteinRecordOut]
```

Add to `backend/app/api/v1/objects.py`:

```python
@router.get("/{object_id}/protein-records", response_model=ProteinRecordsOut)
async def list_protein_records(
    object_id: PydanticObjectId,
    owner: OwnerDep,
    offset: int = 0,
    limit: int = 100,
    q: str | None = None,
) -> ProteinRecordsOut:
    """The proteins in a protein FASTA, paged and searchable.

    A file with no indexed records returns an empty list rather than a 404:
    the object exists, and "this file has no protein records" is an answer the
    tab renders, not an error the client branches on.
    """
    obj = await object_service.get_object(object_id, owner=owner)

    query = ProteinRecord.find(ProteinRecord.object_id == obj.id)
    if q:
        # Escaped: a user typing `NP_009342.1` must have the dot matched
        # literally rather than as a wildcard, and an unescaped box is a way
        # to hand Mongo a pathological pattern.
        pattern = re.escape(q)
        query = query.find(
            {
                "$or": [
                    {"identifier": {"$regex": pattern, "$options": "i"}},
                    {"description": {"$regex": pattern, "$options": "i"}},
                ]
            }
        )

    total = await query.count()
    rows = (
        await query.sort(ProteinRecord.ordinal).skip(offset).limit(limit).to_list()
    )

    return ProteinRecordsOut(
        total=total,
        truncated=bool(obj.facts.get("protein_records_truncated")),
        rows=[
            ProteinRecordOut(
                ordinal=r.ordinal,
                identifier=r.identifier,
                description=r.description,
                length=r.length,
                has_reference=r.ref_accession is not None,
            )
            for r in rows
        ],
    )
```

Add `import re` and the `ProteinRecord` / schema imports at the top of the
module, following its existing import grouping.

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/api/test_protein_records_api.py -q`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/objects.py backend/app/api/v1/schemas.py backend/tests/api/test_protein_records_api.py
git commit -m "feat(api): serve a paged, searchable protein record list for a protein FASTA"
```

---

### Task 5: Resolve an accession to a structure

**Files:**
- Create: `backend/app/models/protein_structure_lookup.py`
- Create: `backend/app/services/protein_structure.py`
- Modify: `backend/app/models/__init__.py` (register `ProteinStructureLookup`)
- Test: `backend/tests/services/test_protein_structure.py`

**Interfaces:**
- Consumes: `RefKind` / `ProteinRef` (Task 1).
- Produces: `StructureHit` (frozen dataclass: `accession: str`, `protein_name: str | None`, `pdb_ids: list[str]`) and `async def resolve(ref: ProteinRef) -> StructureHit | None`. Task 6 calls it.

Implements spec R14–R22.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_protein_structure.py`:

```python
"""Resolving a protein accession to its deposited structures.

Every test patches the transport. None reach the network: a suite that
silently depends on UniProt being up fails for reasons having nothing to do
with this code -- the rule `test_structure_lookup.py` states for the same
reason.

The two selection tests below encode measurements taken against the live API
on 2026-08-17, recorded in the design doc. They are the reason a selection
rule exists at all rather than "take the first result".
"""

import json

import pytest
import pytest_asyncio
from app.metadata.protein_headers import ProteinRef, RefKind
from app.models import ProteinStructureLookup
from app.services import protein_structure

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


def _entry(accession, *, pdb=(), reviewed=True, name="Enolase 1"):
    return {
        "primaryAccession": accession,
        "entryType": "UniProtKB reviewed (Swiss-Prot)"
        if reviewed
        else "UniProtKB unreviewed (TrEMBL)",
        "proteinDescription": {"recommendedName": {"fullName": {"value": name}}},
        "uniProtKBCrossReferences": [
            {"database": "PDB", "id": p} for p in pdb
        ]
        + [{"database": "STRING", "id": "ignored"}],
    }


def _patch(monkeypatch, results):
    def fake_get(url, *, timeout=None):
        fake_get.last_url = url
        return json.dumps({"results": results}).encode()

    monkeypatch.setattr(protein_structure, "_get", fake_get)
    return fake_get


async def test_uniprot_accession_resolves_to_its_pdb_ids(monkeypatch):
    _patch(monkeypatch, [_entry("P00924", pdb=["1EBG", "1EBH"])])

    hit = await protein_structure.resolve(
        ProteinRef(kind=RefKind.UNIPROT, accession="P00924")
    )

    assert hit is not None
    assert hit.accession == "P00924"
    assert hit.pdb_ids == ["1EBG", "1EBH"]
    assert hit.protein_name == "Enolase 1"


async def test_refseq_accession_queries_the_cross_reference_index(monkeypatch):
    """R15. A RefSeq ID is not a UniProt accession and cannot be looked up as one."""
    fake_get = _patch(monkeypatch, [_entry("P00924", pdb=["1EBG"])])

    await protein_structure.resolve(
        ProteinRef(kind=RefKind.REFSEQ, accession="NP_009342")
    )

    assert "xref%3Arefseq-NP_009342" in fake_get.last_url


async def test_prefers_a_reviewed_entry(monkeypatch):
    """R16. Measured: xref:refseq-NP_000537 returns three entries for TP53."""
    _patch(
        monkeypatch,
        [
            _entry("K7PPA8", pdb=[], reviewed=False, name="isoform"),
            _entry("P04637", pdb=["1A1U"], reviewed=True, name="Cellular tumor antigen p53"),
        ],
    )

    hit = await protein_structure.resolve(
        ProteinRef(kind=RefKind.REFSEQ, accession="NP_000537")
    )

    assert hit.accession == "P04637"


async def test_prefers_a_structure_bearing_entry_among_equals(monkeypatch):
    """R17. Two reviewed entries, only one with structures -- take that one."""
    _patch(
        monkeypatch,
        [
            _entry("Q00001", pdb=[], reviewed=True),
            _entry("P04637", pdb=["1A1U", "1AIE"], reviewed=True),
        ],
    )

    hit = await protein_structure.resolve(
        ProteinRef(kind=RefKind.REFSEQ, accession="NP_000537")
    )

    assert hit.accession == "P04637"


async def test_resolution_with_no_structures_is_a_hit_not_a_miss(monkeypatch):
    """R28. "Resolved but has no structure" and "did not resolve" are different
    sentences in the UI, so they must be different return values here."""
    _patch(monkeypatch, [_entry("Q00001", pdb=[])])

    hit = await protein_structure.resolve(
        ProteinRef(kind=RefKind.UNIPROT, accession="Q00001")
    )

    assert hit is not None
    assert hit.pdb_ids == []


async def test_result_is_cached_by_accession(monkeypatch):
    """R18. A second view of the same record must not re-query UniProt."""
    fake_get = _patch(monkeypatch, [_entry("P00924", pdb=["1EBG"])])
    calls = []

    def counting(url, *, timeout=None):
        calls.append(url)
        return fake_get(url, timeout=timeout)

    monkeypatch.setattr(protein_structure, "_get", counting)
    ref = ProteinRef(kind=RefKind.UNIPROT, accession="P00924")

    await protein_structure.resolve(ref)
    await protein_structure.resolve(ref)

    assert len(calls) == 1


async def test_a_miss_is_cached_too(monkeypatch):
    """R19. The no-structure case is the majority; an uncached miss means
    every view re-queries an accession that will never resolve."""
    calls = []

    def empty(url, *, timeout=None):
        calls.append(url)
        return json.dumps({"results": []}).encode()

    monkeypatch.setattr(protein_structure, "_get", empty)
    ref = ProteinRef(kind=RefKind.UNIPROT, accession="Q99999")

    assert await protein_structure.resolve(ref) is None
    assert await protein_structure.resolve(ref) is None
    assert len(calls) == 1


async def test_transport_failure_returns_none_and_does_not_raise(monkeypatch):
    """R35. A UniProt outage reports "no structure", never a 500."""

    def boom(url, *, timeout=None):
        raise TimeoutError("uniprot unreachable")

    monkeypatch.setattr(protein_structure, "_get", boom)

    assert (
        await protein_structure.resolve(
            ProteinRef(kind=RefKind.UNIPROT, accession="P00924")
        )
        is None
    )


async def test_transport_failure_is_not_cached(monkeypatch):
    """An outage must not poison the cache with a permanent miss.

    A cached failure is indistinguishable from a cached "no structure", and
    this collection has no expiry -- so caching an outage would make a
    temporary problem permanent until someone dropped the collection.
    """
    def boom(url, *, timeout=None):
        raise TimeoutError("uniprot unreachable")

    monkeypatch.setattr(protein_structure, "_get", boom)
    ref = ProteinRef(kind=RefKind.UNIPROT, accession="P00924")
    await protein_structure.resolve(ref)

    assert await ProteinStructureLookup.find_one(
        ProteinStructureLookup.accession == "P00924"
    ) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_protein_structure.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.protein_structure'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/models/protein_structure_lookup.py`:

```python
"""Cached accession-to-structure resolutions.

Deliberately *not* the `structure_lookups` collection, which serves the
variants table. That one is keyed by `(gene, taxid)` and carries a
sequence-length guard, because a gene symbol is not an identifier -- UniProt
may attach one symbol to several proteins, and the length is what tells them
apart.

An accession is an identifier. There is no ambiguity to guard against and no
residue position to guard with, so reusing that collection would mean stuffing
a non-gene key into a field called `gene` while defeating the guard that is the
whole reason the field exists.

Negative results are cached, and are the majority. Nothing here is
authoritative: it can be dropped and rebuilt, which is also how a stale entry
gets fixed, since UniProt gains structures over time and this has no expiry.
"""

from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument


class ProteinStructureLookup(TimestampedDocument):
    """What one accession resolved to."""

    # The accession as queried -- a UniProt accession or an unversioned RefSeq
    # protein ID. Unique across both kinds, which is safe because their formats
    # cannot collide.
    accession: str
    # The UniProt accession that was selected. Differs from `accession` for a
    # RefSeq query, and is surfaced to the user so a mis-resolution is visible
    # rather than silent.
    resolved_accession: str | None = None
    protein_name: str | None = None
    pdb_ids: list[str] = []

    class Settings:
        name = "protein_structure_lookups"
        indexes = [
            IndexModel([("accession", ASCENDING)], unique=True, name="uniq_accession"),
        ]
```

Register it in `backend/app/models/__init__.py` exactly as Task 2 registered
`ProteinRecord` (import, `ALL_MODELS`, `__all__`).

Create `backend/app/services/protein_structure.py`:

```python
"""Resolving a protein accession to its deposited structures.

The sibling of `structure_lookup.py`, which answers the same question from a
gene symbol. The difference is not cosmetic: that module's central problem is
that a symbol is ambiguous, and most of its code is the length guard that
disambiguates. An accession is unambiguous, so none of that applies -- which
is why this is a separate module and a separate cache rather than a parameter
on that one.

What replaces it is a *selection* problem specific to RefSeq. Measured against
the live API on 2026-08-17: `xref:refseq-NP_000537` (human TP53) returns three
UniProt entries -- P04637 with 295 PDB structures, alongside isoform entries
with none. Taking the first result would show "no structure available" for one
of the best-characterised proteins in the PDB.

A known limitation, recorded because nothing here can catch it: a
version-stripped RefSeq ID can cross-reference to an unexpected entry.
Measured, `xref:refseq-NP_009342` returns a 212aa entry where yeast CDC19 is
roughly 500aa. The gene-symbol path would catch that with its length guard;
here there is no residue position to check against. The mitigation is that the
resolved accession and protein name are returned and displayed, so a wrong
answer is visible to the reader rather than silent.

Uses stdlib urllib rather than httpx, matching `uniprot.py` and
`structure_lookup.py`; httpx is a dev-only dependency here.
"""

import asyncio
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass

from app.logging import get_logger
from app.metadata.protein_headers import ProteinRef, RefKind
from app.models import ProteinStructureLookup

log = get_logger(__name__)

_SEARCH_URL = "https://rest.uniprot.org/uniprotkb/search"

# Matches structure_lookup's budget, for the same reason: long enough for a
# cold UniProt response, short enough that a click does not appear to hang.
_TIMEOUT_SECONDS = 20.0

# A RefSeq cross-reference returns a handful of entries at most (TP53, the
# worst case measured, returns three). Enough to apply the selection rule
# without turning one lookup into a scan.
_MAX_CANDIDATES = 10


@dataclass(frozen=True)
class StructureHit:
    """One accession resolved, with whatever structures it has.

    An empty `pdb_ids` is a successful resolution, not a failure. The UI says
    "no experimental structure has been deposited" for that, and "this header
    doesn't name a known protein" for a None return -- two different sentences
    that must not be collapsed into one.
    """

    accession: str
    protein_name: str | None
    pdb_ids: list[str]


def _get(url: str, *, timeout: float = _TIMEOUT_SECONDS) -> bytes:
    """The transport, isolated so tests can replace it without a network."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _query_for(ref: ProteinRef) -> str:
    if ref.kind is RefKind.REFSEQ:
        # UniProt's cross-reference index, keyed on the unversioned accession.
        return f"xref:refseq-{ref.accession}"
    return f"accession:{ref.accession}"


def _pdb_ids(entry: dict) -> list[str]:
    """PDB cross-references only.

    The list holds dozens of databases. AlphaFoldDB in particular is
    deliberately excluded: a predicted model and a solved one warrant different
    confidence, and prediction is the follow-up this design defers rather than
    something to smuggle in behind the same button.
    """
    refs = entry.get("uniProtKBCrossReferences")
    if not isinstance(refs, list):
        return []
    return [
        ref["id"]
        for ref in refs
        if isinstance(ref, dict)
        and ref.get("database") == "PDB"
        and isinstance(ref.get("id"), str)
    ]


def _is_reviewed(entry: dict) -> bool:
    entry_type = (entry.get("entryType") or "").lower()
    return "reviewed" in entry_type and "unreviewed" not in entry_type


def _protein_name(entry: dict) -> str | None:
    description = entry.get("proteinDescription")
    if not isinstance(description, dict):
        return None
    recommended = description.get("recommendedName")
    if not isinstance(recommended, dict):
        return None
    full = recommended.get("fullName")
    if not isinstance(full, dict):
        return None
    value = full.get("value")
    return value if isinstance(value, str) else None


def _choose(entries: list) -> StructureHit | None:
    """The best candidate: reviewed first, then structure-bearing.

    Both halves are load-bearing and both come from measurement. Reviewed-first
    keeps an isoform entry from beating the curated one; structure-bearing
    among equals keeps a reviewed entry with no PDB references from hiding a
    reviewed one with 295 of them.
    """
    candidates = [e for e in entries if isinstance(e, dict) and e.get("primaryAccession")]
    if not candidates:
        return None

    best = max(
        candidates,
        key=lambda e: (_is_reviewed(e), bool(_pdb_ids(e))),
    )
    return StructureHit(
        accession=best["primaryAccession"],
        protein_name=_protein_name(best),
        pdb_ids=_pdb_ids(best),
    )


async def resolve(ref: ProteinRef) -> StructureHit | None:
    """The structures for one accession, or None if it resolved to nothing.

    None covers an unresolvable accession and a UniProt outage alike. They are
    distinguished in the log, not in the return type, because the UI can act on
    neither -- matching the contract `structure_lookup.resolve_structure`
    states for the same reason.
    """
    cached = await ProteinStructureLookup.find_one(
        ProteinStructureLookup.accession == ref.accession
    )
    if cached is not None:
        if cached.resolved_accession is None:
            return None
        return StructureHit(
            accession=cached.resolved_accession,
            protein_name=cached.protein_name,
            pdb_ids=list(cached.pdb_ids),
        )

    query = urllib.parse.urlencode(
        {
            "query": _query_for(ref),
            "fields": "accession,xref_pdb,protein_name,reviewed",
            "format": "json",
            "size": str(_MAX_CANDIDATES),
        }
    )

    try:
        # A blocking socket on the event loop would stall every other request.
        raw = await asyncio.to_thread(
            _get, f"{_SEARCH_URL}?{query}", timeout=_TIMEOUT_SECONDS
        )
        results = json.loads(raw).get("results", [])
    except Exception as exc:
        # Deliberately not cached. A cached failure is indistinguishable from a
        # cached "no structure", and this collection has no expiry -- so an
        # outage would become permanent until someone dropped the collection.
        log.info(
            "protein_structure_lookup_failed", accession=ref.accession, error=str(exc)
        )
        return None

    hit = _choose(results if isinstance(results, list) else [])
    await _remember(ref.accession, hit)
    return hit


async def _remember(accession: str, hit: StructureHit | None) -> None:
    """Store a result, including a negative one.

    Upsert rather than insert: two views of the same accession can reach here
    concurrently, and the unique index would turn the loser into an error over
    an answer that is already correct.
    """
    values = {
        "resolved_accession": hit.accession if hit else None,
        "protein_name": hit.protein_name if hit else None,
        "pdb_ids": hit.pdb_ids if hit else [],
    }
    await ProteinStructureLookup.find_one(
        ProteinStructureLookup.accession == accession
    ).upsert(
        {"$set": values},
        on_insert=ProteinStructureLookup(accession=accession, **values),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_protein_structure.py -q`
Expected: PASS, 9 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/protein_structure_lookup.py backend/app/models/__init__.py backend/app/services/protein_structure.py backend/tests/services/test_protein_structure.py
git commit -m "feat(api): resolve a protein accession to its PDB structures via UniProt"
```

---

### Task 6: Serve one record's structure

**Files:**
- Modify: `backend/app/api/v1/objects.py`
- Modify: `backend/app/api/v1/schemas.py`
- Test: `backend/tests/api/test_protein_structure_api.py`

**Interfaces:**
- Consumes: `resolve` / `StructureHit` (Task 5), `ProteinRecord` (Task 2).
- Produces: `GET /objects/{object_id}/protein-records/{ordinal}/structure` returning `{"identifier", "accession", "protein_name", "pdb_ids", "state"}` where `state` is one of `resolved`, `no_structure`, `no_reference`, `lookup_failed`. Task 7 renders each state.

Implements spec R21, R22, R27, R28. The explicit `state` field exists so the client never infers which of four sentences to show from the shape of a null.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_protein_structure_api.py`:

```python
"""One record's structure, and the four states it can be in.

The states are explicit in the response rather than inferred from nulls,
because the UI says four genuinely different things and a client deriving
them from `accession is None` gets "no structure deposited" and "header names
nothing" backwards -- which is exactly the confusion the existing variants
modal's comments warn about.
"""

import pytest
import pytest_asyncio
from app.metadata.protein_headers import RefKind
from app.models import ProteinRecord
from app.services import protein_structure

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture
async def records(protein_object):
    await ProteinRecord.insert_many(
        [
            ProteinRecord(
                object_id=protein_object.id,
                ordinal=0,
                identifier="NP_009342.1",
                description="Cdc19p",
                length=500,
                byte_offset=0,
                ref_kind=RefKind.REFSEQ,
                ref_accession="NP_009342",
            ),
            ProteinRecord(
                object_id=protein_object.id,
                ordinal=1,
                identifier="KLLIPMDF_00023",
                description="hypothetical protein",
                length=143,
                byte_offset=600,
            ),
        ]
    )
    return protein_object


async def test_resolved_record_returns_pdb_ids(client, records, monkeypatch):
    async def fake_resolve(ref):
        return protein_structure.StructureHit(
            accession="P00549", protein_name="Pyruvate kinase 1", pdb_ids=["1A3W"]
        )

    monkeypatch.setattr(protein_structure, "resolve", fake_resolve)

    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/0/structure"
    )
    body = resp.json()

    assert body["state"] == "resolved"
    assert body["pdb_ids"] == ["1A3W"]
    # R21: the resolved accession and name are surfaced so a mis-resolution is
    # visible to the reader rather than silent.
    assert body["accession"] == "P00549"
    assert body["protein_name"] == "Pyruvate kinase 1"


async def test_resolved_but_structureless_record_is_its_own_state(
    client, records, monkeypatch
):
    """R28. The majority outcome, and it must not read as a failure."""

    async def fake_resolve(ref):
        return protein_structure.StructureHit(
            accession="P00549", protein_name="Pyruvate kinase 1", pdb_ids=[]
        )

    monkeypatch.setattr(protein_structure, "resolve", fake_resolve)

    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/0/structure"
    )

    assert resp.json()["state"] == "no_structure"
    assert resp.json()["accession"] == "P00549"


async def test_record_naming_no_accession_never_queries_uniprot(
    client, records, monkeypatch
):
    """R27. There is nothing to look up, so looking anyway is a wasted call."""
    called = []

    async def fake_resolve(ref):
        called.append(ref)
        return None

    monkeypatch.setattr(protein_structure, "resolve", fake_resolve)

    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/1/structure"
    )

    assert resp.json()["state"] == "no_reference"
    assert called == []


async def test_lookup_failure_is_distinct_from_no_structure(
    client, records, monkeypatch
):
    """R22. An outage is retryable; "no structure deposited" is not."""

    async def fake_resolve(ref):
        return None

    monkeypatch.setattr(protein_structure, "resolve", fake_resolve)

    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/0/structure"
    )

    assert resp.status_code == 200
    assert resp.json()["state"] == "lookup_failed"


async def test_unknown_ordinal_is_a_404(client, records):
    resp = await client.get(
        f"/api/v1/objects/{records.id}/protein-records/999/structure"
    )
    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/api/test_protein_structure_api.py -q`
Expected: FAIL — 404, the route does not exist

- [ ] **Step 3: Write minimal implementation**

Add to `backend/app/api/v1/schemas.py`:

```python
class ProteinStructureState(StrEnum):
    """Which of four sentences the viewer shows.

    Explicit rather than inferred from null fields. A client deriving these
    from `accession is None` cannot tell "this header names nothing we can
    resolve" from "UniProt was unreachable" -- and those need a retry button
    and no retry button respectively.
    """

    RESOLVED = "resolved"
    NO_STRUCTURE = "no_structure"
    NO_REFERENCE = "no_reference"
    LOOKUP_FAILED = "lookup_failed"


class ProteinStructureOut(BaseModel):
    identifier: str
    state: ProteinStructureState
    accession: str | None = None
    protein_name: str | None = None
    pdb_ids: list[str] = []
```

Add to `backend/app/api/v1/objects.py`:

```python
@router.get(
    "/{object_id}/protein-records/{ordinal}/structure",
    response_model=ProteinStructureOut,
)
async def get_protein_record_structure(
    object_id: PydanticObjectId, ordinal: int, owner: OwnerDep
) -> ProteinStructureOut:
    """The structure for one record of a protein FASTA.

    Resolved on selection rather than for the whole list, for the reason the
    variants viewer records: most records resolve to nothing, and pre-resolving
    a page would spend a round trip per row to decide how buttons look.

    Always a 200 for a record that exists. Every outcome the user can act on --
    including a UniProt outage -- is a state in the body rather than a status
    code the client has to branch on.
    """
    obj = await object_service.get_object(object_id, owner=owner)

    record = await ProteinRecord.find_one(
        ProteinRecord.object_id == obj.id, ProteinRecord.ordinal == ordinal
    )
    if record is None:
        raise NotFoundError(f"No protein record {ordinal} for this file.")

    if record.ref_accession is None or record.ref_kind is None:
        # Nothing to look up. Querying anyway would spend a round trip to learn
        # what the header already said.
        return ProteinStructureOut(
            identifier=record.identifier,
            state=ProteinStructureState.NO_REFERENCE,
        )

    hit = await protein_structure.resolve(
        ProteinRef(kind=record.ref_kind, accession=record.ref_accession)
    )
    if hit is None:
        return ProteinStructureOut(
            identifier=record.identifier,
            state=ProteinStructureState.LOOKUP_FAILED,
        )

    return ProteinStructureOut(
        identifier=record.identifier,
        state=ProteinStructureState.RESOLVED
        if hit.pdb_ids
        else ProteinStructureState.NO_STRUCTURE,
        accession=hit.accession,
        protein_name=hit.protein_name,
        pdb_ids=hit.pdb_ids,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/api/test_protein_structure_api.py -q`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/objects.py backend/app/api/v1/schemas.py backend/tests/api/test_protein_structure_api.py
git commit -m "feat(api): report a protein record's structure and why it has none"
```

---

### Task 7: Extract the iCn3D frame

**Files:**
- Create: `frontend/src/components/Icn3dFrame.tsx`
- Modify: `frontend/src/components/StructureViewerModal.tsx`

**Interfaces:**
- Consumes: nothing.
- Produces: `<Icn3dFrame pdbId={string} title={string} />`, rendering the iframe with its load timeout and offline escape hatch. Task 8 uses it.

Implements spec R31. This is a **pure refactor** committed separately from the feature that consumes it, per CLAUDE.md's separable-commits rule — a mechanical extraction and a behaviour change in one commit is a commit nobody can review or revert.

- [ ] **Step 1: Create the shared component**

Create `frontend/src/components/Icn3dFrame.tsx` by moving the iframe, the
`icn3dUrl` helper, the `LOAD_TIMEOUT_MS` constant, and the `frameState` effect
out of `StructureViewerModal.tsx` unchanged. Preserve every comment — they
record why the timeout exists (an iframe fires no error event for a request
that never answers) and why no residue is selected (iCn3D's selector needs an
explicit chain, and guessing one highlights a real residue on the wrong
molecule).

The component owns its own `frameState` and renders both the failure box and
the iframe, so a consumer passes a PDB ID and a title and nothing else.

- [ ] **Step 2: Rewire the existing modal**

In `StructureViewerModal.tsx`, delete the extracted code and render
`<Icn3dFrame pdbId={pdbId} title={...} />` in its place. The modal keeps its
resolution logic, its heading, and its own empty states.

- [ ] **Step 3: Verify the variants viewer still works**

Bring the worktree stack up and check the existing path is unchanged — this is
a refactor, so any visible difference is a regression:

```bash
./ops/worktree-up.sh
```

Open a project with a called VCF at http://localhost:5273, open the variants
table, and click a structure button. Confirm the structure still renders, and
that an offline case still shows the escape-hatch link.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/Icn3dFrame.tsx frontend/src/components/StructureViewerModal.tsx
git commit -m "refactor(ui): extract the iCn3D iframe so more than one view can embed it"
```

---

### Task 8: The Structure tab

**Files:**
- Create: `frontend/src/components/ProteinStructureTab.tsx`
- Modify: `frontend/src/api/client.ts` (two calls)
- Modify: `frontend/src/api/types/` (add a `protein.ts` module, exported the way the directory's existing modules are)
- Modify: `frontend/src/components/DetailPanel.tsx` (`tabsFor` plus the tab body)

**Interfaces:**
- Consumes: `Icn3dFrame` (Task 7), both endpoints (Tasks 4 and 6).
- Produces: the user-visible feature.

Implements spec R23–R30. There is no headless component-testing setup in this repo and none is expected; verification is manual, per CLAUDE.md.

- [ ] **Step 1: Add the API types**

Create `frontend/src/api/types/protein.ts`:

```typescript
export interface ProteinRecordRow {
  ordinal: number;
  identifier: string;
  description: string;
  length: number;
  /** Whether the header named an accession at all. Distinguishes "nothing to
   *  look up" from "looked up and found no structure" without a round trip
   *  per row. */
  has_reference: boolean;
}

export interface ProteinRecords {
  total: number;
  /** The file held more records than the index cap, so this list is partial. */
  truncated: boolean;
  rows: ProteinRecordRow[];
}

/** Which of four sentences the viewer shows. Sent by the server rather than
 *  derived here: "no structure deposited" and "UniProt was unreachable" need
 *  different copy and only one of them is retryable. */
export type ProteinStructureState =
  | "resolved"
  | "no_structure"
  | "no_reference"
  | "lookup_failed";

export interface ProteinStructure {
  identifier: string;
  state: ProteinStructureState;
  accession: string | null;
  protein_name: string | null;
  pdb_ids: string[];
}
```

Export it from the types directory following whatever pattern the existing
modules there use.

- [ ] **Step 2: Add the API client calls**

In `frontend/src/api/client.ts`, beside `variantStructure`:

```typescript
  /** One page of a protein FASTA's records, optionally filtered.
   *
   *  `q` matches identifier or description: a user does not know which field
   *  holds the text they remember. */
  proteinRecords: (
    objectId: string,
    opts: { offset?: number; limit?: number; q?: string } = {},
  ) => {
    const params = new URLSearchParams();
    if (opts.offset) params.set("offset", String(opts.offset));
    if (opts.limit) params.set("limit", String(opts.limit));
    if (opts.q) params.set("q", opts.q);
    const query = params.toString();
    return request<ProteinRecords>(
      `/objects/${objectId}/protein-records${query ? `?${query}` : ""}`,
    );
  },

  /** The structure for one record, resolved on selection.
   *
   *  Resolved per record rather than per page for the reason the variants
   *  viewer records: most records resolve to nothing, and pre-resolving would
   *  spend a round trip per row to decide how buttons look. */
  proteinRecordStructure: (objectId: string, ordinal: number) =>
    request<ProteinStructure>(
      `/objects/${objectId}/protein-records/${ordinal}/structure`,
    ),
```

- [ ] **Step 3: Build the tab**

Create `frontend/src/components/ProteinStructureTab.tsx`. Requirements it must
satisfy, each traceable to the spec:

- A searchable, paged record list on the left (R25, R26), debouncing the search
  input so typing does not issue a request per keystroke.
- When the list is truncated, say so and give the count (R6) — presented as an
  estimate when the file exceeded `FASTA_EXACT_LIMIT`, since the count is then
  itself estimated.
- Selecting a record fetches its structure and renders `<Icn3dFrame>` when
  `state === "resolved"`, using `pdb_ids[0]`.
- Four distinct messages, one per state. Copy, matching the existing modal's
  register — plain, and never making the common case read as a failure:
  - `no_structure` (R28): "No experimental structure has been deposited for
    {protein_name ?? accession}. Most proteins don't have one."
  - `no_reference` (R27): "This record's header doesn't name a protein we can
    look up. Headers from annotation tools usually don't."
  - `lookup_failed` (R22): "Couldn't reach UniProt to look this up." — with a
    retry control, the only state that gets one.
  - `resolved`: show the accession and protein name (R21), linked to UniProt
    the way `StructureViewerModal` links it.
- A **disabled** "Predict structure" button in every state (R29, R30), with a
  title attribute reading "Structure prediction isn't available yet." It must
  not be wired to anything.

Follow the existing components' styling conventions — reuse `chrom-note` and
`error-box` classes as `StructureViewerModal` does rather than introducing new
ones.

- [ ] **Step 4: Wire the tab in**

In `DetailPanel.tsx`'s `tabsFor`, add the tab gated on role (R23, R24). Place
it after Results and before Quality — it is the "what came out" question for a
protein file:

```typescript
  // A protein FASTA is the only object whose records are indexed, so it is
  // the only one with anything to show here (R24).
  if (obj.role === "protein" && obj.format.kind === "fasta") {
    tabs.push({ id: "structure", label: "Structure" });
  }
```

Then render `<ProteinStructureTab objectId={obj.id} />` in a `TabPanel` under
`{tab === "structure" && ...}`, following the surrounding tabs' structure.

- [ ] **Step 5: Verify in the browser**

```bash
./ops/worktree-up.sh
```

At http://localhost:5273, with a protein FASTA in the library, check each of:

1. The Structure tab appears for a `.faa` and **not** for a BAM, VCF, or genome FASTA.
2. The record list loads, pages, and searches by both identifier and description.
3. A RefSeq-headed record (from an NCBI `protein.faa`) resolves and renders a structure.
4. A record whose header names nothing shows the `no_reference` copy.
5. The Predict button is visible, disabled, and explains itself in every state.

If you have no protein FASTA to hand, download one through the app's existing
NCBI assembly dialog by selecting the `protein` component.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/ProteinStructureTab.tsx frontend/src/api/client.ts frontend/src/api/types/ frontend/src/components/DetailPanel.tsx
git commit -m "feat(ui): add a structure tab that browses a protein FASTA's proteins in 3D"
```

---

### Task 9: Check the indexer against a real file

**Files:** none — this is verification, not code.

CLAUDE.md is explicit that a green suite over hand-built fixtures is not
enough here, and names the precedent: the Actions-tab suggestion rules passed
a full green suite while counting `protein.faa` and `cds_from_genomic.fna` as
alignable references, because every test fed the rules objects that already
looked the way the rules expected. This task is the "check it against the real
database" step that would have caught it.

- [ ] **Step 1: Ingest a real protein FASTA**

Through the running worktree stack, download an NCBI assembly's `protein`
component, or add a `.faa` you already have.

- [ ] **Step 2: Check what was actually indexed**

```bash
docker compose -p bioflow-issue-494-17d77a exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models import ProteinRecord

async def main():
    await connect_to_mongo()
    total = await ProteinRecord.find_all().count()
    print('total records:', total)
    for r in await ProteinRecord.find_all().limit(5).to_list():
        print(f'{r.ordinal:>6} {r.identifier:<20} {r.ref_kind} {r.ref_accession} len={r.length} off={r.byte_offset}')
    resolvable = await ProteinRecord.find(ProteinRecord.ref_accession != None).count()
    print(f'with an accession: {resolvable}/{total}')

asyncio.run(main())
"
```

Use the project name `worktree-up.sh` actually created — check
`./ops/worktree-up.sh --list` if the name above does not match.

What to confirm, and what each would mean if wrong:

- **The record count matches the file's `grep -c '^>'`.** A mismatch means the
  scan is dropping or double-counting records.
- **Descriptions are populated.** Empty descriptions mean the split is wrong
  and the list is unusable for picking a protein.
- **`ref_accession` is populated for a RefSeq file.** All-null on a
  `protein.faa` means the header regex does not match real NCBI headers — the
  single most likely place for this feature to be quietly broken, since it
  would leave every record showing the `no_reference` message.
- **Byte offsets point at `>` characters.** Verify one by hand against the file.

- [ ] **Step 3: Confirm the ingest cost is acceptable (R32)**

Compare the ingest duration for the protein FASTA against a similarly sized
non-protein FASTA, which skips indexing entirely. R32 allows a 50% increase.

- [ ] **Step 4: Confirm the page and search budget (R33)**

R33 allows 500 ms for one page, including a search. The risk is that
description search is a substring regex, which no index serves — so it is the
query most likely to blow the budget on a large file.

```bash
docker compose -p bioflow-issue-494-17d77a exec api python -c "
import asyncio, time
from app.db.client import connect_to_mongo
from app.models import ProteinRecord

async def main():
    await connect_to_mongo()
    obj = (await ProteinRecord.find_all().limit(1).to_list())[0].object_id

    for label, q in [('page', None), ('identifier', 'NP_0'), ('description', 'kinase')]:
        start = time.perf_counter()
        query = ProteinRecord.find(ProteinRecord.object_id == obj)
        if q:
            query = query.find({'\$or': [
                {'identifier': {'\$regex': q, '\$options': 'i'}},
                {'description': {'\$regex': q, '\$options': 'i'}},
            ]})
        await query.count()
        await query.sort(ProteinRecord.ordinal).limit(100).to_list()
        print(f'{label}: {(time.perf_counter() - start) * 1000:.0f} ms')

asyncio.run(main())
"
```

All three must come in under 500 ms. If description search does not, the
fallback is a Mongo text index on `description` — the spec's Layer 1 records
why one was not added up front, so adding it is a deliberate reversal to note
in the PR rather than a silent fix.

- [ ] **Step 5: Tear the stack down**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 6: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS with no new failures. Read the count, not the exit code.

---

### Task 10: File the follow-up tickets and open the PR

- [ ] **Step 1: File the prediction ticket**

```bash
gh issue create --title "[Feature]: Predict a protein structure when none is deposited" --label "type:feature,area:backend,area:frontend,status:specification document,priority:low" --body "The Structure tab added in #477 shows a disabled \"Predict structure\" button. This ticket makes it real.

It is the answer for two of that tab's four states: a record whose header names no database accession (annotation-tool output such as \`>KLLIPMDF_00023 hypothetical protein\`, where every record hits this), and a record that resolves to a UniProt entry with no deposited structure — roughly two thirds of resolved proteins.

Needs a decision on what runs the prediction: a hosted service, or a local model with the weights and GPU that implies. The design doc for #477 deliberately did not settle this.

Note the sequence bytes are already addressable — \`ProteinRecord.byte_offset\` was recorded at ingest for exactly this."
```

- [ ] **Step 2: File the sequence-similarity ticket**

```bash
gh issue create --title "[Feature]: Resolve protein records by sequence when the header names no accession" --label "type:feature,area:backend,status:specification document,priority:low" --body "#477 resolves a protein FASTA record to a structure by parsing an accession out of its header (UniProt \`sp|...|\` or RefSeq \`NP_\`/\`XP_\`/\`WP_\`). A header naming neither — which is every record of a Prokka or Bakta annotation — resolves to nothing.

This ticket adds a sequence-based fallback: UniProt peptide search for an exact match, or a BLAST-style similarity search for a near one.

Deferred from #477 for two reasons worth keeping: a similarity search is a slow external job rather than a request-time lookup, and a near-match introduces a confidence question that identifier resolution does not have — the design doc for #477 discusses why a confidently wrong protein is worse than no protein.

\`ProteinRecord.byte_offset\` was recorded at ingest so one record's sequence can be read without rescanning the file."
```

- [ ] **Step 3: Rebase onto main**

```bash
git fetch origin main
git rebase origin/main
```

- [ ] **Step 4: Confirm the work survived the rebase**

```bash
git diff origin/main...HEAD --stat
```

Check the file list matches what this plan touched, and skim for anything that
looks reverted.

- [ ] **Step 5: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --title "feat(ui): view the 3D structure of a protein from a protein FASTA" --body "$(cat <<'EOF'
Adds a Structure tab to protein FASTA objects: browse the proteins in the file, pick one, see its 3D structure.

Closes #477

## Why this shape

Most of the machinery already existed. `StructureViewerModal` renders structures for the variants table through an embedded iCn3D iframe, and `structure_lookup.py` resolves proteins through UniProt — but keyed on a gene symbol, which a protein FASTA does not have. This keys on the record header instead and reuses the rest.

## What it does

- Parses UniProt (`sp|P00924|...`) and RefSeq (`NP_`/`XP_`/`WP_`) accessions out of FASTA headers.
- Indexes every record of a protein FASTA at ingest (capped at 150,000) with identifier, description, length, and byte offset.
- Resolves an accession to PDB IDs through UniProt, cached by accession.
- Adds a paged, searchable record list and renders the selection through the extracted `Icn3dFrame`.
- Ships the "Predict structure" control disabled, per the issue.

## Two live-API measurements shaped this

A RefSeq cross-reference query can return several UniProt entries — `xref:refseq-NP_000537` (TP53) returns three, one with 295 structures and one with none — so entry selection is a rule (reviewed first, then structure-bearing), not "take the first result".

A version-stripped RefSeq ID can resolve to an unexpected entry: `NP_009342` returns a 212aa entry where yeast CDC19 is ~500aa. The gene-symbol path catches that class of error with its length guard; there is no residue position to guard with here. The mitigation is that the resolved accession and protein name are displayed, so a mis-resolution is visible rather than silent. This is recorded as a known limitation in the design doc.

## Notes for review

- `parsers.py` stays pure — the record write happens in the `ingest_headers` caller, not the parser.
- That handler is `HandlerMode.THREAD`, so the write goes through `run_from_thread`; `asyncio.run()` there would break Motor.
- A failure to index never fails an ingest.
- The `Icn3dFrame` extraction is its own commit, ahead of the feature that uses it.

Design doc: `docs/superpowers/specs/2026-08-17-protein-structure-viewer-design.md`

Follow-ups filed: structure prediction, and sequence-similarity resolution.
EOF
)"
```

- [ ] **Step 6: Label the PR**

```bash
gh pr edit <N> --add-label "type:feature" --add-label "area:backend" --add-label "area:frontend"
```

`.github/release.yml` categorizes release notes by label, not by the title's
prefix, so an unlabelled PR lands under "Other changes".

- [ ] **Step 7: Watch CI and merge**

Poll until every check reports pass — not pending, and not just the ones you
looked at:

```bash
gh pr checks <N> --watch
```

```bash
gh pr view <N> --json mergeable,mergeStateStatus
```

`UNSTABLE` means checks are still running; keep waiting. A real conflict
(`DIRTY`) means rebase on `origin/main` and push again. A red check means read
the job log and fix it — CI runs `ruff` rules the local suite does not.

Once every check passes and `mergeable` is `MERGEABLE`:

```bash
gh pr merge <N> --rebase --delete-branch
```

- [ ] **Step 8: Update the issue and clean up**

Comment on #477 with the merged PR and the two follow-up issue numbers, and
move its label from `status: implementation plan` to whatever the repo uses for
completed work. Then remove the worktree, per CLAUDE.md:

```bash
./ops/worktree-up.sh --down
```
