# Deferred TODO Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three deferred issues — a silent metadata-edit loss on role conversion, a GC-content figure measured only from the front of a reference file, and the absence of longest/shortest contig reporting.

**Architecture:** Three independent changes. Tasks 1–2 are frontend-only (React, manual browser verification). Tasks 3–5 and 6–8 are backend parser/stats work driven by pytest, each followed by a small frontend render task. Nothing shares state; the three groups can land in any order.

**Tech Stack:** Python 3 + pytest (backend), React + TypeScript (frontend), Docker Compose for both.

**Spec:** `docs/superpowers/specs/2026-07-29-todo-batch-design.md`

---

## Conventions for this plan

**Running backend tests.** Always inside the `api` container, never a host venv (the host venv hits Mongo replica-set errors):

```bash
docker compose exec api python -m pytest tests/ -q
```

**Seeing frontend changes.** `web` runs `vite dev` with bind-mounted source, so edits are live on save at localhost:5173 with no restart. No rebuild needed for Tasks 1, 2, 5, 8.

**Backend changes.** `api` runs `uvicorn --reload`, so parser edits take effect on the next request. But re-ingesting a file runs through `worker`, which does **not** hot-reload. After any change to `parsers.py` or `sequence_stats.py`, before re-testing an ingest:

```bash
docker compose restart worker
```

Skipping this makes a correct fix look broken — the job runs the old in-memory code.

**Facts are untyped.** `obj.facts` is a plain dict server-side and `Record<string, unknown>` client-side. No schema or migration work is needed to add keys.

---

## File Structure

**Frontend**
- `frontend/src/components/SchemaMetadataEditor.tsx` — add optional `onDirtyChange` prop. Dirty state stays local; the prop only mirrors it out.
- `frontend/src/components/DetailPanel.tsx` — hold the mirrored dirty flag, wire editor → converter.
- `frontend/src/components/RoleConverter.tsx` — two-step confirm when dirty.
- `frontend/src/components/AssemblyFacts.tsx` — GC label by sampling mode; Longest/Shortest row.
- `frontend/src/components/FactsTable.tsx` — labels and suppressions for new keys (non-reference FASTA path).

**Backend**
- `backend/app/storage/sequence_stats.py` — strided sampling in `fasta_stats`.
- `backend/app/storage/parsers.py` — per-sequence lengths in `_parse_fasta`.
- `backend/tests/storage/test_sequence_stats.py` — sampling tests.
- `backend/tests/storage/test_parsers.py` — length tests.

**Docs**
- `docs/TODO.md` — remove the three entries as they land.

---

## Task 1: Lift the metadata dirty flag

Mirror `SchemaMetadataEditor`'s local `dirty` state outward so `DetailPanel` can pass it to `RoleConverter`. No behavior change on its own — Task 2 consumes it.

**Files:**
- Modify: `frontend/src/components/SchemaMetadataEditor.tsx:7-13` (Props), `:36` (state), `:54` (after the resync effect)
- Modify: `frontend/src/components/DetailPanel.tsx:776-783`

- [ ] **Step 1: Add the optional prop to the Props interface**

In `SchemaMetadataEditor.tsx`, replace the `Props` interface (lines 7–13):

```ts
interface Props {
  value: Record<string, unknown>;
  formatKind: string;
  role: ObjectRole | null;
  onSave: (next: Record<string, unknown>) => void;
  saving?: boolean;
  /**
   * Mirrors the editor's internal dirty flag outward so a parent can warn
   * before an action that would discard in-progress edits. Optional: the
   * editor is fully functional without it.
   */
  onDirtyChange?: (dirty: boolean) => void;
}
```

- [ ] **Step 2: Destructure the new prop**

Replace line 25:

```ts
export function SchemaMetadataEditor({ value, formatKind, role, onSave, saving, onDirtyChange }: Props) {
```

- [ ] **Step 3: Fire the callback when dirty changes**

Insert immediately after the existing resync `useEffect` (after line 54, before `const setField`):

```ts
  // Mirror dirty outward. Separate from the resync effect above so that
  // effect's dependency list and its early-bail behavior are untouched.
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
```

- [ ] **Step 4: Hold the flag in DetailPanel**

In `DetailPanel.tsx`, add near the component's other `useState` calls (the file already imports `useState`):

```ts
  const [metadataDirty, setMetadataDirty] = useState(false);
```

- [ ] **Step 5: Wire the editor**

Replace the `SchemaMetadataEditor` element (lines 776–783):

```tsx
        <SchemaMetadataEditor
          key={obj.role ?? "none"}
          value={obj.metadata}
          formatKind={obj.format.kind}
          role={obj.role}
          onSave={onSave}
          saving={saving}
          onDirtyChange={setMetadataDirty}
        />
```

The `key` remount stays. It is still what makes the conversion safe; Task 2 only stops it being silent.

- [ ] **Step 6: Verify the app still builds and behaves**

Open localhost:5173, select a FASTA object, type into a metadata field, click Save. Expect: saving works exactly as before, no console errors. Nothing visible changes yet.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/SchemaMetadataEditor.tsx frontend/src/components/DetailPanel.tsx
git commit -m "refactor: expose metadata editor dirty state to parent"
```

---

## Task 2: Confirm before a conversion discards edits

**Files:**
- Modify: `frontend/src/components/RoleConverter.tsx` (whole file)
- Modify: `frontend/src/components/DetailPanel.tsx:826`

- [ ] **Step 1: Pass the flag to the converter**

In `DetailPanel.tsx`, replace line 826:

```tsx
      <RoleConverter obj={obj} metadataDirty={metadataDirty} />
```

- [ ] **Step 2: Rewrite RoleConverter with the two-step confirm**

Replace the whole of `RoleConverter.tsx`:

```tsx
import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { DataObject } from "../api/types";

interface Props {
  obj: DataObject;
  /** True when the metadata editor holds unsaved edits. */
  metadataDirty?: boolean;
}

/** Formats where reference-vs-reads is genuinely ambiguous. */
const CONVERTIBLE_FORMATS = ["fasta", "fastq"];

/**
 * Converts a file between reads and reference.
 *
 * Both directions are the same PATCH with a different value, and the change is
 * cheap and reversible -- so a clean conversion converts on one click, with no
 * confirmation step that would be friction without benefit.
 *
 * The exception is unsaved metadata: DetailPanel remounts the editor on a role
 * change (its schema changes underneath), which discards in-progress edits.
 * That is correct but not something to do silently, so a dirty editor gets a
 * confirm step.
 */
export function RoleConverter({ obj, metadataDirty = false }: Props) {
  const qc = useQueryClient();
  const isReference = obj.role === "reference";
  const [confirming, setConfirming] = useState(false);

  // Saving elsewhere in the panel clears the hazard; don't leave a stale
  // warning on screen.
  useEffect(() => {
    if (!metadataDirty) setConfirming(false);
  }, [metadataDirty]);

  const convert = useMutation({
    mutationFn: (role: "reference" | null) => api.updateObject(obj.id, { role }),
    onSuccess: (_r, role) => {
      setConfirming(false);
      qc.invalidateQueries({ queryKey: ["object", obj.id] });
      // The left panel re-sections off this value.
      qc.invalidateQueries({ queryKey: ["objects", obj.project_id] });
      qc.invalidateQueries({ queryKey: ["search"] });
      notify.success(
        role === "reference"
          ? `${obj.name} is now a reference`
          : `${obj.name} is now reads`,
      );
    },
    onError: (e: Error) => notify.error(e.message),
  });

  // A BAM or VCF has an unambiguous role already; offering to convert it
  // invites confusion rather than solving a problem.
  if (
    !isReference &&
    !CONVERTIBLE_FORMATS.includes(obj.format.kind.toLowerCase())
  ) {
    return null;
  }

  const doConvert = () => convert.mutate(isReference ? null : "reference");

  const onClick = () => {
    if (metadataDirty && !confirming) {
      setConfirming(true);
      return;
    }
    doConvert();
  };

  return (
    <div className="section">
      <div className="section-title">Role</div>
      {confirming && (
        <div className="warn-box" style={{ marginBottom: 8 }}>
          You have unsaved metadata edits. Converting will discard them.
        </div>
      )}
      <div style={{ display: "flex", gap: 8 }}>
        <button
          type="button"
          className="btn"
          onClick={onClick}
          disabled={convert.isPending}
        >
          {convert.isPending
            ? "Converting…"
            : confirming
              ? "Convert anyway"
              : isReference
                ? "Convert back to reads"
                : "Convert to reference"}
        </button>
        {confirming && !convert.isPending && (
          <button
            type="button"
            className="btn"
            onClick={() => setConfirming(false)}
          >
            Cancel
          </button>
        )}
      </div>
      <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}>
        {isReference
          ? "Moves this back to the Reads section and restores the sequencing metadata fields. Nothing is lost either way."
          : "Marks this as a reference genome. It will move to the References section and show assembly metadata."}
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Verify the dirty path**

At localhost:5173, open a FASTA object. Type into a metadata field but do **not** save. Click Convert.

Expected: the warn box appears, the button reads "Convert anyway", a Cancel button appears, and **no conversion happens**. The object stays in its current section.

- [ ] **Step 4: Verify Cancel**

Click Cancel. Expected: warning and Cancel disappear, the button returns to "Convert to reference", and the text typed in Step 3 is still in the metadata field.

- [ ] **Step 5: Verify the confirmed conversion**

Click Convert, then "Convert anyway". Expected: the conversion proceeds, a success toast appears, and the object moves to the References section.

- [ ] **Step 6: Verify the clean path is unchanged**

Select a different FASTA object. Without touching the metadata form, click Convert.

Expected: it converts on the **first** click — no warning, no second click. This is the regression check: the confirm must not appear when there is nothing to lose.

- [ ] **Step 7: Verify saving clears the warning**

Type into a metadata field, click Convert (warning appears), then click Save in the metadata section. Expected: the warning and Cancel button disappear on their own, because the edits are no longer unsaved.

- [ ] **Step 8: Remove the TODO entry**

In `docs/TODO.md`, delete the section `## Warn before a role conversion discards in-progress metadata edits` (lines 337–356), including its "Touches:" line and the trailing blank line.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/components/RoleConverter.tsx frontend/src/components/DetailPanel.tsx docs/TODO.md
git commit -m "feat: warn before a role conversion discards unsaved metadata"
```

---

## Task 3: Failing test for strided GC sampling

This is the test that proves the bug. A FASTA with GC skewed front-to-back: a prefix read sees only the high-GC front and reports a badly wrong whole-file number.

**Files:**
- Modify: `backend/tests/storage/test_sequence_stats.py`

- [ ] **Step 1: Write the failing test**

Add to `test_sequence_stats.py`, at the end of the file:

```python
class TestFastaSampling:
    """GC sampling must describe the file, not just its first chromosome."""

    def write_skewed_fasta(self, path, *, line_len=60, lines_per_half=20_000):
        """High-GC first half, low-GC second half.

        True whole-file GC is 50%: equal halves at 100% and 0%. A prefix read
        that never reaches the second half reports ~100%.
        """
        with open(path, "w") as f:
            f.write(">high_gc\n")
            for _ in range(lines_per_half):
                f.write("GC" * (line_len // 2) + "\n")
            f.write(">low_gc\n")
            for _ in range(lines_per_half):
                f.write("AT" * (line_len // 2) + "\n")
        return path

    def test_strided_sample_spans_the_file(self, tmp_path):
        p = self.write_skewed_fasta(tmp_path / "skewed.fasta")
        # A budget far under the file size forces sampling rather than a full
        # read; the whole point is what happens when we cannot read it all.
        r = ss.fasta_stats(p, Compression.NONE, max_bases=100_000)
        assert r["stats_sampling"] == "strided"
        # True value is 50%. A prefix read gives ~100%, so a generous window
        # still fails loudly on the old behavior.
        assert 40.0 <= r["gc_content_percent"] <= 60.0

    def test_small_file_is_complete_not_sampled(self, tmp_path):
        p = tmp_path / "small.fasta"
        p.write_text(">c1\nGCGCATAT\n")
        r = ss.fasta_stats(p, Compression.NONE)
        assert r["stats_sampling"] == "complete"
        assert r["gc_content_percent"] == 50.0

    def test_gzip_falls_back_to_prefix(self, tmp_path):
        plain = self.write_skewed_fasta(tmp_path / "skewed.fasta")
        gz = tmp_path / "skewed.fasta.gz"
        with open(plain, "rb") as src, gzip.open(gz, "wb") as dst:
            dst.write(src.read())
        r = ss.fasta_stats(gz, Compression.GZIP, max_bases=100_000)
        # Gzip cannot seek cheaply, so the prefix read stands -- but it must be
        # labelled as such rather than claiming to span the file.
        assert r["stats_sampling"] == "prefix"
        assert "gc_content_percent" in r
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
docker compose exec api python -m pytest tests/storage/test_sequence_stats.py::TestFastaSampling -v
```

Expected: all three FAIL with `KeyError: 'stats_sampling'`. `test_strided_sample_spans_the_file` is the important one — note that it would also fail on the GC assertion (reporting ~100%) once the key exists but the read is still a prefix.

- [ ] **Step 3: Commit the failing tests**

```bash
git add backend/tests/storage/test_sequence_stats.py
git commit -m "test: GC sampling must span the file, not its prefix"
```

---

## Task 4: Implement strided GC sampling

**Files:**
- Modify: `backend/app/storage/sequence_stats.py:139-191` (`fasta_stats`)

- [ ] **Step 1: Add the block-count constant**

In `sequence_stats.py`, after `CANCEL_CHECK_READS = 20_000` (line 31):

```python
# Blocks a strided FASTA sample is spread across. Enough to cross every
# chromosome of a human reference, while keeping each block large enough that
# per-seek overhead stays negligible against the bytes read.
FASTA_SAMPLE_BLOCKS = 100
```

- [ ] **Step 2: Replace fasta_stats**

Replace the whole `fasta_stats` function (lines 139–191):

```python
def fasta_stats(
    path: Path,
    compression: Compression,
    *,
    cancel_event: threading.Event | None = None,
    max_bases: int = 50_000_000,
) -> dict:
    """Base composition for a FASTA file (no quality scores to report).

    The cap is a performance guard, not a compromise on correctness -- but a
    capped read taken from the front of a multi-GB reference describes chr1,
    not the assembly, and GC varies enough between chromosomes to mislead. For
    seekable (uncompressed) files the same byte budget is instead spread across
    the whole file. Gzip and BGZF cannot seek cheaply, so they keep the prefix
    read and say so in `stats_sampling`.
    """
    import gzip

    is_compressed = compression in (Compression.GZIP, Compression.BGZF)
    file_size = path.stat().st_size

    counts: Counter[str] = Counter()

    try:
        if not is_compressed and file_size > max_bases:
            seen, mode = _fasta_sample_strided(
                path, counts, max_bases, cancel_event
            )
        else:
            opener = gzip.open if is_compressed else open
            with opener(path, "rt", errors="replace") as fh:
                seen = _fasta_read_block(fh, counts, max_bases, cancel_event)
            # A file smaller than the budget was read end to end: the figure is
            # exact, not an estimate, and should not carry a "sampled" caveat.
            mode = "prefix" if seen >= max_bases else "complete"
    except JobCancelled:
        raise
    except (OSError, EOFError, UnicodeDecodeError) as e:
        log.warning("fasta_stats_failed", path=str(path), error=str(e))
        return {}

    if seen == 0:
        return {}

    facts: dict = {"stats_sampled_bases": seen, "stats_sampling": mode}
    composition = _composition(counts)
    if composition:
        facts["base_composition"] = composition

    gc = counts.get("G", 0) + counts.get("C", 0) + counts.get("g", 0) + counts.get("c", 0)
    acgt = gc + sum(counts.get(b, 0) for b in "ATat")
    if acgt:
        facts["gc_content_percent"] = round(100.0 * gc / acgt, 2)
    return facts


def _fasta_read_block(
    fh,
    counts: Counter[str],
    budget: int,
    cancel_event: threading.Event | None,
) -> int:
    """Count bases from the current handle position until `budget` is reached.

    Returns the number of bases counted. Header lines are skipped and do not
    consume budget.
    """
    seen = 0
    lines = 0
    for line in fh:
        lines += 1
        if line.startswith(">"):
            continue
        seq = line.rstrip("\n")
        counts.update(seq)
        seen += len(seq)
        if seen >= budget:
            break
        if lines % 100_000 == 0 and cancel_event is not None:
            if cancel_event.is_set():
                raise JobCancelled("Cancelled during sequence statistics")
    return seen


def _fasta_sample_strided(
    path: Path,
    counts: Counter[str],
    max_bases: int,
    cancel_event: threading.Event | None,
) -> tuple[int, str]:
    """Spend the byte budget in equal blocks spread across the file.

    Same total bytes read as a prefix scan, so the same cost -- but the sample
    crosses every chromosome instead of stopping inside the first.
    """
    file_size = path.stat().st_size
    per_block = max(1, max_bases // FASTA_SAMPLE_BLOCKS)
    stride = file_size // FASTA_SAMPLE_BLOCKS

    seen = 0
    with open(path, errors="replace") as fh:
        for i in range(FASTA_SAMPLE_BLOCKS):
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled("Cancelled during sequence statistics")
            fh.seek(i * stride)
            if i > 0:
                # A seek lands mid-line: that partial line would start counting
                # from an arbitrary column, and could be the tail of a header.
                # Discard it and start clean on the next line boundary.
                fh.readline()
            seen += _fasta_read_block(fh, counts, per_block, cancel_event)
    return seen, "strided"
```

- [ ] **Step 3: Run the new tests**

```bash
docker compose exec api python -m pytest tests/storage/test_sequence_stats.py::TestFastaSampling -v
```

Expected: all three PASS.

- [ ] **Step 4: Run the full backend suite for regressions**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: no failures. `TestFastaStats` and `test_fasta_parser_includes_composition` must still pass — small fixture files now take the `complete` path, which reads identically to the old behavior.

- [ ] **Step 5: Commit**

```bash
git add backend/app/storage/sequence_stats.py
git commit -m "fix: sample FASTA GC content across the file, not its prefix"
```

---

## Task 5: Label the GC row by sampling mode

**Files:**
- Modify: `frontend/src/components/AssemblyFacts.tsx:23-24, 86-102`
- Modify: `frontend/src/components/FactsTable.tsx:50-78`

- [ ] **Step 1: Read the new fact**

In `AssemblyFacts.tsx`, after line 24 (`const sampledBases = ...`):

```ts
  const sampling = facts.stats_sampling as string | undefined;
```

- [ ] **Step 2: Replace the GC row**

Replace the GC block (lines 86–102):

```tsx
        {gc !== undefined && (
          <>
            {/* What this number means depends on how it was measured: a small
                file is counted exactly, a large uncompressed one is sampled
                across the whole file, and a compressed one is still a prefix
                read because gzip cannot seek cheaply. Objects ingested before
                strided sampling have no stats_sampling key and keep the
                original conservative label. */}
            <dt>
              {sampling === "complete"
                ? "GC content"
                : sampling === "strided"
                  ? "GC content (sampled across file)"
                  : "GC content (sampled)"}
            </dt>
            <dd>
              {gc}%
              {sampling !== "complete" && sampledBases !== undefined && (
                <span style={{ color: "var(--text-faint)" }}>
                  {" "}
                  from {formatBases(sampledBases)} sampled
                </span>
              )}
            </dd>
          </>
        )}
```

- [ ] **Step 3: Suppress the raw key in the generic table**

`AssemblyFacts` renders only for reference-role objects; a FASTA left as reads falls through to `FactsTable`, which would print `stats_sampling` as a raw row. In `FactsTable.tsx`, add to the `SUPPRESSED` set beside the existing `stats_sampled_bases` entry (line 70):

```ts
  "stats_sampled_bases",
  "stats_sampling",
```

- [ ] **Step 4: Verify in the browser**

At localhost:5173, open a reference object whose FASTA is small (under 50 MB — most test fixtures).

Expected: the row reads "GC content" with no "(sampled)" caveat and no "from N sampled" suffix, because the whole file was counted.

Note: to see a `strided` label you need an uncompressed FASTA over 50 MB, re-ingested after `docker compose restart worker`. If none is handy, the `complete` case plus the passing backend tests are sufficient verification here.

- [ ] **Step 5: Remove the TODO entry**

In `docs/TODO.md`, delete the section `## Sample GC content across the file instead of a prefix` (lines 358–379).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/AssemblyFacts.tsx frontend/src/components/FactsTable.tsx docs/TODO.md
git commit -m "feat: label GC content by how it was actually sampled"
```

---

## Task 6: Failing test for per-sequence FASTA lengths

**Files:**
- Modify: `backend/tests/storage/test_parsers.py`

- [ ] **Step 1: Write the failing tests**

Add at the end of `test_parsers.py`:

```python
class TestFastaSequenceLengths:
    """Per-sequence lengths, and the longest/shortest across an assembly."""

    def test_lengths_and_extremes(self, tmp_path):
        p = tmp_path / "ref.fasta"
        p.write_text(">c1\nACGT\n>c2\nACGTACGTAC\n>c3\nAC\n")
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        assert facts["sequence_lengths"] == {"c1": 4, "c2": 10, "c3": 2}
        assert facts["sequence_longest"] == {"name": "c2", "length": 10}
        assert facts["sequence_shortest"] == {"name": "c3", "length": 2}

    def test_wrapped_records_sum_across_lines(self, tmp_path):
        p = tmp_path / "wrapped.fasta"
        # 3 lines x 60 bases: a real FASTA wraps, and a per-line count would
        # report 60 instead of 180.
        p.write_text(">chr1\n" + ("A" * 60 + "\n") * 3 + ">chr2\nAC\n")
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        assert facts["sequence_lengths"]["chr1"] == 180
        assert facts["sequence_longest"] == {"name": "chr1", "length": 180}

    def test_extremes_come_from_beyond_the_stored_window(self, tmp_path):
        """The stored dict is capped, but longest/shortest are not.

        Bounding the extremes to the first MAX_STORED_CONTIGS would report the
        wrong longest contig for any assembly with more sequences than that --
        which is most of them.
        """
        n = parsers.MAX_STORED_CONTIGS + 10
        with open(tmp_path / "many.fasta", "w") as f:
            for i in range(n):
                # Records grow, so both extremes sit outside the first 50:
                # the shortest is record 0... so make record n-1 longest and
                # deliberately place the shortest late as well.
                f.write(f">c{i}\n" + "A" * (100 + i) + "\n")
            f.write(">tiny\nA\n")
        facts = parsers.parse(tmp_path / "many.fasta", FormatKind.FASTA, Compression.NONE)
        assert len(facts["sequence_lengths"]) == parsers.MAX_STORED_CONTIGS
        assert facts["sequence_longest"] == {"name": f"c{n - 1}", "length": 100 + n - 1}
        assert facts["sequence_shortest"] == {"name": "tiny", "length": 1}

    def test_single_sequence_is_both_extremes(self, tmp_path):
        p = tmp_path / "one.fasta"
        p.write_text(">only\nACGTACGT\n")
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        assert facts["sequence_longest"] == {"name": "only", "length": 8}
        assert facts["sequence_shortest"] == {"name": "only", "length": 8}

    def test_complete_parse_is_not_flagged_partial(self, tmp_path):
        p = tmp_path / "ref.fasta"
        p.write_text(">c1\nACGT\n")
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        assert "sequence_lengths_partial" not in facts
```

- [ ] **Step 2: Run to verify they fail**

```bash
docker compose exec api python -m pytest tests/storage/test_parsers.py::TestFastaSequenceLengths -v
```

Expected: the first four FAIL with `KeyError: 'sequence_lengths'` / `'sequence_longest'`. `test_complete_parse_is_not_flagged_partial` passes trivially today — it guards the flag added in Task 7.

- [ ] **Step 3: Commit the failing tests**

```bash
git add backend/tests/storage/test_parsers.py
git commit -m "test: FASTA parsing reports per-sequence lengths and extremes"
```

---

## Task 7: Implement per-sequence FASTA lengths

**Files:**
- Modify: `backend/app/storage/parsers.py:416-463` (`_parse_fasta`)

- [ ] **Step 1: Replace _parse_fasta**

Replace the whole function (lines 416–463):

```python
def _parse_fasta(path: Path, compression: Compression, cancel) -> dict:
    """Count sequences exactly for small files, estimate for large ones."""
    facts: dict = {}
    file_size = path.stat().st_size
    # A reference genome FASTA is a few GB; counting '>' lines across that is
    # cheap relative to a FASTQ scan, but not free. Cap it.
    exact_limit = 256 * 1024 * 1024

    names: list[str] = []
    lengths: dict[str, int] = {}
    count = 0
    total_bases = 0
    read_bytes = 0
    truncated = False

    # Per-record accumulation. The current record is only committed when the
    # next header proves it complete, so a record cut by the byte limit is
    # never reported at a truncated length.
    current_name: str | None = None
    current_len = 0
    longest: tuple[str, int] | None = None
    shortest: tuple[str, int] | None = None

    def commit() -> None:
        nonlocal longest, shortest
        if current_name is None:
            return
        if len(lengths) < MAX_STORED_CONTIGS:
            lengths[current_name] = current_len
        # Extremes track every record, not just the stored window: capping them
        # would report the wrong longest contig for most real assemblies.
        if longest is None or current_len > longest[1]:
            longest = (current_name, current_len)
        if shortest is None or current_len < shortest[1]:
            shortest = (current_name, current_len)

    with _open_text(path, compression) as fh:
        for line in fh:
            read_bytes += len(line)
            if line.startswith(">"):
                commit()
                count += 1
                name = line[1:].strip().split()[0] if line[1:].strip() else ""
                if len(names) < MAX_STORED_CONTIGS:
                    names.append(name)
                current_name = name
                current_len = 0
            else:
                n = len(line.rstrip("\n"))
                total_bases += n
                current_len += n
            if count % 500 == 0:
                _check(cancel)
            if compression is Compression.NONE and read_bytes > exact_limit:
                truncated = True
                break

    if truncated:
        # The in-progress record is mid-sequence and every later record is
        # unseen, so it is dropped rather than committed at a partial length.
        facts["sequence_count_estimate"] = int(count * (file_size / read_bytes))
        facts["sequence_count_exact"] = False
        facts["sequence_lengths_partial"] = True
    else:
        commit()
        facts["sequence_count"] = count
        facts["sequence_count_exact"] = True
        facts["total_bases"] = total_bases

    if names:
        facts["sequence_names"] = names
        if count > MAX_STORED_CONTIGS:
            facts["sequence_names_truncated"] = True

    if lengths:
        facts["sequence_lengths"] = lengths
    # Emitted even when partial -- they are the true extremes of what was
    # parsed, and sequence_lengths_partial marks them as not final. Lengths are
    # never extrapolated: there is no sound way to guess an unseen contig's
    # length from a byte ratio.
    if longest is not None:
        facts["sequence_longest"] = {"name": longest[0], "length": longest[1]}
    if shortest is not None:
        facts["sequence_shortest"] = {"name": shortest[0], "length": shortest[1]}

    from app.storage import sequence_stats

    facts.update(
        sequence_stats.fasta_stats(path, compression, cancel_event=cancel)
    )
    return facts
```

- [ ] **Step 2: Run the new tests**

```bash
docker compose exec api python -m pytest tests/storage/test_parsers.py::TestFastaSequenceLengths -v
```

Expected: all five PASS.

- [ ] **Step 3: Run the full backend suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: no failures. The existing `sequence_count` / `sequence_names` / `total_bases` behavior is unchanged.

- [ ] **Step 4: Commit**

```bash
git add backend/app/storage/parsers.py
git commit -m "feat: record per-sequence FASTA lengths and assembly extremes"
```

---

## Task 8: Show longest and shortest in the assembly panel

**Files:**
- Modify: `frontend/src/components/AssemblyFacts.tsx:19-28, 80-85`
- Modify: `frontend/src/components/FactsTable.tsx:40-46, 50-78`

- [ ] **Step 1: Read the new facts**

In `AssemblyFacts.tsx`, add alongside the other `facts.*` reads near the top of
the component (after line 28, `const namesTruncated = ...`; if Task 5 is
already done, this sits just below its `sampling` line):

```ts
  type NamedLength = { name: string; length: number };
  const longest = facts.sequence_longest as NamedLength | undefined;
  const shortest = facts.sequence_shortest as NamedLength | undefined;
  const lengthsPartial = facts.sequence_lengths_partial === true;
```

- [ ] **Step 2: Add the rows**

In `AssemblyFacts.tsx`, insert immediately after the "Total bases" block (after line 85, before the GC block):

```tsx
        {longest !== undefined && shortest !== undefined && (
          <>
            <dt>Longest</dt>
            <dd>
              <span className="mono">{longest.name}</span> ·{" "}
              {formatBases(longest.length)}
            </dd>
            <dt>Shortest</dt>
            <dd>
              <span className="mono">{shortest.name}</span> ·{" "}
              {formatBases(shortest.length)}
            </dd>
          </>
        )}
```

- [ ] **Step 3: Note the partial case**

In `AssemblyFacts.tsx`, insert immediately after the closing `</dl>` of the main list (after line 103, inside the `hasAnything && (...)` block):

```tsx
      {lengthsPartial && longest !== undefined && (
        <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}>
          Longest and shortest are partial — the file was truncated during
          parsing, so later sequences were not measured.
        </div>
      )}
```

- [ ] **Step 4: Handle the generic table**

A FASTA left as reads renders through `FactsTable`. In `FactsTable.tsx`, add to `LABELS` (beside line 43):

```ts
  total_bases: "Total bases",
  sequence_longest: "Longest sequence",
  sequence_shortest: "Shortest sequence",
```

And add to `SUPPRESSED`. Task 5 Step 3 already added `"stats_sampling"` there —
do **not** repeat it. Add only these two, immediately below it:

```ts
  "sequence_lengths_partial",
  // Rendered as the Longest/Shortest rows in AssemblyFacts; a 50-entry dict
  // would swamp the generic table.
  "sequence_lengths",
```

If Task 5 has not been done (the groups are independent and may land in any
order), add `"stats_sampling"` as well.

- [ ] **Step 5: Render the named-length shape**

`renderValue` would print `{name, length}` as `[object Object]`. In `FactsTable.tsx`, add inside `renderValue`, immediately after the `typeof value === "boolean"` line (line 128):

```tsx
  if (key === "sequence_longest" || key === "sequence_shortest") {
    const v = value as { name: string; length: number };
    return (
      <span>
        <span className="mono">{v.name}</span> · {formatNumber(v.length)} bp
      </span>
    );
  }
```

- [ ] **Step 6: Restart the worker and re-ingest**

Parser changes only take effect for newly ingested files, and `worker` does not hot-reload:

```bash
docker compose restart worker
```

Then upload a multi-record FASTA (or re-ingest an existing one) and open it as a reference.

- [ ] **Step 7: Verify in the browser**

Expected: the Assembly section shows Longest and Shortest rows between "Total bases" and "GC content", each as `name · size`, with sizes matching the file. Existing reference objects ingested before this change show no such rows and no errors — the facts key is simply absent.

- [ ] **Step 8: Remove the TODO entry**

In `docs/TODO.md`, delete the section `## Extract per-sequence lengths for FASTA` (lines 381–399).

- [ ] **Step 9: Commit**

```bash
git add frontend/src/components/AssemblyFacts.tsx frontend/src/components/FactsTable.tsx docs/TODO.md
git commit -m "feat: show longest and shortest sequence in the assembly panel"
```

---

## Final verification

- [ ] **Step 1: Full backend suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 2: Confirm the TODO entries are gone**

```bash
grep -n "dirty-state\|Sample GC content\|per-sequence lengths" docs/TODO.md
```

Expected: no output. Any hit means a Task 2 / 5 / 8 doc step was missed.

- [ ] **Step 3: Confirm no stale TODO pointers remain in code**

```bash
grep -rn "docs/TODO.md" frontend/src backend/app
```

Expected: no output. The comment in `AssemblyFacts.tsx:88-90` pointing at the GC TODO is replaced in Task 5; a remaining hit means it survived.
