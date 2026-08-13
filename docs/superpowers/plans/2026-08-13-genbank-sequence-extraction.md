# GenBank Sequence Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract a GenBank record's `ORIGIN` sequence into a first-class derived FASTA `DataObject` so it can be used as an alignment reference.

**Architecture:** A new streaming module (`pipelines/genbank_sequence.py`) does its own pass over the file, skipping the feature block and writing `ORIGIN` straight to an output handle — the inverse of `genbank_reader.py`, whose no-sequence-in-memory guarantee must not be weakened. A THREAD handler wraps it, an applier ingests the result as an `ObjectRole.REFERENCE` object with `derived_from` pointing at the GenBank, and a launcher guards against re-extraction by querying for that derived object rather than storing a flag.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor (MongoDB), pytest, React + TanStack Query, TypeScript.

**Spec:** `docs/superpowers/specs/2026-08-13-genbank-sequence-extraction-design.md`

---

## Background for the implementer

You likely have no context on this codebase. Read these before starting:

- `backend/app/pipelines/genbank_reader.py` — the existing streaming reader. Its docstring makes a **memory guarantee**: a record's `ORIGIN` block is "stepped over line by line and never accumulated." `build_annotation_db` depends on this. **Do not modify this file to accumulate sequence.** Task 2 lifts one helper out of it; that is the only change it gets.
- `backend/app/queue/annotation_handlers.py` — the two sibling handlers. Yours joins them.
- `backend/app/queue/results.py:1733-1785` (`_apply_export_annotation_subset`) — the applier you are copying. Yours differs in exactly one line: `role=ObjectRole.REFERENCE` instead of `ObjectRole.ANNOTATION`.
- `backend/app/services/pipeline_service.py:2580` (`launch_annotation_export`) — the launcher you are copying.

### The GenBank `ORIGIN` format

Verified against `backend/tests/fixtures/genbank/two_records.gbff`:

```
ORIGIN
        1 agcttttcat tctgactgca acgggcaata tgtctctgtg tggattaaaa aaagagtgtc
       61 tgatagcagc ttctgaactg gttacctgcc gtgagtaaat taaaatttta ttgacttagg
//
```

Each line is a right-aligned base counter, then up to six space-separated 10-base blocks. To recover sequence: strip the leading counter and remove all whitespace. The block ends at a line starting with `//`.

### Test fixtures that already exist

- `backend/tests/fixtures/genbank/two_records.gbff` — **two** `LOCUS` records but only **one** `ORIGIN` block. Accessions `NC_000001.3` and `NC_000002.1` (from `VERSION`).
- `backend/tests/fixtures/genbank/two_records.gbff.gz` — gzipped, for GS-6.
- `backend/tests/fixtures/genbank/ecoli_slice.gbff` — one record, `NC_000913.3`, 40000 bp.

Do not invent new fixtures unless a task says to.

### Running tests

**You are in a worktree.** A bare `docker compose exec api pytest` would silently test `main`'s code, not yours. Always use:

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_genbank_sequence.py -q
```

---

## File Structure

**Create:**
- `backend/app/pipelines/genbank_sequence.py` — streaming `ORIGIN` → FASTA. Pure file I/O; knows nothing about jobs, objects, or the database.
- `backend/tests/pipelines/test_genbank_sequence.py` — unit tests for the above.
- `backend/tests/services/test_genbank_sequence_launch.py` — guard tests.

**Modify:**
- `backend/app/pipelines/genbank_reader.py` — extract the accession helper (Task 2). No behavior change.
- `backend/app/queue/annotation_handlers.py` — add the `extract_genbank_sequence` handler.
- `backend/app/queue/results.py` — add `_apply_extract_genbank_sequence` and register it.
- `backend/app/services/pipeline_service.py` — add `launch_extract_genbank_sequence` and the guard.
- `backend/app/api/v1/pipelines.py` — add POST and GET endpoints.
- `backend/app/pipelines/node_types.py` — add the `EXCLUDED_LAUNCHES` entry.
- `frontend/src/api/types.ts` — add `genbank_has_sequence` and the derived-reference response type.
- `frontend/src/api/client.ts` — add the two API calls.
- `frontend/src/components/AnnotationFeatureTable.tsx` — render the three states.

---

## Task 1: The sequence-line parser

The smallest testable unit: turning one `ORIGIN` line into bases. Isolated first so the streaming logic in Task 3 has a verified primitive.

**Files:**
- Create: `backend/app/pipelines/genbank_sequence.py`
- Test: `backend/tests/pipelines/test_genbank_sequence.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_genbank_sequence.py`:

```python
"""Unit tests for GenBank ORIGIN sequence extraction."""

from app.pipelines import genbank_sequence


class TestSequenceLine:
    def test_strips_counter_and_spaces(self):
        line = "        1 agcttttcat tctgactgca acgggcaata"
        assert genbank_sequence.sequence_line_bases(line) == (
            "agcttttcattctgactgcaacgggcaata"
        )

    def test_handles_a_later_counter(self):
        line = "       61 tgatagcagc ttctgaactg"
        assert genbank_sequence.sequence_line_bases(line) == "tgatagcagcttctgaactg"

    def test_blank_line_yields_nothing(self):
        assert genbank_sequence.sequence_line_bases("   ") == ""

    def test_line_without_counter_still_reads(self):
        # Not every writer emits the counter; the bases are what matter.
        assert genbank_sequence.sequence_line_bases("agct tttc") == "agcttttc"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_genbank_sequence.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.genbank_sequence'`

- [ ] **Step 3: Write the minimal implementation**

Create `backend/app/pipelines/genbank_sequence.py`:

```python
"""Extraction of a GenBank record's ORIGIN block into FASTA.

The inverse of `genbank_reader`, and deliberately a separate module. That
reader guarantees it never accumulates sequence, and `build_annotation_db`
depends on the guarantee; this module exists to do the one thing that
guarantee forbids. Two passes with opposite priorities are easier to reason
about than one pass with a mode flag.

Memory here is bounded the same way: a sequence line is written to the output
handle as it is read, so a 300MB ORIGIN block never becomes a 300MB string.
"""


def sequence_line_bases(line: str) -> str:
    """The bases on one ORIGIN line.

    A line is a right-aligned base counter followed by up to six
    space-separated 10-base blocks:

        1 agcttttcat tctgactgca acgggcaata

    Dropping the leading numeric token and removing whitespace recovers the
    sequence. A line with no counter is read as all bases, since the counter
    is a convenience for human readers rather than something to rely on.
    """
    parts = line.split()
    if not parts:
        return ""
    if parts[0].isdigit():
        parts = parts[1:]
    return "".join(parts)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_genbank_sequence.py -q`
Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/genbank_sequence.py backend/tests/pipelines/test_genbank_sequence.py
git commit -m "feat(pipelines): parse bases out of a GenBank ORIGIN line"
```

---

## Task 2: Share the accession helper (GS-4)

Both modules must name a contig identically. Spec D-3: this is shared code, not copied code, because drift would be silent — a GenBank and its extracted FASTA disagreeing on contig names breaks length matching.

**Files:**
- Modify: `backend/app/pipelines/genbank_reader.py`
- Test: `backend/tests/pipelines/test_genbank_sequence.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_genbank_sequence.py`:

```python
from app.pipelines import genbank_reader


class TestAccessionFor:
    def test_prefers_version(self):
        assert genbank_reader.accession_for(
            version="NC_000001.3", accession="NC_000001", locus_name="NC_000001"
        ) == "NC_000001.3"

    def test_falls_back_to_accession(self):
        assert genbank_reader.accession_for(
            version="", accession="NC_000001", locus_name="OTHER"
        ) == "NC_000001"

    def test_falls_back_to_locus_name(self):
        assert genbank_reader.accession_for(
            version="", accession="", locus_name="OTHER"
        ) == "OTHER"

    def test_falls_back_to_unknown(self):
        assert genbank_reader.accession_for(
            version="", accession="", locus_name=""
        ) == "unknown"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_genbank_sequence.py -q`
Expected: FAIL — `AttributeError: module 'app.pipelines.genbank_reader' has no attribute 'accession_for'`

- [ ] **Step 3: Add the helper**

In `backend/app/pipelines/genbank_reader.py`, add this function at module level, immediately after the `_open_text` function (before `iter_records`):

```python
def accession_for(*, version: str, accession: str, locus_name: str) -> str:
    """Name a contig: VERSION, then ACCESSION, then the LOCUS name.

    Shared with `genbank_sequence` rather than duplicated there. The versioned
    accession is what NCBI's paired FASTA uses in its deflines, so a GenBank
    and its sibling FASTA agree on contig names -- which they must, because
    contig lengths may arrive from a reference's facts and are matched by
    name. Two copies of this fallback would drift, and the drift would be
    silent: lengths would simply stop matching.
    """
    return version or accession or locus_name or "unknown"
```

- [ ] **Step 4: Use it in `flush()`**

In `iter_records`, replace this line inside `flush()`:

```python
        record.accession = version or accession or locus_name or "unknown"
```

with:

```python
        record.accession = accession_for(
            version=version, accession=accession, locus_name=locus_name
        )
```

Also update `flush()`'s docstring: replace the first line `"""Close the current record, naming its contig.` and the paragraph beneath it with:

```python
        """Close the current record, naming its contig via `accession_for`."""
```

(The rationale that used to live here now lives on `accession_for` itself, where the other caller can see it.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_genbank_sequence.py tests/pipelines/test_genbank_reader.py -q`
Expected: PASS — the new accession tests plus every existing reader test. **If any existing reader test fails, you changed behavior; revert and retry.**

- [ ] **Step 6: Commit**

```bash
git add backend/app/pipelines/genbank_reader.py backend/tests/pipelines/test_genbank_sequence.py
git commit -m "refactor(pipelines): share GenBank contig naming between reader and extractor"
```

---

## Task 3: Stream a GenBank file to FASTA (GS-1, GS-2, GS-3, GS-5, GS-6, GS-7)

**Files:**
- Modify: `backend/app/pipelines/genbank_sequence.py`
- Test: `backend/tests/pipelines/test_genbank_sequence.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_genbank_sequence.py`:

```python
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent.parent / "fixtures" / "genbank"


def _read_fasta(path: Path) -> list[tuple[str, str]]:
    """Parse a FASTA into (defline, sequence) pairs, for assertions."""
    records: list[tuple[str, str]] = []
    name = None
    chunks: list[str] = []
    for line in path.read_text().splitlines():
        if line.startswith(">"):
            if name is not None:
                records.append((name, "".join(chunks)))
            name = line[1:]
            chunks = []
        else:
            chunks.append(line)
    if name is not None:
        records.append((name, "".join(chunks)))
    return records


class TestWriteFasta:
    def test_writes_one_record_from_ecoli_slice(self, tmp_path):
        dest = tmp_path / "out.fna"
        count = genbank_sequence.write_fasta(
            source=FIXTURES / "ecoli_slice.gbff", dest=dest
        )
        assert count == 1
        records = _read_fasta(dest)
        assert len(records) == 1
        assert records[0][0] == "NC_000913.3"
        assert set(records[0][1]) <= set("acgtnACGTN")

    def test_uses_versioned_accession_as_defline(self, tmp_path):
        dest = tmp_path / "out.fna"
        genbank_sequence.write_fasta(
            source=FIXTURES / "two_records.gbff", dest=dest
        )
        # two_records.gbff has two LOCUS records but only one ORIGIN block,
        # so only the record carrying sequence is emitted.
        assert [name for name, _ in _read_fasta(dest)] == ["NC_000001.3"]

    def test_wraps_at_sixty_columns(self, tmp_path):
        dest = tmp_path / "out.fna"
        genbank_sequence.write_fasta(
            source=FIXTURES / "ecoli_slice.gbff", dest=dest
        )
        body = [l for l in dest.read_text().splitlines() if not l.startswith(">")]
        assert all(len(line) <= 60 for line in body)
        # Every line but the last of a record must be exactly full.
        assert all(len(line) == 60 for line in body[:-1])

    def test_reads_gzipped_input(self, tmp_path):
        plain = tmp_path / "plain.fna"
        gzipped = tmp_path / "gz.fna"
        genbank_sequence.write_fasta(
            source=FIXTURES / "two_records.gbff", dest=plain
        )
        genbank_sequence.write_fasta(
            source=FIXTURES / "two_records.gbff.gz", dest=gzipped
        )
        assert plain.read_text() == gzipped.read_text()

    def test_no_sequence_yields_zero_records(self, tmp_path):
        source = tmp_path / "featureless.gbff"
        source.write_text(
            "LOCUS       NC_000003               10 bp    DNA     linear\n"
            "VERSION     NC_000003.1\n"
            "FEATURES             Location/Qualifiers\n"
            "     gene            1..10\n"
            "//\n"
        )
        dest = tmp_path / "out.fna"
        assert genbank_sequence.write_fasta(source=source, dest=dest) == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_genbank_sequence.py -q`
Expected: FAIL — `AttributeError: module 'app.pipelines.genbank_sequence' has no attribute 'write_fasta'`

- [ ] **Step 3: Write the implementation**

Add to `backend/app/pipelines/genbank_sequence.py`. Add these imports at the top of the file, below the docstring:

```python
import gzip
from pathlib import Path
from typing import TextIO

from app.pipelines.genbank_reader import accession_for

# FASTA convention, and what NCBI emits.
_WRAP = 60
```

Then add these functions below `sequence_line_bases`:

```python
def _open_text(path: Path) -> TextIO:
    """Gzip-aware line reader.

    Sniffed by magic bytes rather than extension, matching
    `genbank_reader._open_text`: a file downloaded from NCBI is gzipped
    whether or not whoever renamed it kept the suffix.
    """
    with open(path, "rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", errors="replace")
    return open(path, errors="replace")


class _WrappedWriter:
    """Writes bases at a fixed column width without buffering the record.

    The carry is at most `_WRAP` characters, which is what keeps this
    module's memory flat: a 300MB ORIGIN block is written as it is read
    rather than assembled into a 300MB string first.
    """

    def __init__(self, fh: TextIO):
        self._fh = fh
        self._carry = ""

    def write(self, bases: str) -> None:
        chunk = self._carry + bases
        cut = len(chunk) - (len(chunk) % _WRAP)
        for i in range(0, cut, _WRAP):
            self._fh.write(chunk[i : i + _WRAP] + "\n")
        self._carry = chunk[cut:]

    def finish(self) -> None:
        """Flush the trailing partial line, if any."""
        if self._carry:
            self._fh.write(self._carry + "\n")
            self._carry = ""


def write_fasta(*, source: Path, dest: Path) -> int:
    """Write every ORIGIN block in `source` to `dest` as FASTA.

    Returns the number of records written, which is not the number of records
    in the file: a record with no ORIGIN block contributes nothing. A caller
    that needs "this file had no sequence at all" checks for a zero return.

    One pass, streaming both ways. The feature block is skipped rather than
    parsed -- that is `genbank_reader`'s job, and doing it again here would
    make this module the second place that has to be right about qualifiers.
    """
    written = 0
    version = accession = locus_name = ""
    in_origin = False
    writer: _WrappedWriter | None = None

    with _open_text(source) as fh, open(dest, "w") as out:
        for raw in fh:
            line = raw.rstrip("\n")

            if line.startswith("LOCUS"):
                if writer is not None:
                    writer.finish()
                    writer = None
                in_origin = False
                version = accession = locus_name = ""
                parts = line.split()
                if len(parts) > 1:
                    locus_name = parts[1]
                continue

            if line.startswith("//"):
                if writer is not None:
                    writer.finish()
                    writer = None
                in_origin = False
                continue

            if line.startswith("ORIGIN"):
                in_origin = True
                name = accession_for(
                    version=version, accession=accession, locus_name=locus_name
                )
                out.write(f">{name}\n")
                writer = _WrappedWriter(out)
                written += 1
                continue

            if in_origin:
                if writer is not None:
                    writer.write(sequence_line_bases(line))
                continue

            if line.startswith("VERSION"):
                parts = line.split()
                if len(parts) > 1:
                    version = parts[1]
                continue

            if line.startswith("ACCESSION"):
                parts = line.split()
                if len(parts) > 1:
                    accession = parts[1]
                continue

        if writer is not None:
            writer.finish()

    return written
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_genbank_sequence.py -q`
Expected: PASS, 13 passed

- [ ] **Step 5: Add the agreement test (GS-4)**

This is the test that catches Task 2's helper drifting apart from its callers. Append to the test file:

```python
class TestAgreesWithReader:
    """GS-4: the two modules must name the same record identically.

    The reason this matters is in `accession_for`'s docstring: contig lengths
    are matched by name between a GenBank and its extracted FASTA.
    """

    @pytest.mark.parametrize(
        "fixture", ["ecoli_slice.gbff", "two_records.gbff"]
    )
    def test_accessions_match(self, fixture, tmp_path):
        source = FIXTURES / fixture
        dest = tmp_path / "out.fna"
        genbank_sequence.write_fasta(source=source, dest=dest)

        extracted = [name for name, _ in _read_fasta(dest)]
        from_reader = [
            r.accession
            for r in genbank_reader.iter_records(source)
            if r.has_sequence
        ]
        assert extracted == from_reader
```

- [ ] **Step 6: Run it**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_genbank_sequence.py -q`
Expected: PASS, 15 passed

- [ ] **Step 7: Commit**

```bash
git add backend/app/pipelines/genbank_sequence.py backend/tests/pipelines/test_genbank_sequence.py
git commit -m "feat(pipelines): stream a GenBank ORIGIN block into wrapped FASTA"
```

---

## Task 4: The handler (GS-8, GS-9)

**Files:**
- Modify: `backend/app/queue/annotation_handlers.py`
- Test: `backend/tests/pipelines/test_genbank_sequence.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_genbank_sequence.py`:

```python
from app.config import settings
from app.errors import PermanentError
from app.queue import annotation_handlers
from app.queue.registry import JobContext


def _ctx(payload: dict) -> JobContext:
    """A real JobContext, matching test_annotation_contig_lengths_fact.py.

    The real class rather than a fake: a hand-rolled stand-in drifts from
    JobContext silently, and these handlers are cheap to drive for real.
    """
    return JobContext(
        job_id="j1",
        payload=payload,
        epoch=1,
        attempts=1,
        owner="local",
    )


class TestExtractHandler:
    def test_rejects_a_file_with_no_sequence(self, tmp_path, monkeypatch):
        # Redirects _prepare_workdir's output under tmp_path, the same way the
        # existing annotation handler tests isolate their scratch space.
        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
        source = tmp_path / "featureless.gbff"
        source.write_text(
            "LOCUS       NC_000003               10 bp    DNA     linear\n"
            "VERSION     NC_000003.1\n"
            "//\n"
        )
        ctx = _ctx(
            {
                "object_id": "507f1f77bcf86cd799439011",
                "genbank_path": str(source),
                "output_name": "out.fna",
            }
        )
        with pytest.raises(PermanentError, match="no sequence"):
            annotation_handlers.extract_genbank_sequence(ctx)

    def test_writes_the_fasta_and_reports_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
        ctx = _ctx(
            {
                "object_id": "507f1f77bcf86cd799439011",
                "genbank_path": str(FIXTURES / "ecoli_slice.gbff"),
                "output_name": "ecoli_slice.fna",
            }
        )
        result = annotation_handlers.extract_genbank_sequence(ctx)
        assert result["record_count"] == 1
        assert result["output"]["name"] == "ecoli_slice.fna"
        assert Path(result["output"]["tmp_path"]).exists()
```

If `settings.bioinfo_home` turns out not to redirect `settings.tmp_dir` on your
branch, check how `_prepare_workdir` resolves `settings.tmp_dir`
(`backend/app/queue/pipeline_handlers.py:802`) and patch whichever attribute it
actually reads.

- [ ] **Step 2: Run the test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_genbank_sequence.py -q`
Expected: FAIL — `AttributeError: module 'app.queue.annotation_handlers' has no attribute 'extract_genbank_sequence'`

- [ ] **Step 3: Add the handler**

In `backend/app/queue/annotation_handlers.py`, add `genbank_sequence` to the existing `from app.pipelines import (...)` block, keeping alphabetical order — it goes between `genbank_reader` and any later entry:

```python
from app.pipelines import (
    annotation_db,
    annotation_export,
    annotation_hierarchy,
    annotation_parse,
    annotation_stats,
    genbank_parse,
    genbank_reader,
    genbank_sequence,
)
```

Then append this handler at the end of the file:

```python
@handler(
    "extract_genbank_sequence",
    # THREAD for the same reason its two siblings are: the work is file I/O
    # in this process, with no binary to spawn or kill via process group.
    mode=HandlerMode.THREAD,
    job_class=JobClass.COMPUTE,
    # 512MB regardless of input size. genbank_sequence streams, so memory is
    # flat in the size of the ORIGIN block -- a 300MB sequence costs no more
    # here than a 300KB one.
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
)
def extract_genbank_sequence(ctx: JobContext) -> dict:
    """Write a GenBank file's ORIGIN sequence out as a FASTA reference."""
    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("extract_genbank_sequence requires an 'object_id'")

    source = Path(ctx.payload["genbank_path"])
    if not source.exists():
        raise PermanentError(f"genbank file is missing: {source}")

    # _prepare_workdir, not a bare tmp path: it puts the output under
    # settings.tmp_dir, which shares a filesystem with objects/, so ingesting
    # the finished file is an atomic rename rather than a copy of what may be
    # a very large FASTA. It also wipes the directory on entry, so a retry
    # does not inherit a half-written file.
    work = _prepare_workdir(ctx, "genbank_sequence")
    dest = work / ctx.payload["output_name"]

    ctx.progress(phase="extract", pct=0.1, message="extracting sequence")
    written = genbank_sequence.write_fasta(source=source, dest=dest)

    # Read from the file rather than trusting the `genbank_has_sequence` fact
    # that offered this action: the fact was recorded by an earlier job and
    # the file may have been replaced since.
    if written == 0:
        raise PermanentError(
            "this genbank file contains no sequence to extract"
        )

    log.info(
        "genbank_sequence_extracted",
        object_id=str(object_id),
        records=written,
    )
    return {
        "object_id": str(object_id),
        "record_count": written,
        "output": {"tmp_path": str(dest), "name": ctx.payload["output_name"]},
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_genbank_sequence.py -q`
Expected: PASS, 17 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/annotation_handlers.py backend/tests/pipelines/test_genbank_sequence.py
git commit -m "feat(pipelines): add the extract_genbank_sequence handler"
```

---

## Task 5: The applier (GS-10 … GS-14, GS-16)

**Files:**
- Modify: `backend/app/queue/results.py`

- [ ] **Step 1: Add the applier**

In `backend/app/queue/results.py`, add this immediately after `_apply_export_annotation_subset` (which ends at line 1785):

```python
async def _apply_extract_genbank_sequence(result: dict, *, owner: str) -> None:
    """Register a GenBank's extracted sequence as a new reference.

    The same shape as `_apply_export_annotation_subset` above, with one
    material difference: `role=ObjectRole.REFERENCE`, not ANNOTATION. That
    role is the whole point -- it is what puts the extracted FASTA in every
    reference picker, which is what makes the GenBank's sequence usable for
    alignment (#348). No sidecar role: this is a first-class object a person
    chooses, not scaffolding the explorer hides.
    """
    from app.services import object_service, run_service

    object_id = result.get("object_id")
    output = result.get("output")
    if not output or not object_id:
        return

    source = await DataObject.get(PydanticObjectId(object_id))
    if source is None:
        log.warning("genbank_sequence_parent_missing", object_id=object_id)
        return

    job_id = result.get("job_id")

    try:
        reference = await object_service.ingest_local_file(
            owner=source.owner,
            project_id=source.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.REFERENCE,
            derived_from=[source.id],
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts={"genbank_source_record_count": result.get("record_count")},
            # The extracted sequence describes the same biology as its source.
            metadata=dict(source.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error(
            "genbank_sequence_ingest_failed", object_id=object_id, error=str(e)
        )
        return

    run_id = await run_service.run_for_job(PydanticObjectId(job_id)) if job_id else None
    if run_id is not None:
        await run_service.record_outputs(run_id, [reference.id], owner=reference.owner)

    log.info(
        "genbank_sequence_applied",
        object_id=object_id,
        reference_id=str(reference.id),
    )
```

- [ ] **Step 2: Register it**

In the applier map (around line 2767, where `"export_annotation_subset": _apply_export_annotation_subset,` appears), add directly beneath that entry:

```python
    "extract_genbank_sequence": _apply_extract_genbank_sequence,
```

- [ ] **Step 3: Verify the module imports cleanly**

Run: `./backend/run-worktree-tests.sh tests/queue/ -q`
Expected: PASS — no collection errors. An `ImportError` or `NameError` here means a symbol used in the applier is not imported in this module; check `ObjectRole` and `Path` are already imported at the top (they are, for the sibling applier).

- [ ] **Step 4: Commit**

```bash
git add backend/app/queue/results.py
git commit -m "feat(pipelines): ingest an extracted GenBank sequence as a reference"
```

---

## Task 6: The launcher and its guard (GS-17 … GS-20)

**Files:**
- Modify: `backend/app/services/pipeline_service.py`
- Test: `backend/tests/services/test_genbank_sequence_launch.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_genbank_sequence_launch.py`:

```python
"""Guard tests for GenBank sequence extraction (#348, GS-17..GS-20).

The guard reads the world rather than a stored flag: it asks whether a
REFERENCE object derived from this GenBank exists. These tests pin the two
consequences that fall out of that choice -- a deleted reference makes the
action available again, and a renamed one does not.
"""

import pytest
import pytest_asyncio
from beanie import PydanticObjectId

from app.models import DataObject, ObjectRole, ObjectStatus
from app.services import pipeline_service
from tests.services import helpers


@pytest_asyncio.fixture
async def project(beanie_models):
    return await helpers.make_project(f"gb-proj-{PydanticObjectId()}")


async def _object(project, *, name, role, derived_from=None):
    """Insert a minimal DataObject.

    No Blob: `existing_extracted_sequence` is a pure query over the objects
    collection and never resolves content, so blob plumbing would be noise.
    """
    obj = DataObject(
        project_id=project.id,
        owner=project.owner,
        name=name,
        size=100,
        status=ObjectStatus.READY,
        role=role,
        derived_from=derived_from or [],
    )
    await obj.insert()
    return obj


@pytest.mark.asyncio
class TestExistingExtraction:
    async def test_none_when_nothing_derived(self, project):
        gb = await _object(project, name="x.gbff", role=ObjectRole.ANNOTATION)
        assert await pipeline_service.existing_extracted_sequence(gb.id) is None

    async def test_finds_a_derived_reference(self, project):
        gb = await _object(project, name="x.gbff", role=ObjectRole.ANNOTATION)
        ref = await _object(
            project, name="x.fna", role=ObjectRole.REFERENCE, derived_from=[gb.id]
        )
        found = await pipeline_service.existing_extracted_sequence(gb.id)
        assert found is not None and found.id == ref.id

    async def test_a_rename_does_not_hide_it(self, project):
        """GS-20: the query keys on derived_from and role, never on name."""
        gb = await _object(project, name="x.gbff", role=ObjectRole.ANNOTATION)
        ref = await _object(
            project,
            name="renamed-by-user.fna",
            role=ObjectRole.REFERENCE,
            derived_from=[gb.id],
        )
        found = await pipeline_service.existing_extracted_sequence(gb.id)
        assert found is not None and found.id == ref.id

    async def test_ignores_a_derived_object_of_another_role(self, project):
        """An exported annotation subset is also derived_from the GenBank."""
        gb = await _object(project, name="x.gbff", role=ObjectRole.ANNOTATION)
        await _object(
            project,
            name="subset.gff",
            role=ObjectRole.ANNOTATION,
            derived_from=[gb.id],
        )
        assert await pipeline_service.existing_extracted_sequence(gb.id) is None
```

This uses `helpers.make_project` and the explicit `DataObject(...)` construction
that `backend/tests/services/conftest.py` already uses (see its
`vcf_stats_object_factory`), rather than a shared object fixture — there is no
`make_object` fixture in this repo.

- [ ] **Step 2: Run the test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_genbank_sequence_launch.py -q`
Expected: FAIL — `AttributeError: module 'app.services.pipeline_service' has no attribute 'existing_extracted_sequence'`

- [ ] **Step 3: Add the guard query and the launcher**

In `backend/app/services/pipeline_service.py`, add both functions immediately after `launch_annotation_export` (which ends at line 2616):

```python
async def existing_extracted_sequence(
    object_id: PydanticObjectId,
) -> DataObject | None:
    """The FASTA reference already extracted from this GenBank, if any.

    The whole of #348's idempotency guard. Deliberately a query rather than a
    fact on the GenBank: a stored "already extracted" flag would go stale the
    moment the user deleted the reference, and deletion here is hard, not
    soft (`object_service.delete_object`), so asking the database is both
    simpler and self-healing. Keyed on derived_from and role, never on name,
    so renaming the reference does not produce a second extraction.

    Filtered by role because an exported annotation subset is also
    `derived_from` the same GenBank; only a REFERENCE is this file's
    extracted sequence.
    """
    return await DataObject.find(
        DataObject.derived_from == object_id,
        DataObject.role == ObjectRole.REFERENCE,
    ).first_or_none()


async def launch_extract_genbank_sequence(
    *, object_id: PydanticObjectId, owner: str
):
    """Queue extraction of a GenBank's ORIGIN sequence into a FASTA reference.

    Derives an object, so the job's result goes through
    `_apply_extract_genbank_sequence`. Refuses rather than queueing when one
    already exists: extraction takes no parameters, so a second run would
    write a byte-identical duplicate of a possibly very large reference and
    put two indistinguishable entries in every picker.
    """
    from app.queue import queue

    gb = await object_service.get_object(object_id, owner=owner)
    _check_annotation_stats_callable(gb)

    if gb.format.kind is not FormatKind.GENBANK:
        raise ValidationError(
            f"{gb.name!r} is {gb.format.kind.value}, not a GenBank file",
            details={"object_id": str(gb.id), "kind": gb.format.kind.value},
        )

    existing = await existing_extracted_sequence(gb.id)
    if existing is not None:
        raise ConflictError(
            f"{gb.name!r} already has an extracted sequence: {existing.name!r}",
            details={
                "object_id": str(gb.id),
                "reference_id": str(existing.id),
                "reference_name": existing.name,
            },
        )

    digest, path = await _resolve_readable(gb)
    # Same reasoning as launch_annotation_stats: the THREAD handler reads
    # ctx.payload["genbank_path"] directly and does no blob resolution.
    path = path or str(blob_path(digest))

    stem = Path(gb.name).stem
    # A .gbff.gz leaves a .gbff behind after one stem strip.
    if stem.endswith(".gbff") or stem.endswith(".gb"):
        stem = Path(stem).stem

    return await queue.enqueue(
        "extract_genbank_sequence",
        owner=owner,
        payload={
            "object_id": str(gb.id),
            "genbank_path": path,
            "output_name": f"{stem}.fna",
        },
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
        max_attempts=2,
        dedup_key=f"genbank_sequence:{gb.id}",
        project_id=gb.project_id,
        object_id=gb.id,
    )
```

**Check the imports at the top of `pipeline_service.py` before running.** This code uses `ConflictError`, `FormatKind`, `ObjectRole`, `DataObject`, and `Path`. Most are already imported for neighbouring functions; add whichever are missing to the existing import blocks rather than adding new ones. Verify with:

```bash
grep -n "ConflictError\|FormatKind\|^from pathlib" backend/app/services/pipeline_service.py | head
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_genbank_sequence_launch.py -q`
Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/services/test_genbank_sequence_launch.py
git commit -m "feat(pipelines): guard GenBank sequence extraction against a second run"
```

---

## Task 7: The endpoints

**Files:**
- Modify: `backend/app/api/v1/pipelines.py`

- [ ] **Step 1: Add both endpoints**

In `backend/app/api/v1/pipelines.py`, add after the `get_annotation_export_count` endpoint:

```python
class GenBankSequenceRequest(BaseModel):
    object_id: PydanticObjectId


@router.get("/genbanksequence/{object_id}")
async def get_extracted_sequence(
    object_id: PydanticObjectId, owner: OwnerDep
) -> dict:
    """The reference already extracted from this GenBank, or nulls.

    The same query the launcher's guard runs, exposed so the Results tab's
    control and the launcher cannot disagree about whether extraction has
    happened (GS-25).
    """
    await object_service.get_object(object_id, owner=owner)
    existing = await pipeline_service.existing_extracted_sequence(object_id)
    if existing is None:
        return {"reference_id": None, "reference_name": None}
    return {
        "reference_id": str(existing.id),
        "reference_name": existing.name,
    }


@router.post(
    "/genbanksequence", response_model=JobOut, status_code=status.HTTP_201_CREATED
)
async def launch_extract_genbank_sequence(
    body: GenBankSequenceRequest, owner: OwnerDep
) -> JobOut:
    """Queue extraction of a GenBank's ORIGIN sequence into a FASTA reference."""
    job = await pipeline_service.launch_extract_genbank_sequence(
        object_id=body.object_id, owner=owner
    )
    return JobOut.of(job)
```

- [ ] **Step 2: Verify the routes load**

Run: `./backend/run-worktree-tests.sh tests/api/ -q`
Expected: PASS — no collection or route-registration errors.

- [ ] **Step 3: Commit**

```bash
git add backend/app/api/v1/pipelines.py
git commit -m "feat(api): add endpoints to extract and look up a GenBank's sequence"
```

---

## Task 8: Classify the launcher (GS-26)

**Files:**
- Modify: `backend/app/pipelines/node_types.py`

- [ ] **Step 1: Run the exhaustiveness test to see it fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py -q`
Expected: FAIL — `test_every_launch_function_is_classified` reports
`pipeline_service.launch_extract_genbank_sequence` as unclassified.

- [ ] **Step 2: Add the exclusion**

In `backend/app/pipelines/node_types.py`, add to the `EXCLUDED_LAUNCHES` set, directly after the `launch_annotation_export` entry:

```python
        # User-triggered from the Results tab of a GenBank annotation, with no
        # parameters at all -- the same class as launch_annotation_export
        # above, and like it, it derives an object. Not a graph step: the
        # input is one specific GenBank the user is looking at, and the
        # output is a reference that any downstream node picks up by role
        # rather than by wiring.
        # TODO(#371): revisit alongside its siblings if a canvas node type
        # for on-demand annotation work is designed.
        "pipeline_service.launch_extract_genbank_sequence",
```

- [ ] **Step 3: Run the WHOLE exhaustiveness class**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py::TestExhaustiveness -v`
Expected: PASS — **every** test in the class, not just the one from Step 1.

This is not a formality. CLAUDE.md records that #355 added a `NodeTypeSpec` and an exclusion for the same launcher in two commits: each satisfied the test its own issue named, while together they broke `test_no_launcher_is_both_used_and_excluded` in the same class. That test stayed red until someone ran the whole file. If it fails here, you have added this launcher to `NODE_TYPES` as well as `EXCLUDED_LAUNCHES` — remove the `NODE_TYPES` entry; this launcher is excluded only.

- [ ] **Step 4: Commit**

```bash
git add backend/app/pipelines/node_types.py
git commit -m "fix(pipelines): classify the GenBank sequence launcher as excluded"
```

---

## Task 9: Frontend types and API client

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Add the fact to the facts type**

In `frontend/src/api/types.ts`, find `genbank_record_count?: number;` (line ~2083) and add directly beneath it:

```typescript
  genbank_has_sequence?: boolean;
```

This fact has been written by the backend since #294 and read by nothing; this is the first consumer.

- [ ] **Step 2: Add the response type**

In the same file, near the other pipeline response types, add:

```typescript
export interface ExtractedSequence {
  reference_id: string | null;
  reference_name: string | null;
}
```

- [ ] **Step 3: Add the two API calls**

In `frontend/src/api/client.ts`, follow the existing export/annotation-stats call style exactly (match how neighbouring functions build the URL and handle responses — check `annotationstats` calls first):

```typescript
export async function getExtractedSequence(
  objectId: string,
): Promise<ExtractedSequence> {
  return http<ExtractedSequence>(`/pipelines/genbanksequence/${objectId}`);
}

export async function extractGenBankSequence(objectId: string): Promise<Job> {
  return http<Job>("/pipelines/genbanksequence", {
    method: "POST",
    body: JSON.stringify({ object_id: objectId }),
  });
}
```

Adjust the helper name (`http`), the import of `ExtractedSequence` and `Job`, and the body/serialization convention to match what the surrounding functions in that file actually do.

- [ ] **Step 4: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors mentioning `genbanksequence`, `ExtractedSequence`, or `genbank_has_sequence`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat(frontend): add the GenBank sequence extraction API calls"
```

---

## Task 10: The Results tab control (GS-21 … GS-25)

**Files:**
- Modify: `frontend/src/components/AnnotationFeatureTable.tsx`

Context: line ~197 already computes `const isGenBank = facts.genbank_record_count != null;` and uses it to **hide** the export control, because GenBank features are not line-addressable. Your control renders in that same GenBank branch — a slot that is empty today.

- [ ] **Step 1: Add the query**

Near the existing `exportCountQuery` (line ~208), add:

```typescript
  // Only for GenBank files that actually carry sequence. The same query the
  // launcher's guard runs, so the button and the server cannot disagree
  // about whether extraction has already happened.
  const hasSequence = isGenBank && facts.genbank_has_sequence === true;
  const extractedQuery = useQuery({
    queryKey: ["genbanksequence", objectId],
    queryFn: () => getExtractedSequence(objectId),
    enabled: hasSequence,
  });
```

- [ ] **Step 2: Add the mutation**

Near the existing `exportMutation` (line ~239), add:

```typescript
  const extractMutation = useMutation({
    mutationFn: () => extractGenBankSequence(objectId),
    onSuccess: () => {
      // The job is queued, not finished; the object appears when the applier
      // runs, so both this query and the project's object list must refetch.
      qc.invalidateQueries({ queryKey: ["genbanksequence", objectId] });
      qc.invalidateQueries({ queryKey: ["objects"] });
    },
  });
```

Match the exact invalidation keys the neighbouring `exportMutation` uses for the object list — read it first rather than assuming `["objects"]`.

- [ ] **Step 3: Render the three states**

In the JSX, in the GenBank branch where the export control is hidden, add:

```tsx
{hasSequence && (
  <div className="mt-3">
    {extractedQuery.data?.reference_id ? (
      <span className="text-sm text-muted">
        Sequence extracted →{" "}
        <a href={`/objects/${extractedQuery.data.reference_id}`}>
          {extractedQuery.data.reference_name}
        </a>
      </span>
    ) : (
      <button
        onClick={() => extractMutation.mutate()}
        disabled={extractMutation.isPending || extractedQuery.isLoading}
      >
        {extractMutation.isPending
          ? "Queuing extraction…"
          : "Extract sequence"}
      </button>
    )}
  </div>
)}
```

Match the surrounding className and button conventions — copy them from the export control rather than using the placeholders above verbatim. Check how other components link to an object (`/objects/:id` may not be the route); grep for an existing object link in `ProjectExplorer.tsx` and use that.

- [ ] **Step 4: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/AnnotationFeatureTable.tsx
git commit -m "feat(frontend): offer sequence extraction on a GenBank's Results tab"
```

---

## Task 11: Full suite and manual verification (GS-15)

- [ ] **Step 1: Run the whole backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: all pass. **Read the count, not the exit code** — CLAUDE.md is explicit that "green" means reading the number.

If unrelated DB-touching tests fail in a rotating pattern, another worktree stack is sharing Mongo. Check with `./ops/worktree-up.sh --list` before assuming your change caused it.

- [ ] **Step 2: Bring up this worktree's stack**

```bash
./ops/worktree-up.sh
```

UI on 5273, API on 8100. Do **not** use plain `docker compose` from a worktree — it would repoint the main 5173 stack at this branch.

- [ ] **Step 3: Verify the real path (GS-15)**

This is the step no unit test substitutes for. In the browser at localhost:5273:

1. Open a project with a GenBank file that has sequence (upload `backend/tests/fixtures/genbank/ecoli_slice.gbff` if none exists).
2. Compute its annotation results, open the Results tab, confirm "Extract sequence" appears.
3. Click it, wait for the job.
4. Confirm the control is replaced by a link to the new reference.
5. **Open an alignment dialog and confirm the extracted FASTA is selectable as a reference.** GS-15 is the requirement at risk: a reference that no picker offers passes every test in this plan. CLAUDE.md records the precedent — suggestion rules passed a green suite while refusing to align a project with one usable reference.
6. Reload the page and confirm the link persists, not the button.

- [ ] **Step 4: Tear the stack down**

```bash
./ops/worktree-up.sh --down
```

A stack you brought up for testing is yours to bring down.

- [ ] **Step 5: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --fill
```

Then label it `type:feature`, plus `area:backend`, `area:frontend`, `area:pipelines`, and `area:provenance` (matching #348's labels), and add `Closes #348` to the description if `--fill` did not carry it.

- [ ] **Step 6: Watch CI and fix what it finds**

`gh pr create` returns before any check has run. Poll:

```bash
gh pr checks <N>
```

until every check reports pass or fail — a "pending" read seconds after creation means the run has not started, not that you are done. Watch for `ruff check` failing on import order (`I001`) even when the local suite was green; that has caught real bugs on this repo twice. Read the job log, apply the minimal fix, push, re-poll. Do not leave a red check for the user.

---

## Spec coverage

| Requirement | Task |
|---|---|
| GS-1, GS-2, GS-3, GS-5, GS-6, GS-7 | 3 |
| GS-4 | 2, 3 (agreement test) |
| GS-8, GS-9 | 4 |
| GS-10 … GS-14, GS-16 | 5 |
| GS-15 | 11 (manual) |
| GS-17 … GS-20 | 6 |
| GS-21 … GS-25 | 9, 10 |
| GS-26 | 8 |
