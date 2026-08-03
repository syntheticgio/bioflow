# Prior Runs on Pipeline Suggestion Cards — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show up to three prior runs of the same pipeline on each Actions-tab suggestion card, with each run's date, output file links, and status.

**Architecture:** A new `attach_prior_runs` function in `suggestion_service` runs after the cards are built, matching `PipelineRun` documents against each card's launch payload structurally — no new stored field, so runs already in the database appear immediately. It costs two extra queries total regardless of card count. `PipelineSuggestions.tsx` renders whatever the server decided; it does no filtering, sorting, or status derivation.

**Tech Stack:** Python 3.12 / FastAPI / Beanie (MongoDB) on the backend; React + TypeScript + TanStack Query on the frontend. Tests: pytest.

**Design doc:** `docs/superpowers/specs/2026-08-03-suggestion-card-prior-runs-design.md`

---

## Critical context for the engineer

Read this before Task 1. Four facts about this codebase decide whether the
matcher is correct, and three of them are counter-intuitive.

**1. Where an alignment's aligner and reference actually live.**

A card's launch body nests the aligner inside `params`:

```python
card["launch"]["body"] == {
    "object_id": "665...",
    "reference_id": "667...",
    "params": {"aligner": "bwa-mem2", "preset": None, ...},
}
```

A `PipelineRun` created by `launch_alignment` stores it at the top of its own
`params` (`backend/app/services/pipeline_service.py:1204-1208`):

```python
run.params == {"aligner": "bwa-mem2", "preset": None, ..., "read_group": {...}}
```

And the **reference is not in `run.params` at all** — it is an entry in
`run.inputs` with `role == RunInputRole.REFERENCE`. A matcher that compared
only `params` would show alignments against *different genomes* as prior runs
of the same card. This is the single most important correctness point in the
plan; Task 4 exists specifically to pin it.

**2. `params` holds more than parameters.** An alignment's `params` carries a
`read_group` dict built partly from the object's own name. Comparing `params`
wholesale with `==` would make almost nothing match. Only named fields are
compared.

**3. A trim run's tool is not in `params`.** `create_run` stores it in the
`PipelineRun.tool` field (`pipeline_service.py:318`), while the card carries
it at `body["tool"]`. So the match table's values are field names
*interpreted per kind*, not blind `params` keys.

**4. `suggestions_for` returns `list[dict]`, not `list[SuggestionCard]`.** It
calls `.as_dict()` on each card before returning
(`suggestion_service.py:1000`). `attach_prior_runs` therefore takes and
mutates dicts.

**Running the tests.** This work happens in a worktree, so the normal
`docker compose exec api python -m pytest` command is wrong — it would
silently test main's code. Use:

```bash
./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q
```

---

## File structure

| File | Responsibility | Change |
| --- | --- | --- |
| `backend/app/services/prior_runs.py` | Finding and shaping a card's prior runs. All the matching logic. | Create |
| `backend/app/services/suggestion_service.py` | Card assembly. Gains one call to `attach_prior_runs` and one dataclass field. | Modify |
| `backend/tests/services/test_prior_runs.py` | Matcher unit tests. | Create |
| `backend/tests/services/test_suggestion_service.py` | Gains the `stub_db` seam so existing tests keep passing. | Modify |
| `frontend/src/api/types.ts` | `PipelineSuggestion` gains `prior_runs`. | Modify |
| `frontend/src/components/PipelineSuggestions.tsx` | Renders the prior-runs block. | Modify |
| `frontend/src/styles.css` | Styles for the block. | Modify |

The matcher lives in its own module rather than inside `suggestion_service`
because that file is already 1003 lines, and the matching rules are a
self-contained unit with one entry point — exactly the shape that belongs
behind its own boundary.

---

## Task 1: The prior-runs module skeleton and its output shape

**Files:**
- Create: `backend/app/services/prior_runs.py`
- Create: `backend/tests/services/test_prior_runs.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_prior_runs.py`:

```python
"""Matching a suggestion card to the runs that already did its work.

Table-driven for the same reason the suggestion rules are: the value is in
pinning each branch, especially the two where the data does not live where
you would expect it (the reference is an input, not a param; a trim's tool is
a run field, not a param).
"""

from datetime import datetime
from types import SimpleNamespace

from app.models import RunInputRole, RunKind, RunStatus
from app.services.prior_runs import run_matches_card


def _run(kind=RunKind.ALIGNMENT, params=None, tool=None, inputs=()):
    """A stand-in for PipelineRun carrying only what the matcher reads."""
    return SimpleNamespace(
        id="run1",
        kind=kind,
        params=params or {},
        tool=tool,
        inputs=list(inputs),
        outputs=[],
        created_at=datetime(2026, 8, 1, 12, 0),
    )


def _input(object_id, role):
    return SimpleNamespace(object_id=object_id, name="x", role=role)


def _align_card(aligner="bwa-mem2", reference_id="ref1"):
    return {
        "kind": "align",
        "launch": {
            "endpoint": "/pipelines/align",
            "body": {
                "object_id": "obj1",
                "reference_id": reference_id,
                "params": {"aligner": aligner},
            },
        },
    }


class TestAlignmentMatching:
    def test_same_aligner_and_reference_matches(self):
        run = _run(
            params={"aligner": "bwa-mem2", "read_group": {"ID": "x"}},
            inputs=[_input("ref1", RunInputRole.REFERENCE)],
        )
        assert run_matches_card(run, _align_card()) is True

    def test_a_different_aligner_does_not_match(self):
        run = _run(
            params={"aligner": "minimap2"},
            inputs=[_input("ref1", RunInputRole.REFERENCE)],
        )
        assert run_matches_card(run, _align_card(aligner="bwa-mem2")) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_prior_runs.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.prior_runs'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/prior_runs.py`:

```python
"""Which runs already did what a suggestion card offers.

A card is a specific offer -- this aligner, against this reference, on this
file -- so a prior run is one whose recorded parameters match that offer, not
merely one of the same kind. That is what lets a run launched by hand through
the Computations dialog count as a prior run of the card: the match is on the
data, never on a flag saying a card created it.

The matching is structural and happens at query time. The alternative, a
signature hashed into the run at launch, was rejected because it would show
"no prior runs" on every card until the user re-launched everything -- and
would fail silently the first time a default moved in pipeline_service.
"""

from app.models import RunInputRole, RunKind

# The fields that distinguish two runs of the same kind. A kind absent from
# this table matches on kind alone, which is the deliberate default: an
# unlisted kind over-matches rather than showing nothing, and over-matching is
# visible on screen while under-matching is silent.
#
# Values are field names interpreted per kind, NOT blind `params` keys --
# `_run_value` below is where each name is resolved, because the same
# conceptual parameter lives in different places on the two sides. See the
# module's tests for the two that bite.
_MATCH_FIELDS: dict[RunKind, tuple[str, ...]] = {
    RunKind.ALIGNMENT: ("aligner", "reference_id"),
    RunKind.TRIM: ("tool",),
}

# Which RunKind a card's `kind` corresponds to. Cards whose kind is absent
# have no run kind that could match them and never show prior runs.
_CARD_RUN_KINDS: dict[str, RunKind] = {
    "align": RunKind.ALIGNMENT,
    "preprocess": RunKind.TRIM,
}


def _run_value(run, field: str):
    """Read a match field off a run, from wherever that kind actually keeps it.

    Three of these do not live in `params`, which is the whole reason this
    function exists rather than a `run.params.get(field)` at the call site.
    """
    if field == "reference_id":
        # Not a parameter at all: the reference is an input with a role. A
        # comparison that walked `params` would call two alignments against
        # different genomes the same run.
        for item in run.inputs:
            if item.role is RunInputRole.REFERENCE:
                return str(item.object_id)
        return None
    if field == "tool":
        # `create_run` stores a trim's tool in the run's own `tool` field.
        return run.tool
    return run.params.get(field)


def _card_value(card: dict, field: str):
    """Read a match field off a card's launch body."""
    body = (card.get("launch") or {}).get("body") or {}
    if field == "reference_id":
        return body.get("reference_id")
    if field == "tool":
        return body.get("tool")
    # Everything else nests under `params` on this side, even where the run
    # keeps it at the top of its own.
    return (body.get("params") or {}).get(field)


def run_matches_card(run, card: dict) -> bool:
    """True when this run did what this card offers.

    Only the named fields are compared. Comparing `params` wholesale would
    match almost nothing: an alignment's params carry a `read_group` dict
    built partly from the object's own name.
    """
    expected_kind = _CARD_RUN_KINDS.get(card.get("kind"))
    if expected_kind is None or run.kind is not expected_kind:
        return False

    for field in _MATCH_FIELDS.get(expected_kind, ()):
        if _run_value(run, field) != _card_value(card, field):
            return False
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_prior_runs.py -q`
Expected: PASS — 2 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/prior_runs.py backend/tests/services/test_prior_runs.py
git commit -m "feat: match a suggestion card to runs with the same parameters"
```

---

## Task 2: Pin the reference-is-an-input trap

The bug this feature is most likely to ship with. Task 1's implementation
already handles it; this task proves it and locks it against a future
"simplification" that moves everything to `params`.

**Files:**
- Modify: `backend/tests/services/test_prior_runs.py`

- [ ] **Step 1: Write the failing test**

Append to `class TestAlignmentMatching` in
`backend/tests/services/test_prior_runs.py`:

```python
    def test_a_different_reference_does_not_match(self):
        """The reference is an *input* with a role, never a param.

        A matcher that only walked `run.params` would pass this test's setup
        as a match -- same aligner, same kind -- and show the user an
        alignment against a completely different genome as a prior run of
        this card.
        """
        run = _run(
            params={"aligner": "bwa-mem2"},
            inputs=[_input("OTHER_GENOME", RunInputRole.REFERENCE)],
        )
        assert run_matches_card(run, _align_card(reference_id="ref1")) is False

    def test_a_run_with_no_reference_input_does_not_match(self):
        """Absent is not equal to whatever the card asked for."""
        run = _run(params={"aligner": "bwa-mem2"}, inputs=[])
        assert run_matches_card(run, _align_card(reference_id="ref1")) is False

    def test_read_group_differences_are_ignored(self):
        """`params` holds more than parameters.

        `read_group` is built partly from the object's own name, so a
        wholesale `params ==` comparison would make almost nothing match.
        """
        run = _run(
            params={"aligner": "bwa-mem2", "read_group": {"ID": "anything"}},
            inputs=[_input("ref1", RunInputRole.REFERENCE)],
        )
        assert run_matches_card(run, _align_card()) is True
```

Add a new class below it:

```python
class TestTrimMatching:
    def _trim_card(self, tool="fastp"):
        return {
            "kind": "preprocess",
            "launch": {
                "endpoint": "/pipelines/trim",
                "body": {"object_id": "obj1", "tool": tool, "params": {}},
            },
        }

    def test_same_tool_matches(self):
        run = _run(kind=RunKind.TRIM, tool="fastp")
        assert run_matches_card(run, self._trim_card()) is True

    def test_a_different_tool_does_not_match(self):
        """A trim's tool lives in the run's own `tool` field, not `params`."""
        run = _run(kind=RunKind.TRIM, tool="cutadapt")
        assert run_matches_card(run, self._trim_card(tool="fastp")) is False


class TestKindGating:
    def test_a_trim_run_never_matches_an_align_card(self):
        run = _run(kind=RunKind.TRIM, tool="fastp")
        assert run_matches_card(run, _align_card()) is False

    def test_a_card_with_no_corresponding_run_kind_never_matches(self):
        """Assemble, variants and the rest have no entry yet, and a card that
        cannot name a run kind must show nothing rather than everything."""
        run = _run(kind=RunKind.ALIGNMENT)
        assert run_matches_card(run, {"kind": "assemble", "launch": None}) is False
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_prior_runs.py -q`
Expected: PASS — 9 passed. (These pass against Task 1's implementation; they
are regression locks, not new behaviour. If any fails, Task 1's `_run_value`
is wrong — fix it there rather than weakening the test.)

- [ ] **Step 3: Commit**

```bash
git add backend/tests/services/test_prior_runs.py
git commit -m "test: pin that a card's reference is matched from run inputs"
```

---

## Task 3: Shape one run into a display row

**Files:**
- Modify: `backend/app/services/prior_runs.py`
- Modify: `backend/tests/services/test_prior_runs.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_prior_runs.py`:

```python
from app.services.prior_runs import row_for_run


class TestRowShape:
    def test_a_succeeded_run_carries_its_outputs(self):
        run = _run()
        run.outputs = ["out1", "out2"]
        names = {"out1": "sample_R1.fastq.gz", "out2": "sample_R2.fastq.gz"}
        row = row_for_run(run, RunStatus.SUCCEEDED, names)
        assert row["status"] == "succeeded"
        assert row["run_id"] == "run1"
        assert [o["name"] for o in row["outputs"]] == [
            "sample_R1.fastq.gz",
            "sample_R2.fastq.gz",
        ]
        assert all(o["exists"] for o in row["outputs"])

    def test_a_failed_run_has_no_outputs(self):
        """The row that motivates the feature: no file to link, so the status
        word carries it. Hiding these invites the same failed launch again."""
        run = _run()
        run.outputs = []
        row = row_for_run(run, RunStatus.FAILED, {})
        assert row["status"] == "failed"
        assert row["outputs"] == []

    def test_a_deleted_output_is_marked_rather_than_dropped(self):
        """The run still happened; only the file is gone. A dropped row would
        make a real run look like it produced nothing."""
        run = _run()
        run.outputs = ["gone"]
        row = row_for_run(run, RunStatus.SUCCEEDED, {})
        assert row["outputs"] == [
            {"object_id": "gone", "name": "(deleted)", "exists": False}
        ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_prior_runs.py -q`
Expected: FAIL — `ImportError: cannot import name 'row_for_run'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/prior_runs.py`:

```python
def row_for_run(run, status, names: dict) -> dict:
    """One run as the frontend renders it.

    `names` maps output object id to its current name; an id missing from it
    has been deleted. The row keeps the entry either way -- the run still
    happened, and dropping it would make a real run look like it produced
    nothing.

    No file size: it was considered and cut. An output whose size changed
    unexpectedly is a real signal, but a weak one beside knowing the run
    failed, and two numbers on a row whose job is to say "this already
    happened" is one too many.
    """
    outputs = []
    for object_id in run.outputs:
        key = str(object_id)
        outputs.append(
            {
                "object_id": key,
                "name": names.get(key, "(deleted)"),
                "exists": key in names,
            }
        )

    return {
        "run_id": str(run.id),
        "finished_at": run.created_at,
        "status": status.value,
        "outputs": outputs,
    }
```

Add `RunStatus` to the module's imports:

```python
from app.models import RunInputRole, RunKind, RunStatus
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_prior_runs.py -q`
Expected: PASS — 12 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/prior_runs.py backend/tests/services/test_prior_runs.py
git commit -m "feat: shape a matched run into a card display row"
```

---

## Task 4: Gather prior runs for a whole card list

The query layer: two extra queries total, regardless of how many cards a file
has.

**Files:**
- Modify: `backend/app/services/prior_runs.py`
- Modify: `backend/tests/services/test_prior_runs.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_prior_runs.py`:

Async tests need no marker: `pyproject.toml` sets `asyncio_mode = "auto"`,
which is why no existing test in this suite carries one.

```python
from unittest.mock import patch

from app.services.prior_runs import attach_prior_runs


@contextmanager
def stub_runs(runs=(), statuses=None, names=None):
    """Cut the three database seams `attach_prior_runs` reaches through."""
    with (
        patch("app.services.prior_runs._runs_touching",
              return_value=list(runs)),
        patch("app.services.prior_runs.run_service.status_for_many",
              return_value=statuses or {}),
        patch("app.services.prior_runs._output_names",
              return_value=names or {}),
    ):
        yield


class TestAttachPriorRuns:
    async def test_a_matching_run_lands_on_its_card(self):
        run = _run(
            params={"aligner": "bwa-mem2"},
            inputs=[_input("ref1", RunInputRole.REFERENCE)],
        )
        run.outputs = ["out1"]
        cards = [_align_card()]
        with stub_runs(
            runs=[run],
            statuses={"run1": RunStatus.SUCCEEDED},
            names={"out1": "sample.bam"},
        ):
            await attach_prior_runs(cards, _fake_obj(), owner="local")
        assert len(cards[0]["prior_runs"]) == 1
        assert cards[0]["prior_runs"][0]["outputs"][0]["name"] == "sample.bam"

    async def test_a_card_with_no_matching_run_gets_an_empty_list(self):
        cards = [_align_card()]
        with stub_runs():
            await attach_prior_runs(cards, _fake_obj(), owner="local")
        assert cards[0]["prior_runs"] == []

    async def test_running_and_waiting_runs_are_omitted(self):
        """The card records what has happened; Activity owns work in flight."""
        for status in (RunStatus.RUNNING, RunStatus.WAITING):
            run = _run(
                params={"aligner": "bwa-mem2"},
                inputs=[_input("ref1", RunInputRole.REFERENCE)],
            )
            cards = [_align_card()]
            with stub_runs(runs=[run], statuses={"run1": status}):
                await attach_prior_runs(cards, _fake_obj(), owner="local")
            assert cards[0]["prior_runs"] == []

    async def test_a_failed_run_is_kept(self):
        run = _run(
            params={"aligner": "bwa-mem2"},
            inputs=[_input("ref1", RunInputRole.REFERENCE)],
        )
        cards = [_align_card()]
        with stub_runs(runs=[run], statuses={"run1": RunStatus.FAILED}):
            await attach_prior_runs(cards, _fake_obj(), owner="local")
        assert [r["status"] for r in cards[0]["prior_runs"]] == ["failed"]

    async def test_at_most_three_runs_newest_first(self):
        runs = []
        for n in range(5):
            run = _run(
                params={"aligner": "bwa-mem2"},
                inputs=[_input("ref1", RunInputRole.REFERENCE)],
            )
            run.id = f"run{n}"
            run.created_at = datetime(2026, 8, 1, 12, n)
            runs.append(run)
        cards = [_align_card()]
        with stub_runs(
            runs=runs,
            statuses={f"run{n}": RunStatus.SUCCEEDED for n in range(5)},
        ):
            await attach_prior_runs(cards, _fake_obj(), owner="local")
        assert [r["run_id"] for r in cards[0]["prior_runs"]] == [
            "run4", "run3", "run2",
        ]
```

Add these imports at the top of the test file, beside the existing ones:

```python
from contextlib import contextmanager
```

And a local `_fake_obj` helper, below `_align_card`:

```python
def _fake_obj(obj_id="obj1", project_id="proj1"):
    return SimpleNamespace(id=obj_id, project_id=project_id, owner="local")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_prior_runs.py -q`
Expected: FAIL — `ImportError: cannot import name 'attach_prior_runs'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/prior_runs.py`:

```python
# Runs still in flight. The card is a record of what has happened, and the
# Activity view already owns work in progress -- a card claiming a prior run
# that has not produced anything yet would link to a file that is not there.
_IN_FLIGHT = frozenset({RunStatus.WAITING, RunStatus.RUNNING})

# Three, per the design. Enough to see a pattern, few enough that the card
# stays a card.
_MAX_ROWS = 3


async def _runs_touching(obj) -> list:
    """Every run in this project that took this object as an input."""
    return await PipelineRun.find(
        {
            "owner": obj.owner,
            "project_id": obj.project_id,
            "inputs.object_id": obj.id,
        }
    ).to_list()


async def _output_names(object_ids: list, *, owner: str) -> dict:
    """Current name for each output id. A missing id has been deleted."""
    if not object_ids:
        return {}
    objects = await DataObject.find(
        {"owner": owner, "_id": {"$in": object_ids}}
    ).to_list()
    return {str(o.id): o.name for o in objects}


async def attach_prior_runs(cards: list[dict], obj, *, owner: str) -> None:
    """Give every card the runs that already did its work.

    Mutates the cards in place -- `suggestions_for` has already converted them
    to dicts by the time this runs, and returning a parallel list the caller
    had to zip back up would be a second thing to keep in order.

    Two queries plus one status derivation for the whole list, not per card:
    the cost of this feature must not scale with how many cards a file has.
    """
    for card in cards:
        card["prior_runs"] = []

    runs = await _runs_touching(obj)
    if not runs:
        return

    # `status_for_many` is already owner-scoped and already two queries rather
    # than 2N -- the reason this does not derive status itself.
    statuses = await run_service.status_for_many(
        [run.id for run in runs], owner=owner
    )

    finished = [
        run
        for run in runs
        if statuses.get(run.id) is not None
        and statuses[run.id] not in _IN_FLIGHT
    ]
    if not finished:
        return

    names = await _output_names(
        [oid for run in finished for oid in run.outputs], owner=owner
    )

    finished.sort(key=lambda r: r.created_at, reverse=True)

    for card in cards:
        card["prior_runs"] = [
            row_for_run(run, statuses[run.id], names)
            for run in finished
            if run_matches_card(run, card)
        ][:_MAX_ROWS]
```

Add the imports this needs to the top of the module. All five model names are
re-exported from `app.models`, which is how the rest of this codebase imports
them — replace the `from app.models.run import ...` line added in Task 3 with
the single line below rather than keeping both:

```python
from app.models import (
    DataObject,
    PipelineRun,
    RunInputRole,
    RunKind,
    RunStatus,
)
from app.services import run_service
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_prior_runs.py -q`
Expected: PASS — 17 passed

If `test_a_matching_run_lands_on_its_card` fails on the `statuses` lookup,
check that the stub's keys match the runs' `id` values (`"run1"` in both).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/prior_runs.py backend/tests/services/test_prior_runs.py
git commit -m "feat: gather prior runs for a card list in two queries"
```

---

## Task 5: Wire it into the suggestions endpoint

**Files:**
- Modify: `backend/app/services/suggestion_service.py:52-86` (the dataclass), `:1000` (the return)
- Modify: `backend/tests/services/test_suggestion_service.py:903-933` (`stub_db`)

- [ ] **Step 1: Write the failing test**

Append to `class TestSuggestionsFor` in
`backend/tests/services/test_suggestion_service.py`:

```python
    async def test_every_card_carries_a_prior_runs_list(self):
        """Absent would make the frontend guard a field that is always sent;
        empty is the honest answer for a file nothing has been run on."""
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)), stub_db():
            cards = await suggestions_for(_fake_obj())
        assert cards
        assert all(c["prior_runs"] == [] for c in cards)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q`
Expected: FAIL — `KeyError: 'prior_runs'`

- [ ] **Step 3: Write the implementation**

In `backend/app/services/suggestion_service.py`, add the field to
`SuggestionCard` (after `launch`, around line 74):

```python
    # Runs that already did what this card offers. Filled by
    # `attach_prior_runs` after the builders run, never by a builder -- it is
    # a database question, and the builders are deliberately synchronous and
    # pure.
    prior_runs: list = field(default_factory=list)
```

Add `field` to the dataclasses import at the top:

```python
from dataclasses import dataclass, field
```

Add it to `as_dict` (after the `launch` entry):

```python
            "prior_runs": self.prior_runs,
```

Then in `suggestions_for`, replace the final `return cards` (line ~1000)
with:

```python
    from app.services.prior_runs import attach_prior_runs

    # After the builders, not inside them: this is the one part of a card that
    # is a database question rather than a rule about the file.
    await attach_prior_runs(cards, obj, owner=obj.owner)
    return cards
```

The import is function-local to match how `pipelines.py` imports
`suggestion_service` — `prior_runs` imports `run_service`, and a module-level
import here would add a cycle risk for no benefit.

Now add the seam to `stub_db` in the test file so the existing tests do not
hit the database. In `backend/tests/services/test_suggestion_service.py`,
inside the `with (...)` block of `stub_db`, add a fourth patch:

```python
        patch("app.services.prior_runs._runs_touching", return_value=[]),
```

and extend the docstring's first line to say "four database seams" rather
than "three".

- [ ] **Step 4: Run the whole file to verify**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q`
Expected: PASS — every test in the file, including the new one.

- [ ] **Step 5: Run the full backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS. Read the count, not just the exit code. Anything that
asserts on a card's exact dict shape elsewhere will surface here.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat: attach prior runs to every pipeline suggestion card"
```

---

## Task 6: Check the matcher against the real database

Per CLAUDE.md: the last two suggestion-rule bugs were both things hand-built
fixtures could not expose, because the fixtures already looked the way the
rules expected. This task is not optional and has no test file — it is a
one-off check against real objects.

**Files:** none modified.

- [ ] **Step 1: Bring the worktree stack up**

```bash
./ops/worktree-up.sh
```

- [ ] **Step 2: Find a real object with alignment runs and check its cards**

`worktree-up.sh` names its stack `biopipe-wt-<branch-slug>`; it prints the
name on startup. Substitute it for `<PROJECT>` below, or read it from
`docker compose ls`.

```bash
docker compose -p <PROJECT> exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models import DataObject, PipelineRun
from app.services.suggestion_service import suggestions_for

async def main():
    await connect_to_mongo()
    run = await PipelineRun.find({'kind': 'alignment'}).sort('-created_at').first_or_none()
    if run is None:
        print('no alignment runs in this database; align something first')
        return
    reads = next(i for i in run.inputs if i.role == 'reads')
    obj = await DataObject.get(reads.object_id)
    print('object:', obj.name)
    for card in await suggestions_for(obj):
        print(card['kind'], '->', [
            (r['status'], [o['name'] for o in r['outputs']])
            for r in card['prior_runs']
        ])

asyncio.run(main())
"
```

- [ ] **Step 3: Confirm what you see**

Check three things by eye against the project in the UI:

1. The align card's prior runs are alignments **of this file against this
   card's reference** — not against some other genome in the project.
2. The output names listed are the files that alignment actually produced.
3. No `.bai` or other sidecar appears as an output row.

If item 1 is wrong, `_run_value`'s `reference_id` branch is not reading
`inputs` — go back to Task 1. If item 3 is wrong, sidecars are landing in
`run.outputs`, which is a `record_outputs` question rather than a matcher one;
note it and filter sidecars in `row_for_run` by checking
`DataObject.sidecar_of`.

- [ ] **Step 4: Commit nothing, or commit a fix**

No code change expected. If the check exposed one, commit it with a message
naming what the real data showed.

---

## Task 7: Frontend type and rendering

**Files:**
- Modify: `frontend/src/api/types.ts:1284-1293`
- Modify: `frontend/src/components/PipelineSuggestions.tsx`

- [ ] **Step 1: Add the types**

In `frontend/src/api/types.ts`, above `PipelineSuggestion`:

```typescript
/** One output file of a prior run. `exists` is false once the file has been
 *  deleted -- the run still happened, so the row keeps its recorded name and
 *  renders as plain text rather than a dead link. */
export interface PriorRunOutput {
  object_id: string;
  name: string;
  exists: boolean;
}

/** A run that already did what a card offers. Failed runs are included and
 *  carry no outputs: a card that hid its failures would invite the same
 *  failed launch again. */
export interface PriorRun {
  run_id: string;
  finished_at: string;
  status: "succeeded" | "partial" | "failed";
  outputs: PriorRunOutput[];
}
```

And add the field to `PipelineSuggestion`, after `launch`:

```typescript
  prior_runs: PriorRun[];
```

- [ ] **Step 2: Render the block**

In `frontend/src/components/PipelineSuggestions.tsx`, add the import:

```typescript
import { Link } from "react-router-dom";
import type { PipelineSuggestion, PriorRun } from "../api/types";
```

Add this component above `PipelineSuggestions`:

```typescript
/** What this card has already produced.
 *
 * Failed runs are listed deliberately. They have no output to link, so the
 * status word carries the row -- and a user who cannot see that the last two
 * launches failed is a user about to launch a third time.
 */
function PriorRuns({
  runs,
  projectId,
}: {
  runs: PriorRun[];
  projectId: string;
}) {
  return (
    <div className="prior-runs">
      {runs.map((run) => (
        <div key={run.run_id} className="prior-run">
          <span className="prior-run-date">
            {new Date(run.finished_at).toLocaleDateString(undefined, {
              month: "short",
              day: "numeric",
            })}
          </span>
          <span className="prior-run-outputs">
            {run.outputs.map((out) =>
              out.exists ? (
                <Link
                  key={out.object_id}
                  to={`/p/${projectId}?sel=object:${out.object_id}`}
                  className="prior-run-link"
                >
                  {out.name}
                </Link>
              ) : (
                <span key={out.object_id} className="prior-run-gone">
                  {out.name}
                </span>
              ),
            )}
          </span>
          <span className={`prior-run-status is-${run.status}`}>
            {run.status}
          </span>
        </div>
      ))}
    </div>
  );
}
```

Change the component signature to take the project id, since the link needs
it:

```typescript
export function PipelineSuggestions({
  objectId,
  projectId,
}: {
  objectId: string;
  projectId: string;
}) {
```

And inside the card's JSX, between the `suggestion-why` div and the button:

```tsx
            {card.prior_runs.length > 0 && (
              <PriorRuns runs={card.prior_runs} projectId={projectId} />
            )}
            <button
              type="button"
              className={card === firstAvailable ? "btn primary" : "btn"}
              onClick={() => launch.mutate(card)}
              disabled={!available || launch.isPending}
            >
              {card.prior_runs.length > 0 ? "Launch again" : "Launch"}
            </button>
```

Add the count marker beside the category, replacing the existing
`suggestion-category` div:

```tsx
            <div className="suggestion-card-top">
              <div className="suggestion-category">{card.category}</div>
              {card.prior_runs.length > 0 && (
                <div className="prior-runs-count">
                  {card.prior_runs.length} prior run
                  {card.prior_runs.length === 1 ? "" : "s"}
                </div>
              )}
            </div>
```

- [ ] **Step 3: Pass the project id from the caller**

In `frontend/src/components/DetailPanel.tsx:1187`, change:

```tsx
          <PipelineSuggestions objectId={obj.id} />
```

to:

```tsx
          <PipelineSuggestions objectId={obj.id} projectId={obj.project_id} />
```

- [ ] **Step 4: Verify it compiles**

Run: `docker compose -p <PROJECT> exec web npx tsc --noEmit`

where `<PROJECT>` is the `biopipe-wt-<branch-slug>` name `worktree-up.sh`
prints. Expected: no output (clean). `DataObject.project_id` is confirmed
present in `types.ts:89`, so `obj.project_id` in `DetailPanel` resolves.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/components/PipelineSuggestions.tsx frontend/src/components/DetailPanel.tsx
git commit -m "feat: render prior runs on suggestion cards"
```

---

## Task 8: Styles

**Files:**
- Modify: `frontend/src/styles.css:3289-3335` (beside the other `.suggestion-*` rules)

- [ ] **Step 1: Add the rules**

Insert after `.suggestion-why` (line ~3329):

```css
/* The card's top row: category on the left, prior-run count on the right,
   which is where the mockup puts it and where it stays out of the title's
   way. */
.suggestion-card-top {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
}

.prior-runs-count {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-faint);
}

/* Hairlines above and below rather than a filled panel: this is a list
   inside the card, not a second card. margin-top:auto is dropped here --
   .suggestion-why above already owns pushing the block down. */
.prior-runs {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 0;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}

.prior-run {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 11px;
}

.prior-run-date {
  color: var(--text-faint);
  white-space: nowrap;
}

/* Takes the slack so the status stays hard right, and wraps rather than
   overflowing when a paired trim lists two long filenames. */
.prior-run-outputs {
  flex: 1;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
}

.prior-run-link {
  color: var(--accent);
  text-decoration: none;
  overflow-wrap: anywhere;
}

.prior-run-link:hover {
  text-decoration: underline;
}

.prior-run-gone {
  color: var(--text-faint);
  text-decoration: line-through;
}

.prior-run-status {
  white-space: nowrap;
  color: var(--text-dim);
}

/* Only failure earns colour. A green "succeeded" on every row would spend
   the card's one loud signal on its least surprising outcome. */
.prior-run-status.is-failed {
  color: var(--danger);
}
```

- [ ] **Step 2: Check the variable names exist**

Run: `grep -n "\-\-danger\|--text-faint\|--accent" frontend/src/styles.css | head -5`
Expected: each name appears in a `:root` block. If `--danger` is absent, find
the theme's error colour with `grep -n "^  --.*error\|^  --.*red" frontend/src/styles.css`
and use that name instead.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/styles.css
git commit -m "style: prior-runs list on suggestion cards"
```

---

## Task 9: Manual verification in the browser

There is no headless component-testing setup in this repo and none is
expected — this is the verification step for everything in Tasks 7 and 8.

**Files:** none modified.

- [ ] **Step 1: Rebuild the worktree stack**

```bash
./ops/worktree-up.sh
```

- [ ] **Step 2: Check all four states at localhost:5273**

Open a project, select a file, open the **Actions** tab.

1. **No prior runs** — a freshly uploaded FASTQ. The cards must look exactly
   as they do on localhost:5173 today: no count marker, no list, button reads
   "Launch". Compare the two side by side; this is the state most likely to
   regress silently.
2. **Succeeded** — a FASTQ you have aligned. The align card shows the date,
   the BAM as a link, and "succeeded". No `.bai` row.
3. **Failed** — align a file against a broken or missing reference to
   produce one, or find an existing failed run. The row shows a date and
   "failed" in the danger colour, with no link.
4. **Paired trim** — a paired FASTQ you have trimmed. One row, two links
   (R1 and R2), one status.

- [ ] **Step 3: Check the link works**

Click an output link. It must open that file in the explorer's detail panel —
the same behaviour as clicking a file in the Activity view.

- [ ] **Step 4: Check the count matches the rows**

The marker says "2 prior runs" and there are exactly two rows. If a paired
trim shows one row and the marker says two, the count is being taken from
outputs rather than runs.

- [ ] **Step 5: Take down the worktree stack**

```bash
./ops/worktree-up.sh --down
```

---

## Task 10: Merge and close out

- [ ] **Step 1: Run the full suite one more time**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS. Read the count.

- [ ] **Step 2: Merge to main**

```bash
git checkout main && git pull && git merge --no-ff -
```

If `main` moved, re-run the suite after merging rather than assuming the
earlier green still holds.

- [ ] **Step 3: Push**

```bash
git push origin main
```

- [ ] **Step 4: Confirm the 5173 stack is still on the main checkout**

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

The source path must be the main checkout, not a path under
`.claude/worktrees/`. `worktree-up.sh` avoids repointing it by construction,
so this is a confirmation rather than an expected fix. If it is wrong, run
`docker compose up -d --build api web worker` from the main checkout root.

---

## Out of scope

- Re-running with the same outputs, or any caching. "Launch again" launches.
- Prior runs on the Computations dialogs. This is a suggestion-card feature.
- Any change to what a run records at launch time.
- Match-table entries for kinds beyond alignment and trim. Those cards show
  no prior runs until someone adds an entry, which is the visible-rather-than-
  wrong default.
