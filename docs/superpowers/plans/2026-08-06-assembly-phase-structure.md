# Assembly Phase Structure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `assembly_runner.AssemblyProgress` a `phase_index`/`phase_total` derived from the stages a given Flye run will actually execute, so assembly job cards show "step N of M" like every other runner.

**Architecture:** Flye builds its whole job list at launch (`flye/main.py:_create_job_list`), so the stage sequence is knowable before the process starts. A pure function `flye_stage_order(params)` returns that sequence -- seven stages, or six when `iterations == 0` drops `polishing`. `AssemblyProgress` takes it as a constructor argument and indexes into it on the **raw** Flye stage name, not the display label. Any stage outside the declared list leaves `phase_index` null, degrading to exactly today's raw-name display.

**Tech Stack:** Python 3, dataclasses, pytest. Backend only -- the frontend already renders the counter when both fields are non-null (`ActivityView.tsx:354`, `JobList.tsx:70`) and needs no change.

**Spec:** [`docs/superpowers/specs/2026-08-06-assembly-phase-structure-design.md`](../specs/2026-08-06-assembly-phase-structure-design.md)

---

## Before You Start

**Run the tests from the worktree, not with plain `docker compose exec`.** Inside a worktree, `docker compose exec api python -m pytest` silently tests *main's* code -- the `api` container bind-mounts the main checkout. Every command in this plan uses:

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py -q
```

which mounts this worktree's source and gets its own throwaway Mongo.

**Background you need for Task 1.** The stage list comes from reading Flye's own source in the image. To see it yourself:

```bash
docker compose -p biopipe exec -T api bash -c 'sed -n "/^def _create_job_list/,/^def /p" /usr/lib/python3/dist-packages/flye/main.py'
```

Seven `Job*` classes get appended in a fixed order. `consensus` is skipped when `read_type == "subasm"` (a mode BioFlow never sends -- `assembler_registry` offers six modes and that is not one of them), and `polishing` is skipped when `--iterations 0` (which BioFlow **can** send: `assembly_params.MIN_ITERATIONS = 0`). `trestle` is commented out entirely in 2.9.5.

## File Structure

- `backend/app/pipelines/assembly_runner.py` -- add `_FLYE_STAGES`, `flye_stage_order()`; extend `AssemblyProgress` with `stage_order`, `_stage`, `phase_index`, `phase_total`; drop the `trestle` label; correct the class docstring.
- `backend/app/queue/assembly_handlers.py:95` -- pass the derived stage order at construction.
- `backend/app/models/job.py:113-118` -- correct the comment that names assembly as the runner that cannot declare a phase list.
- `backend/tests/pipelines/test_assembly_runner.py` -- new `TestFlyeStageOrder` and `TestAssemblyPhaseStructure` classes.
- `backend/tests/pipelines/test_progress_parser_fixtures.py` -- assert phase structure against the real Flye log.

---

### Task 1: `flye_stage_order` derives the stage list from params

**Files:**
- Modify: `backend/app/pipelines/assembly_runner.py`
- Test: `backend/tests/pipelines/test_assembly_runner.py`

- [ ] **Step 1: Write the failing tests**

Add this class to `backend/tests/pipelines/test_assembly_runner.py`, after the existing `TestAssemblyProgress` class:

```python
class TestFlyeStageOrder:
    """The stage list Flye will actually run, derived from params.

    Flye builds its whole job list at launch (`_create_job_list`), so this is
    knowable before the process starts -- which is what makes an honest
    phase_total possible at all.
    """

    def test_default_params_run_all_seven_stages(self):
        order = assembly_runner.flye_stage_order(FlyeParams())
        assert order == (
            "configure",
            "assembly",
            "consensus",
            "repeat",
            "contigger",
            "polishing",
            "finalize",
        )

    def test_zero_iterations_drops_polishing(self):
        """`--iterations 0` skips JobPolishing, so declaring 7 would leave the
        counter jumping from 5/7 to 7/7 with nothing at 6."""
        order = assembly_runner.flye_stage_order(FlyeParams(iterations=0))
        assert "polishing" not in order
        assert len(order) == 6
        assert order[-1] == "finalize"

    def test_extra_iterations_do_not_add_stages(self):
        """Polishing is one stage regardless of how many rounds it runs."""
        order = assembly_runner.flye_stage_order(FlyeParams(iterations=5))
        assert len(order) == 7

    def test_every_stage_has_a_display_label(self):
        """_STAGE_LABELS and _FLYE_STAGES are two hand-maintained structures
        in parallel: a stage in one and not the other is skipped silently
        rather than raised, which is the shape CLAUDE.md flags (the same trap
        COMPONENT_ORDER carried against COMPONENTS)."""
        assert set(assembly_runner._STAGE_LABELS) == set(
            assembly_runner._FLYE_STAGES
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py::TestFlyeStageOrder -q
```

Expected: 4 failures. The first three with `AttributeError: module 'app.pipelines.assembly_runner' has no attribute 'flye_stage_order'`; the fourth with an `AssertionError` on the set comparison, because `_STAGE_LABELS` currently contains a `trestle` key and `_FLYE_STAGES` does not exist yet.

- [ ] **Step 3: Add the stage tuple and the function**

In `backend/app/pipelines/assembly_runner.py`, replace the `_STAGE_LABELS` block (currently at lines 62-71, just below the `_STAGE_RE` definition) with the following. Note the `trestle` entry is **deleted** -- `JobTrestle` is commented out in 2.9.5's `_create_job_list` and `--trestle` is marked deprecated in `--help`, so that stage cannot be emitted:

```python
# The stages Flye runs, in order. Not a guess and not open-ended:
# `flye/main.py:_create_job_list` appends these seven jobs at launch, before
# any work starts, so the sequence is knowable before the process runs.
_FLYE_STAGES: tuple[str, ...] = (
    "configure",
    "assembly",
    "consensus",
    "repeat",
    "contigger",
    "polishing",
    "finalize",
)

_STAGE_LABELS: dict[str, str] = {
    "configure": "configuring",
    "assembly": "assembling draft",
    "consensus": "building consensus",
    "repeat": "resolving repeats",
    "contigger": "generating contigs",
    "polishing": "polishing",
    "finalize": "finishing",
}


def flye_stage_order(params: FlyeParams) -> tuple[str, ...]:
    """The stages this particular run will execute, in order.

    Mirrors the two conditionals in `_create_job_list`. Only one of them can
    fire here: `consensus` is skipped for `read_type == "subasm"`, which is
    not among the modes `assembler_registry` offers, while `--iterations 0`
    genuinely does drop polishing and is selectable in the dialog
    (`MIN_ITERATIONS = 0`).
    """
    if params.iterations > 0:
        return _FLYE_STAGES
    return tuple(stage for stage in _FLYE_STAGES if stage != "polishing")
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py::TestFlyeStageOrder -q
```

Expected: `4 passed`.

- [ ] **Step 5: Run the whole assembly-runner file to catch the trestle removal**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py -q
```

Expected: all pass. No existing test feeds `>>>STAGE: trestle`, so deleting the key breaks nothing. If something does fail here, it is a test asserting the old label map -- read it before changing it.

- [ ] **Step 6: Commit**

```bash
git add backend/app/pipelines/assembly_runner.py backend/tests/pipelines/test_assembly_runner.py
git commit -m "feat(assembly): derive Flye's stage order from params (#55)

Flye builds its full job list at launch, so the stage sequence is known
before the run starts: seven stages, or six when --iterations 0 drops
polishing. Also drops the trestle label -- JobTrestle is commented out
in 2.9.5 and --trestle is deprecated, so that stage cannot be emitted.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `AssemblyProgress` reports phase index and total

**Files:**
- Modify: `backend/app/pipelines/assembly_runner.py` (the `AssemblyProgress` dataclass)
- Test: `backend/tests/pipelines/test_assembly_runner.py`

- [ ] **Step 1: Write the failing tests**

Add this class to `backend/tests/pipelines/test_assembly_runner.py`, after `TestFlyeStageOrder`:

```python
class TestAssemblyPhaseStructure:
    """"Step N of M" for assembly. The UI renders the counter only when index
    and total are both non-null, so every case below is either a real pair or
    a deliberate fallback to the phase name alone.
    """

    def _progress(self, iterations: int = 1) -> AssemblyProgress:
        return AssemblyProgress(
            stage_order=assembly_runner.flye_stage_order(
                FlyeParams(iterations=iterations)
            )
        )

    def test_first_stage_is_step_one(self):
        progress = self._progress()
        progress.feed(">>>STAGE: configure")
        assert progress.phase_index == 1
        assert progress.phase_total == 7

    def test_index_advances_through_the_whole_run(self):
        progress = self._progress()
        seen = []
        for stage in assembly_runner._FLYE_STAGES:
            progress.feed(f">>>STAGE: {stage}")
            seen.append(progress.phase_index)
        assert seen == [1, 2, 3, 4, 5, 6, 7]

    def test_final_stage_is_the_last_step(self):
        progress = self._progress()
        progress.feed(">>>STAGE: finalize")
        assert progress.phase_index == 7
        assert progress.phase_total == 7

    def test_zero_iterations_finishes_at_six_of_six(self):
        """The case that decided the design: a flat constant would report
        finalize as 7 of 7 on a run that only ever executes six stages."""
        progress = self._progress(iterations=0)
        progress.feed(">>>STAGE: contigger")
        assert progress.phase_index == 5
        progress.feed(">>>STAGE: finalize")
        assert progress.phase_index == 6
        assert progress.phase_total == 6

    def test_index_is_null_before_any_stage_line(self):
        progress = self._progress()
        assert progress.phase_index is None
        assert progress.phase == "starting"

    def test_unknown_stage_shows_its_name_without_a_step_number(self):
        """A future Flye stage must not borrow the previous stage's number.
        Null index means the UI drops the counter and shows the name alone --
        exactly what shipped before this feature."""
        progress = self._progress()
        progress.feed(">>>STAGE: repeat")
        assert progress.phase_index == 4
        progress.feed(">>>STAGE: newthing")
        assert progress.phase == "newthing"
        assert progress.phase_index is None
        assert progress.phase_total == 7

    def test_snapshot_carries_both_keys(self):
        progress = self._progress()
        progress.feed(">>>STAGE: repeat")
        snap = progress.snapshot()
        assert snap["phase_index"] == 4
        assert snap["phase_total"] == 7
        assert snap["phase"] == "resolving repeats"
        assert snap["pct"] is None

    def test_snapshot_omits_both_keys_without_a_declared_order(self):
        """executor.py's parser contract: omit keys you do not know rather
        than passing None, which ctx.progress() would write over a value it
        should have left alone."""
        snap = AssemblyProgress().snapshot()
        assert "phase_index" not in snap
        assert "phase_total" not in snap
        assert snap["phase"] == "starting"

    def test_duplicate_labels_do_not_confuse_the_index(self):
        """Index keys on the raw stage name, not the display label. Two
        stages sharing a label must still report distinct steps."""
        progress = self._progress()
        progress.feed(">>>STAGE: consensus")
        first = progress.phase_index
        progress.feed(">>>STAGE: contigger")
        assert progress.phase_index != first
        assert progress.phase_index == 5
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py::TestAssemblyPhaseStructure -q
```

Expected: failures with `TypeError: AssemblyProgress.__init__() got an unexpected keyword argument 'stage_order'`.

- [ ] **Step 3: Rewrite the `AssemblyProgress` dataclass**

Replace the whole `AssemblyProgress` class in `backend/app/pipelines/assembly_runner.py` with the following. Two things to notice: the docstring's old claim that Flye's list is open-ended is **gone** (it was wrong, and it is the reason this feature was skipped in #24), while the duration argument against a *percentage* is kept because it is still true. And `_stage` uses `field(init=False, repr=False)` so it stays internal state rather than a constructor argument.

Add `field` to the existing dataclasses import at the top of the file:

```python
from dataclasses import dataclass, field
```

Then:

```python
@dataclass
class AssemblyProgress:
    """Turns Flye's own log into a phase name and a "step N of M".

    Deliberately no percentage. Flye's stages differ in duration by more than
    an order of magnitude -- polishing alone can outlast everything before it
    -- so a bar derived from stage position would sit at one value for most
    of a run and then jump. A step counter makes no duration claim, which is
    why it is safe here where a bar is not.

    `stage_order` comes from `flye_stage_order(params)` and defaults to empty,
    in which case no phase structure is reported at all and the display falls
    back to the phase name alone.
    """

    name: str = "flye"
    phase: str = "starting"
    stage_order: tuple[str, ...] = ()
    # The raw Flye stage name behind `phase`. Kept separately because
    # `_STAGE_LABELS` is not injective by construction -- two stages sharing a
    # display label would otherwise both resolve to the first one's index.
    _stage: str | None = field(default=None, init=False, repr=False)

    def feed(self, line: str) -> bool:
        """Consume a log line. True if the phase changed."""
        match = _STAGE_RE.search(line)
        if not match:
            return False
        stage = match.group(1).lower()
        # An unmapped stage still counts: a future Flye adding one should
        # display its raw name rather than leave the phase stuck on the
        # previous stage, which would read as a hang.
        phase = _STAGE_LABELS.get(stage, stage)
        if phase == self.phase:
            return False
        self._stage = stage
        self.phase = phase
        return True

    @property
    def phase_index(self) -> int | None:
        """Position in `stage_order`, 1-based for "step N of M" display.

        None for a stage this run did not declare -- a future Flye stage
        borrowing the previous stage's number would be worse than no number.
        """
        if self._stage is None or self._stage not in self.stage_order:
            return None
        return self.stage_order.index(self._stage) + 1

    @property
    def phase_total(self) -> int | None:
        return len(self.stage_order) or None

    def message(self) -> str:
        return self.phase

    def snapshot(self) -> dict:
        # No pct: see the class docstring. phase_index/phase_total appear only
        # when a stage order was declared -- a parser omits keys it does not
        # know rather than passing None over a value ctx.progress() would
        # otherwise leave alone.
        snap = {"pct": None, "phase": self.phase, "message": self.message()}
        if self.stage_order:
            snap["phase_index"] = self.phase_index
            snap["phase_total"] = self.phase_total
        return snap
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py -q
```

Expected: all pass, including the five pre-existing `TestAssemblyProgress` tests, which construct `AssemblyProgress()` with no arguments and must keep working unchanged.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/assembly_runner.py backend/tests/pipelines/test_assembly_runner.py
git commit -m "feat(assembly): report phase_index/phase_total from the stage order (#55)

Indexes on the raw Flye stage name rather than the display label, which
is not injective. An undeclared stage reports a null index, so the UI
falls back to the phase name alone exactly as it does today.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: The handler passes the run's stage order

**Files:**
- Modify: `backend/app/queue/assembly_handlers.py:95`

This is the change that makes the feature reach a real job. Without it every test above passes and the UI shows nothing new.

- [ ] **Step 1: Make the edit**

In `backend/app/queue/assembly_handlers.py`, find line 95:

```python
    progress = assembly_runner.AssemblyProgress()
```

Replace it with:

```python
    progress = assembly_runner.AssemblyProgress(
        stage_order=assembly_runner.flye_stage_order(params)
    )
```

`params` is already in scope (bound at line 76). No `isinstance` guard is needed: `build_assembly_command` raises `ValueError` for any assembler other than Flye about ten lines earlier, so a non-`FlyeParams` never reaches this line.

- [ ] **Step 2: Verify the handler still imports and the params flow works**

```bash
./backend/run-worktree-tests.sh tests/queue/ -q
```

Expected: all pass. If `tests/queue/` is large and slow, `-k assembly` narrows it.

- [ ] **Step 3: Commit**

```bash
git add backend/app/queue/assembly_handlers.py
git commit -m "feat(assembly): pass the run's stage order to the progress parser (#55)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Assert phase structure against the real Flye log

**Files:**
- Modify: `backend/tests/pipelines/test_progress_parser_fixtures.py`
- Read: `backend/tests/fixtures/tool_logs/flye-2.9.5.log`

The fixture is a real 2.9.5 run that aborted after the assembly stage, so it emits `>>>STAGE: configure` and `>>>STAGE: assembly` and nothing further. That is enough to prove the index tracks a genuine log rather than only hand-built strings -- which is the whole point of this file, per its module docstring about a stage table that silently stopped matching.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/pipelines/test_progress_parser_fixtures.py`, add these two methods to the existing `TestFlyeFixture` class:

```python
    def test_phase_index_tracks_the_real_log(self):
        """The fixture aborts after `assembly`, so it can only prove the
        first two steps -- but it proves them against bytes Flye wrote."""
        parser = AssemblyProgress(
            stage_order=assembly_runner.flye_stage_order(FlyeParams())
        )
        indices = []
        for line in (FIXTURES / "flye-2.9.5.log").read_text().splitlines():
            if parser.feed(line):
                indices.append(parser.phase_index)
        assert indices == [1, 2]
        assert parser.phase_total == 7

    def test_default_parser_reports_no_phase_structure(self):
        """Constructed without a stage order, the fixture replay must still
        produce today's behaviour: names, no numbers."""
        parser = AssemblyProgress()
        for line in (FIXTURES / "flye-2.9.5.log").read_text().splitlines():
            parser.feed(line)
        assert parser.phase_index is None
        assert "phase_index" not in parser.snapshot()
```

Add the imports these need to the top of the file, alongside the existing `from app.pipelines.assembly_runner import AssemblyProgress`:

```python
from app.pipelines import assembly_runner
from app.pipelines.assembly_params import FlyeParams
```

- [ ] **Step 2: Run the tests**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_progress_parser_fixtures.py -q
```

Expected: all pass, including the two pre-existing `TestFlyeFixture` tests.

If `test_phase_index_tracks_the_real_log` fails with `indices == [1]`, the `>>>STAGE:` regex matched only one line -- check the fixture still contains both stage lines with `grep '>>>STAGE' backend/tests/fixtures/tool_logs/flye-2.9.5.log`.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/pipelines/test_progress_parser_fixtures.py
git commit -m "test(assembly): assert phase structure against the real Flye log (#55)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Correct the comment that says assembly cannot do this

**Files:**
- Modify: `backend/app/models/job.py:113-118`

A comment explaining why code looks a certain way is the kind that misleads longest when it goes stale -- this repo has already paid for that with `ToolMeta.runnable`'s note about cutadapt.

- [ ] **Step 1: Make the edit**

In `backend/app/models/job.py`, find this comment above the `phase_index` field:

```python
    # "Step 2 of 5" -- only where a runner can declare its phase list up
    # front. Most can (fastp, align_runner); assembly_runner deliberately
    # cannot, because Flye's own stage list is not closed and displaying an
    # unrecognized stage raw is preferred there to a stale phase_total. Both
    # null means "unstructured -- render the phase name alone", which is the
    # correct representation for that case, not a placeholder.
```

Replace it with:

```python
    # "Step 2 of 5" -- only where a runner can declare its phase list up
    # front, which every runner now can: fastp and align_runner from a flat
    # constant, assembly_runner from `flye_stage_order(params)`, since Flye
    # builds its whole job list at launch and only `--iterations 0` varies it.
    # Both null still means "unstructured -- render the phase name alone",
    # which is the correct representation for a stage no runner declared
    # (a future Flye adding one), not a placeholder.
```

- [ ] **Step 2: Verify nothing asserted the old wording**

```bash
./backend/run-worktree-tests.sh tests/queue/test_job_context_progress.py -q
```

Expected: all pass. Note `tests/queue/test_job_context_progress.py:107` has a docstring referencing "Flye's open-ended stages" -- it describes a parser omitting the keys, which is still a real supported case (a default-constructed `AssemblyProgress`), so the test itself stays. Update only its docstring wording if you touch it; do not delete the test.

- [ ] **Step 3: Commit**

```bash
git add backend/app/models/job.py
git commit -m "docs: assembly can declare a phase list after all (#55)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Full suite, then verify against a real assembly

**Files:** none modified unless the suite finds something.

- [ ] **Step 1: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Read the **count**, not the exit code. Compare against the tree's baseline (roughly 1872 passing before this work; this plan adds 15). A handful of rotating DB-touching failures means two test runs are sharing Mongo -- `run-worktree-tests.sh` gives this run its own, so that should not happen here, but if you see it, re-run before investigating the code.

- [ ] **Step 2: Bring up the worktree stack**

```bash
./ops/worktree-up.sh
```

UI on 5273, API on 8100. This does **not** disturb the main instance on 5173.

- [ ] **Step 3: Run a real assembly and watch the counter**

Fixture tests feed hand-built strings that already look the way the code expects; CLAUDE.md is explicit that a real run is what actually verifies this. In the UI at `localhost:5273`, start an assembly on the smallest long-read set available in the project and watch the job card.

Expected: the card reads `configuring (step 1/7)`, then `assembling draft (step 2/7)`, and so on. Confirm specifically that the number **advances** rather than sitting at 1 -- a stuck counter means `_stage` is not being set in `feed`.

- [ ] **Step 4: Verify the `iterations = 0` path**

In the assembly dialog, set polishing iterations to 0 and start a second run. Expected: the total reads `/6` throughout, and the run ends at `finishing (step 6/6)`.

If the dialog does not expose iterations, verify this path directly instead. The worktree stack's project name is `biopipe-wt-<slug>` where the slug comes from the worktree directory name (`ops/worktree-up.sh:58`); confirm yours with `docker compose ls | grep biopipe-wt`:

```bash
docker compose -p biopipe-wt-nifty-liskov-9fbd93 exec -T api python -c "
from app.pipelines import assembly_runner
from app.pipelines.assembly_params import FlyeParams
p = assembly_runner.AssemblyProgress(stage_order=assembly_runner.flye_stage_order(FlyeParams(iterations=0)))
p.feed('>>>STAGE: finalize')
print(p.snapshot())
"
```

Expected output includes `'phase_index': 6, 'phase_total': 6`.

- [ ] **Step 5: Tear down the worktree stack**

```bash
./ops/worktree-up.sh --down
```

---

### Task 7: Close out the backlog and the issue

**Files:**
- Check: `docs/TODO.md`

- [ ] **Step 1: Check whether a TODO entry covers this**

```bash
grep -n -i "phase_index\|phase_total\|step N of M" docs/TODO.md
```

If an entry exists, append ` — FIXED` to its heading, write a note saying what shipped and where the code lives, note that the implementation **departed from #55's premise** (the stage list turned out to be closed, so the issue's "leave it null permanently" option was moot), and move the whole entry to `docs/TODO-done.md`. If no entry matches, there is nothing to close here -- this was tracked in the issue, not the backlog.

- [ ] **Step 2: Merge to main and push**

`main` is this project's dev trunk. With a green suite and a clean `main`, merge and push without asking.

```bash
git -C /Users/syntheticgio/Programming/local-bio-pipeliner checkout main && git -C /Users/syntheticgio/Programming/local-bio-pipeliner merge --no-ff claude/issue-55-spec-impl-a3ed15 && git -C /Users/syntheticgio/Programming/local-bio-pipeliner push origin main
```

If `main` moved under you, re-run the suite after merging rather than assuming the earlier green still holds.

- [ ] **Step 3: Update the issue**

```bash
gh issue edit 55 --remove-label "status: implementation plan" --add-label "status:ready"
```

Then comment with what shipped and close it, noting the premise correction so the next reader is not confused by the issue text:

```bash
gh issue close 55 --comment "Shipped. \`flye_stage_order(params)\` derives the stage list (7 stages, or 6 when \`--iterations 0\` drops polishing) and \`AssemblyProgress\` indexes into it on the raw stage name. An undeclared stage reports a null index, so the UI falls back to the phase name alone exactly as before.

Note for anyone reading this issue later: its premise was wrong. Flye's stage list is not open-ended -- \`_create_job_list\` builds the whole thing at launch -- so neither of the two options this issue proposed was the one taken."
```

---

## Notes for the implementer

**Why index on the raw stage name.** `_STAGE_LABELS` maps stage names to display strings and is not injective by construction -- before this change, `repeat` and `trestle` both rendered "resolving repeats". Indexing on the label would silently resolve both to the first match. The raw name is Flye's own identity for a stage and the same vocabulary `--resume-from` accepts.

**Why `stage_order` defaults to empty rather than being required.** Two existing test files construct `AssemblyProgress()` with no arguments to test stage-label parsing, which has nothing to do with phase structure. A required argument would force edits to tests that are not about this feature. The empty default is also the honest representation for "no declared order" -- which is what `snapshot()` keys off.

**What is deliberately not here.** No percentage for assembly: the duration argument against it is unchanged and is preserved in the class docstring. No frontend change: both call sites already render the counter when index and total are non-null.
