# Chromosome Names on the Strip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Label chromosome-strip bars `IV` and `MT` instead of `1136` and `1224`, from names NCBI publishes, fetched once at ingest.

**Architecture:** A new `lookup_sequence_names()` in `app/metadata/assembly.py` calls NCBI's `sequence_reports` endpoint and returns a flat accession→label map covering both RefSeq and GenBank accessions. `enrich_from_assembly` stores it as `facts.sequence_labels`. The frontend classifier attaches labels to bars; the strip prefers them over its accession-digit fallback. The strip stays a pure local render with no network call, no loading state, and no new failure mode.

**Tech Stack:** Python 3.12 / FastAPI / pytest on the backend; React 18 / TypeScript / Vitest on the frontend.

**Spec:** `docs/superpowers/specs/2026-07-31-chromosome-labels-design.md`

---

## Conventions verified against this repo

- **Backend tests run in the `api` container, from the main repo root:**
  `docker compose exec api python -m pytest tests/ -q`
  A bare host `.venv` hits Mongo replica-set errors the container does not.
- **Frontend tests run in the `web` container:**
  `docker compose exec -T web npx vitest run`
- **Never run `docker compose` from a worktree** — the bind mounts are relative and would silently repoint the shared stack at that branch. Always `cd /Users/syntheticgio/Programming/local-bio-pipeliner` first.
- **The `web` and `api` containers mount the MAIN repo's source**, not a worktree. To exercise worktree code, copy files into the main repo, run, then delete the copies and confirm `git status --porcelain` there is empty. Never commit in the main repo.
- **Fixture convention:** real captured NCBI payloads in `backend/tests/fixtures/`, loaded via `Path(__file__).resolve().parents[1] / "fixtures" / "<name>.json"`. See `backend/tests/storage/test_assembly_accession.py:13`.
- **`_get` from `app.metadata.sra`** is the shared HTTP helper: it throttles, retries, and returns `None` rather than raising. `assembly.lookup` already uses it (`assembly.py:223`).
- **Enrichment facts merge on top of parser facts** (`queue/results.py:131-136`), so `sequence_labels` lands beside `sequence_lengths` without either clobbering the other.
- **`worker` does not hot-reload.** After changing anything under `app/`, run `docker compose restart worker` before testing an ingest, or the job silently runs the old code.

## Fixtures already captured

These are **real** NCBI responses, already written to `backend/tests/fixtures/` and committed as part of this work. Do not regenerate them:

| File | Contents | The case it covers |
|---|---|---|
| `ncbi_seqreports_GCF_000146045.2.json` | 17 records | `I`–`XVI` plus `MT`; both accession namespaces |
| `ncbi_seqreports_GCF_000002445.2.json` | 12 records | **Two records both with `chr_name: "11"`** — the duplicate trap |
| `ncbi_seqreports_GCF_000001405.40_slice.json` | 7 records | Human: 3 chromosomes, `MT`, 3 unlocalized scaffolds |

Verified contents:
- yeast `I` → `NC_001133.9` / `BK006935.2`; `MT` → `NC_001224.1`
- Aspergillus `11` → `NT_165288.1` (`chr11-scaffold01`) **and** `NT_165287.1` (`chr11-scaffold02`), both `unlocalized-scaffold`
- human `1` → `NT_187361.1` (`HSCHR1_CTG1_UNLOCALIZED`), `unlocalized-scaffold`

## File structure

| File | Responsibility |
|---|---|
| `backend/app/metadata/assembly.py` (modify) | `parse_sequence_reports()` + `lookup_sequence_names()` |
| `backend/tests/metadata/test_sequence_names.py` (create) | Parser tests against the real fixtures |
| `backend/app/metadata/enrich.py` (modify) | Attach `sequence_labels` to the enrichment facts |
| `frontend/src/lib/chromosomes.ts` (modify) | `Bar.label`; read `facts.sequence_labels` |
| `frontend/src/lib/chromosomes.test.ts` (modify) | Label attachment cases |
| `frontend/src/components/ChromosomeStrip.tsx` (modify) | Prefer `bar.label` for the caption |

Task order: pure parser first (fully tested against real payloads), then the network wrapper, then wiring, then the frontend, then the message correction, then verification.

---

### Task 1: Parse a sequence report into a label map

Pure function, no network. This is where the duplicate-`chr_name` trap is defused.

**Files:**
- Modify: `backend/app/metadata/assembly.py`
- Create: `backend/tests/metadata/test_sequence_names.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/metadata/test_sequence_names.py`:

```python
"""Per-sequence chromosome names from NCBI's sequence_reports endpoint.

Every fixture is a real captured response. The Aspergillus one exists
specifically because two of its records share `chr_name: "11"` -- labelling by
chr_name alone would put two bars reading "11" on the strip, one of them the
largest bar there is.
"""

import json
from pathlib import Path

from app.metadata import assembly

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


YEAST = "ncbi_seqreports_GCF_000146045.2.json"
ASPERGILLUS = "ncbi_seqreports_GCF_000002445.2.json"
HUMAN = "ncbi_seqreports_GCF_000001405.40_slice.json"


class TestParseSequenceReports:
    def test_maps_both_accession_namespaces_to_one_label(self):
        """One lookup must label the GCA file and the GCF file alike."""
        labels = assembly.parse_sequence_reports(_load(YEAST))
        assert labels["NC_001133.9"] == "I"
        assert labels["BK006935.2"] == "I"

    def test_labels_the_mitochondrion(self):
        labels = assembly.parse_sequence_reports(_load(YEAST))
        assert labels["NC_001224.1"] == "MT"

    def test_distinguishes_records_sharing_a_chr_name(self):
        """The regression a naive chr_name implementation produces.

        Both of these report chr_name "11"; they are different scaffolds and
        must not both read "11".
        """
        labels = assembly.parse_sequence_reports(_load(ASPERGILLUS))
        assert labels["NT_165288.1"] == "chr11-scaffold01"
        assert labels["NT_165287.1"] == "chr11-scaffold02"
        assert labels["NT_165288.1"] != labels["NT_165287.1"]

    def test_assembled_molecules_still_use_chr_name(self):
        labels = assembly.parse_sequence_reports(_load(ASPERGILLUS))
        assert labels["NC_008409.1"] == "1"
        assert labels["NC_005063.2"] == "2"

    def test_human_unlocalized_scaffolds_get_their_own_names(self):
        labels = assembly.parse_sequence_reports(_load(HUMAN))
        assert labels["NC_000001.11"] == "1"
        assert labels["NT_187361.1"] == "HSCHR1_CTG1_UNLOCALIZED"
        assert labels["NC_012920.1"] == "MT"

    def test_caps_the_map(self):
        """Bounded like sequence_lengths: the strip draws 24 bars and lists the
        rest, so labels past the cap have nothing to label."""
        reports = [
            {
                "chr_name": str(i),
                "refseq_accession": f"NC_{i:06d}.1",
                "role": "assembled-molecule",
                "sort_order": i,
            }
            for i in range(200)
        ]
        labels = assembly.parse_sequence_reports({"reports": reports})
        assert len(labels) <= assembly.MAX_STORED_LABELS

    def test_survives_malformed_payloads(self):
        """Same never-raises contract as parse_report."""
        assert assembly.parse_sequence_reports({}) == {}
        assert assembly.parse_sequence_reports({"reports": None}) == {}
        assert assembly.parse_sequence_reports({"reports": "nope"}) == {}
        assert assembly.parse_sequence_reports({"reports": [None, 3, "x"]}) == {}
        # A record with no usable accession contributes nothing but must not raise.
        assert assembly.parse_sequence_reports({"reports": [{"chr_name": "I"}]}) == {}
        # A record with an accession but no name has nothing to say either.
        assert (
            assembly.parse_sequence_reports(
                {"reports": [{"refseq_accession": "NC_1.1"}]}
            )
            == {}
        )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose exec -T api python -m pytest tests/metadata/test_sequence_names.py -q
```

Expected: FAIL — `AttributeError: module 'app.metadata.assembly' has no attribute 'parse_sequence_reports'`.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/metadata/assembly.py`, add near the top alongside `DATASETS`:

```python
# Bounded like the parser's MAX_STORED_CONTIGS: the strip draws at most 24 bars
# and lists the remainder from the stored lengths, so labels past this window
# would have nothing to label.
MAX_STORED_LABELS = 50
```

Add after `parse_report`:

```python
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
    # sort_order is the assembly's own ordering, so a truncated map keeps the
    # leading sequences rather than an arbitrary slice.
    records = [r for r in reports if isinstance(r, dict)]
    records.sort(key=lambda r: _int(r.get("sort_order")) or 0)

    for record in records:
        if len(labels) >= MAX_STORED_LABELS:
            break
        role = _text(record.get("role"))
        if role == "assembled-molecule":
            label = _text(record.get("chr_name")) or _text(record.get("sequence_name"))
        else:
            label = _text(record.get("sequence_name")) or _text(record.get("chr_name"))
        if not label:
            continue
        for key in ("refseq_accession", "genbank_accession"):
            accession = _text(record.get(key))
            if accession:
                labels[accession] = label

    return labels
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose exec -T api python -m pytest tests/metadata/test_sequence_names.py -q
```

Expected: PASS — 7 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/metadata/assembly.py backend/tests/metadata/test_sequence_names.py backend/tests/fixtures/ncbi_seqreports_*.json
git commit -m "feat: parse NCBI sequence reports into a label map"
```

---

### Task 2: Fetch the sequence report

The network wrapper, mirroring `lookup()`'s structure and its never-raises contract.

**Files:**
- Modify: `backend/app/metadata/assembly.py`
- Modify: `backend/tests/metadata/test_sequence_names.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/metadata/test_sequence_names.py`:

```python
from unittest.mock import patch


class TestLookupSequenceNames:
    def test_returns_labels_from_a_real_payload(self):
        body = (FIXTURES / YEAST).read_bytes()
        with patch("app.metadata.assembly._get", return_value=body) as get:
            labels = assembly.lookup_sequence_names("GCF_000146045.2")
        assert labels["NC_001133.9"] == "I"
        assert "sequence_reports" in get.call_args[0][0]

    def test_rejects_a_malformed_accession_without_calling_out(self):
        with patch("app.metadata.assembly._get") as get:
            assert assembly.lookup_sequence_names("not-an-accession") is None
        get.assert_not_called()

    def test_returns_none_when_the_request_fails(self):
        with patch("app.metadata.assembly._get", return_value=None):
            assert assembly.lookup_sequence_names("GCF_000146045.2") is None

    def test_returns_none_on_unparseable_json(self):
        with patch("app.metadata.assembly._get", return_value=b"<html>nope"):
            assert assembly.lookup_sequence_names("GCF_000146045.2") is None

    def test_returns_none_rather_than_an_empty_map(self):
        """An empty map and a failed lookup are the same to the caller, and
        None keeps a meaningless `sequence_labels: {}` out of facts."""
        with patch("app.metadata.assembly._get", return_value=b'{"reports": []}'):
            assert assembly.lookup_sequence_names("GCF_000146045.2") is None

    def test_never_raises_when_the_helper_explodes(self):
        with patch("app.metadata.assembly._get", side_effect=RuntimeError("boom")):
            assert assembly.lookup_sequence_names("GCF_000146045.2") is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose exec -T api python -m pytest tests/metadata/test_sequence_names.py -q
```

Expected: FAIL — `has no attribute 'lookup_sequence_names'`.

- [ ] **Step 3: Write minimal implementation**

Add to `backend/app/metadata/assembly.py`, after `lookup`:

```python
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
```

Note: unlike `lookup`, there is no unversioned-accession fallback. `lookup` has
already resolved the accession by the time this is called, so a second round of
version guessing would just be a wasted request.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose exec -T api python -m pytest tests/metadata/test_sequence_names.py -q
```

Expected: PASS — 13 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/metadata/assembly.py backend/tests/metadata/test_sequence_names.py
git commit -m "feat: fetch per-sequence chromosome names from NCBI"
```

---

### Task 3: Store the labels at ingest

**Files:**
- Modify: `backend/app/metadata/enrich.py:203` (the `result.facts = meta.to_facts()` line)
- Modify: `backend/tests/metadata/test_sequence_names.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/metadata/test_sequence_names.py`:

```python
from app.models import FormatKind


class TestEnrichmentStoresLabels:
    def _meta(self):
        return assembly.AssemblyMetadata(
            accession="GCF_000146045.2", assembly_name="R64"
        )

    def test_labels_land_in_facts(self):
        labels = {"NC_001133.9": "I"}
        with (
            patch("app.metadata.assembly.lookup", return_value=self._meta()),
            patch("app.metadata.assembly.lookup_sequence_names", return_value=labels),
        ):
            result = enrich.enrich_from_assembly(
                filename="GCF_000146045.2_R64_genomic.fna",
                existing_metadata={},
                format_kind=FormatKind.FASTA,
            )
        assert result.facts["sequence_labels"] == labels

    def test_a_failed_name_lookup_leaves_the_rest_intact(self):
        """The names are a bonus. Losing them must not cost the stats that the
        assembly lookup already succeeded in fetching."""
        with (
            patch("app.metadata.assembly.lookup", return_value=self._meta()),
            patch("app.metadata.assembly.lookup_sequence_names", return_value=None),
        ):
            result = enrich.enrich_from_assembly(
                filename="GCF_000146045.2_R64_genomic.fna",
                existing_metadata={},
                format_kind=FormatKind.FASTA,
            )
        assert "sequence_labels" not in result.facts
        assert result.facts["ncbi_assembly_name"] == "R64"

    def test_a_raising_name_lookup_does_not_break_ingest(self):
        with (
            patch("app.metadata.assembly.lookup", return_value=self._meta()),
            patch(
                "app.metadata.assembly.lookup_sequence_names",
                side_effect=RuntimeError("boom"),
            ),
        ):
            result = enrich.enrich_from_assembly(
                filename="GCF_000146045.2_R64_genomic.fna",
                existing_metadata={},
                format_kind=FormatKind.FASTA,
            )
        assert "sequence_labels" not in result.facts
        assert result.facts["ncbi_assembly_name"] == "R64"
```

Add `enrich` to the module's imports if it is not already there:
`from app.metadata import assembly, enrich`

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose exec -T api python -m pytest tests/metadata/test_sequence_names.py -q
```

Expected: FAIL — `KeyError: 'sequence_labels'` on the first of the three.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/metadata/enrich.py`, replace `result.facts = meta.to_facts()` with:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose exec -T api python -m pytest tests/metadata/test_sequence_names.py -q
```

Expected: PASS — 16 tests.

- [ ] **Step 5: Stop the existing tests making live network calls**

**This will bite, and it is not hypothetical.** Nine tests in
`backend/tests/storage/test_assembly_accession.py` (lines 232-321) patch
`assembly.lookup` but know nothing about `lookup_sequence_names`. After Step 3
every one of them reaches NCBI for real: slow, flaky, and dependent on the
machine being online.

Add an autouse fixture to that file's enrichment test class so the new call is
stubbed everywhere by default:

```python
    @pytest.fixture(autouse=True)
    def _no_sequence_names(self):
        """The label lookup is a second request these tests never meant to make.

        Without this they all hit NCBI live once enrichment started fetching
        names.
        """
        with patch.object(assembly, "lookup_sequence_names", return_value=None):
            yield
```

Put it in the class containing the `enrich_from_assembly` tests. If they are not
in a class, make it a module-level `@pytest.fixture(autouse=True)`.

- [ ] **Step 6: Run the whole backend suite for regressions**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose exec -T api python -m pytest tests/ -q
```

Expected: PASS, and noticeably not slower than before. If the run takes
appreciably longer, something is still making a real request — find it rather
than accepting it.

To prove the stub actually covers them, run that file with networking blocked
and confirm it still passes:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose exec -T api python -m pytest tests/storage/test_assembly_accession.py -q
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/metadata/enrich.py backend/tests/metadata/test_sequence_names.py backend/tests/storage/test_assembly_accession.py
git commit -m "feat: store per-sequence chromosome names at ingest"
```

---

### Task 4: Attach labels to bars

**Files:**
- Modify: `frontend/src/lib/chromosomes.ts`
- Modify: `frontend/src/lib/chromosomes.test.ts`

- [ ] **Step 1: Write the failing test**

Append inside the `describe` block in `frontend/src/lib/chromosomes.test.ts`:

```ts
  it("labels bars from sequence_labels", () => {
    const view = classifyChromosomes({
      sequence_names: Object.keys(YEAST_LENGTHS),
      sequence_lengths: YEAST_LENGTHS,
      sequence_labels: { "NC_001136.10": "IV", "NC_001224.1": "MT" },
    });
    if (view.kind !== "drawable") throw new Error("expected drawable");
    expect(view.bars[0].label).toBe("IV");
    expect(view.bars.find((b) => b.name === "NC_001224.1")?.label).toBe("MT");
  });

  it("leaves bars unlabelled when a name has no entry", () => {
    const view = classifyChromosomes({
      sequence_names: Object.keys(YEAST_LENGTHS),
      sequence_lengths: YEAST_LENGTHS,
      sequence_labels: { "NC_001136.10": "IV" },
    });
    if (view.kind !== "drawable") throw new Error("expected drawable");
    expect(view.bars.find((b) => b.name === "NC_001133.9")?.label).toBeUndefined();
  });

  // Existing references have no labels at all and must be untouched.
  it("is unchanged when sequence_labels is absent", () => {
    const view = classifyChromosomes({
      sequence_names: Object.keys(YEAST_LENGTHS),
      sequence_lengths: YEAST_LENGTHS,
    });
    if (view.kind !== "drawable") throw new Error("expected drawable");
    expect(view.bars).toHaveLength(17);
    expect(view.bars.every((b) => b.label === undefined)).toBe(true);
  });

  // Labels are cosmetic: a garbage value must not change classification.
  it("ignores a wrong-typed sequence_labels", () => {
    const view = classifyChromosomes({
      sequence_names: Object.keys(YEAST_LENGTHS),
      sequence_lengths: YEAST_LENGTHS,
      sequence_labels: "not an object",
    });
    expect(view.kind).toBe("drawable");
  });
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && cp <worktree>/frontend/src/lib/chromosomes.ts <worktree>/frontend/src/lib/chromosomes.test.ts frontend/src/lib/ && docker compose exec -T web npx vitest run src/lib/chromosomes.test.ts
```

Expected: FAIL — `expected undefined to be 'IV'`.

- [ ] **Step 3: Write minimal implementation**

In `frontend/src/lib/chromosomes.ts`, extend `Bar`:

```ts
export interface Bar {
  name: string;
  length: number;
  /** NCBI's name for this sequence ("IV", "MT", "chr11-scaffold01"), when the
   *  assembly lookup found one. Absent on references ingested before labels
   *  were fetched, and on any locally-assembled file. */
  label?: string;
}
```

In `classifyChromosomes`, read the map alongside `lengths`:

```ts
  const labels =
    facts.sequence_labels &&
    typeof facts.sequence_labels === "object" &&
    !Array.isArray(facts.sequence_labels)
      ? (facts.sequence_labels as Record<string, string>)
      : {};
```

Then attach when building `entries`:

```ts
  const entries: Bar[] = Object.entries(lengths).map(([name, length]) => {
    const label = labels[name];
    return {
      name,
      length: Number(length) || 0,
      ...(typeof label === "string" && label ? { label } : {}),
    };
  });
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose exec -T web npx vitest run src/lib/chromosomes.test.ts
```

Expected: PASS — 17 tests.

Then clean up the main repo:

```bash
rm frontend/src/lib/chromosomes.ts frontend/src/lib/chromosomes.test.ts && git status --porcelain
```

Expected: empty output.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/chromosomes.ts frontend/src/lib/chromosomes.test.ts
git commit -m "feat: attach NCBI chromosome names to bars"
```

---

### Task 5: Show the labels, and fix the wrong message

Two changes to the strip. The second is a correction to what already shipped:
the `needs-qc` copy tells users to re-run QC, but `facts.sequence_lengths` is
written only by the ingest parser (`storage/parsers.py:495`), while `run_qc` is
a FASTQ read-quality handler that never touches it. Running QC does nothing for
the strip. Re-ingest is the action that works — verified on the real
`GCA_000146045.2_R64_genomic.fna`, which went from no lengths to 16 entries.

**Files:**
- Modify: `frontend/src/components/ChromosomeStrip.tsx`

No component tests: this repo has no jsdom and zero `.test.tsx` files.
Verification is Task 6, in the browser.

- [ ] **Step 1: Prefer the label for the caption**

Find where the bar caption text is derived from the accession and prefer
`bar.label` when present, keeping the existing derivation as the fallback:

```tsx
const caption = bar.label ?? shortLabel(bar.name);
```

(`shortLabel` is whatever the existing accession-digits helper is called — read
the file and use its real name rather than renaming it.)

The tooltip and accessible name must carry the **full** accession plus length,
and the label too when there is one, so nothing is only available abbreviated:

```tsx
const described = bar.label
  ? `${bar.label} · ${bar.name} · ${formatBases(bar.length)}`
  : `${bar.name} · ${formatBases(bar.length)}`;
```

Use `described` for both the `<title>` and the `aria-label`.

- [ ] **Step 2: Correct the needs-qc copy**

Replace the message text:

```tsx
        <div className="chrom-note">
          Sequence lengths weren’t measured for this file. Re-ingest it to draw
          the chromosome map — the Computations panel has the button.
        </div>
```

Do **not** rename the `needs-qc` union tag; it is referenced by the classifier
and its tests, and renaming buys the user nothing.

- [ ] **Step 3: Verify it type-checks**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && cp <worktree>/frontend/src/lib/chromosomes.ts <worktree>/frontend/src/components/ChromosomeStrip.tsx frontend/src/lib/ frontend/src/components/ && docker compose exec -T web npx tsc --noEmit
```

(Copy each file to its matching directory.) Expected: no errors. Then restore
the main repo and confirm `git status --porcelain` is empty.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ChromosomeStrip.tsx
git commit -m "feat: label bars with NCBI names, and point at re-ingest"
```

---

### Task 6: Verify against real data

This is the step CLAUDE.md asks for: check the rule against the real database,
not only its unit tests.

- [ ] **Step 1: Merge to main and restart the worker**

The stack runs from the main repo, and `worker` does not hot-reload. From the
main repo root:

```bash
git merge --no-ff <branch> && docker compose up -d --build api worker
```

- [ ] **Step 2: Re-ingest the yeast reference**

```bash
python3 -c "import urllib.request;req=urllib.request.Request('http://localhost:5173/api/v1/objects/6a6a3416ca692855997b9ece/reingest',method='POST',data=b'');print(urllib.request.urlopen(req,timeout=60).read())"
```

- [ ] **Step 3: Confirm the labels landed**

After ~20 seconds:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose exec -T api python -c "
import asyncio
from app.db.client import get_db, connect_to_mongo
from bson import ObjectId
async def main():
    await connect_to_mongo(); db=get_db()
    d = await db.objects.find_one({'_id': ObjectId('6a6a3416ca692855997b9ece')})
    labels = (d.get('facts') or {}).get('sequence_labels') or {}
    print('labels:', len(labels))
    for k in ['NC_001136.10','NC_001224.1','NC_001133.9']:
        print(' ', k, '->', labels.get(k))
asyncio.run(main())"
```

Expected: `NC_001136.10 -> IV`, `NC_001224.1 -> MT`, `NC_001133.9 -> I`.

- [ ] **Step 4: Re-ingest the GCA file and confirm the GenBank namespace**

Object `6a6a340fca692855997b9ecb`, names like `BK006935.2`. Same procedure.
Expected: `BK006935.2 -> I`. **This is the cross-namespace claim** — if these
come back unlabelled, keying on both accessions is not working.

- [ ] **Step 5: Check the Aspergillus duplicate case**

Re-ingest `6a6a9b75...` (`GCF_000002445.2_ASM244v1_genomic.fna`, the one that
has lengths). Expected: `NT_165288.1 -> chr11-scaffold01` and
`NT_165287.1 -> chr11-scaffold02` — two different labels, not two "11"s.

- [ ] **Step 6: Look at it in the browser**

At localhost:5173, open the yeast reference's Quality tab. Expect bars captioned
`IV`, `VII`, … `MT` instead of `1136`, `1139`, … `1224`. Hover a bar: the
tooltip must still carry the full accession and length. Confirm the strip is
otherwise unchanged — same bar count, same ranking, viewer still opens.

- [ ] **Step 7: Confirm the untouched path still works**

Open a reference that has **not** been re-ingested. It must render exactly as
before, with accession-digit captions and no errors.

- [ ] **Step 8: Commit any fixes**

```bash
git add -A && git commit -m "fix: <what the verification pass turned up>"
```

---

## Notes for the implementer

- **The duplicate-`chr_name` case is the whole reason for the `role` check.** If a test starts passing with a naive `chr_name` implementation, the test is wrong, not the rule.
- **Labels are cosmetic and must never affect classification.** A missing, empty, or wrong-typed `sequence_labels` changes which caption a bar shows and nothing else — not which bucket a reference lands in, not which bars are drawn, not which are linkable.
- **A failed name lookup must not cost the assembly stats.** It is a second request layered onto a lookup that already succeeded.
- **Do not add a re-ingest button to the strip.** The Computations panel already has one; the message points at it.
- **Run `docker compose` from the main repo root, never a worktree.**
