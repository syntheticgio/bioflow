# Contamination & Library Complexity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Adapter Content and Sequence Duplication Levels charts to the reads QC tab, computed from a new whole-file scan during the QC job.

**Architecture:** A new `contamination_stats.py` module makes one full-file pass over a FASTQ, accumulating per-position adapter matches and a FastQC-compatible duplication histogram together. `_run_short_read_qc` calls it after FastQC and merges its facts; failures are swallowed like FastQC's. Two hand-rolled SVG components render the facts in the existing `.qc-charts` grid.

**Tech Stack:** Python 3 (stdlib only -- no new dependencies), pytest, React + TypeScript, hand-rolled SVG (no charting library).

**Spec:** `docs/superpowers/specs/2026-08-09-contamination-library-complexity-design.md`

---

## Background the engineer needs

**Read the spec first.** It explains *why* this scans the whole file instead of
sampling, which is the one decision most likely to look wrong.

Some conventions in this repo that are not obvious:

- **Run backend tests from this worktree with `./backend/run-worktree-tests.sh`,
  never `docker compose exec api python -m pytest`.** The `api` container mounts
  the *main* checkout, so the latter silently tests main's code, not yours.
- **Never run bare `docker compose` from this worktree.** A `PreToolUse` hook
  blocks it. To see the UI, use `./ops/worktree-up.sh` (UI on 5273).
- Facts are merged, not replaced, by `_apply_run_qc` in `queue/results.py`. No
  applier change is needed for new fact keys -- they flow through generically.
- `FactsTable.tsx` already suppresses every `qc_*` key, so new facts will not
  leak into the generic fact table.

## File Structure

**Create:**
- `backend/app/pipelines/contamination_stats.py` -- the scan. Pure functions
  over a file path; no job, database, or settings knowledge. This is what makes
  it testable without a container.
- `backend/tests/pipelines/test_contamination_stats.py` -- its tests.
- `frontend/src/components/ContaminationCharts.tsx` -- both chart components.

**Modify:**
- `backend/app/queue/pipeline_handlers.py` -- call the scan in
  `_run_short_read_qc` (~line 505, after the FastQC block).
- `frontend/src/api/types.ts` -- add fact types to `QcFacts` (~line 1216).
- `frontend/src/components/DetailPanel.tsx` -- mount both charts in the
  `.qc-charts` grid (~line 1023).
- `frontend/src/components/QcReport.tsx` -- Duplication row prefers the new
  whole-file number (~line 97).

**Not modified:** `queue/results.py` (facts merge generically),
`styles.css` (the grid is already `auto-fit, minmax(320px, 1fr)` and reflows to
2x2 on its own), `sequence_stats.py` (ingest-time sampling is unchanged).

---

## Task 1: Adapter probe constants and matching

**Files:**
- Create: `backend/app/pipelines/contamination_stats.py`
- Test: `backend/tests/pipelines/test_contamination_stats.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_contamination_stats.py`:

```python
from app.pipelines import contamination_stats as cs


def test_known_probes_are_twelve_bases():
    """FastQC matches the first 12bp of each adapter; a longer or shorter
    probe would silently change sensitivity."""
    for name, seq in cs.KNOWN_ADAPTERS:
        assert len(seq) == cs.PROBE_LENGTH, name


def test_build_probes_without_detection_returns_known_set():
    probes = cs.build_probes([])
    assert [p[0] for p in probes] == [n for n, _ in cs.KNOWN_ADAPTERS]


def test_build_probes_appends_detected_sequence():
    probes = cs.build_probes(["TTTTCCCCGGGGAAAA"])
    assert probes[-1] == ("Detected", "TTTTCCCCGGGG")


def test_build_probes_drops_detected_duplicate_of_known_kit():
    """A detected sequence that IS a known kit must not draw a second,
    identical curve on the chart."""
    nextera = dict(cs.KNOWN_ADAPTERS)["Nextera Transposase"]
    probes = cs.build_probes([nextera + "ACGTACGT"])
    assert [p[0] for p in probes] == [n for n, _ in cs.KNOWN_ADAPTERS]


def test_build_probes_ignores_short_or_empty_detection():
    assert cs.build_probes([""]) == cs.build_probes([])
    assert cs.build_probes(["ACGT"]) == cs.build_probes([])
    assert cs.build_probes([None]) == cs.build_probes([])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_contamination_stats.py -q`
Expected: FAIL -- `ModuleNotFoundError: No module named 'app.pipelines.contamination_stats'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/pipelines/contamination_stats.py`:

```python
"""Adapter content and duplication levels from a whole-file FASTQ scan.

Unlike `storage/sequence_stats.py`, which samples 200k reads at ingest, this
reads the entire file. That is deliberate and is the reason the module exists
separately: FastQC's duplication correction extrapolates from the point where
its sequence dictionary froze to the file's *total* read count, so a sampled
`total_count` would extrapolate to the sample rather than to the library, and
report ">1k duplicates" meaning ">1k within a 200k window".

Runs inside the QC job, which the user has already opted into and which
already passes fastp over the same whole file.
"""

import gzip
import threading
from pathlib import Path

from app.errors import JobCancelled
from app.logging import get_logger
from app.models import Compression

log = get_logger(__name__)

# FastQC matches the first 12bp of each adapter rather than the whole thing:
# long enough to be specific, short enough to still match a read that ran off
# the end of the fragment with only part of the adapter present.
PROBE_LENGTH = 12

# The kits FastQC ships, plus the two homopolymer artifacts. PolyG is not
# optional decoration: on NovaSeq/NextSeq two-colour chemistry, *absence* of
# signal reads as G, so poly-G tails are among the most common artifacts in
# current data.
KNOWN_ADAPTERS: tuple[tuple[str, str], ...] = (
    ("Illumina Universal", "AGATCGGAAGAG"),
    ("Illumina Small RNA 3'", "TGGAATTCTCGG"),
    ("Illumina Small RNA 5'", "GATCGTCGGACT"),
    ("Nextera Transposase", "CTGTCTCTTATA"),
    ("PolyA", "AAAAAAAAAAAA"),
    ("PolyG", "GGGGGGGGGGGG"),
)


def build_probes(detected: list[str | None]) -> list[tuple[str, str]]:
    """The probe set for one file: the known kits plus whatever fastp found.

    The detected sequence is what the fixed list cannot supply -- a custom or
    unusual adapter still gets a curve. It is dropped when it duplicates a
    known kit, because two identical overlapping lines on the chart read as a
    rendering bug rather than as agreement.
    """
    probes = list(KNOWN_ADAPTERS)
    known = {seq for _, seq in KNOWN_ADAPTERS}

    for seq in detected:
        if not seq or len(seq) < PROBE_LENGTH:
            continue
        head = seq[:PROBE_LENGTH].upper()
        if head in known:
            continue
        probes.append(("Detected", head))
        known.add(head)

    return probes
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_contamination_stats.py -q`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/contamination_stats.py backend/tests/pipelines/test_contamination_stats.py
git commit -m "feat(qc): adapter probe set for contamination scan"
```

---

## Task 2: FastQC's duplication correction formula

**Files:**
- Modify: `backend/app/pipelines/contamination_stats.py`
- Test: `backend/tests/pipelines/test_contamination_stats.py`

This is the formula from FastQC's `DuplicationLevel.java`. Port it exactly --
the numbers it produces are the ones users compare against FastQC reports.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_contamination_stats.py`:

```python
def test_corrected_count_returns_observations_when_nothing_was_missed():
    """When the dictionary never froze (count_at_limit == total), every
    sequence was seen, so there is nothing to correct."""
    assert cs.get_corrected_count(1000, 1000, 1, 500) == 500


def test_corrected_count_returns_observations_when_no_room_to_hide():
    """If fewer sequences remain than the freeze point, another sequence at
    this level could not have been missed."""
    assert cs.get_corrected_count(900, 1000, 1, 500) == 500


def test_corrected_count_scales_up_when_sequences_were_missed():
    """A dictionary that froze early saw a small slice of the file, so the
    observed count under-counts and the correction must exceed it."""
    corrected = cs.get_corrected_count(1_000, 1_000_000, 1, 100)
    assert corrected > 100


def test_corrected_count_grows_as_freeze_point_shrinks():
    """The earlier the freeze, the more was missed, so the larger the
    correction -- this is the direction that makes the estimate meaningful."""
    early = cs.get_corrected_count(1_000, 1_000_000, 1, 100)
    late = cs.get_corrected_count(500_000, 1_000_000, 1, 100)
    assert early > late


def test_corrected_count_is_near_observed_for_high_duplication():
    """A sequence appearing very often is almost certain to have been caught
    before the freeze, so its count needs little correction."""
    corrected = cs.get_corrected_count(1_000, 1_000_000, 50_000, 10)
    assert 10 <= corrected < 11
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_contamination_stats.py -q`
Expected: FAIL -- `AttributeError: module ... has no attribute 'get_corrected_count'`

- [ ] **Step 3: Write the implementation**

Append to `backend/app/pipelines/contamination_stats.py`:

```python
def get_corrected_count(
    count_at_limit: int,
    total_count: int,
    duplication_level: int,
    number_of_observations: int,
) -> float:
    """Estimate how many sequences at this duplication level we never saw.

    Ported from FastQC's `DuplicationLevel.getCorrectedCount`. The dictionary
    stops accepting new sequences at 100k distinct entries, so a file larger
    than that contributes sequences we never recorded. This computes the
    probability of *not* having seen a sequence with this duplication level
    within the first `count_at_limit` reads, inverts it, and scales the
    observed count by the result.

    Both early exits are from the original and are not merely optimisations:
    they are the cases where the correction is provably 1.0.
    """
    # Nothing froze: every distinct sequence in the file is in the dictionary.
    if count_at_limit == total_count:
        return float(number_of_observations)

    # Not enough reads left to hide another sequence at this level.
    if total_count - number_of_observations < count_at_limit:
        return float(number_of_observations)

    # The probability below which correcting would not move the count by even
    # 0.01 of an observation. Past this point the corrected value is so close
    # to the observed one that continuing the loop buys nothing.
    limit_of_caring = 1.0 - (
        number_of_observations / (number_of_observations + 0.01)
    )

    p_not_seeing = 1.0
    for i in range(count_at_limit):
        p_not_seeing *= (
            (total_count - i) - duplication_level
        ) / (total_count - i)
        if p_not_seeing < limit_of_caring:
            p_not_seeing = 0.0
            break

    p_seeing = 1.0 - p_not_seeing
    if p_seeing == 0.0:
        return float(number_of_observations)

    return number_of_observations / p_seeing
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_contamination_stats.py -q`
Expected: PASS, 10 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/contamination_stats.py backend/tests/pipelines/test_contamination_stats.py
git commit -m "feat(qc): port FastQC duplication correction formula"
```

---

## Task 3: Duplication slot binning

**Files:**
- Modify: `backend/app/pipelines/contamination_stats.py`
- Test: `backend/tests/pipelines/test_contamination_stats.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_contamination_stats.py`:

```python
import pytest


@pytest.mark.parametrize(
    "level,expected_slot",
    [
        (1, 0),      # seen once -> first slot
        (9, 8),      # last exact slot
        (10, 9),     # ">10" begins
        (50, 9),     # tempDupSlot 49, still ">10"
        (51, 10),    # tempDupSlot 50 -> ">50"
        (100, 10),
        (101, 11),   # ">100"
        (500, 11),
        (501, 12),   # ">500"
        (1000, 12),
        (1001, 13),  # ">1k"
        (5000, 13),
        (5001, 14),  # ">5k"
        (10000, 14),
        (10001, 15), # ">10k"
        (99999, 15),
    ],
)
def test_slot_boundaries(level, expected_slot):
    """Boundaries are off-by-one traps: FastQC bins on `level - 1`, so the
    ">50" slot actually starts at level 51."""
    assert cs.slot_for_level(level) == expected_slot


def test_slot_labels_match_slot_count():
    assert len(cs.DUPLICATION_LABELS) == cs.DUPLICATION_SLOTS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_contamination_stats.py -q`
Expected: FAIL -- `AttributeError: module ... has no attribute 'slot_for_level'`

- [ ] **Step 3: Write the implementation**

Append to `backend/app/pipelines/contamination_stats.py`:

```python
DUPLICATION_SLOTS = 16

DUPLICATION_LABELS: tuple[str, ...] = (
    "1", "2", "3", "4", "5", "6", "7", "8", "9",
    ">10", ">50", ">100", ">500", ">1k", ">5k", ">10k",
)


def slot_for_level(duplication_level: int) -> int:
    """Which histogram slot a duplication level falls in.

    Binning is on `duplication_level - 1`, matching FastQC. That is why ">50"
    starts at level 51 rather than 50 -- a detail worth preserving, since the
    whole point of porting the algorithm is that the bins line up with the
    FastQC reports people compare against.
    """
    temp = duplication_level - 1

    # The negative guard is FastQC's, for duplication levels past 2^31.
    if temp > 9999 or temp < 0:
        return 15
    if temp > 4999:
        return 14
    if temp > 999:
        return 13
    if temp > 499:
        return 12
    if temp > 99:
        return 11
    if temp > 49:
        return 10
    if temp > 9:
        return 9
    return temp
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_contamination_stats.py -q`
Expected: PASS, 27 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/contamination_stats.py backend/tests/pipelines/test_contamination_stats.py
git commit -m "feat(qc): duplication level slot binning"
```

---

## Task 4: The duplication accumulator

**Files:**
- Modify: `backend/app/pipelines/contamination_stats.py`
- Test: `backend/tests/pipelines/test_contamination_stats.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_contamination_stats.py`:

```python
def test_duplication_tracker_freezes_at_the_unique_limit(monkeypatch):
    """Past the limit, new sequences are dropped but existing ones keep
    counting -- that is what keeps total_count a true whole-file count."""
    monkeypatch.setattr(cs, "OBSERVATION_CUTOFF", 3)
    tracker = cs.DuplicationTracker()

    for seq in ("AAA", "CCC", "GGG"):
        tracker.add(seq)
    tracker.add("TTT")   # dropped: dictionary is frozen
    tracker.add("AAA")   # counted: already present

    assert tracker.total_count == 5
    assert tracker.sequences == {"AAA": 2, "CCC": 1, "GGG": 1}


def test_duplication_tracker_truncates_to_fifty_bases():
    """Reads differing only past 50bp are the same fragment for duplication
    purposes -- this tolerates end-of-read quality decay."""
    tracker = cs.DuplicationTracker()
    tracker.add("A" * 50 + "CCCC")
    tracker.add("A" * 50 + "GGGG")

    assert tracker.sequences == {"A" * 50: 2}


def test_duplication_tracker_records_count_at_unique_limit(monkeypatch):
    monkeypatch.setattr(cs, "OBSERVATION_CUTOFF", 2)
    tracker = cs.DuplicationTracker()

    tracker.add("AAA")
    tracker.add("CCC")   # freezes here, at 2 reads
    tracker.add("GGG")
    tracker.add("TTT")

    assert tracker.count_at_unique_limit == 2
    assert tracker.total_count == 4


def test_duplication_result_on_a_fully_unique_library():
    tracker = cs.DuplicationTracker()
    for i in range(100):
        tracker.add(f"SEQ{i:04d}")

    result = tracker.result()

    assert result["percent_unique"] == pytest.approx(100.0)
    # Everything sits in the "seen once" slot.
    assert result["percentages"][0] == pytest.approx(100.0)
    assert sum(result["percentages"][1:]) == pytest.approx(0.0)


def test_duplication_result_on_a_fully_duplicated_library():
    """One fragment, 100 copies: 1% unique, and all of the library sits in
    the >50 slot (level 100 -> tempDupSlot 99 -> slot 10)."""
    tracker = cs.DuplicationTracker()
    for _ in range(100):
        tracker.add("AAAA")

    result = tracker.result()

    assert result["percent_unique"] == pytest.approx(1.0)
    assert result["percentages"][10] == pytest.approx(100.0)


def test_duplication_result_is_empty_for_no_reads():
    assert cs.DuplicationTracker().result() == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_contamination_stats.py -q`
Expected: FAIL -- `AttributeError: module ... has no attribute 'DuplicationTracker'`

- [ ] **Step 3: Write the implementation**

Append to `backend/app/pipelines/contamination_stats.py`:

```python
# FastQC's limits, kept identical so the numbers line up with its reports.
# The dictionary caps at ~100k x 50 chars, so memory is bounded at roughly
# 10-15 MB no matter how large the file is.
OBSERVATION_CUTOFF = 100_000
DUPLICATION_SEQUENCE_LENGTH = 50


class DuplicationTracker:
    """Counts distinct read prefixes, freezing the dictionary at the cutoff.

    Freezing stops the dictionary growing; it does *not* stop the scan. Reads
    past the freeze still increment sequences already known and still count
    toward `total_count`, which is what makes the correction in `result()`
    an extrapolation to the whole library rather than to a sample.
    """

    def __init__(self) -> None:
        self.sequences: dict[str, int] = {}
        self.total_count = 0
        self.count_at_unique_limit = 0
        self._frozen = False

    def add(self, seq: str) -> None:
        self.total_count += 1
        key = seq[:DUPLICATION_SEQUENCE_LENGTH]

        if key in self.sequences:
            self.sequences[key] += 1
            if not self._frozen:
                self.count_at_unique_limit = self.total_count
            return

        if self._frozen:
            return

        self.sequences[key] = 1
        self.count_at_unique_limit = self.total_count
        if len(self.sequences) >= OBSERVATION_CUTOFF:
            self._frozen = True

    def result(self) -> dict:
        """The corrected histogram, as percentages of the library."""
        if not self.total_count:
            return {}

        # "How many distinct sequences were seen exactly N times."
        collated: dict[int, int] = {}
        for count in self.sequences.values():
            collated[count] = collated.get(count, 0) + 1

        percentages = [0.0] * DUPLICATION_SLOTS
        dedup_total = 0.0
        raw_total = 0.0

        for level, observations in collated.items():
            corrected = get_corrected_count(
                self.count_at_unique_limit,
                self.total_count,
                level,
                observations,
            )
            dedup_total += corrected
            raw_total += corrected * level
            percentages[slot_for_level(level)] += corrected * level

        if raw_total == 0:
            return {}

        percentages = [round(100.0 * p / raw_total, 4) for p in percentages]

        return {
            "labels": list(DUPLICATION_LABELS),
            "percentages": percentages,
            "percent_unique": round(100.0 * dedup_total / raw_total, 2),
            "total_reads": self.total_count,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_contamination_stats.py -q`
Expected: PASS, 33 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/contamination_stats.py backend/tests/pipelines/test_contamination_stats.py
git commit -m "feat(qc): duplication tracker with frozen-dictionary correction"
```

---

## Task 5: The adapter accumulator

**Files:**
- Modify: `backend/app/pipelines/contamination_stats.py`
- Test: `backend/tests/pipelines/test_contamination_stats.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_contamination_stats.py`:

```python
def test_adapter_tracker_marks_every_position_from_the_match_onward():
    """The cumulative rule: once a read has run into adapter it stays in
    adapter, which is what makes the plotted curve monotonic."""
    probes = [("Test", "ACGTACGTACGT")]
    tracker = cs.AdapterTracker(probes, max_positions=20)
    tracker.add("TTTTT" + "ACGTACGTACGT")

    counts = tracker.counts["Test"]
    assert counts[:5] == [0, 0, 0, 0, 0]
    assert all(c == 1 for c in counts[5:17])


def test_adapter_tracker_counts_only_the_earliest_match():
    """A probe occurring twice must not double-count the read."""
    probes = [("Test", "AAAAAAAAAAAA")]
    tracker = cs.AdapterTracker(probes, max_positions=40)
    tracker.add("A" * 12 + "CGT" + "A" * 12)

    assert tracker.counts["Test"][0] == 1
    assert max(tracker.counts["Test"]) == 1


def test_adapter_tracker_result_is_percentage_of_reads():
    probes = [("Test", "ACGTACGTACGT")]
    tracker = cs.AdapterTracker(probes, max_positions=20)
    tracker.add("ACGTACGTACGT")
    tracker.add("TTTTTTTTTTTT")
    tracker.add("TTTTTTTTTTTT")
    tracker.add("TTTTTTTTTTTT")

    result = tracker.result()
    series = result["series"][0]

    assert series["name"] == "Test"
    assert series["values"][0] == pytest.approx(25.0)


def test_adapter_tracker_keeps_all_zero_series():
    """Dropping empty series is the frontend's job -- the facts record what
    was probed for, so 'we looked and found none' stays distinguishable from
    'we never looked'."""
    probes = [("Test", "ACGTACGTACGT")]
    tracker = cs.AdapterTracker(probes, max_positions=10)
    tracker.add("TTTTTTTTTT")

    result = tracker.result()

    assert result["series"][0]["name"] == "Test"
    assert all(v == 0.0 for v in result["series"][0]["values"])


def test_adapter_tracker_result_is_empty_for_no_reads():
    assert cs.AdapterTracker([("Test", "ACGTACGTACGT")]).result() == {}


def test_adapter_tracker_truncates_positions_to_the_cap():
    probes = [("Test", "ACGTACGTACGT")]
    tracker = cs.AdapterTracker(probes, max_positions=5)
    tracker.add("T" * 200)

    assert len(tracker.result()["positions"]) == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_contamination_stats.py -q`
Expected: FAIL -- `AttributeError: module ... has no attribute 'AdapterTracker'`

- [ ] **Step 3: Write the implementation**

Append to `backend/app/pipelines/contamination_stats.py`:

```python
# Same cap and reasoning as `sequence_stats.MAX_POSITIONS`: the useful detail
# is at the start of the read, and an uncapped array would allocate megabytes
# per file on long-read input.
MAX_POSITIONS = 1_000


class AdapterTracker:
    """Per-position cumulative adapter matches, one counter row per probe."""

    def __init__(
        self,
        probes: list[tuple[str, str]],
        *,
        max_positions: int = MAX_POSITIONS,
    ) -> None:
        self.probes = probes
        self.max_positions = max_positions
        self.counts: dict[str, list[int]] = {
            name: [0] * max_positions for name, _ in probes
        }
        self.reads = 0
        self.longest_read = 0

    def add(self, seq: str) -> None:
        self.reads += 1
        self.longest_read = max(
            self.longest_read, min(len(seq), self.max_positions)
        )

        for name, probe in self.probes:
            index = seq.find(probe)
            if index < 0 or index >= self.max_positions:
                continue
            # Cumulative: a read that has entered adapter stays in adapter for
            # the rest of its length.
            row = self.counts[name]
            for pos in range(index, self.max_positions):
                row[pos] += 1

    def result(self) -> dict:
        if not self.reads or not self.longest_read:
            return {}

        width = self.longest_read
        return {
            "positions": list(range(1, width + 1)),
            "series": [
                {
                    "name": name,
                    "values": [
                        round(100.0 * c / self.reads, 4)
                        for c in self.counts[name][:width]
                    ],
                }
                for name, _ in self.probes
            ],
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_contamination_stats.py -q`
Expected: PASS, 39 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/contamination_stats.py backend/tests/pipelines/test_contamination_stats.py
git commit -m "feat(qc): per-position cumulative adapter tracker"
```

---

## Task 6: The file scan

**Files:**
- Modify: `backend/app/pipelines/contamination_stats.py`
- Test: `backend/tests/pipelines/test_contamination_stats.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_contamination_stats.py`:

```python
import gzip
import threading

from app.errors import JobCancelled
from app.models import Compression


def _write_fastq(path, reads):
    lines = []
    for i, seq in enumerate(reads):
        lines += [f"@read{i}", seq, "+", "I" * len(seq)]
    path.write_text("\n".join(lines) + "\n")


def test_scan_reports_both_statistics(tmp_path):
    path = tmp_path / "reads.fastq"
    _write_fastq(path, ["ACGT" * 15] * 10)

    facts = cs.scan_contamination(path, Compression.NONE)

    assert facts["qc_duplication_scanned_reads"] == 10
    assert facts["qc_percent_unique"] == pytest.approx(10.0)
    assert facts["qc_duplication_levels"]["labels"][0] == "1"
    assert facts["qc_adapter_content"]["series"]


def test_scan_detects_adapter_read_through(tmp_path):
    """A fragment shorter than the read: the tail is Nextera adapter."""
    path = tmp_path / "reads.fastq"
    _write_fastq(path, ["TTTTTTTT" + "CTGTCTCTTATA"] * 4)

    facts = cs.scan_contamination(path, Compression.NONE)
    series = {s["name"]: s["values"] for s in facts["qc_adapter_content"]["series"]}

    assert series["Nextera Transposase"][8] == pytest.approx(100.0)
    assert series["Nextera Transposase"][0] == pytest.approx(0.0)
    assert all(v == 0.0 for v in series["Illumina Universal"])


def test_scan_includes_detected_adapter_probe(tmp_path):
    path = tmp_path / "reads.fastq"
    _write_fastq(path, ["TTTTCCCCGGGGAAAA"] * 3)

    facts = cs.scan_contamination(
        path, Compression.NONE, detected_adapters=["TTTTCCCCGGGG"]
    )
    names = [s["name"] for s in facts["qc_adapter_content"]["series"]]

    assert "Detected" in names


def test_scan_reads_gzipped_input(tmp_path):
    path = tmp_path / "reads.fastq.gz"
    body = "\n".join(["@r", "ACGT" * 15, "+", "I" * 60]) + "\n"
    path.write_bytes(gzip.compress(body.encode()))

    facts = cs.scan_contamination(path, Compression.GZIP)

    assert facts["qc_duplication_scanned_reads"] == 1


def test_scan_returns_empty_for_an_empty_file(tmp_path):
    path = tmp_path / "empty.fastq"
    path.write_text("")

    assert cs.scan_contamination(path, Compression.NONE) == {}


def test_scan_returns_empty_rather_than_raising_on_unreadable_input(tmp_path):
    """A scan failure must not fail the QC job -- same contract as FastQC's."""
    assert cs.scan_contamination(tmp_path / "missing.fastq", Compression.NONE) == {}


def test_scan_honours_cancellation(tmp_path):
    path = tmp_path / "reads.fastq"
    _write_fastq(path, ["ACGT" * 15] * 50)

    cancel = threading.Event()
    cancel.set()

    with pytest.raises(JobCancelled):
        cs.scan_contamination(
            path, Compression.NONE, cancel_event=cancel, cancel_check_reads=1
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_contamination_stats.py -q`
Expected: FAIL -- `AttributeError: module ... has no attribute 'scan_contamination'`

- [ ] **Step 3: Write the implementation**

Append to `backend/app/pipelines/contamination_stats.py`:

```python
CANCEL_CHECK_READS = 20_000


def scan_contamination(
    path: Path,
    compression: Compression,
    *,
    detected_adapters: list[str | None] | None = None,
    cancel_event: threading.Event | None = None,
    cancel_check_reads: int = CANCEL_CHECK_READS,
) -> dict:
    """Adapter content and duplication levels, from one whole-file pass.

    Returns `qc_`-prefixed facts ready to merge into a QC result, or `{}` when
    the file could not be read. Every failure short of cancellation is
    swallowed to a warning: this is the optional half of a QC run, exactly
    like FastQC, and a scan that cannot parse the file must not cost the user
    the facts that fastp did produce.
    """
    opener = (
        gzip.open
        if compression in (Compression.GZIP, Compression.BGZF)
        else open
    )

    probes = build_probes(list(detected_adapters or []))
    adapters = AdapterTracker(probes)
    duplication = DuplicationTracker()
    reads = 0

    try:
        with opener(path, "rt", errors="replace") as fh:
            while True:
                header = fh.readline()
                if not header:
                    break
                seq = fh.readline().rstrip("\n")
                fh.readline()  # '+' separator
                qual = fh.readline()
                if not qual:
                    break

                reads += 1
                adapters.add(seq)
                duplication.add(seq)

                if reads % cancel_check_reads == 0 and cancel_event is not None:
                    if cancel_event.is_set():
                        raise JobCancelled("Cancelled during contamination scan")
    except JobCancelled:
        raise
    except (OSError, EOFError, UnicodeDecodeError) as e:
        log.warning("contamination_scan_failed", path=str(path), error=str(e))
        return {}

    if not reads:
        return {}

    facts: dict = {"qc_duplication_scanned_reads": reads}

    adapter_result = adapters.result()
    if adapter_result:
        facts["qc_adapter_content"] = adapter_result

    dup_result = duplication.result()
    if dup_result:
        facts["qc_percent_unique"] = dup_result.pop("percent_unique")
        dup_result.pop("total_reads", None)
        facts["qc_duplication_levels"] = dup_result

    return facts
```

Note the cancellation check runs *before* the `JobCancelled` re-raise clause
catches it -- `JobCancelled` is re-raised deliberately so a cancelled job
still cancels, while genuine read errors return `{}`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_contamination_stats.py -q`
Expected: PASS, 46 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/contamination_stats.py backend/tests/pipelines/test_contamination_stats.py
git commit -m "feat(qc): whole-file contamination scan entry point"
```

---

## Task 7: Wire the scan into the QC job

**Files:**
- Modify: `backend/app/queue/pipeline_handlers.py:505-515`
- Test: `backend/tests/pipelines/test_contamination_stats.py`

The scan needs the file's compression, which the QC payload does not carry.
`reads_in` is an `in_`-prefixed symlink whose name preserves the original
extension, but sniffing the magic bytes is more reliable than trusting it --
and `storage/detect.py` already does exactly that.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_contamination_stats.py`:

```python
def test_compression_of_sniffs_gzip(tmp_path):
    """BGZF is gzip with an extra subfield, and `detect_compression`
    distinguishes them -- both must open with the gzip reader, so the scan
    treats them the same and this test accepts either."""
    path = tmp_path / "reads.fastq.gz"
    path.write_bytes(gzip.compress(b"@r\nACGT\n+\nIIII\n"))

    assert cs.compression_of(path) in (Compression.GZIP, Compression.BGZF)


def test_compression_of_sniffs_plain_text(tmp_path):
    path = tmp_path / "reads.fastq"
    path.write_text("@r\nACGT\n+\nIIII\n")

    assert cs.compression_of(path) == Compression.NONE


def test_compression_of_defaults_to_none_when_unreadable(tmp_path):
    assert cs.compression_of(tmp_path / "missing.fastq") == Compression.NONE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_contamination_stats.py -q`
Expected: FAIL -- `AttributeError: module ... has no attribute 'compression_of'`

- [ ] **Step 3: Add `compression_of` to the scan module**

Append to `backend/app/pipelines/contamination_stats.py`:

```python
def compression_of(path: Path) -> Compression:
    """Sniff a file's compression from its magic bytes.

    The QC payload carries no compression field, and the path handed to this
    module is a symlink created to give fastp a readable filename. Reading the
    first bytes is both more direct and more reliable than re-deriving it from
    that name.
    """
    from app.storage.detect import detect_compression

    try:
        with open(path, "rb") as fh:
            return detect_compression(fh.read(64))
    except OSError:
        return Compression.NONE
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_contamination_stats.py -q`
Expected: PASS, 49 passed

- [ ] **Step 5: Verify `detect_compression`'s signature before wiring**

Run: `grep -n "def detect_compression" -A 12 backend/app/storage/detect.py`
Expected: a function taking a `bytes` head and returning `Compression`. If its
name or signature differs, adapt the call above rather than the test.

- [ ] **Step 6: Call the scan from `_run_short_read_qc`**

In `backend/app/queue/pipeline_handlers.py`, add the import beside the other
pipeline imports at the top of the file:

```python
from app.pipelines import contamination_stats
```

This module imports `PermanentError` and `RetryableError` from `app.errors`
but **not** `JobCancelled`, which the snippet below needs. Extend the existing
line 16 import:

```python
from app.errors import JobCancelled, PermanentError, RetryableError
```

Then in `_run_short_read_qc`, immediately after the FastQC block
(`facts["qc_fastqc_report"] = fastqc_name`) and *before* the
`facts["qc_read_chemistry"] = ReadChemistry.SHORT.value` line, insert:

```python
    # Adapter content and duplication levels, from a whole-file pass. Wrapped
    # exactly like FastQC above: this is the optional half of the run, and a
    # scan that fails must not cost the user the fastp facts that succeeded.
    ctx.progress(phase="contamination", pct=0.9, message="scanning for adapters")
    try:
        detected = facts.get("qc_adapters") or {}
        facts.update(
            contamination_stats.scan_contamination(
                reads_in,
                contamination_stats.compression_of(reads_in),
                detected_adapters=[
                    detected.get("read1_sequence"),
                    detected.get("read2_sequence"),
                ],
                cancel_event=ctx.cancel_event,
            )
        )
    except JobCancelled:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("qc_contamination_failed", job_id=ctx.job_id, error=str(e))
```

- [ ] **Step 7: Confirm the import landed**

Run: `grep -n "JobCancelled" backend/app/queue/pipeline_handlers.py | head -3`
Expected: the import from Step 6 is present. Without it the new block raises
`NameError` on its `except JobCancelled:` clause the first time QC runs --
which the unit tests will not catch, because they call the scan directly
rather than through the handler.

`ctx.cancel_event` needs no such check: it is a `threading.Event` field on
`JobContext` in `backend/app/queue/registry.py:79`, which is also where
`check_cancel()` lives.

- [ ] **Step 8: Run the full backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS. Read the count -- it should be the pre-existing total plus 49.
Any failure in `tests/queue/` means the handler change broke an existing QC
test; fix it before continuing rather than proceeding to the frontend.

- [ ] **Step 9: Commit**

```bash
git add backend/app/pipelines/contamination_stats.py backend/app/queue/pipeline_handlers.py backend/tests/pipelines/test_contamination_stats.py
git commit -m "feat(qc): run contamination scan during short-read QC"
```

---

## Task 8: Frontend fact types

**Files:**
- Modify: `frontend/src/api/types.ts:1216`

- [ ] **Step 1: Add the types**

In `frontend/src/api/types.ts`, inside the `QcFacts` interface, after the
`qc_adapters` block and before `qc_fastp_report`:

```typescript
  /**
   * Per-position cumulative adapter percentages, one series per probe.
   * Written by the whole-file contamination scan; absent on files QC'd
   * before it existed, which is why every consumer treats it as optional.
   */
  qc_adapter_content?: {
    positions: number[];
    series: { name: string; values: number[] }[];
  };
  /** FastQC's 16-slot duplication histogram, as percentages of the library. */
  qc_duplication_levels?: {
    labels: string[];
    percentages: number[];
  };
  /** Whole-file, correction-adjusted. Preferred over `qc_duplication_rate`. */
  qc_percent_unique?: number;
  qc_duplication_scanned_reads?: number;
```

- [ ] **Step 2: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors. (If the repo has no `tsc` script configured, the type
errors will surface in the Vite dev server in Task 11 instead.)

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/types.ts
git commit -m "feat(qc): fact types for contamination charts"
```

---

## Task 9: The chart components

**Files:**
- Create: `frontend/src/components/ContaminationCharts.tsx`

There is no headless component-testing setup in this repo (no jsdom, zero
`.test.tsx` files) and none is expected -- these are verified in the browser in
Task 11.

- [ ] **Step 1: Write the components**

Create `frontend/src/components/ContaminationCharts.tsx`:

```tsx
import { useState } from "react";

/**
 * Adapter content and duplication levels.
 *
 * Hand-rolled SVG for the same reason `SequenceCharts.tsx` is: these are
 * fixed, simple shapes, and the smallest charting dependency would outweigh
 * the entire rest of the bundle.
 *
 * Both self-suppress when their facts are absent, so a file QC'd before the
 * contamination scan existed renders the tab exactly as it did before.
 */

interface AdapterSeries {
  name: string;
  values: number[];
}

// Distinct enough to tell six overlapping curves apart, and themeable like
// the base colours in SequenceCharts.
const SERIES_COLORS = [
  "var(--accent)",
  "var(--base-t, #f85149)",
  "var(--base-c, #4a9eff)",
  "var(--base-g, #d29922)",
  "var(--base-a, #3fb950)",
  "var(--base-other, #a371f7)",
  "var(--base-n, #8b949e)",
];

export function AdapterContentChart({
  positions,
  series,
}: {
  positions: number[];
  series: AdapterSeries[];
}) {
  const [hover, setHover] = useState<number | null>(null);
  if (!positions?.length || !series?.length) return null;

  // A probe that never matched is not a finding, and six flat lines along the
  // axis hide the one line that matters. The facts keep every probe; the
  // chart shows the ones with something to say.
  const present = series.filter((s) => s.values.some((v) => v > 0));

  if (!present.length) {
    return (
      <div style={{ color: "var(--text-dim)", fontSize: 12, padding: "8px 0" }}>
        No adapter sequence detected.
      </div>
    );
  }

  const w = 460;
  const h = 210;
  const pad = { top: 10, right: 96, bottom: 26, left: 34 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  // Scaled to what was observed, not to 100%. A 4% adapter curve flattened
  // against a full-height axis communicates nothing, and 4% is worth seeing.
  const observed = Math.max(...present.flatMap((s) => s.values));
  const yMax = Math.max(Math.ceil(observed * 1.15), 1);

  const maxPos = positions[positions.length - 1];
  const x = (p: number) => pad.left + ((p - 1) / Math.max(maxPos - 1, 1)) * plotW;
  const y = (v: number) => pad.top + plotH - (Math.min(v, yMax) / yMax) * plotH;

  const ticks = [0, yMax / 2, yMax];

  return (
    <div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block" }}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const box = e.currentTarget.getBoundingClientRect();
          const px = ((e.clientX - box.left) / box.width) * w;
          const frac = (px - pad.left) / plotW;
          const idx = Math.round(frac * (positions.length - 1));
          setHover(idx >= 0 && idx < positions.length ? idx : null);
        }}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line
              x1={pad.left}
              x2={w - pad.right}
              y1={y(t)}
              y2={y(t)}
              stroke="var(--border)"
              strokeWidth="1"
            />
            <text
              x={pad.left - 5}
              y={y(t) + 3}
              textAnchor="end"
              fontSize="9"
              fill="var(--text-faint)"
            >
              {t.toFixed(t < 10 ? 1 : 0)}%
            </text>
          </g>
        ))}

        {present.map((s, i) => (
          <path
            key={s.name}
            d={s.values
              .map((v, j) => `${j ? "L" : "M"} ${x(positions[j])} ${y(v)}`)
              .join(" ")}
            fill="none"
            stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
            strokeWidth="1.8"
          />
        ))}

        {hover != null && (
          <line
            x1={x(positions[hover])}
            x2={x(positions[hover])}
            y1={pad.top}
            y2={pad.top + plotH}
            stroke="var(--text-faint)"
            strokeWidth="1"
          />
        )}

        {present.map((s, i) => (
          <g key={s.name}>
            <line
              x1={w - pad.right + 6}
              x2={w - pad.right + 16}
              y1={pad.top + 8 + i * 13}
              y2={pad.top + 8 + i * 13}
              stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
              strokeWidth="2"
            />
            <text
              x={w - pad.right + 20}
              y={pad.top + 11 + i * 13}
              fontSize="8"
              fill="var(--text-dim)"
            >
              {s.name}
            </text>
          </g>
        ))}

        <text
          x={pad.left + plotW / 2}
          y={h - 6}
          textAnchor="middle"
          fontSize="9"
          fill="var(--text-faint)"
        >
          position in read (bp)
        </text>
      </svg>

      {hover != null && (
        <div style={{ fontSize: 11, color: "var(--text-dim)" }}>
          Position {positions[hover]}:{" "}
          {present
            .map((s) => `${s.name} ${s.values[hover].toFixed(2)}%`)
            .join(" · ")}
        </div>
      )}
    </div>
  );
}

/**
 * Bars rather than FastQC's line chart: the x axis is ordinal bins of uneven
 * width (1, 2, ... >500, >1k), so a connecting line would imply an
 * interpolation between >500 and >1k that does not exist.
 */
export function DuplicationLevelsChart({
  labels,
  percentages,
  percentUnique,
  scannedReads,
}: {
  labels: string[];
  percentages: number[];
  percentUnique?: number;
  scannedReads?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  if (!labels?.length || !percentages?.length) return null;

  const w = 460;
  const h = 210;
  const pad = { top: 10, right: 12, bottom: 34, left: 34 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const yMax = Math.max(Math.ceil(Math.max(...percentages)), 1);
  const barW = plotW / labels.length;
  const y = (v: number) => pad.top + plotH - (Math.min(v, yMax) / yMax) * plotH;

  return (
    <div>
      {percentUnique != null && (
        <div style={{ fontSize: 12, marginBottom: 6 }}>
          <strong>{percentUnique.toFixed(1)}%</strong>
          <span style={{ color: "var(--text-dim)" }}> of the library is unique</span>
        </div>
      )}

      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        style={{ maxWidth: w, display: "block" }}
        onMouseLeave={() => setHover(null)}
      >
        {[0, yMax / 2, yMax].map((t) => (
          <g key={t}>
            <line
              x1={pad.left}
              x2={w - pad.right}
              y1={y(t)}
              y2={y(t)}
              stroke="var(--border)"
              strokeWidth="1"
            />
            <text
              x={pad.left - 5}
              y={y(t) + 3}
              textAnchor="end"
              fontSize="9"
              fill="var(--text-faint)"
            >
              {t.toFixed(0)}%
            </text>
          </g>
        ))}

        {percentages.map((p, i) => (
          <rect
            key={labels[i]}
            x={pad.left + i * barW + 1}
            y={y(p)}
            width={Math.max(barW - 2, 1)}
            height={Math.max(pad.top + plotH - y(p), 0)}
            fill="var(--accent)"
            opacity={hover === i ? 1 : 0.75}
            onMouseEnter={() => setHover(i)}
          />
        ))}

        {labels.map((label, i) => (
          <text
            key={label}
            x={pad.left + i * barW + barW / 2}
            y={pad.top + plotH + 12}
            textAnchor="middle"
            fontSize="7"
            fill="var(--text-faint)"
          >
            {label}
          </text>
        ))}

        <text
          x={pad.left + plotW / 2}
          y={h - 4}
          textAnchor="middle"
          fontSize="9"
          fill="var(--text-faint)"
        >
          times a sequence appears
        </text>
      </svg>

      <div style={{ fontSize: 11, color: "var(--text-dim)" }}>
        {hover != null
          ? `Seen ${labels[hover]}x: ${percentages[hover].toFixed(2)}% of the library`
          : scannedReads != null
            ? `${scannedReads.toLocaleString()} reads, whole file`
            : ""}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/ContaminationCharts.tsx
git commit -m "feat(qc): adapter content and duplication level charts"
```

---

## Task 10: Mount the charts and prefer the better duplication number

**Files:**
- Modify: `frontend/src/components/DetailPanel.tsx:1023`
- Modify: `frontend/src/components/QcReport.tsx:97-104`

- [ ] **Step 1: Mount the charts**

In `frontend/src/components/DetailPanel.tsx`, add to the imports near the
existing `SequenceCharts` import (~line 27):

```tsx
import { AdapterContentChart, DuplicationLevelsChart } from "./ContaminationCharts";
```

Inside the `.qc-charts` grid, immediately after the `{curve && ...}` block and
before the `{showChromStrip && ...}` line, insert:

```tsx
          {/* Contamination and library complexity, from the whole-file QC
              scan. Both self-suppress on files QC'd before that scan existed,
              so the grid keeps its old two-up shape for them. */}
          {obj.facts.qc_adapter_content != null && (
            <div className="qc-chart">
              <div className="section-title">Adapter content</div>
              <AdapterContentChart
                positions={(obj.facts.qc_adapter_content as never as { positions: number[] }).positions}
                series={(obj.facts.qc_adapter_content as never as { series: { name: string; values: number[] }[] }).series}
              />
            </div>
          )}
          {obj.facts.qc_duplication_levels != null && (
            <div className="qc-chart">
              <div className="section-title">Sequence duplication levels</div>
              <DuplicationLevelsChart
                labels={(obj.facts.qc_duplication_levels as never as { labels: string[] }).labels}
                percentages={(obj.facts.qc_duplication_levels as never as { percentages: number[] }).percentages}
                percentUnique={obj.facts.qc_percent_unique as number | undefined}
                scannedReads={obj.facts.qc_duplication_scanned_reads as number | undefined}
              />
            </div>
          )}
```

Note the `as never as` casts match the existing idiom in this file (`composition
as never`, `curve as never`) -- `obj.facts` is `Record<string, unknown>` here,
not `QcFacts`.

- [ ] **Step 2: Update the grid's render condition**

Still in `DetailPanel.tsx`, the grid is gated on
`{(composition || curve || showChromStrip) && (`. A file with contamination
facts but no composition or curve would otherwise render neither chart. Change
that line to:

```tsx
      {(composition ||
        curve ||
        showChromStrip ||
        obj.facts.qc_adapter_content != null ||
        obj.facts.qc_duplication_levels != null) && (
```

- [ ] **Step 3: Prefer the whole-file duplication number**

In `frontend/src/components/QcReport.tsx`, replace the existing duplication
block (lines ~97-104):

```tsx
        {qc.qc_duplication_rate != null && (
          <>
            <dt>Duplication</dt>
            {/* Inverted: a high duplication rate is the bad direction, where a
                high Q30 is the good one. */}
            <dd>{quality(qc.qc_duplication_rate, 0.3, { goodWhenLow: true })}</dd>
          </>
        )}
```

with:

```tsx
        {/* The whole-file scan's number wins over fastp's when it exists:
            fastp reports duplication from its own sampled estimate, while
            `qc_percent_unique` comes from a full pass with FastQC's
            frozen-dictionary correction applied. fastp's value stays in the
            facts for provenance -- it is a real measurement -- but showing
            both would put two methods' answers side by side, disagreeing, on
            the same screen. */}
        {(qc.qc_percent_unique != null || qc.qc_duplication_rate != null) && (
          <>
            <dt>Duplication</dt>
            {/* Inverted: a high duplication rate is the bad direction, where a
                high Q30 is the good one. */}
            <dd>
              {quality(
                qc.qc_percent_unique != null
                  ? 1 - qc.qc_percent_unique / 100
                  : qc.qc_duplication_rate,
                0.3,
                { goodWhenLow: true },
              )}
            </dd>
          </>
        )}
```

- [ ] **Step 4: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/DetailPanel.tsx frontend/src/components/QcReport.tsx
git commit -m "feat(qc): mount contamination charts, prefer whole-file duplication"
```

---

## Task 11: Verify in the browser

**Files:** none modified unless a defect is found.

Manual browser testing is the actual verification step for anything UI-facing
in this repo. Do not skip it and do not substitute the unit tests for it.

- [ ] **Step 1: Start this worktree's stack**

```bash
./ops/worktree-up.sh
```

Expected: UI on http://localhost:5273, API on 8100. This is a *separate* stack
from the main one on 5173, which keeps serving `main` throughout.

- [ ] **Step 2: Run QC on a real FASTQ**

In the UI at localhost:5273, open a project with a short-read FASTQ, open the
file, and run QC from the QC tab.

- [ ] **Step 3: Confirm the charts render**

Expected on the QC tab:
- Four charts in the grid, reflowing 2x2 at normal window width. The CSS is
  `repeat(auto-fit, minmax(320px, 1fr))`, so no stylesheet change should be
  needed -- if the layout breaks, that is a real finding to fix here.
- "Adapter content" shows either curves or the "No adapter sequence detected"
  line. Both are correct outcomes; a clean modern library often has almost no
  adapter.
- "Sequence duplication levels" shows bars, with the percent-unique headline
  above and the read count below.
- The "Duplication" row in the Quality control table is consistent with the
  percent-unique headline (they must sum to ~100%).

- [ ] **Step 4: Confirm a high-duplication file**

Run QC on a file with genuinely high duplication if the project has one -- an
amplicon or RNA-seq library is a good candidate. A chart verified only against
a clean file is not verified: only a duplicated file exercises the bins, the
axis scaling, and the correction.

If no such file exists, generate one:

```bash
python3 -c "
seq='ACGT'*15
with open('/tmp/dup_test.fastq','w') as f:
    for i in range(50000):
        s = seq if i % 2 else f'{i:060d}'.replace('0','A').replace('1','C')
        f.write(f'@r{i}\n{s}\n+\n{\"I\"*len(s)}\n')
"
```

Ingest `/tmp/dup_test.fastq` and run QC on it. Expected: roughly 50% of the
library in a high-duplication slot, and a percent-unique well below 100.

- [ ] **Step 5: Confirm an old file still renders**

Open a FASTQ that was QC'd *before* this change (one you have not re-run).
Expected: the tab renders exactly as it did before -- two charts, and the
Duplication row still populated from fastp's rate. This is the backward
compatibility path and it is the one most likely to be quietly broken.

- [ ] **Step 6: Stop the worktree stack**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 7: Commit any fixes**

Only if Steps 3-5 turned up defects. Otherwise nothing to commit.

---

## Task 12: Documentation and close-out

**Files:**
- Modify: `docs/TODO.md` / `docs/TODO-done.md` (only if an entry covers this)

- [ ] **Step 1: Check for a TODO entry**

Run: `grep -in "adapter\|duplication\|contamination" docs/TODO.md`

If an entry covers this work, append ` — FIXED` to its heading, add a note
saying what shipped and where the code lives, note what the implementation did
differently from the plan, and move the whole entry to `docs/TODO-done.md`.
If there is no such entry, skip this task -- do not invent one.

- [ ] **Step 2: Run the full suite one more time**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS. Read the count, not just the exit code.

- [ ] **Step 3: Merge to main and push**

Per CLAUDE.md: once the suite is green and `main` is clean, merge and push
without asking. If `main` has moved, re-run the suite after merging rather
than assuming the earlier green still holds.

```bash
git checkout main
git pull
git merge claude/contamination-library-complexity-viz-bb3412
./backend/run-worktree-tests.sh tests/ -q
git push origin main
```

- [ ] **Step 4: Update the tracking issue**

If a GitHub issue tracks this work, update it with the implementation status
and set the appropriate `status:` label.
