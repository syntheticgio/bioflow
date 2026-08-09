# Sequence Composition & Bias QC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-sequence GC distribution chart and a per-base N content chart to the reads QC tab, with a three-tier expected-GC reference curve and an N-spike grading rule.

**Architecture:** The GC histogram comes from ingest (`sequence_stats.fastq_stats`, which already computes and discards the per-read GC values); the N curve comes from fastp's `content_curves.N`, currently parsed and dropped. A new `expected_gc` service resolves the reference curve from a project reference FASTA, then a cited genome table, then nothing. Two new hand-rolled SVG charts render them.

**Tech Stack:** Python 3 / FastAPI / Beanie (MongoDB) / pytest on the backend; React + TypeScript / Vite / vitest on the frontend. No charting library — the existing `SequenceCharts.tsx` convention is hand-rolled SVG.

**Spec:** [`docs/superpowers/specs/2026-08-09-sequence-composition-bias-qc-design.md`](../specs/2026-08-09-sequence-composition-bias-qc-design.md)

---

## Before you start

**You are working in a git worktree.** Two commands differ from what you may be used to, and using the wrong one silently tests or runs *main's* code instead of this branch's:

- **Tests:** `./backend/run-worktree-tests.sh tests/ -q` (NOT `docker compose exec api python -m pytest`)
- **Running the app:** `./ops/worktree-up.sh` — serves this worktree's UI at **localhost:5273**, API on 8100. Never plain `docker compose` from here; a `PreToolUse` hook blocks it.

Frontend unit tests run from `frontend/`: `npx vitest run src/lib/readQuality.test.ts`.

## File Structure

**Backend — create:**
- `backend/app/services/expected_gc.py` — the three-tier cascade. One responsibility: answer "what GC should this file have, and who says so?" Kept out of `object_service` because it is a read-only derivation with no ownership or persistence concerns.
- `backend/tests/services/test_expected_gc.py` — its tests.

**Backend — modify:**
- `backend/app/storage/sequence_stats.py` — swap the `per_read_gc` list for a histogram counter (~line 88, 152).
- `backend/app/pipelines/fastp_runner.py` — parse `content_curves.N` in `parse_qc_facts` (~line 211).
- `backend/app/api/v1/schemas.py` — `ExpectedGc` model + field on `ObjectDetail` (line 173).
- `backend/app/api/v1/objects.py` — populate it in `get_object` (line 44).
- `backend/tests/storage/test_sequence_stats.py`, `backend/tests/pipelines/test_fastp_runner.py` — extend.

**Frontend — modify:**
- `frontend/src/components/SequenceCharts.tsx` — two new exported components.
- `frontend/src/components/DetailPanel.tsx` — mount them in the `qc-charts` grid (line 1006).
- `frontend/src/lib/readQuality.ts` — the N-spike rule.
- `frontend/src/lib/readQuality.test.ts` — its tests.
- `frontend/src/api/types.ts` — `expected_gc` on the detail type.
- `frontend/src/components/HelpCalculations.tsx` — document the rule and the cascade.

---

### Task 1: GC histogram at ingest

**Files:**
- Modify: `backend/app/storage/sequence_stats.py:88,112,152`
- Test: `backend/tests/storage/test_sequence_stats.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/storage/test_sequence_stats.py`. The existing `write_fastq` helper defaults to `seq="AAAACCCGGT"` — 3 G/C out of 10 bases, so every read is exactly 30% GC.

```python
class TestGcHistogram:
    def test_bins_every_read_at_its_own_gc(self, tmp_path):
        """The default fixture read is 30% GC (C=3, G=2, of 10 bases), so 100
        identical reads must land in one bin of 100 -- not spread."""
        r = ss.fastq_stats(write_fastq(tmp_path / "t.fastq", 100), Compression.NONE)
        assert r["gc_per_read_histogram"] == [{"gc_percent": 50, "count": 100}]

    def test_two_populations_produce_two_bins(self, tmp_path):
        """The contamination signal the chart exists to show: a mixed library
        is two peaks, and an aggregate mean would hide it between them."""
        p = tmp_path / "t.fastq"
        with open(p, "w") as f:
            for i in range(30):
                f.write(f"@a{i}\nAAAAAAAAAA\n+\nIIIIIIIIII\n")   # 0% GC
            for i in range(70):
                f.write(f"@b{i}\nGGGGGGGGGG\n+\nIIIIIIIIII\n")   # 100% GC
        hist = ss.fastq_stats(p, Compression.NONE)["gc_per_read_histogram"]
        assert hist == [
            {"gc_percent": 0, "count": 30},
            {"gc_percent": 100, "count": 70},
        ]

    def test_bins_are_sorted_and_sparse(self, tmp_path):
        """Sorted so the chart can draw it as a line without re-sorting;
        sparse so an empty bin costs nothing in the document."""
        p = tmp_path / "t.fastq"
        with open(p, "w") as f:
            f.write("@a\nGGGGGGGGGG\n+\nIIIIIIIIII\n")           # 100%
            f.write("@b\nAAAAAAAAAA\n+\nIIIIIIIIII\n")           # 0%
            f.write("@c\nACGTACGTAC\n+\nIIIIIIIIII\n")           # 50%
        hist = ss.fastq_stats(p, Compression.NONE)["gc_per_read_histogram"]
        assert [b["gc_percent"] for b in hist] == [0, 50, 100]

    def test_mean_is_unchanged_by_the_histogram(self, tmp_path):
        """Regression guard: `gc_per_read_mean` is an existing fact other
        surfaces read, and swapping the accumulator must not move it."""
        r = ss.fastq_stats(write_fastq(tmp_path / "t.fastq", 100), Compression.NONE)
        assert r["gc_per_read_mean"] == 50.0

    def test_reads_with_no_acgt_are_not_binned(self, tmp_path):
        """An all-N read has no GC to speak of. Counting it as 0% GC would
        invent a peak at zero out of unsequenced bases."""
        p = tmp_path / "t.fastq"
        with open(p, "w") as f:
            f.write("@a\nNNNNNNNNNN\n+\nIIIIIIIIII\n")
            f.write("@b\nACGTACGTAC\n+\nIIIIIIIIII\n")
        hist = ss.fastq_stats(p, Compression.NONE)["gc_per_read_histogram"]
        assert hist == [{"gc_percent": 50, "count": 1}]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/storage/test_sequence_stats.py -q -k GcHistogram`
Expected: FAIL — `KeyError: 'gc_per_read_histogram'`

- [ ] **Step 3: Implement**

In `backend/app/storage/sequence_stats.py`, replace the `per_read_gc` list with a counter.

At line ~88, replace:

```python
    per_read_gc: list[float] = []
```

with:

```python
    # Binned at integer GC% rather than kept as a list of floats. The
    # distribution is what the chart draws and the mean is derived from the
    # same counts, so the per-read values themselves are never needed -- and a
    # 200k-read sample previously held a 200k-element float list purely to
    # average it.
    gc_histogram: Counter[int] = Counter()
```

At line ~111, replace:

```python
                if acgt:
                    per_read_gc.append(100.0 * gc / acgt)
```

with:

```python
                # Reads with no A/C/G/T at all (all-N) are skipped: they have
                # no GC ratio, and binning them at 0% would invent a peak out
                # of unsequenced bases.
                if acgt:
                    gc_histogram[round(100.0 * gc / acgt)] += 1
```

At line ~152, replace:

```python
    if per_read_gc:
        facts["gc_per_read_mean"] = round(sum(per_read_gc) / len(per_read_gc), 2)
```

with:

```python
    if gc_histogram:
        binned = sorted(gc_histogram.items())
        facts["gc_per_read_histogram"] = [
            {"gc_percent": pct, "count": n} for pct, n in binned
        ]
        total = sum(gc_histogram.values())
        facts["gc_per_read_mean"] = round(
            sum(pct * n for pct, n in binned) / total, 2
        )
```

`Counter` is already imported at the top of the file.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/storage/test_sequence_stats.py -q`
Expected: PASS, including the pre-existing tests in the file.

- [ ] **Step 5: Commit**

```bash
git add backend/app/storage/sequence_stats.py backend/tests/storage/test_sequence_stats.py
git commit -m "feat(qc): bin per-read GC into a histogram at ingest"
```

---

### Task 2: N-per-position curve from fastp

**Files:**
- Modify: `backend/app/pipelines/fastp_runner.py:211-228`
- Test: `backend/tests/pipelines/test_fastp_runner.py:504-542`

**Context you need:** `SAMPLE_REPORT` (line 252) has no `read1_before_filtering` block at all today. You will add one. The real fastp output for that block was captured from the tool in this project's image:

```
read1_before_filtering keys: total_reads, total_bases, q20_bases, q30_bases,
  total_cycles, quality_curves, content_curves, kmer_count,
  overrepresented_sequences
content_curves keys: A, T, C, G, N, GC
N values look like: [0, 0, 0, 0, 0.666667, 0.333333, 0, 0, 0, 0]
```

**Values are fractions, not percentages.** They need `* 100`.

There is an existing test at line 532, `test_drops_the_per_cycle_curves`, asserting no `histogram` key survives into facts. Its subject is fastp's `insert_size.histogram` (512 zeros) and the quality curves — not the N curve — and it stays true after this change because `qc_n_per_position` contains no key named `histogram`. Leave it alone but read it before you start, so you know why it is there.

- [ ] **Step 1: Write the failing tests**

First add a `read1_before_filtering` block to `SAMPLE_REPORT` in `backend/tests/pipelines/test_fastp_runner.py`. Insert it after the `"duplication": {"rate": 0.012},` line:

```python
    "read1_before_filtering": {
        "total_reads": 20000,
        "total_cycles": 8,
        "quality_curves": {"mean": [36.0] * 8},
        "content_curves": {
            "A": [0.25] * 8,
            "T": [0.25] * 8,
            "C": [0.25] * 8,
            "G": [0.25] * 8,
            # Fractions, exactly as fastp writes them. Cycle 5 (1-indexed) is
            # a 40% N spike; every other cycle is clean.
            "N": [0.0, 0.0, 0.0, 0.0, 0.4, 0.0, 0.0, 0.0],
            "GC": [0.5] * 8,
        },
    },
```

Then add to `class TestParseQcFacts`:

```python
    def test_reports_n_content_per_cycle(self, facts):
        """fastp writes fractions; the app's facts are percentages
        everywhere else (base_composition, gc_content_percent), so these are
        scaled at parse time rather than in the chart."""
        assert facts["qc_n_per_position"] == [
            {"position": 1, "percent": 0.0},
            {"position": 2, "percent": 0.0},
            {"position": 3, "percent": 0.0},
            {"position": 4, "percent": 0.0},
            {"position": 5, "percent": 40.0},
            {"position": 6, "percent": 0.0},
            {"position": 7, "percent": 0.0},
            {"position": 8, "percent": 0.0},
        ]

    def test_an_all_zero_n_curve_is_omitted(self, tmp_path):
        """The common case for clean Illumina data. A flat line at zero is a
        chart that never says anything, so absent means 'nothing to report'
        the way every other block in QcReport self-suppresses."""
        report = json.loads(json.dumps(SAMPLE_REPORT))
        report["read1_before_filtering"]["content_curves"]["N"] = [0.0] * 8
        p = tmp_path / "qc.json"
        p.write_text(json.dumps(report))
        assert "qc_n_per_position" not in fastp_runner.parse_qc_facts(p)

    def test_a_missing_curves_block_is_not_fatal(self, tmp_path):
        """An older fastp, or a report written before this block existed."""
        report = json.loads(json.dumps(SAMPLE_REPORT))
        del report["read1_before_filtering"]
        p = tmp_path / "qc.json"
        p.write_text(json.dumps(report))
        facts = fastp_runner.parse_qc_facts(p)
        assert "qc_n_per_position" not in facts
        assert facts["qc_tool"] == "fastp"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_fastp_runner.py -q -k "n_content or n_curve or curves_block"`
Expected: FAIL — `KeyError: 'qc_n_per_position'`

- [ ] **Step 3: Implement**

In `backend/app/pipelines/fastp_runner.py`, add this helper immediately above `def parse_qc_facts`:

```python
def _n_content_curve(raw: dict) -> list[dict] | None:
    """Percent N per cycle, from fastp's `content_curves`.

    fastp writes fractions (0.4 for 40%); every other fact in this app is a
    percentage, so the scaling happens here rather than in the chart.

    Returns None when the curve is absent or entirely zero. All-zero is the
    ordinary state of clean Illumina data, and a flat line at zero is a chart
    that never says anything -- so it is reported as nothing to say, which is
    how the rest of the QC facts signal the same thing.

    Read1 only. A paired run's read2 curve is a second chart's worth of
    question, and worth adding when someone has a file where the two differ.
    """
    curve = (
        raw.get("read1_before_filtering", {})
        .get("content_curves", {})
        .get("N")
    )
    if not curve or not any(curve):
        return None
    return [
        {"position": i + 1, "percent": round(value * 100, 4)}
        for i, value in enumerate(curve)
    ]
```

Then in `parse_qc_facts`, after the `facts = {...}` dict literal and before the `adapters = ...` line, add:

```python
    n_curve = _n_content_curve(raw)
    if n_curve:
        facts["qc_n_per_position"] = n_curve
```

The trailing `return {k: v for k, v in facts.items() if v is not None}` already handles omission, but the explicit guard keeps the key out entirely rather than relying on that filter.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_fastp_runner.py -q`
Expected: PASS — all tests in the file, including `test_drops_the_per_cycle_curves`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/fastp_runner.py backend/tests/pipelines/test_fastp_runner.py
git commit -m "feat(qc): parse per-cycle N content from fastp's content_curves"
```

---

### Task 3: The cited genome table

**Files:**
- Create: `backend/app/services/expected_gc.py`
- Create: `backend/tests/services/test_expected_gc.py`

This task builds tier 2 and the data structures. Tier 1 (the project reference) is Task 4, because it needs a database query and this does not.

**Every entry carries a citation.** This follows the `TOOL_META` precedent in `backend/app/pipelines/tools.py`: a number on a surface that reads as authoritative is worse than a blank if it is wrong. Verify each value against the source named before writing it down — do not recall them.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/services/test_expected_gc.py`:

```python
"""The expected-GC cascade.

Tier 1 (a measured project reference) has its own tests in this file once the
database seam exists; these cover the table and the resolution order.
"""

import pytest

from app.services import expected_gc


class TestGenomeTable:
    def test_every_entry_carries_a_citation(self):
        """The reason the table is allowed to exist at all. A GC percentage
        drawn as an authoritative reference curve with no source is the
        fabricated-value failure TOOL_META's required fields exist to stop."""
        for key, entry in expected_gc.GENOME_GC.items():
            assert entry.citation, f"{key} has no citation"
            assert entry.source_name, f"{key} has no source_name"

    def test_every_value_is_a_plausible_percentage(self):
        for key, entry in expected_gc.GENOME_GC.items():
            assert 0 < entry.percent < 100, f"{key} has an impossible GC"

    def test_keys_are_already_normalized(self):
        """Lookup normalizes the user's input, not the table. A table key that
        is not already normalized is unreachable, silently."""
        from app.models import normalize_organism

        for key in expected_gc.GENOME_GC:
            assert key == normalize_organism(key)


class TestFromOrganism:
    def test_resolves_a_known_organism(self):
        got = expected_gc.from_organism("Homo sapiens")
        assert got is not None
        assert got.source == "table"
        assert 40 < got.percent < 42

    def test_is_case_and_whitespace_insensitive(self):
        """'homo sapiens' and 'Homo  sapiens' are one species; normalize_organism
        is what the OrganismBlurb cache already keys on."""
        a = expected_gc.from_organism("homo  sapiens")
        b = expected_gc.from_organism("Homo sapiens")
        assert a is not None and b is not None
        assert a.percent == b.percent

    def test_an_unknown_organism_resolves_to_nothing(self):
        assert expected_gc.from_organism("Nonexistent organism") is None

    def test_blank_input_resolves_to_nothing(self):
        assert expected_gc.from_organism("") is None
        assert expected_gc.from_organism(None) is None

    def test_attribution_names_the_organism_and_the_assembly(self):
        """The curve must always be able to say where its number came from.

        `source_name` lives on the table entry, not on the resolved
        ExpectedGc -- the resolved object carries only the finished
        attribution string, so this reads the table for the expected text."""
        got = expected_gc.from_organism("Escherichia coli")
        assert got is not None
        assert "Escherichia coli" in got.attribution
        assert expected_gc.GENOME_GC["escherichia coli"].source_name in got.attribution
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/services/test_expected_gc.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.expected_gc'`

- [ ] **Step 3: Implement**

Create `backend/app/services/expected_gc.py`:

```python
"""What GC content a read file should be expected to show, and who says so.

Read files are not graded on GC and this does not change that -- see
`readQuality.ts`, which has always refused to demote on GC because expected GC
is a property of the organism and an unusual value is not evidence of a
problem without knowing the source. This module supplies the missing half of
that sentence: when the source *is* known, the QC chart can draw what to
expect, attributed to whatever said so.

Three tiers, in order of how much they can be trusted:

1. Measured from a reference genome in the same project (`from_project`).
   A real measurement of a real file the user has.
2. A small table of well-established genomes, each with a citation.
3. Nothing. The chart omits the curve and offers a fitted normal instead,
   labelled as a fit to the observed data rather than an expectation of it.

`OrganismBlurb` is deliberately not a source here. It is AI-generated prose --
"Nothing here is authoritative... it is page colour", by its own docstring --
and a recalled GC percentage rendered as an authoritative reference line is
exactly the fabricated value that TOOL_META's required `license`/`citation`
fields exist to prevent.
"""

from dataclasses import dataclass

from app.models import normalize_organism


@dataclass(frozen=True)
class GenomeGc:
    """One published genome's GC content, and where the figure came from."""

    percent: float
    # The assembly or database the figure is quoted from, e.g. "GRCh38".
    source_name: str
    # Enough to check the number without guessing which release was meant.
    citation: str


@dataclass(frozen=True)
class ExpectedGc:
    """A resolved expectation, ready to draw and to attribute."""

    percent: float
    # "reference" (measured from a project file) or "table" (published value).
    source: str
    # Human-readable, shown next to the curve. Always says where it came from.
    attribution: str


# Hand-maintained, and deliberately short. This is the third of the three
# registry shapes CLAUDE.md describes: its keys belong to an open vocabulary
# (any organism a user might type), so it cannot be derived from an enum and
# must not try to be exhaustive. A miss falls to tier 3, which is a correct
# outcome rather than a gap -- the wrong instinct here is to pad the table with
# half-remembered values to raise the hit rate.
#
# Keys must be `normalize_organism` output; a test enforces it, because an
# unnormalized key is silently unreachable.
GENOME_GC: dict[str, GenomeGc] = {
    "homo sapiens": GenomeGc(
        percent=40.9,
        source_name="GRCh38",
        citation="Ensembl GRCh38.p14 assembly statistics",
    ),
    "mus musculus": GenomeGc(
        percent=42.0,
        source_name="GRCm39",
        citation="Ensembl GRCm39 assembly statistics",
    ),
    "escherichia coli": GenomeGc(
        percent=50.8,
        source_name="K-12 MG1655",
        citation="NCBI RefSeq NC_000913.3 assembly statistics",
    ),
    "saccharomyces cerevisiae": GenomeGc(
        percent=38.3,
        source_name="R64",
        citation="SGD R64-1-1 genome statistics",
    ),
    "drosophila melanogaster": GenomeGc(
        percent=42.0,
        source_name="BDGP6",
        citation="FlyBase BDGP6 assembly statistics",
    ),
    "caenorhabditis elegans": GenomeGc(
        percent=35.4,
        source_name="WBcel235",
        citation="WormBase WBcel235 assembly statistics",
    ),
    "arabidopsis thaliana": GenomeGc(
        percent=36.1,
        source_name="TAIR10",
        citation="TAIR10 genome release statistics",
    ),
    "plasmodium falciparum": GenomeGc(
        percent=19.3,
        source_name="3D7",
        citation="PlasmoDB 3D7 genome statistics",
    ),
}


def from_organism(organism: str | None) -> ExpectedGc | None:
    """Tier 2: a published figure for a named organism.

    Never infers the organism -- from a filename, from read content, or from
    anything else. An organism guessed and then drawn as an authoritative
    reference curve is the same fabrication risk the module docstring rejects
    `OrganismBlurb` for.
    """
    if not organism or not organism.strip():
        return None
    entry = GENOME_GC.get(normalize_organism(organism))
    if entry is None:
        return None
    return ExpectedGc(
        percent=entry.percent,
        source="table",
        attribution=(
            f"expected {entry.percent}% for {organism.strip()} "
            f"({entry.source_name})"
        ),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_expected_gc.py -q`
Expected: PASS — 9 tests.

- [ ] **Step 5: Verify each GC figure against its cited source**

Do not skip this. Check each of the eight values against the source named in its `citation` field, and correct any that are wrong. A wrong number here is worse than an absent one because the chart presents it as authoritative. If a figure cannot be confirmed, delete that entry rather than shipping an unverified value — a smaller table is not a defect.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/expected_gc.py backend/tests/services/test_expected_gc.py
git commit -m "feat(qc): cited genome GC table for the expected-GC cascade"
```

---

### Task 4: Tier 1 — measured from a project reference

**Files:**
- Modify: `backend/app/services/expected_gc.py`
- Test: `backend/tests/services/test_expected_gc.py`

**The trap this task exists to avoid** is the one CLAUDE.md records against `suggestion_service`: a project that has downloaded an NCBI assembly also holds `protein.faa` and `cds_from_genomic.fna`, which are the same `FormatKind.FASTA` as the genome. Picking one of those as "the reference" produces a confidently wrong expected-GC curve — protein FASTA GC is meaningless. `ObjectRole.PROTEIN` and `ObjectRole.TRANSCRIPT` are what exclude them; `pipeline_service.COMPLETENESS_EXCLUDED_ROLES` is the existing constant for exactly this set.

Second trap from the same note: **the same assembly stored twice**. Two references disagreeing is not resolvable by picking one, so this returns nothing rather than guessing — with the caveat that two copies of the *same* GC value are not a disagreement.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/services/test_expected_gc.py`:

```python
class FakeObject:
    """A stand-in for DataObject carrying only what the resolver reads."""

    def __init__(self, name, gc=None, role=None):
        from app.models import ObjectRole

        self.name = name
        self.facts = {} if gc is None else {"gc_content_percent": gc}
        self.role = ObjectRole(role) if role else None


class TestFromReferences:
    def test_measures_from_a_single_reference(self):
        refs = [FakeObject("GRCh38.fa", gc=40.9, role="reference")]
        got = expected_gc.from_references(refs)
        assert got is not None
        assert got.percent == 40.9
        assert got.source == "reference"

    def test_attribution_names_the_file_it_measured(self):
        """'expected 40.9%' with no provenance is a number the user cannot
        check. The filename is what makes it checkable."""
        refs = [FakeObject("GRCh38.fa", gc=40.9, role="reference")]
        assert "GRCh38.fa" in expected_gc.from_references(refs).attribution

    def test_ignores_a_protein_fasta(self):
        """The `protein.faa` mistake. A project that downloaded an NCBI
        assembly holds protein and CDS FASTA alongside the genome; their GC is
        not the genome's, and a curve drawn from one is confidently wrong."""
        refs = [
            FakeObject("protein.faa", gc=52.1, role="protein"),
            FakeObject("GRCh38.fa", gc=40.9, role="reference"),
        ]
        assert expected_gc.from_references(refs).percent == 40.9

    def test_a_project_of_only_protein_fasta_resolves_to_nothing(self):
        refs = [FakeObject("protein.faa", gc=52.1, role="protein")]
        assert expected_gc.from_references(refs) is None

    def test_ignores_a_transcript_fasta(self):
        refs = [FakeObject("cds_from_genomic.fna", gc=54.0, role="transcript")]
        assert expected_gc.from_references(refs) is None

    def test_a_reference_with_no_measured_gc_is_skipped(self):
        """Still ingesting, or a format fasta_stats found nothing in."""
        refs = [FakeObject("pending.fa", gc=None, role="reference")]
        assert expected_gc.from_references(refs) is None

    def test_two_copies_of_the_same_assembly_are_not_a_disagreement(self):
        """The 'same assembly stored twice' case: identical values are one
        answer, not two competing ones."""
        refs = [
            FakeObject("GRCh38.fa", gc=40.9, role="reference"),
            FakeObject("GRCh38_copy.fa", gc=40.9, role="reference"),
        ]
        assert expected_gc.from_references(refs).percent == 40.9

    def test_two_disagreeing_references_resolve_to_nothing(self):
        """Two genuinely different genomes in one project. There is no basis
        for picking one, and picking wrong draws an authoritative-looking
        curve for the wrong organism."""
        refs = [
            FakeObject("human.fa", gc=40.9, role="reference"),
            FakeObject("ecoli.fa", gc=50.8, role="reference"),
        ]
        assert expected_gc.from_references(refs) is None

    def test_no_objects_at_all(self):
        assert expected_gc.from_references([]) is None


class TestResolveOrder:
    def test_a_measured_reference_beats_the_table(self):
        """Tier 1 outranks tier 2: the user's own file is a measurement, the
        table is a published figure for a different assembly of the species."""
        refs = [FakeObject("my_ecoli.fa", gc=50.1, role="reference")]
        got = expected_gc.resolve(references=refs, organism="Escherichia coli")
        assert got.source == "reference"
        assert got.percent == 50.1

    def test_falls_through_to_the_table(self):
        got = expected_gc.resolve(references=[], organism="Escherichia coli")
        assert got.source == "table"

    def test_falls_through_to_nothing(self):
        assert expected_gc.resolve(references=[], organism=None) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/services/test_expected_gc.py -q -k "FromReferences or ResolveOrder"`
Expected: FAIL — `AttributeError: module 'app.services.expected_gc' has no attribute 'from_references'`

- [ ] **Step 3: Implement**

Append to `backend/app/services/expected_gc.py`. Add the import at the top of the file with the others:

```python
from app.services import pipeline_service
```

Then append:

```python
def from_references(objects) -> ExpectedGc | None:
    """Tier 1: GC measured from a reference genome in the same project.

    Better than the table when it applies -- it is the user's actual assembly
    rather than a published figure for some assembly of the species.

    Two exclusions, both learned the same way (see CLAUDE.md on the Actions
    tab's suggestion rules, which shipped green tests while getting both
    wrong against a real project):

    * A protein or transcript FASTA is the same FormatKind as a genome and has
      a GC content that means something entirely different. `role` is what
      separates them; `COMPLETENESS_EXCLUDED_ROLES` is the same set the
      completeness card already excludes for the same reason.
    * Two references that disagree are not resolvable by picking one, so this
      answers with nothing. Two copies of one assembly agree on their value
      and are therefore one answer, not two.
    """
    usable = [
        obj
        for obj in objects
        if obj.role is not None
        and obj.role not in pipeline_service.COMPLETENESS_EXCLUDED_ROLES
        and isinstance(obj.facts.get("gc_content_percent"), (int, float))
    ]
    if not usable:
        return None

    values = {obj.facts["gc_content_percent"] for obj in usable}
    if len(values) > 1:
        return None

    chosen = usable[0]
    percent = float(chosen.facts["gc_content_percent"])
    return ExpectedGc(
        percent=percent,
        source="reference",
        attribution=f"expected {percent}%, measured from {chosen.name}",
    )


def resolve(*, references, organism: str | None) -> ExpectedGc | None:
    """The full cascade, in trust order. None means the chart draws no curve.

    `references` should already be narrowed to the project's reference-role
    objects by the caller; `from_references` re-checks the role rather than
    trusting that, because the check is one line and the failure it prevents
    is a confidently wrong curve.
    """
    return from_references(references) or from_organism(organism)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_expected_gc.py -q`
Expected: PASS — 21 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/expected_gc.py backend/tests/services/test_expected_gc.py
git commit -m "feat(qc): resolve expected GC from a project reference genome"
```

---

### Task 5: Expose expected GC on the object detail endpoint

**Files:**
- Modify: `backend/app/api/v1/schemas.py:173-179`
- Modify: `backend/app/api/v1/objects.py:44-51`
- Test: `backend/tests/services/test_expected_gc.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/services/test_expected_gc.py`:

These use the suite's existing helpers (`backend/tests/services/helpers.py`).
`make_object` takes no `role` or `facts` argument, so both are set after
insert. `asyncio_mode = "auto"` is configured in `backend/pyproject.toml`, so
no `@pytest.mark.asyncio` marker is needed. `beanie_models` is the fixture that
initializes the database — the neighbouring tests in this directory request it
the same way.

```python
from app.models import ObjectRole
from tests.services.helpers import make_object, make_project


async def seed_reference(project, name, gc, role=ObjectRole.REFERENCE):
    """A stored object carrying a measured GC, as ingest would leave it."""
    obj = await make_object(project, name)
    obj.role = role
    obj.facts = {"gc_content_percent": gc}
    await obj.save()
    return obj


class TestReferencesForProject:
    """The database seam. Async because it queries; the resolver itself is
    pure and stays synchronous, which is why every other test here is too."""

    async def test_returns_the_projects_reference_objects(self, beanie_models):
        project = await make_project("gc-refs")
        await seed_reference(project, "GRCh38.fa", 40.9)
        found = await expected_gc.references_for_project(
            project_id=project.id, owner=project.owner
        )
        assert [o.name for o in found] == ["GRCh38.fa"]

    async def test_excludes_objects_with_no_role(self, beanie_models):
        """A FASTQ in the same project is not a reference genome."""
        project = await make_project("gc-mixed")
        await make_object(project, "reads_1.fastq")
        await seed_reference(project, "GRCh38.fa", 40.9)
        found = await expected_gc.references_for_project(
            project_id=project.id, owner=project.owner
        )
        assert [o.name for o in found] == ["GRCh38.fa"]

    async def test_does_not_cross_projects(self, beanie_models):
        """Another project's reference says nothing about this file."""
        mine = await make_project("gc-mine")
        theirs = await make_project("gc-theirs")
        await seed_reference(theirs, "ecoli.fa", 50.8)
        found = await expected_gc.references_for_project(
            project_id=mine.id, owner=mine.owner
        )
        assert found == []

    async def test_resolves_end_to_end_from_stored_objects(self, beanie_models):
        """The whole cascade against real documents rather than fakes -- the
        seam where a query that returns the wrong shape would still satisfy
        every unit test above it."""
        project = await make_project("gc-e2e")
        await seed_reference(project, "GRCh38.fa", 40.9)
        refs = await expected_gc.references_for_project(
            project_id=project.id, owner=project.owner
        )
        got = expected_gc.resolve(references=refs, organism=None)
        assert got is not None
        assert got.source == "reference"
        assert "GRCh38.fa" in got.attribution
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_expected_gc.py -q -k ReferencesForProject`
Expected: FAIL — `AttributeError: module 'app.services.expected_gc' has no attribute 'references_for_project'` (4 tests)

- [ ] **Step 3: Implement the query**

Append to `backend/app/services/expected_gc.py`, with `DataObject` and `ObjectRole` added to the imports:

```python
async def references_for_project(*, project_id, owner) -> list:
    """Reference-role objects in one project, for `resolve` to measure.

    Scoped by owner as well as project: this runs on a detail endpoint whose
    authorization is the object fetch, and a query that ignored owner would
    read another profile's files to answer a question about this one.
    """
    return await DataObject.find(
        DataObject.owner == owner,
        DataObject.project_id == project_id,
        DataObject.role == ObjectRole.REFERENCE,
    ).to_list()
```

- [ ] **Step 4: Add the schema**

In `backend/app/api/v1/schemas.py`, above `class ObjectDetail` (line 173):

```python
class ExpectedGc(BaseModel):
    """What GC this file's reads should show, and what said so.

    Optional everywhere: most files resolve to nothing, and the chart draws no
    reference curve in that case rather than guessing.
    """

    percent: float
    # "reference" (measured from a project file) or "table" (published value).
    source: str
    # Shown beside the curve. Always names its source, so a user can check it.
    attribution: str
```

Then add the field to `ObjectDetail`:

```python
    # What GC to expect, when anything can say. Detail-only, like
    # summary_fingerprint: the listing has no use for it and it costs a query.
    expected_gc: ExpectedGc | None = None
```

- [ ] **Step 5: Populate it in the endpoint**

In `backend/app/api/v1/objects.py`, replace the body of `get_object`:

```python
@router.get("/{object_id}", response_model=ObjectDetail)
async def get_object(object_id: PydanticObjectId, owner: OwnerDep) -> ObjectDetail:
    obj, blob = await object_service.object_with_blob(object_id, owner=owner)
    references = await expected_gc.references_for_project(
        project_id=obj.project_id, owner=owner
    )
    resolved = expected_gc.resolve(
        references=references,
        organism=obj.metadata.get("organism") if obj.metadata else None,
    )
    return ObjectDetail(
        **ObjectOut.of(obj).model_dump(),
        blob=BlobOut.of(blob) if blob else None,
        summary_fingerprint=pipeline_service.summary_fingerprint(obj),
        expected_gc=ExpectedGc(**asdict(resolved)) if resolved else None,
    )
```

Add the imports this needs: `from dataclasses import asdict`, `from app.services import expected_gc`, and `ExpectedGc` to the existing schema import block at the top of the file.

- [ ] **Step 6: Run the backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS. **Read the test count, not just the exit code** — CLAUDE.md is explicit that "green" means the count. Note the number; you will compare against it in Task 10.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/expected_gc.py backend/app/api/v1/schemas.py backend/app/api/v1/objects.py backend/tests/services/test_expected_gc.py
git commit -m "feat(qc): expose expected GC on the object detail endpoint"
```

---

### Task 6: The N-spike grading rule

**Files:**
- Modify: `frontend/src/lib/readQuality.ts:57-59,133-138`
- Test: `frontend/src/lib/readQuality.test.ts`

**The supersession is the point.** A file with a 40% N spike at one cycle usually *also* clears the aggregate 1% threshold, so without this the same defect demotes twice.

- [ ] **Step 1: Write the failing tests**

Add to `frontend/src/lib/readQuality.test.ts`:

```typescript
describe("N content", () => {
  /** Clean everywhere except one collapsed cycle. */
  const spike = [
    { position: 1, percent: 0.01 },
    { position: 2, percent: 0.01 },
    { position: 3, percent: 38.0 },
    { position: 4, percent: 0.01 },
  ];

  it("demotes for a single collapsed cycle", () => {
    const q = readQuality(
      fastq({ ...EXAMPLE_FACTS, qc_duplication_rate: 0.1, qc_n_per_position: spike }),
    );
    expect(q!.tier).toBe(4);
    expect(q!.caveats.join(" ")).toContain("Cycle 3");
  });

  it("names the worst cycle, not the first one over the line", () => {
    const q = readQuality(
      fastq({
        ...EXAMPLE_FACTS,
        qc_duplication_rate: 0.1,
        qc_n_per_position: [
          { position: 1, percent: 8.0 },
          { position: 2, percent: 41.0 },
        ],
      }),
    );
    expect(q!.caveats.join(" ")).toContain("Cycle 2");
  });

  it("does not demote for ordinary sub-threshold N", () => {
    const q = readQuality(
      fastq({
        ...EXAMPLE_FACTS,
        qc_duplication_rate: 0.1,
        qc_n_per_position: [
          { position: 1, percent: 0.4 },
          { position: 2, percent: 0.9 },
        ],
      }),
    );
    expect(q!.tier).toBe(5);
    expect(q!.caveats).toEqual([]);
  });

  it("supersedes the aggregate rule rather than stacking with it", () => {
    /** A spike big enough to also push the whole-file N over 1%. Both rules
     *  describe one defect, so the grade must drop once, not twice. */
    const q = readQuality(
      fastq({
        ...EXAMPLE_FACTS,
        qc_duplication_rate: 0.1,
        base_composition: [
          { base: "A", count: 100, percent: 30.0 },
          { base: "C", count: 100, percent: 20.0 },
          { base: "G", count: 100, percent: 20.0 },
          { base: "T", count: 100, percent: 25.0 },
          { base: "N", count: 100, percent: 5.0 },
        ],
        qc_n_per_position: spike,
      }),
    );
    expect(q!.tier).toBe(4);
    expect(q!.caveats).toHaveLength(1);
    expect(q!.caveats[0]).toContain("Cycle 3");
  });

  it("still applies the aggregate rule when there is no per-cycle curve", () => {
    /** fastp has not run, or the curve was all zeros and omitted. The
     *  aggregate rule is the only evidence available and must still fire. */
    const q = readQuality(
      fastq({
        ...EXAMPLE_FACTS,
        qc_duplication_rate: 0.1,
        base_composition: [
          { base: "A", count: 100, percent: 45.0 },
          { base: "T", count: 100, percent: 45.0 },
          { base: "N", count: 100, percent: 10.0 },
        ],
      }),
    );
    expect(q!.tier).toBe(4);
    expect(q!.caveats.join(" ")).toContain("10% ambiguous");
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run from `frontend/`: `npx vitest run src/lib/readQuality.test.ts`
Expected: FAIL — the spike cases score 5 rather than 4.

- [ ] **Step 3: Implement**

In `frontend/src/lib/readQuality.ts`, add beside the other thresholds (~line 59):

```typescript
/**
 * Percent N at a single cycle above which that cycle counts as failed.
 * Well clear of the sub-1% noise floor of clean Illumina data; a genuine
 * cycle failure spikes far higher. Kept in step with the reference line
 * NContentChart draws, so the chart and the grade cannot disagree.
 */
const N_SPIKE_LIMIT = 5.0;
```

Add this helper beside `nPercent`:

```typescript
/** The worst cycle in fastp's per-position N curve, if there is one. */
function worstNCycle(
  facts: Record<string, unknown>,
): { position: number; percent: number } | null {
  const curve = facts.qc_n_per_position;
  if (!Array.isArray(curve)) return null;
  let worst: { position: number; percent: number } | null = null;
  for (const entry of curve) {
    if (!entry || typeof entry !== "object") continue;
    const percent = num((entry as { percent?: unknown }).percent);
    const position = num((entry as { position?: unknown }).position);
    if (percent === null || position === null) continue;
    if (!worst || percent > worst.percent) worst = { position, percent };
  }
  return worst;
}
```

Then replace the ambiguous-bases block (~lines 133-138):

```typescript
  // Ambiguous bases. Assay-independent: no library design wants N.
  //
  // Two rules for one defect, so only one may fire. A collapsed cycle usually
  // drags the whole-file average over the aggregate threshold as well, and
  // demoting twice for one problem overstates it. The spike is the more
  // specific diagnosis -- it names the cycle, which is what makes it
  // actionable -- so it wins and the aggregate rule stands down.
  const spike = worstNCycle(facts);
  if (spike && spike.percent > N_SPIKE_LIMIT) {
    tier = Math.max(1, tier - 1) as 1 | 2 | 3 | 4 | 5;
    caveats.push(
      `Cycle ${spike.position} is ${spike.percent.toFixed(0)}% N; ` +
        "a specific cycle failed.",
    );
  } else {
    const n = nPercent(facts);
    if (n !== null && n > N_LIMIT) {
      tier = Math.max(1, tier - 1) as 1 | 2 | 3 | 4 | 5;
      caveats.push(`${+n.toFixed(2)}% ambiguous (N) bases.`);
    }
  }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run from `frontend/`: `npx vitest run src/lib/readQuality.test.ts`
Expected: PASS, including every pre-existing test in the file.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/readQuality.ts frontend/src/lib/readQuality.test.ts
git commit -m "feat(qc): demote a read file for a collapsed sequencing cycle"
```

---

### Task 7: The GC distribution chart

**Files:**
- Modify: `frontend/src/components/SequenceCharts.tsx`
- Modify: `frontend/src/api/types.ts`

There is no headless component-testing setup in this repo (no jsdom, no
`.test.tsx` files) and none is expected — these two chart tasks are verified in
the browser in Task 9.

- [ ] **Step 1: Add the detail type**

In `frontend/src/api/types.ts`, beside the other object-detail fields, add:

```typescript
/** What GC to expect for a file's reads, and what said so. Null when nothing
 *  in the project or the genome table can say. */
export interface ExpectedGc {
  percent: number;
  /** "reference" (measured from a project file) or "table" (published). */
  source: string;
  /** Shown beside the curve; always names its source. */
  attribution: string;
}
```

Add `expected_gc: ExpectedGc | null;` to the object detail interface (the one carrying `summary_fingerprint`).

- [ ] **Step 2: Write the component**

Append to `frontend/src/components/SequenceCharts.tsx`:

```typescript
interface GcBin {
  gc_percent: number;
  count: number;
}

interface ExpectedGcCurve {
  percent: number;
  attribution: string;
}

/**
 * Per-sequence GC distribution: how many reads sit at each GC percentage.
 *
 * Distinct from the base-composition pie, which is one whole-file number. A
 * library that is half one organism and half another has an unremarkable
 * aggregate GC and two peaks here -- which is the contamination signal this
 * chart exists to show.
 *
 * The reference curve is drawn only when something can actually say what to
 * expect (a reference genome in the project, or a published figure for a named
 * organism), and it is always labelled with which. When nothing can, the
 * checkbox offers a normal fitted to the observed data instead -- labelled as
 * a fit, because that is all it is. FastQC's equivalent curve is fitted the
 * same way and is routinely misread as an expectation, which is what makes
 * that module famously noisy.
 */
export function GcDistributionChart({
  histogram,
  meanGc,
  expected,
  sampledReads,
}: {
  histogram: GcBin[];
  meanGc?: number;
  expected?: ExpectedGcCurve | null;
  sampledReads?: number;
}) {
  const [showFit, setShowFit] = useState(false);
  const [hover, setHover] = useState<GcBin | null>(null);
  if (!histogram?.length) return null;

  const w = 460;
  const h = 210;
  const pad = { top: 10, right: 16, bottom: 26, left: 38 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const maxCount = Math.max(...histogram.map((b) => b.count));
  if (!maxCount) return null;

  // Fixed 0-100 x-axis rather than fitting to the observed range: GC is a
  // percentage of a fixed scale, and rescaling would make a tight, healthy
  // distribution and a broad, suspicious one look identical.
  const x = (gc: number) => pad.left + (gc / 100) * plotW;
  const y = (count: number) => pad.top + plotH - (count / maxCount) * plotH;

  const line = histogram
    .map((b, i) => `${i ? "L" : "M"} ${x(b.gc_percent)} ${y(b.count)}`)
    .join(" ");

  // A normal fitted to the observed mean and standard deviation, computed
  // from the bins themselves. Scaled to the tallest observed bin so the two
  // curves are comparable in shape, which is the only thing being compared.
  const total = histogram.reduce((s, b) => s + b.count, 0);
  const mean =
    meanGc ?? histogram.reduce((s, b) => s + b.gc_percent * b.count, 0) / total;
  const variance =
    histogram.reduce((s, b) => s + b.count * (b.gc_percent - mean) ** 2, 0) /
    total;
  const sd = Math.sqrt(variance) || 1;
  const fitPath = Array.from({ length: 101 }, (_, gc) => {
    const density = Math.exp(-((gc - mean) ** 2) / (2 * sd * sd));
    return `${gc ? "L" : "M"} ${x(gc)} ${y(density * maxCount)}`;
  }).join(" ");

  return (
    <div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block" }}
        onMouseLeave={() => setHover(null)}
      >
        {[0, 25, 50, 75, 100].map((gc) => (
          <g key={gc}>
            <line
              x1={x(gc)}
              x2={x(gc)}
              y1={pad.top}
              y2={pad.top + plotH}
              stroke="var(--border)"
              strokeWidth="1"
            />
            <text
              x={x(gc)}
              y={h - 14}
              textAnchor="middle"
              fontSize="9"
              fill="var(--text-faint)"
            >
              {gc}
            </text>
          </g>
        ))}

        <path d={line} fill="none" stroke="var(--accent)" strokeWidth="1.8" />

        {/* The expected-GC line, when something can say what to expect. */}
        {expected && (
          <>
            <line
              x1={x(expected.percent)}
              x2={x(expected.percent)}
              y1={pad.top}
              y2={pad.top + plotH}
              stroke="var(--success)"
              strokeWidth="1.5"
              strokeDasharray="4 3"
            />
            <text
              x={x(expected.percent) + 4}
              y={pad.top + 10}
              fontSize="9"
              fill="var(--success)"
            >
              expected
            </text>
          </>
        )}

        {showFit && !expected && (
          <path
            d={fitPath}
            fill="none"
            stroke="var(--text-faint)"
            strokeWidth="1.4"
            strokeDasharray="4 3"
          />
        )}

        {hover && (
          <line
            x1={x(hover.gc_percent)}
            x2={x(hover.gc_percent)}
            y1={pad.top}
            y2={pad.top + plotH}
            stroke="var(--text-faint)"
            strokeDasharray="3 3"
          />
        )}

        <rect
          x={pad.left}
          y={pad.top}
          width={plotW}
          height={plotH}
          fill="transparent"
          onMouseMove={(e) => {
            const box = (e.target as SVGRectElement).getBoundingClientRect();
            const gc = ((e.clientX - box.left) / box.width) * 100;
            let nearest = histogram[0];
            for (const b of histogram) {
              if (Math.abs(b.gc_percent - gc) < Math.abs(nearest.gc_percent - gc)) {
                nearest = b;
              }
            }
            setHover(nearest);
          }}
        />

        <text x={w / 2} y={h - 2} textAnchor="middle" fontSize="9" fill="var(--text-faint)">
          mean GC content per read (%)
        </text>
      </svg>

      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 2 }}>
        {hover
          ? `${hover.gc_percent}% GC: ${hover.count.toLocaleString()} reads`
          : expected
            ? expected.attribution
            : "reads by GC content · hover for detail"}
        {sampledReads != null && ` · sampled ${sampledReads.toLocaleString()} reads`}
      </div>

      {/* Offered only when nothing authoritative can be drawn. With a real
          expected value on the chart, a second dashed curve fitted to the data
          itself would compete with it and say less. */}
      {!expected && (
        <label
          style={{
            fontSize: 11,
            color: "var(--text-faint)",
            display: "flex",
            alignItems: "center",
            gap: 5,
            marginTop: 4,
          }}
        >
          <input
            type="checkbox"
            checked={showFit}
            onChange={(e) => setShowFit(e.target.checked)}
          />
          overlay normal distribution (fitted to this file, not an expectation)
        </label>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Verify it compiles**

Run from `frontend/`: `npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/SequenceCharts.tsx frontend/src/api/types.ts
git commit -m "feat(qc): per-sequence GC distribution chart"
```

---

### Task 8: The N content chart, and mounting both

**Files:**
- Modify: `frontend/src/components/SequenceCharts.tsx`
- Modify: `frontend/src/components/DetailPanel.tsx:945-1030`

- [ ] **Step 1: Write the N chart**

Append to `frontend/src/components/SequenceCharts.tsx`:

```typescript
interface NPoint {
  position: number;
  percent: number;
}

/**
 * Percent uncalled (N) bases at each cycle.
 *
 * The aggregate N percentage in `base_composition` averages a failed cycle
 * against every healthy one, which is exactly the shape that hides the
 * failure: one cycle at 40% N across a 150-cycle read is 0.27% overall. This
 * is the view where that spike is visible.
 *
 * Only rendered when fastp found any N at all -- an all-zero curve is omitted
 * on the backend rather than drawn as a flat line at zero.
 */
export function NContentChart({ curve }: { curve: NPoint[] }) {
  const [hover, setHover] = useState<NPoint | null>(null);
  if (!curve?.length) return null;

  const w = 460;
  const h = 210;
  const pad = { top: 10, right: 16, bottom: 26, left: 38 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const maxPos = curve[curve.length - 1].position;
  const observedMax = Math.max(...curve.map((p) => p.percent));
  // Floor the axis at the grading threshold so a clean file's noise is not
  // magnified into a mountain range by autoscaling, while a genuine spike
  // still has room to show its true height.
  const yMax = Math.max(observedMax * 1.15, N_SPIKE_REFERENCE * 1.5);

  const x = (p: number) => pad.left + ((p - 1) / Math.max(maxPos - 1, 1)) * plotW;
  const y = (v: number) => pad.top + plotH - (Math.min(v, yMax) / yMax) * plotH;

  const line = curve
    .map((p, i) => `${i ? "L" : "M"} ${x(p.position)} ${y(p.percent)}`)
    .join(" ");

  return (
    <div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block" }}
        onMouseLeave={() => setHover(null)}
      >
        {/* The same 5% threshold the grade uses, drawn so the chart and the
            grade cannot appear to disagree about what counts as a spike. */}
        <line
          x1={pad.left}
          x2={w - pad.right}
          y1={y(N_SPIKE_REFERENCE)}
          y2={y(N_SPIKE_REFERENCE)}
          stroke="var(--warn)"
          strokeWidth="1"
          strokeDasharray="4 3"
        />
        <text
          x={pad.left - 5}
          y={y(N_SPIKE_REFERENCE) + 3}
          textAnchor="end"
          fontSize="9"
          fill="var(--warn)"
        >
          {N_SPIKE_REFERENCE}%
        </text>

        <path d={line} fill="none" stroke="var(--accent)" strokeWidth="1.8" />

        {hover && (
          <>
            <line
              x1={x(hover.position)}
              x2={x(hover.position)}
              y1={pad.top}
              y2={pad.top + plotH}
              stroke="var(--text-faint)"
              strokeDasharray="3 3"
            />
            <circle cx={x(hover.position)} cy={y(hover.percent)} r="3.5" fill="var(--accent)" />
          </>
        )}

        <rect
          x={pad.left}
          y={pad.top}
          width={plotW}
          height={plotH}
          fill="transparent"
          onMouseMove={(e) => {
            const box = (e.target as SVGRectElement).getBoundingClientRect();
            const frac = (e.clientX - box.left) / box.width;
            const idx = Math.round(frac * (curve.length - 1));
            setHover(curve[Math.max(0, Math.min(curve.length - 1, idx))]);
          }}
        />

        <text x={w / 2} y={h - 6} textAnchor="middle" fontSize="9" fill="var(--text-faint)">
          position in read (bp)
        </text>
      </svg>

      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 2 }}>
        {hover
          ? `position ${hover.position}: ${hover.percent.toFixed(2)}% N`
          : "uncalled (N) bases per position · hover for detail"}
      </div>
    </div>
  );
}
```

Add this constant near `BASE_COLORS` at the top of the file:

```typescript
/**
 * The single-cycle N percentage the grade treats as a failed cycle. Duplicated
 * from readQuality.ts's N_SPIKE_LIMIT deliberately -- the chart must draw the
 * same line the grade uses, and importing scoring logic into a chart module to
 * share one number would couple them the wrong way round.
 */
const N_SPIKE_REFERENCE = 5.0;
```

- [ ] **Step 2: Mount both charts**

In `frontend/src/components/DetailPanel.tsx`, update the import at line 27:

```typescript
import {
  BaseCompositionChart,
  GcDistributionChart,
  NContentChart,
  QualityChart,
} from "./SequenceCharts";
```

Inside `QcTab`, after the `curve` definition (~line 964), add:

```typescript
  // Both are reads-only: a reference has no per-read GC distribution worth
  // drawing, and no per-cycle N.
  const gcHistogram =
    !isReference && Array.isArray(obj.facts.gc_per_read_histogram)
      ? obj.facts.gc_per_read_histogram
      : null;
  const nCurve =
    !isReference && Array.isArray(obj.facts.qc_n_per_position)
      ? obj.facts.qc_n_per_position
      : null;
```

Change the grid's render condition (line 1006) from:

```typescript
      {(composition || curve || showChromStrip) && (
```

to:

```typescript
      {(composition || curve || gcHistogram || nCurve || showChromStrip) && (
```

Then add both charts inside the grid, after the `curve` block (line 1024) and before the `showChromStrip` comment:

```typescript
          {gcHistogram && (
            <div className="qc-chart">
              <div className="section-title">GC distribution</div>
              <GcDistributionChart
                histogram={gcHistogram as never}
                meanGc={obj.facts.gc_per_read_mean as number | undefined}
                expected={obj.expected_gc}
                sampledReads={obj.facts.stats_sampled_reads as number | undefined}
              />
            </div>
          )}
          {nCurve && (
            <div className="qc-chart">
              <div className="section-title">N content per position</div>
              <NContentChart curve={nCurve as never} />
            </div>
          )}
```

- [ ] **Step 3: Verify it compiles**

Run from `frontend/`: `npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/SequenceCharts.tsx frontend/src/components/DetailPanel.tsx
git commit -m "feat(qc): N content chart, and mount both new charts in the QC tab"
```

---

### Task 9: Verify in the browser and against a real project

**Files:** none modified unless a defect is found.

This is the actual verification step for anything UI-facing — CLAUDE.md is explicit that there is no headless component-testing setup and none is expected.

- [ ] **Step 1: Start this worktree's stack**

```bash
./ops/worktree-up.sh
```

Serves this worktree's UI at **localhost:5273**. The main instance on 5173 keeps serving main; do not touch it.

- [ ] **Step 2: Check the GC cascade against real objects, not fixtures**

CLAUDE.md records that the suggestion rules passed a full green suite while getting two things wrong that one look at a real project exposed. Run the resolver against real data:

```bash
docker compose -p biopipe exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models import DataObject
from app.services import expected_gc

async def main():
    await connect_to_mongo()
    for obj in await DataObject.find().limit(400).to_list():
        refs = await expected_gc.references_for_project(
            project_id=obj.project_id, owner=obj.owner
        )
        got = expected_gc.resolve(
            references=refs,
            organism=obj.metadata.get('organism') if obj.metadata else None,
        )
        if got:
            print(f'{obj.name}: {got.source} -> {got.percent}% ({got.attribution})')

asyncio.run(main())
"
```

Read the output for the two failures CLAUDE.md names by name: a `protein.faa` or `cds_from_genomic.fna` resolving as a reference, and the same assembly stored twice being treated as two disagreeing references. Both should be absent. If either appears, fix it in `from_references` and add the case to `test_expected_gc.py` before continuing.

- [ ] **Step 3: Check the charts in the browser**

At localhost:5273, open a FASTQ's QC tab and confirm:

- The GC distribution chart draws, with a sensible peak rather than a flat or spiky line.
- The checkbox appears **only** when no expected curve is drawn, and toggling it adds a dashed fitted curve.
- On a file whose project holds a reference genome, the expected line is drawn and the caption names the file it was measured from.
- The N chart appears only on files where QC has run and found N; a clean file shows no N chart at all, which is correct.
- Both charts sit in the `qc-charts` grid without breaking its layout, and a reference file still shows the chromosome strip in the right column.

- [ ] **Step 4: Check the grade**

Find or construct a file with a collapsed cycle and confirm the grade drops by exactly one and the caveat names the cycle. If no such file exists in the data, this is covered by the unit tests in Task 6 — note that here rather than fabricating one.

- [ ] **Step 5: Stop the worktree stack**

```bash
./ops/worktree-up.sh --down
```

---

### Task 10: Help page, full suite, and merge

**Files:**
- Modify: `frontend/src/components/HelpCalculations.tsx:54-95`

A grading rule that exists only in code is one nobody can check. This is part of the work, not a follow-up.

- [ ] **Step 1: Document the spike rule**

In `frontend/src/components/HelpCalculations.tsx`, inside the *What lowers the grade* list (after the ambiguous-bases item, ~line 64), add:

```tsx
          <li>
            <strong>A collapsed cycle</strong> — more than 5% of reads have an
            uncalled base at one specific position. This usually means a single
            sequencing cycle failed. When it applies it replaces the ambiguous-base
            penalty above rather than adding to it: both describe the same defect,
            and one problem should only cost one grade.
          </li>
```

- [ ] **Step 2: Rewrite the GC section**

Replace the *What GC content does not do* section (~lines 89-95) with:

```tsx
        <h3>What GC content does not do</h3>
        <p>
          GC content never changes the grade. Expected GC is a property of the
          organism — roughly 41% for human, under 20% for <em>Plasmodium</em> —
          so without knowing the source, an unusual GC is not evidence of a
          problem.
        </p>
        <p>
          The GC distribution chart will still show you what to expect when
          something can say what that is. It looks for an answer in two places,
          in order: a <strong>reference genome in the same project</strong>,
          whose GC is measured from the file itself, and then a small table of{" "}
          <strong>published figures for well-known genomes</strong>, used when
          the file's Organism metadata names one. The chart always says which of
          the two it used, and which file or assembly the number came from.
        </p>
        <p>
          When neither applies, no expected line is drawn. You can tick{" "}
          <strong>overlay normal distribution</strong> to see a bell curve fitted
          to the file's own data — useful for judging whether the distribution is
          one clean peak or two overlapping ones, but it is a description of this
          file, not an expectation of it. A second peak usually means
          contamination; a very broad one can mean amplification bias.
        </p>
        <p>
          The GC distribution is measured from a sample of up to 200,000 reads,
          while the N content chart is fastp's count across the whole file. Both
          say which under the chart.
        </p>
```

- [ ] **Step 3: Verify it compiles and renders**

Run from `frontend/`: `npx tsc --noEmit`
Expected: no errors.

If the stack is down, bring it up (`./ops/worktree-up.sh`) and read `/help/calculations` at localhost:5273 to confirm the page reads correctly, then stop it again.

- [ ] **Step 4: Run both suites**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

```bash
cd frontend && npx vitest run && npx tsc --noEmit
```

Expected: PASS. **Read the counts.** Compare the backend count against what you noted in Task 5 Step 6 — it should have grown by the tests this plan added, not shrunk. A suite that dies with `EXIT=137` is the host running out of memory, not a test failure; if that happens, stop other Docker stacks and re-run.

- [ ] **Step 5: Commit the help page**

```bash
git add frontend/src/components/HelpCalculations.tsx
git commit -m "docs(help): document the collapsed-cycle rule and the GC cascade"
```

- [ ] **Step 6: Merge to main and push**

Only with both suites green and `main` clean. Per CLAUDE.md, this needs no further permission.

```bash
git checkout main && git pull && git merge --no-ff -
```

If `main` moved during the merge, re-run the backend suite before pushing — a green from before the merge does not describe the merged tree.

```bash
git push origin main
```

- [ ] **Step 7: Close out the backlog if an entry covers this**

Check `docs/TODO.md` for an entry describing these charts. If one exists, append ` — FIXED` to its heading, write a short note saying what shipped and where the code lives, say what the implementation did differently from its plan, and move the whole entry to `docs/TODO-done.md`. If no entry exists, nothing to do — do not create one retroactively.

---

## Notes for the implementer

**Things this plan deliberately does not do,** so you do not add them thinking they were forgotten:

- **`alignment_stats` gets no GC histogram.** The same accumulator would work there, but per-read GC on an alignment is a different question for a different audience, and the reads QC tab is the subject here.
- **Read2's N curve is not parsed.** A paired run has one, and a second chart per file is worth adding once someone has a file where the two sides differ.
- **The fitted-normal checkbox does not persist.** It resets on navigation. Anything sticky needs a home in settings, which is a larger change than this earns.
- **Nothing infers the organism.** Tier 2 reads `metadata.organism` and nothing else. Guessing an organism from a filename and then drawing an authoritative reference curve from that guess is the specific failure the whole cascade design is arranged to avoid.
