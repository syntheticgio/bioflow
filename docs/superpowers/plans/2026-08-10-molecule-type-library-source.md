# Molecule Type / Library Source Fields Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the `assay` metadata field into three: `molecule_type`
(DNA/RNA/Other), `library_source` (SRA's own vocabulary), and the unchanged
`assay`, so "is this RNA-seq or DNA-seq" has a direct answer instead of one
inferred from `assay`'s value. Add a manual, button-triggered inference of
`molecule_type` from a FASTQ's own bases for files with no SRA record.

**Architecture:** Two new `FieldDef` entries in the existing `COMMON_FIELDS`
registry (`backend/app/metadata/schemas.py`), populated by two new mapping
functions in `backend/app/metadata/sra.py` that hang off the already-parsed
(but currently discarded) `SraMetadata.library_source`. A new, side-effect-free
API endpoint samples a FASTQ's sequence lines for `U` and returns a suggested
`molecule_type` without writing to the database. A one-time backfill script
re-derives the two new fields for files ingested before this change.

**Tech Stack:** Python (FastAPI, Beanie/MongoDB), TypeScript/React frontend
(no frontend schema changes needed — `SchemaMetadataEditor` renders whatever
`fields_for()` returns).

**Design doc:** `docs/superpowers/specs/2026-08-10-molecule-type-library-source-design.md`

---

## File Structure

- Modify: `backend/app/metadata/schemas.py` — add `molecule_type` and
  `library_source` `FieldDef`s to `COMMON_FIELDS`.
- Modify: `backend/app/metadata/sra.py` — add `_SOURCE_MAP`,
  `_SOURCE_TO_MOLECULE`, `_map_source()`, `_map_molecule_type()`; extend
  `to_metadata()`.
- Create: `backend/app/metadata/infer_molecule.py` — FASTQ base-composition
  sampling, isolated from `sra.py` since it has nothing to do with SRA and
  isolated from `tile_scanner.py` since it has nothing to do with tiles (it
  only reuses that module's gzip-sniffing helper by duplicating the small
  function, matching this codebase's existing pattern of small
  single-purpose files over shared utility modules for this kind of helper —
  see Task 4 note on why it's duplicated rather than imported).
- Modify: `backend/app/api/v1/objects.py` — add
  `POST /{object_id}/infer-molecule-type`.
- Modify: `backend/app/api/v1/schemas.py` — add `MoleculeTypeInferenceOut`.
- Modify: `frontend/src/api/client.ts` — add `inferMoleculeType`.
- Modify: `frontend/src/components/SchemaMetadataEditor.tsx` — add the Infer
  button, wired to the new endpoint.
- Modify: `frontend/src/components/DetailPanel.tsx` — pass the new `objectId`
  prop through to `SchemaMetadataEditor`.
- Create: `backend/scripts/backfill_molecule_type.py` — one-time backfill,
  modeled on `backend/scripts/backfill_sra_mate_read_numbers.py`.
- Modify: `backend/tests/storage/test_sra.py` — extend `TestMetadataMapping`.
- Modify: `backend/tests/metadata/test_sra_resolver.py` — assert the two new
  `to_metadata()` keys against existing fixtures.
- Create: `backend/tests/metadata/test_infer_molecule.py` — unit tests for
  the new sampling module.
- Create: `backend/tests/api/test_infer_molecule_type_endpoint.py` (check
  Task 6 for the exact existing test file this should sit beside).

---

### Task 1: Add `molecule_type` and `library_source` fields to the schema

**Files:**
- Modify: `backend/app/metadata/schemas.py:132-141` (the existing `assay`
  `FieldDef` in `COMMON_FIELDS`)
- Test: `backend/tests/storage/test_metadata_schemas.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/storage/test_metadata_schemas.py`, inside
`class TestFieldResolution` (after `test_common_fields_apply_to_every_format`,
around line 14):

```python
    def test_molecule_type_and_library_source_are_common_fields(self):
        keys = {f.key for f in schemas.fields_for(FormatKind.FASTQ)}
        assert {"molecule_type", "library_source", "assay"} <= keys

    def test_molecule_type_is_closed_vocabulary(self):
        field = next(f for f in schemas.COMMON_FIELDS if f.key == "molecule_type")
        assert field.type == FieldType.ENUM
        assert field.options == ("DNA", "RNA", "Other")
        assert field.open_vocabulary is False

    def test_library_source_is_open_vocabulary(self):
        field = next(f for f in schemas.COMMON_FIELDS if f.key == "library_source")
        assert field.type == FieldType.ENUM
        assert field.options == (
            "Genomic", "Transcriptomic", "Metagenomic",
            "Metatranscriptomic", "Synthetic", "Viral RNA", "Other",
        )
        assert field.open_vocabulary is True
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest backend/tests/storage/test_metadata_schemas.py -k "molecule_type or library_source" -v
```

Expected: FAIL — `StopIteration` (no field with key `molecule_type` exists
yet) or `AssertionError` on the keys subset check.

- [ ] **Step 3: Add the two `FieldDef`s**

In `backend/app/metadata/schemas.py`, insert immediately **before** the
existing `assay` `FieldDef` (which starts at line 132):

```python
    FieldDef(
        "molecule_type",
        "Molecule type",
        type=FieldType.ENUM,
        options=("DNA", "RNA", "Other"),
        group="Experiment",
        suggested=True,
        help="What the library was made from. Derived from the SRA record's "
             "library source, or inferred from the FASTQ's own bases via the "
             "Infer button when no SRA record is available.",
    ),
    FieldDef(
        "library_source",
        "Library source",
        type=FieldType.ENUM,
        options=("Genomic", "Transcriptomic", "Metagenomic",
                 "Metatranscriptomic", "Synthetic", "Viral RNA", "Other"),
        group="Experiment",
        open_vocabulary=True,
        help="SRA's own library source classification, where available.",
    ),
```

The existing `assay` `FieldDef` (lines 132-141) is unchanged — leave it
exactly as-is, directly after the two new fields.

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec api python -m pytest backend/tests/storage/test_metadata_schemas.py -k "molecule_type or library_source" -v
```

Expected: PASS (3 tests).

- [ ] **Step 5: Run the full schema test file to check nothing else broke**

```bash
docker compose exec api python -m pytest backend/tests/storage/test_metadata_schemas.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/metadata/schemas.py backend/tests/storage/test_metadata_schemas.py
git commit -m "feat(metadata): add molecule type and library source fields"
```

---

### Task 2: Map SRA's `library_source` onto the two new fields

**Files:**
- Modify: `backend/app/metadata/sra.py:100-101` (inside `to_metadata()`) and
  `backend/app/metadata/sra.py:124-139` (beside `_STRATEGY_MAP`/`_map_strategy`)
- Test: `backend/tests/storage/test_sra.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/storage/test_sra.py`, inside `class TestMetadataMapping`
(after `test_library_strategy_maps_to_our_assay_vocabulary`, around line 136):

```python
    def test_library_source_maps_to_our_vocabulary(self, chipseq_xml):
        meta = sra.parse_experiment_xml(chipseq_xml).to_metadata()
        assert meta["library_source"] == "Genomic"  # SRA writes "GENOMIC"

    def test_library_source_derives_molecule_type(self, chipseq_xml):
        meta = sra.parse_experiment_xml(chipseq_xml).to_metadata()
        assert meta["molecule_type"] == "DNA"

    def test_transcriptomic_source_maps_to_rna(self):
        m = sra.SraMetadata(library_source="TRANSCRIPTOMIC")
        out = m.to_metadata()
        assert out["library_source"] == "Transcriptomic"
        assert out["molecule_type"] == "RNA"

    def test_metagenomic_and_synthetic_map_to_dna(self):
        assert sra.SraMetadata(library_source="METAGENOMIC").to_metadata()["molecule_type"] == "DNA"
        assert sra.SraMetadata(library_source="SYNTHETIC").to_metadata()["molecule_type"] == "DNA"

    def test_metatranscriptomic_and_viral_rna_map_to_rna(self):
        assert sra.SraMetadata(library_source="METATRANSCRIPTOMIC").to_metadata()["molecule_type"] == "RNA"
        assert sra.SraMetadata(library_source="VIRAL RNA").to_metadata()["molecule_type"] == "RNA"

    def test_unrecognized_source_passes_through_but_molecule_type_is_other(self):
        """Losing information to an incomplete lookup table would be worse than
        showing SRA's own wording -- same rule test_unknown_strategy_passes_through_unchanged
        applies to assay. molecule_type has no free-text escape hatch (it is a
        closed field), so an unrecognized source becomes "Other" there instead."""
        m = sra.SraMetadata(library_source="OTHER EXOTIC THING")
        out = m.to_metadata()
        assert out["library_source"] == "OTHER EXOTIC THING"
        assert out["molecule_type"] == "Other"

    def test_no_library_source_emits_neither_key(self):
        m = sra.SraMetadata(library_strategy="WGS")
        out = m.to_metadata()
        assert "library_source" not in out
        assert "molecule_type" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec api python -m pytest backend/tests/storage/test_sra.py -k "library_source or molecule_type" -v
```

Expected: FAIL — `KeyError: 'library_source'` (key not yet in the returned
dict).

- [ ] **Step 3: Add the mapping tables and functions**

In `backend/app/metadata/sra.py`, immediately after the existing
`_map_strategy` function (which ends at line 139), add:

```python
# SRA library sources map onto our library_source vocabulary the same way
# strategies do -- anything unrecognized passes through unchanged.
_SOURCE_MAP = {
    "GENOMIC": "Genomic",
    "TRANSCRIPTOMIC": "Transcriptomic",
    "METAGENOMIC": "Metagenomic",
    "METATRANSCRIPTOMIC": "Metatranscriptomic",
    "SYNTHETIC": "Synthetic",
    "VIRAL RNA": "Viral RNA",
    "OTHER": "Other",
}

# Coarse DNA/RNA bucket derived from library source. Not a judgment call SRA
# makes explicitly -- METAGENOMIC and SYNTHETIC are assumed DNA-like (both are
# overwhelmingly genomic-DNA preps in practice), anything not recognized here
# maps to "Other" rather than a guess.
_SOURCE_TO_MOLECULE = {
    "GENOMIC": "DNA",
    "METAGENOMIC": "DNA",
    "SYNTHETIC": "DNA",
    "TRANSCRIPTOMIC": "RNA",
    "METATRANSCRIPTOMIC": "RNA",
    "VIRAL RNA": "RNA",
}


def _map_source(source: str) -> str:
    return _SOURCE_MAP.get(source.strip().upper(), source)


def _map_molecule_type(source: str) -> str:
    return _SOURCE_TO_MOLECULE.get(source.strip().upper(), "Other")
```

Then in `to_metadata()`, immediately after the existing block

```python
        if self.library_strategy:
            out["assay"] = _map_strategy(self.library_strategy)
```

(around line 100-101) add:

```python
        if self.library_source:
            out["library_source"] = _map_source(self.library_source)
            out["molecule_type"] = _map_molecule_type(self.library_source)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec api python -m pytest backend/tests/storage/test_sra.py -k "library_source or molecule_type" -v
```

Expected: PASS (7 tests).

- [ ] **Step 5: Run the full SRA test file**

```bash
docker compose exec api python -m pytest backend/tests/storage/test_sra.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/metadata/sra.py backend/tests/storage/test_sra.py
git commit -m "feat(metadata): derive molecule type and library source from SRA"
```

---

### Task 3: Assert the new fields against the resolver-level fixtures

**Files:**
- Modify: `backend/tests/metadata/test_sra_resolver.py`

This closes the gap between "the mapping functions work in isolation" (Task
2) and "the real fixture parses end-to-end through the resolver the way
production code calls it."

- [ ] **Step 1: Read the existing resolver test file's structure**

```bash
docker compose exec api python -m pytest backend/tests/metadata/test_sra_resolver.py --collect-only -q
```

Find the test class/fixture that already asserts on `library_strategy` for
`SRR11768093` (per the design doc research, this file already tests
`library_strategy`/`library_layout` from the same XML fixture used in Task
2). Add the new assertions as a new test method in that same class, next to
the existing `library_strategy` assertion, following whatever fixture-loading
pattern that file already uses (it will match `chipseq_xml`/`wgs_xml`-style
fixtures from `test_sra.py`, possibly re-declared locally in this file — read
the file first to match its exact fixture names before writing the test).

- [ ] **Step 2: Write the failing test**

Using the pattern found in Step 1 (illustrative — adjust fixture/function
names to match what Step 1 found; the *assertions* below are exact):

```python
    def test_library_source_and_molecule_type_from_resolved_record(self, ...):
        meta = ...  # however this file resolves SRR11768093 today
        result = meta.to_metadata()
        assert result["library_source"] == "Genomic"
        assert result["molecule_type"] == "DNA"
```

- [ ] **Step 3: Run test to verify it fails**

```bash
docker compose exec api python -m pytest backend/tests/metadata/test_sra_resolver.py -k "library_source" -v
```

Expected: FAIL (key not present before Task 2's change — but Task 2 already
landed, so if this fails it means the resolver's own object construction
differs from the direct `parse_experiment_xml` path Task 2 tested; investigate
before proceeding rather than adjusting the test to fit).

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec api python -m pytest backend/tests/metadata/test_sra_resolver.py -k "library_source" -v
```

Expected: PASS.

- [ ] **Step 5: Run the full resolver test file**

```bash
docker compose exec api python -m pytest backend/tests/metadata/test_sra_resolver.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/tests/metadata/test_sra_resolver.py
git commit -m "test(metadata): cover molecule type through the SRA resolver path"
```

---

### Task 4: FASTQ base-composition inference module

**Files:**
- Create: `backend/app/metadata/infer_molecule.py`
- Create: `backend/tests/metadata/test_infer_molecule.py`

**Why the gzip-sniff helper is duplicated, not imported:**
`tile_scanner.py` lives in `backend/app/pipelines/`, a different layer from
`backend/app/metadata/` where this module belongs (metadata inference, not a
pipeline stage). Importing across those two for one seven-line function
would create a dependency where none otherwise exists; duplicating the
(tiny, stable) magic-number check keeps `infer_molecule.py` self-contained.
This mirrors `detect_sequence_type` in `enrich.py`, which also does its own
file reading rather than reaching into `pipelines/`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/metadata/test_infer_molecule.py`:

```python
"""FASTQ base-composition sampling for the manual molecule-type inference button.

Never automatic -- this only runs when a user clicks Infer. See
docs/superpowers/specs/2026-08-10-molecule-type-library-source-design.md for why:
most RNA-seq reads as T (reverse-transcribed to cDNA before sequencing), so
"no U found" is DNA by elimination, not by positive evidence.
"""

import gzip
from pathlib import Path

import pytest

from app.metadata.infer_molecule import infer_molecule_type


def _write_fastq(path: Path, records: list[tuple[str, str]]) -> None:
    """records is a list of (header_suffix, sequence) pairs."""
    lines = []
    for i, (header, seq) in enumerate(records):
        lines.append(f"@read{i} {header}")
        lines.append(seq)
        lines.append("+")
        lines.append("I" * len(seq))
    path.write_text("\n".join(lines) + "\n")


class TestBaseComposition:
    def test_all_t_sequence_is_dna(self, tmp_path):
        fq = tmp_path / "reads.fastq"
        _write_fastq(fq, [("", "ACGTACGTACGT")] * 10)
        result = infer_molecule_type(fq)
        assert result["molecule_type"] == "DNA"
        assert "no U found" in result["basis"]

    def test_sequence_with_u_is_rna(self, tmp_path):
        fq = tmp_path / "reads.fastq"
        _write_fastq(fq, [("", "ACGUACGUACGU")] * 10)
        result = infer_molecule_type(fq)
        assert result["molecule_type"] == "RNA"
        assert "U present" in result["basis"]

    def test_lowercase_u_counts(self, tmp_path):
        fq = tmp_path / "reads.fastq"
        _write_fastq(fq, [("", "acguacgu")])
        result = infer_molecule_type(fq)
        assert result["molecule_type"] == "RNA"

    def test_u_in_a_later_sampled_record_is_still_found(self, tmp_path):
        fq = tmp_path / "reads.fastq"
        records = [("", "ACGTACGT")] * 50 + [("", "ACGUACGU")]
        _write_fastq(fq, records)
        result = infer_molecule_type(fq, sample_reads=51)
        assert result["molecule_type"] == "RNA"


class TestGzipTransparency:
    def test_gzipped_all_t_is_dna(self, tmp_path):
        fq = tmp_path / "reads.fastq.gz"
        content = "@r1\nACGTACGT\n+\nIIIIIIII\n"
        with gzip.open(fq, "wt") as fh:
            fh.write(content)
        result = infer_molecule_type(fq)
        assert result["molecule_type"] == "DNA"

    def test_gzipped_with_u_is_rna(self, tmp_path):
        fq = tmp_path / "reads.fastq.gz"
        content = "@r1\nACGUACGU\n+\nIIIIIIII\n"
        with gzip.open(fq, "wt") as fh:
            fh.write(content)
        result = infer_molecule_type(fq)
        assert result["molecule_type"] == "RNA"


class TestSamplingBound:
    def test_only_samples_requested_number_of_reads(self, tmp_path):
        """A U past the sample window must not be found -- otherwise
        sample_reads is not actually bounding the read."""
        fq = tmp_path / "reads.fastq"
        records = [("", "ACGTACGT")] * 5 + [("", "ACGUACGU")]
        _write_fastq(fq, records)
        result = infer_molecule_type(fq, sample_reads=5)
        assert result["molecule_type"] == "DNA"


class TestEdgeCases:
    def test_empty_file_returns_none_molecule_type(self, tmp_path):
        fq = tmp_path / "empty.fastq"
        fq.write_text("")
        result = infer_molecule_type(fq)
        assert result["molecule_type"] is None
        assert "no sequence" in result["basis"].lower()

    def test_truncated_file_with_no_full_record_returns_none(self, tmp_path):
        fq = tmp_path / "truncated.fastq"
        fq.write_text("@r1\n")  # header only, no sequence line
        result = infer_molecule_type(fq)
        assert result["molecule_type"] is None

    def test_does_not_raise_on_malformed_file(self, tmp_path):
        fq = tmp_path / "garbage.fastq"
        fq.write_bytes(b"\x00\x01\x02not a fastq at all")
        result = infer_molecule_type(fq)
        assert result["molecule_type"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec api python -m pytest backend/tests/metadata/test_infer_molecule.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.metadata.infer_molecule'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/metadata/infer_molecule.py`:

```python
"""Manual, opt-in inference of molecule type from a FASTQ's own bases.

Never called from `enrich_from_sra`, `ingest_headers`, or any scheduled job --
only from the user-triggered `POST /{object_id}/infer-molecule-type` endpoint.
See docs/superpowers/specs/2026-08-10-molecule-type-library-source-design.md.

The signal: presence of `U` in sampled sequence lines means RNA (direct RNA
sequencing -- rare but unambiguous). Its absence defaults to DNA. This is a
real limitation, not an edge case -- most RNA-seq data is reverse-transcribed
to cDNA before sequencing and reads as `T`, identical to DNA, so "no U found"
is DNA by elimination, not by positive evidence. Callers surface `basis`
alongside the result so this doesn't read as more certain than it is.
"""

import gzip
from pathlib import Path
from typing import IO


def _open_fastq(path: Path) -> IO[str]:
    """Open plain or gzipped FASTQ as text, by magic number rather than name."""
    with open(path, "rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", errors="replace")
    return open(path, "rt", errors="replace")


def infer_molecule_type(path: Path, *, sample_reads: int = 2000) -> dict:
    """Sample a FASTQ's sequence lines and report DNA or RNA by base content.

    Never raises for a well-formed or malformed FASTQ; returns
    {"molecule_type": None, "basis": "..."} if the file is empty or no
    sequence lines are found in the sampled region. Caller translates that
    into a 4xx/204 at the API layer -- this function only reads and classifies.
    """
    sequences_seen = 0
    found_u = False
    try:
        with _open_fastq(path) as fh:
            for i, line in enumerate(fh):
                if i % 4 != 1:
                    continue
                sequences_seen += 1
                if "u" in line.lower():
                    found_u = True
                    break
                if sequences_seen >= sample_reads:
                    break
    except OSError:
        return {"molecule_type": None, "basis": "file could not be opened"}

    if sequences_seen == 0:
        return {
            "molecule_type": None,
            "basis": "no sequence lines found in the sampled region",
        }

    if found_u:
        return {
            "molecule_type": "RNA",
            "basis": f"sampled {sequences_seen} reads, U present",
        }
    return {
        "molecule_type": "DNA",
        "basis": f"sampled {sequences_seen} reads, no U found",
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec api python -m pytest backend/tests/metadata/test_infer_molecule.py -v
```

Expected: all PASS (11 tests).

Note on `TestSamplingBound`: the implementation breaks out of the loop as
soon as `found_u` is set, *before* the `sequences_seen >= sample_reads`
check — so a `U` found early counts even past what a smaller `sample_reads`
would otherwise allow, but a `U` positioned strictly after the sample window
(as in that test, where records 0-4 are sampled and record 5 has the `U`)
correctly returns DNA. If this test fails, verify the loop order matches the
code above exactly (the `break` inside the `if "u" in line.lower()` block
must come before the `sequences_seen >= sample_reads` check in iteration
order, not after).

- [ ] **Step 5: Commit**

```bash
git add backend/app/metadata/infer_molecule.py backend/tests/metadata/test_infer_molecule.py
git commit -m "feat(metadata): add manual FASTQ base-composition molecule-type inference"
```

---

### Task 5: API endpoint

**Files:**
- Modify: `backend/app/api/v1/objects.py`
- Test: `backend/tests/api/test_objects.py` (or the file identified in Step 1
  below if a more specific one exists)

- [ ] **Step 1: Find the existing test file for `objects.py` endpoints**

```bash
docker compose exec api python -m pytest backend/tests/api/ --collect-only -q | grep -i object
```

Identify which existing test file covers `reingest_object` or `pair_object`
(the two closest analogues) — add the new endpoint's tests there, matching
its fixture/client setup exactly (owner auth header pattern, `object_id`
creation helper, etc.). The steps below assume a file at
`backend/tests/api/test_objects.py`; substitute the actual path found here.

- [ ] **Step 2: Write the failing tests**

Add to the identified test file, in a new class:

```python
class TestInferMoleculeType:
    async def test_infers_dna_from_a_stored_fastq(self, client, owner_headers, managed_fastq_object):
        """managed_fastq_object is a fixture that creates a DataObject with a
        real FASTQ blob on managed storage -- find or create this fixture by
        matching how download_object's tests get a real file on disk (search
        this test file and conftest.py for an existing managed-blob fixture
        before writing a new one)."""
        resp = await client.post(
            f"/api/v1/objects/{managed_fastq_object.id}/infer-molecule-type",
            headers=owner_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["molecule_type"] in ("DNA", "RNA", None)
        assert "basis" in body

    async def test_does_not_write_to_object_metadata(self, client, owner_headers, managed_fastq_object):
        before = managed_fastq_object.metadata.copy()
        await client.post(
            f"/api/v1/objects/{managed_fastq_object.id}/infer-molecule-type",
            headers=owner_headers,
        )
        await managed_fastq_object.sync()  # or however this test file re-fetches; match existing pattern
        assert managed_fastq_object.metadata == before

    async def test_404_for_object_with_no_stored_content(self, client, owner_headers, object_without_blob):
        resp = await client.post(
            f"/api/v1/objects/{object_without_blob.id}/infer-molecule-type",
            headers=owner_headers,
        )
        assert resp.status_code == 404
```

Adjust fixture names (`client`, `owner_headers`, `managed_fastq_object`,
`object_without_blob`, the re-fetch/sync call) to whatever this test file and
its `conftest.py` actually define — grep for `async def test_reingest` or
`async def test_download_object` in the identified file first and copy its
exact setup pattern rather than guessing fixture names.

- [ ] **Step 3: Run tests to verify they fail**

```bash
docker compose exec api python -m pytest backend/tests/api/test_objects.py -k "InferMoleculeType" -v
```

Expected: FAIL — 404/405 (route does not exist yet).

- [ ] **Step 4: Add the response schema**

In `backend/app/api/v1/schemas.py`, add near `PairRequest` (around line 85):

```python
class MoleculeTypeInferenceOut(BaseModel):
    """Result of sampling a FASTQ's bases for the manual Infer button.

    Deliberately not written to the object -- the caller applies it to an
    in-progress edit and Saves (or doesn't) through the normal metadata PATCH.
    """

    molecule_type: str | None
    basis: str
```

- [ ] **Step 5: Add the endpoint**

In `backend/app/api/v1/objects.py`, add the import for the new schema to the
existing `from app.api.v1.schemas import (...)` block (alphabetical, next to
`ObjectUpdate`), the import for `infer_molecule` next to the other
`app.metadata` usage if any exists (otherwise add
`from app.metadata import infer_molecule`), and `blob_path` if not already
imported (check the existing `download_object` function above `reingest_object`
— it already imports and uses `blob_path` and `BlobStorage`, both already in
scope per the `download_object` code shown during research).

Add the endpoint immediately after `reingest_object` (which ends around line
333, right before the `DELETE /{object_id}` endpoint):

```python
@router.post("/{object_id}/infer-molecule-type", response_model=MoleculeTypeInferenceOut)
async def infer_molecule_type_endpoint(
    object_id: PydanticObjectId, owner: OwnerDep
) -> MoleculeTypeInferenceOut:
    """Sample a FASTQ's own bases to suggest DNA or RNA.

    User-triggered only -- never runs automatically. Returns a suggestion for
    the caller to apply to an in-progress metadata edit; does not write
    anything itself. Runs synchronously: sampling ~2000 reads from the start
    of the file is bounded regardless of the file's total size, unlike
    reingest's full pipeline dispatch.
    """
    obj, blob = await object_service.object_with_blob(object_id, owner=owner)
    if blob is None or not obj.blob_sha256:
        raise NotFoundError("Object has no stored content to sample yet")

    if blob.storage is BlobStorage.EXTERNAL:
        if not blob.external_path:
            raise NotFoundError("External blob has no recorded path")
        target = Path(blob.external_path)
    else:
        target = blob_path(obj.blob_sha256)

    if not target.is_file():
        raise NotFoundError(f"Stored content is not available: {obj.name}")

    result = infer_molecule.infer_molecule_type(target)
    return MoleculeTypeInferenceOut(**result)
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
docker compose exec api python -m pytest backend/tests/api/test_objects.py -k "InferMoleculeType" -v
```

Expected: all PASS.

- [ ] **Step 7: Run the full objects API test file**

```bash
docker compose exec api python -m pytest backend/tests/api/test_objects.py -v
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/v1/objects.py backend/app/api/v1/schemas.py backend/tests/api/test_objects.py
git commit -m "feat(api): add manual molecule-type inference endpoint"
```

---

### Task 5.5: Infer button in the frontend

**Files:**
- Modify: `frontend/src/api/client.ts` (add `inferMoleculeType`, next to
  `reingestObject` around line 312)
- Modify: `frontend/src/components/SchemaMetadataEditor.tsx`

- [ ] **Step 1: Add the API client method**

In `frontend/src/api/client.ts`, immediately after `reingestObject` (ends at
line 315), add:

```ts
  inferMoleculeType: (id: string) =>
    request<{ molecule_type: string | null; basis: string }>(
      `/objects/${id}/infer-molecule-type`,
      { method: "POST" },
    ),
```

- [ ] **Step 2: Add state and the inference handler in `SchemaMetadataEditor`**

In `frontend/src/components/SchemaMetadataEditor.tsx`, add `formatKind` is
already a prop (line 9) — no new prop needed since the component already
knows the object's format.

Add a new state variable next to the existing `showAll` state (line 57):

```tsx
  const [showAll, setShowAll] = useState(false);
  const [inferring, setInferring] = useState(false);
  const [inferBasis, setInferBasis] = useState<string | null>(null);
```

Add the handler function next to `save` (after line 105, before the
`if (!schema)` guard):

```tsx
  const inferMoleculeType = async (objectId: string) => {
    setInferring(true);
    try {
      const result = await api.inferMoleculeType(objectId);
      if (result.molecule_type) {
        setField("molecule_type", result.molecule_type);
      }
      setInferBasis(result.basis);
    } finally {
      setInferring(false);
    }
  };
```

This needs the object's id, which `SchemaMetadataEditor` does not currently
receive as a prop (it only gets `value`, `formatKind`, `role`). Add an
`objectId: string` prop:

```tsx
interface Props {
  value: Record<string, unknown>;
  formatKind: string;
  role: ObjectRole | null;
  objectId: string;
  onSave: (next: Record<string, unknown>) => void;
  saving?: boolean;
  onDirtyChange?: (dirty: boolean) => void;
  dedupeGroups?: string[];
}
```

and destructure it in the function signature (line 37-45):

```tsx
export function SchemaMetadataEditor({
  value,
  formatKind,
  role,
  objectId,
  onSave,
  saving,
  onDirtyChange,
  dedupeGroups = [],
}: Props) {
```

- [ ] **Step 3: Update `SchemaMetadataEditor`'s call site**

At `frontend/src/components/DetailPanel.tsx:1322-1334`, the existing call is:

```tsx
        <SchemaMetadataEditor
          key={obj.role ?? "none"}
          value={obj.metadata}
          formatKind={obj.format.kind}
          role={obj.role}
          onSave={onSave}
          saving={saving}
          onDirtyChange={onDirtyChange}
          dedupeGroups={isReference ? [] : ["Archive"]}
        />
```

Add `objectId={obj.id}` after `formatKind`:

```tsx
        <SchemaMetadataEditor
          key={obj.role ?? "none"}
          value={obj.metadata}
          formatKind={obj.format.kind}
          objectId={obj.id}
          role={obj.role}
          onSave={onSave}
          saving={saving}
          onDirtyChange={onDirtyChange}
          dedupeGroups={isReference ? [] : ["Archive"]}
        />
```

- [ ] **Step 4: Render the Infer button next to the `molecule_type` field**

`FieldInput` (line 234) has no per-field extension point today, so rather
than thread inference-specific props through the generic `FieldInput`, add a
special case in the `visible.map` loop (line 138-145) that renders the
button alongside the field, scoped to `molecule_type` on FASTQ objects only:

```tsx
              {visible.map((f) => (
                <div key={f.key}>
                  <FieldInput
                    field={f}
                    value={values[f.key]}
                    onChange={(v) => setField(f.key, v)}
                  />
                  {f.key === "molecule_type" && formatKind === "fastq" && (
                    <div style={{ marginTop: -4, marginBottom: 7, fontSize: 12 }}>
                      <button
                        type="button"
                        className="btn"
                        style={{ padding: "2px 8px", fontSize: 12 }}
                        disabled={inferring}
                        onClick={() => inferMoleculeType(objectId)}
                      >
                        {inferring ? "Sampling…" : "Infer from FASTQ"}
                      </button>
                      {inferBasis && (
                        <span style={{ color: "var(--text-faint)", marginLeft: 6 }}>
                          {inferBasis}
                        </span>
                      )}
                    </div>
                  )}
                </div>
              ))}
```

This replaces the existing plain `<FieldInput key={f.key} ... />` line
(current lines 139-144) inside the `group.fields` render loop. `formatKind`
is passed into `SchemaMetadataEditor` directly from `obj.format.kind`
(confirmed at the `DetailPanel.tsx` call site above, Step 3) — the literal
`"fastq"` comparison matches how `formatKind` already flows into the
`queryFn: () => api.metadataSchema(formatKind, role)` call at line 50 of this
same file, which passes it straight through as a plain string.

- [ ] **Step 5: Manual verification**

Covered by Task 7, Steps 2-5 below — no automated frontend test exists in
this repo (per CLAUDE.md: "no headless component-testing setup... none is
expected"). Manual browser verification is the test for this task.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/client.ts frontend/src/components/SchemaMetadataEditor.tsx frontend/src/components/DetailPanel.tsx
git commit -m "feat(ui): add Infer-from-FASTQ button for molecule type"
```

---

### Task 6: One-time backfill script

**Files:**
- Create: `backend/scripts/backfill_molecule_type.py`

Modeled directly on `backend/scripts/backfill_sra_mate_read_numbers.py` —
same dry-run-by-default, same piped-execution convention (this repo's `api`
container only bakes in `backend/scripts` at build time, so a script added
since the last image build must be piped in via stdin, not run from a path
inside the container).

- [ ] **Step 1: Write the script**

Create `backend/scripts/backfill_molecule_type.py`:

```python
"""Backfill `molecule_type` and `library_source` for files enriched before
these fields existed.

A one-off data repair, not a feature. `SraMetadata.to_metadata()` now maps
`library_source` onto `molecule_type`/`library_source` (see
docs/superpowers/specs/2026-08-10-molecule-type-library-source-design.md),
but this only runs on ingest or on-demand re-ingest -- there is no periodic
SRA re-verification job in this codebase. Files ingested before this change
have an SRA accession and metadata but neither new field.

Re-fetches each candidate's SRA record by its stored accession and re-derives
just the two new keys, so the backfilled values cannot disagree with what a
fresh ingest of the same file would produce today. Never overwrites a value
already present -- defensive even though the source data guarantees no
candidate has one, since this reuses fields also touched by the ordinary
ingest path and a second run of this same script must be a no-op.

Dry run by default. `--apply` writes.

Piped in rather than run from a path inside the container: the api container
mounts only `backend/app` and `backend/tests`, and `backend/scripts` is baked
into the image at build time -- so a script added since the last build is not
in there to execute.

    docker exec -i biopipe-api-1 python - \\
        < backend/scripts/backfill_molecule_type.py
    docker exec -i biopipe-api-1 python - --apply \\
        < backend/scripts/backfill_molecule_type.py
"""

import argparse
import asyncio
import sys

sys.path.insert(0, "/srv")

from app.db.client import connect_to_mongo  # noqa: E402
from app.metadata import sra  # noqa: E402
from app.models import DataObject  # noqa: E402


def _accession(obj: DataObject) -> str | None:
    return (
        obj.metadata.get("sra_run")
        or obj.metadata.get("sra_experiment")
        or obj.metadata.get("sra_sample")
        or obj.metadata.get("sra_study")
    )


async def main(apply: bool) -> int:
    await connect_to_mongo()

    candidates = [
        obj
        for obj in await DataObject.find().to_list()
        if _accession(obj) and not obj.metadata.get("molecule_type")
    ]

    planned, skipped = [], []
    for obj in candidates:
        accession = _accession(obj)
        try:
            meta = sra.lookup(accession)
        except Exception as exc:  # network/parse failure -- report, don't crash the batch
            skipped.append((obj, str(exc)))
            continue
        derived = meta.to_metadata()
        updates = {
            k: v
            for k, v in derived.items()
            if k in ("molecule_type", "library_source") and not obj.metadata.get(k)
        }
        if updates:
            planned.append((obj, updates))
        else:
            skipped.append((obj, "SRA record has no library_source"))

    print(f"{len(candidates)} objects with an SRA accession and no molecule_type")
    print(f"{len(planned)} to backfill, {len(skipped)} skipped\n")

    for obj, updates in planned:
        print(f"  {obj.name[:56]:58} {updates}")
    for obj, reason in skipped:
        print(f"  {obj.name[:56]:58} SKIPPED ({reason})")

    if not apply:
        print("\nDry run. Re-run with --apply to write.")
        return 0

    for obj, updates in planned:
        obj.metadata = {**obj.metadata, **updates}
        await obj.save()
    print(f"\nUpdated {len(planned)} objects.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.apply)))
```

- [ ] **Step 2: Confirm `sra.lookup` is the correct network-fetch entrypoint**

```bash
docker compose exec api python -c "from app.metadata import sra; print(sra.lookup)"
```

Expected: prints the function signature without error. If `sra.lookup` does
not exist under that name (the design doc's research referenced
`sra.lookup(accession)` from the module docstring in `enrich.py`, but confirm
the exact name here — it may be `sra.fetch` or similar), grep for it:

```bash
docker compose exec api python -c "from app.metadata import sra; print([n for n in dir(sra) if 'look' in n.lower() or 'fetch' in n.lower() or 'resolve' in n.lower()])"
```

Update the script's import/call in Step 1 to match whatever this reveals,
before proceeding.

- [ ] **Step 3: Dry-run against the real database**

```bash
docker exec -i biopipe-api-1 python - < backend/scripts/backfill_molecule_type.py
```

Expected: prints candidate counts and a per-object plan; no errors. Review
the printed plan — if `library_source` values look wrong for any real file,
stop and investigate before applying (this is exactly the "check against the
real database, not only unit tests" rule from this repo's CLAUDE.md).

- [ ] **Step 4: Apply**

```bash
docker exec -i biopipe-api-1 python - --apply < backend/scripts/backfill_molecule_type.py
```

Expected: "Updated N objects."

- [ ] **Step 5: Spot-check one backfilled object in the running app**

Open `http://localhost:5173`, navigate to a file that was in the dry-run's
plan, and confirm `Molecule type` and `Library source` now show values in
the Metadata tab's Experiment group.

- [ ] **Step 6: Re-run dry-run to confirm idempotence**

```bash
docker exec -i biopipe-api-1 python - < backend/scripts/backfill_molecule_type.py
```

Expected: "0 objects with an SRA accession and no molecule_type" (or however
many still genuinely lack a `library_source` in their SRA record) — not the
same N as Step 3, confirming the backfill actually wrote and won't
re-apply.

- [ ] **Step 7: Commit**

```bash
git add backend/scripts/backfill_molecule_type.py
git commit -m "chore(scripts): backfill molecule type and library source from SRA"
```

---

### Task 7: Manual UI verification

**Files:** none (verification only)

- [ ] **Step 1: Rebuild the running stack with all changes**

```bash
docker compose up -d --build api web worker
```

- [ ] **Step 2: Open the app and check the Metadata tab**

Navigate to `http://localhost:5173`, open a FASTQ file with an SRA
accession (e.g. the DRR1066343 example from the original request, after
Task 6's backfill has run against it). Confirm:
- `Molecule type` and `Library source` both appear in the Experiment group,
  positioned before `Assay`.
- Their values are sensible for that file (WGS → `Molecule type: DNA`,
  `Library source: Genomic`).

- [ ] **Step 3: Check the Infer button**

Confirm an "Infer" affordance appears next to `Molecule type` for a FASTQ
object. Click it and confirm:
- The field updates to a suggested value without a page reload.
- Some indication of the basis (e.g. "sampled 2000 reads, no U found") is
  visible near the field, not just a bare value.
- The value is not yet persisted — reload the page without saving and
  confirm the field reverts to whatever was stored before.

- [ ] **Step 4: Confirm the Infer button overwrites an existing value**

On a field that already shows an SRA-derived `DNA`, click Infer again and
confirm it replaces the value in the form (per the design's "always
overwrites" decision) rather than refusing or prompting.

- [ ] **Step 5: Save and confirm persistence**

Click Save after an Infer, reload the page, and confirm the inferred value
is now what's stored.

- [ ] **Step 6: Restart worker if any queue-handler-adjacent code was touched**

Not expected to be necessary for this plan (no `pipeline_handlers.py`
changes), but if anything unexpected required a change there:

```bash
docker compose restart worker
```

- [ ] **Step 7: No commit for this task** — verification only. If Steps 2-5
  reveal a bug, fix it as a new task/commit before considering the plan
  complete, rather than folding an undocumented fix into this checklist.

---

## Plan Self-Review Notes

**Spec coverage:**
- Three-field schema split → Task 1.
- SRA mapping (`library_source` → `library_source` + `molecule_type`) →
  Task 2, verified end-to-end → Task 3.
- No filename/content inference on ingest → satisfied by omission (no task
  adds one); `to_metadata()` only emits the new keys when
  `self.library_source` is set, same gating as `library_strategy`/`assay`
  today.
- Manual FASTQ inference button, sampling ~2000 reads, U-presence signal,
  gzip-transparent, never automatic → Task 4 (module) + Task 5 (endpoint) +
  Task 7 (UI wiring verification — see note below on frontend scope).
- Overwrite-on-infer, no confirmation → encoded in Task 5's endpoint
  docstring and Task 7's Step 4 verification.
- Backfill for existing files, one-time script not a standing job → Task 6.
- Tests per the design doc's test list → Tasks 1-6 each include their
  corresponding test file.

**Frontend scope note:** the design doc states no `SchemaMetadataEditor.tsx`
changes are needed for the two new *schema-driven* fields (Task 1) — true,
they render automatically via `fields_for()`. The Infer *button* is a
genuinely new frontend affordance, covered by Task 5.5 with exact TSX against
the file as it exists today (both `SchemaMetadataEditor.tsx` and its
`DetailPanel.tsx:1322-1334` call site were read in full during plan-writing):
new `objectId` prop, `inferMoleculeType` handler, a per-field render branch
for `molecule_type` on FASTQ objects, and the exact call-site diff. No
unresolved guesses remain in this task.

**Type consistency check:** `MoleculeTypeInferenceOut` (Task 5) fields
(`molecule_type: str | None`, `basis: str`) match `infer_molecule_type()`'s
return dict (Task 4) key-for-key. The backfill script (Task 6) reads
`derived.items()` from the same `to_metadata()` shape established in Task 2
(`molecule_type`/`library_source` string keys) — consistent throughout.
