# Fact-Grounded Provenance Narrative Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Given any data object, produce a deterministic, gap-honest methods report of everything that made it, with an optional model-rendered prose version that can never introduce a fact the report does not already show.

**Architecture:** A walker (`provenance_walker.py`) traverses `derived_from` upward and returns a frozen `ProvenanceChain`. Two pure functions over that chain -- a markdown renderer (`provenance_report.py`) and a prompt builder (`provenance_prompt.py`) -- have no database access, which is what makes them testable without Mongo. Two routes: one returns the structured report with no model involvement, one performs the model call behind a containment check that discards prose introducing unsupported versions or tool names.

**Tech Stack:** Python 3.12 / FastAPI / Beanie (Motor) / pytest on the backend; React + TanStack Query on the frontend. AI calls go through `app/services/ai/` (`resolve` + `complete`), never to an HTTP endpoint directly.

**Spec:** [`docs/superpowers/specs/2026-08-06-provenance-narrative-design.md`](../specs/2026-08-06-provenance-narrative-design.md)

---

## Critical context before you start

Four things about this repo will cost you hours if you learn them by discovery.

**1. Run tests with the worktree script, not `docker compose exec`.**
From a worktree, `docker compose exec api python -m pytest` silently tests
*main's* code -- the `api` container bind-mounts the main checkout. Use:

```bash
./backend/run-worktree-tests.sh tests/services/test_provenance_walker.py -q
```

It mounts this worktree's source and gives the run its own throwaway Mongo
replica set. That private Mongo matters: `conftest.py` drops every collection
in `biopipe_test` at session start, so sharing Mongo with the running stack
makes two test runs wipe each other mid-test. That reads as flaky tests when
it is actually two runs fighting over one database.

**2. `ai.complete()` never raises and never returns `None`.**
It returns `Completion | ToolCall | Failure`. Checking `if result is None`
type-checks, reads as correct, and treats every failure as a success. Always
`isinstance(result, Completion)`.

**3. `timing_service.records_for_object()` includes failed runs on purpose.**
Do not "fix" this by switching to a filtered accessor. Provenance is the one
reader that wants failures -- a step that failed and was retried is exactly
what a methods reader needs. The filtered accessor (`_modelled()`) exists for
the predictive models, which must never see failures.

**4. Sidecars are excluded during the walk, not filtered at render.**
`.bai`/`.tbi`/`.fai` objects have `sidecar_of` set. They must never reach the
prompt, so they are skipped at traversal.

---

## File structure

| File | Responsibility | Touches DB? |
|---|---|---|
| `backend/app/services/provenance_walker.py` (create) | Types (`Gap`, `Step`, `Node`, `ProvenanceChain`), the verb table, the traversal | Yes -- the only one |
| `backend/app/services/provenance_report.py` (create) | `ProvenanceChain` -> markdown | No |
| `backend/app/services/provenance_prompt.py` (create) | `ProvenanceChain` -> (system, user); containment check | No |
| `backend/app/models/ai.py` (modify) | Add `TaskSlot.PROVENANCE_NARRATIVE` + label | -- |
| `backend/app/api/v1/schemas.py` (modify) | `ProvenanceNarrativeOut`, `ProvenanceProseOut` | -- |
| `backend/app/api/v1/objects.py` (modify) | The two routes | -- |
| `frontend/src/api/client.ts` (modify) | Two client functions | -- |
| `frontend/src/components/ProvenanceNarrative.tsx` (create) | The panel section | -- |
| `frontend/src/components/DetailPanel.tsx` (modify) | Mount it beside `Computations` | -- |

Tests mirror the source paths under `backend/tests/`.

---

## Task 1: Walker types and the verb table

The types come first because every later task is typed against them. No
traversal yet -- this task ends with a verb table that provably covers the
registry.

**Files:**
- Create: `backend/app/services/provenance_walker.py`
- Create: `backend/tests/services/test_provenance_verbs.py`

- [ ] **Step 1: Write the failing exhaustiveness test**

This is the most important test in the plan. It is the structural guard
against the failure CLAUDE.md documents: a hand-maintained registry keyed by
something enumerable, where a missing entry is silently skipped rather than
raised. Adding STAR cost a `build_index` job that reported success while
storing none of its eight index files, because `_SIDECAR_ROLES` had no entry
and the code skipped rather than failed.

Create `backend/tests/services/test_provenance_verbs.py`:

```python
"""Every registered handler must be classified as narrating or not.

The failure this prevents: someone adds a pipeline handler, forgets the verb
table, and provenance reports silently omit that step forever. Nothing else
would catch it -- the walker would just render a chain with a hole in it, and
every fixture in this suite would keep passing.
"""

from app.queue import registry
from app.services.provenance_walker import _NO_NARRATIVE_STEP, _STEP_VERBS


def test_every_registered_handler_is_classified():
    registry.load_handlers()
    names = set(registry.all_handlers())

    # Guards against a vacuous pass: if handler modules were never imported,
    # the registry is empty and `set() == set() | set()` would be True.
    assert names, "registry empty -- handler modules were not imported"

    assert names == set(_STEP_VERBS) | _NO_NARRATIVE_STEP


def test_no_handler_is_both_narrating_and_not():
    assert not (set(_STEP_VERBS) & _NO_NARRATIVE_STEP)
```

- [ ] **Step 2: Run it to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_provenance_verbs.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.provenance_walker'`

- [ ] **Step 3: Create the module with types and verb table**

Create `backend/app/services/provenance_walker.py`:

```python
"""Walking a data object's ancestry into a fact set a methods section can use.

The output of this module is consumed by two renderers that never touch the
database (`provenance_report.py`, `provenance_prompt.py`). Keeping all I/O
here is what lets those two be tested as pure functions over hand-built
chains -- which matters because they are where the anti-fabrication rules
live, and those need exhaustive cheap tests.

Steps come from `produced_by_job`, not from the `*_provenance` fact keys in
`queue/results.py`. Several appliers set `produced_by_job` while passing
`facts=` with no named provenance builder at all, so a facts-first walk would
drop those steps silently. Anchoring on the job means the worst case is a
step rendered with an explicit "parameters not recorded" gap.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

from beanie import PydanticObjectId

from app.models.object import ObjectRole

# How deep an ancestry may be before we assume the data is cyclic. Real
# chains are 3-6 deep; this is a guard against hand-edited documents, not an
# expected limit.
MAX_DEPTH = 64


class GapKind(StrEnum):
    """Why a fact a methods section wants is not available.

    Five of these are states a real chain reaches, and they are kept distinct
    because they have different remedies: a missing version is a probe bug, a
    missing parameter set is a historical artifact from before that step had
    a provenance builder, and a share boundary is permanent and expected.
    Collapsing them into one "unknown" would tell the user nothing about
    whether they can fix it.
    """

    VERSION_UNRECORDED = "version_unrecorded"
    PARAMS_UNRECORDED = "params_unrecorded"
    SHARE_BOUNDARY = "share_boundary"
    DANGLING_PARENT = "dangling_parent"
    DEPTH_EXCEEDED = "depth_exceeded"


@dataclass(frozen=True)
class Gap:
    kind: GapKind
    # Which object the gap is attached to, so the renderer can place it in
    # the position the fact would have occupied.
    object_id: PydanticObjectId | None = None
    detail: str | None = None


@dataclass(frozen=True)
class Step:
    """One job that produced an object."""

    job_type: str
    verb: str
    tool: str | None = None
    tool_version: str | None = None
    params: dict = field(default_factory=dict)
    ran_at: datetime | None = None
    outcome: str | None = None
    gaps: tuple[Gap, ...] = ()


@dataclass(frozen=True)
class Node:
    object_id: PydanticObjectId
    name: str
    role: ObjectRole | None
    kind: Literal["spine", "supporting"]
    produced_by: Step | None
    parents: tuple[PydanticObjectId, ...] = ()


@dataclass(frozen=True)
class ProvenanceChain:
    target: Node
    nodes: dict[PydanticObjectId, Node]
    order: tuple[PydanticObjectId, ...]
    gaps: tuple[Gap, ...]
    branches: tuple[tuple[PydanticObjectId, ...], ...] = ()

    @property
    def gap_count(self) -> int:
        return len(self.gaps)


# Handler name -> how a methods section says it happened.
#
# Verified against `registry.all_handlers()` rather than recalled. The
# companion frozenset below covers every registered handler this dict
# deliberately omits; `test_provenance_verbs.py` asserts the two partition
# the registry exactly.
#
# These strings are prose, and nothing can mechanically check that
# "quantified with" is the right phrase for `quantify` -- the same staleness
# risk `ToolMeta.usage` carries. What the test does guarantee is that a
# wrong-but-present verb is the worst case, never a silently absent step.
_STEP_VERBS: dict[str, str] = {
    "trim_reads": "trimmed with",
    "align_reads": "aligned with",
    "call_variants": "variant-called with",
    "annotate_variants": "annotated with",
    "assemble_reads": "assembled with",
    "assemble_upload": "assembled with",
    "polish_assembly": "polished with",
    "scaffold_assembly": "scaffolded with",
    "quantify": "quantified with",
    "differential_expression": "tested for differential expression with",
    "consensus_from_alignment": "called a consensus with",
    "run_qc": "quality-checked with",
    "assess_completeness": "assessed for completeness with",
    "assess_misassemblies": "assessed for misassemblies with",
    "download_sra_run": "downloaded from the SRA",
    "download_assembly": "downloaded from NCBI",
    "download_uniprot": "downloaded from UniProt",
    "download_lineage": "downloaded",
}

# Registered handlers that legitimately produce no narrative step.
_NO_NARRATIVE_STEP: frozenset[str] = frozenset(
    {
        # Sidecar-only outputs, excluded at traversal anyway.
        "build_index",
        "index_bam",
        # Statistics written back onto an existing object rather than
        # producing one; the numbers already show in the file panel.
        "run_bam_stats",
        "run_vcf_stats",
        "ingest_headers",
        # Bookkeeping on bytes already ingested.
        "hash_blob",
        "register_hash",
        "verify_blob",
        "verify_files",
        # Infrastructure; touches no project data.
        "install_tool",
        "uninstall_tool",
        # Housekeeping reapers and GC.
        "gc_blobs",
        "reap_pipeline_scratch",
        "reap_report_dirs",
        "reap_uploads",
        # AI features that write a field rather than producing an object.
        "summarize_object",
        "answer_project_question",
        # Test-only.
        "noop",
        "sleep_test",
    }
)

# Fact-key convention: `<verb>_by` names the tool that did a step. This is an
# open vocabulary on purpose -- a future `polished_by` renders generically
# rather than needing a table entry. Forcing every key to have a hand-written
# phrase would turn "we have no phrase for this" into a wrong guess, which is
# worse than a clumsy sentence.
GENERIC_VERB = "processed with"
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/test_provenance_verbs.py -q
```

Expected: 2 passed. If it fails listing handler names, the registry has
changed since this plan was written -- add each missing name to whichever set
is correct and note it in the commit message.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/provenance_walker.py backend/tests/services/test_provenance_verbs.py
git commit -m "Add provenance walker types and registry-checked verb table"
```

---

## Task 2: Fact extraction from an object's facts dict

Pure functions, no traversal yet. Extracting the tool and version from a
`facts` dict is where the `*_by` convention gets read.

**Files:**
- Modify: `backend/app/services/provenance_walker.py`
- Create: `backend/tests/services/test_provenance_facts.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/services/test_provenance_facts.py`:

```python
"""Reading tool and version out of the open `facts` vocabulary.

Table-driven against the real key names the `*_provenance` builders in
`queue/results.py` write, not invented ones -- the point is that this keeps
working when a builder is added.
"""

import pytest

from app.services.provenance_walker import extract_tool_facts


@pytest.mark.parametrize(
    "facts,expected_tool,expected_version",
    [
        # align_provenance
        (
            {"aligned_by": "bwa-mem2", "aligner_version": "2.2.1"},
            "bwa-mem2",
            "2.2.1",
        ),
        # trim provenance
        (
            {"trimmed_by": "fastp", "trim_tool_version": "0.23.4"},
            "fastp",
            "0.23.4",
        ),
        # variant_provenance
        (
            {"variants_called_by": "clair3", "variant_caller_version": "1.0.4"},
            "clair3",
            "1.0.4",
        ),
        # assembly_provenance
        (
            {"assembled_by": "flye", "assembler_version": "2.9.3"},
            "flye",
            "2.9.3",
        ),
        # counts_provenance
        (
            {"counted_by": "featurecounts", "featurecounts_version": "2.0.6"},
            "featurecounts",
            "2.0.6",
        ),
        # A future builder nobody updated this module for: the tool is still
        # found by convention, and the version is simply absent.
        ({"polished_by": "racon"}, "racon", None),
        # Tool recorded, version missing -- the common probe-failure case.
        ({"aligned_by": "bwa-mem2"}, "bwa-mem2", None),
        # Nothing at all.
        ({}, None, None),
    ],
)
def test_extract_tool_facts(facts, expected_tool, expected_version):
    tool, version = extract_tool_facts(facts)
    assert tool == expected_tool
    assert version == expected_version


def test_ignores_non_string_by_values():
    """`sra_fields_applied` is a list, not a tool name."""
    tool, _ = extract_tool_facts({"sra_fields_applied": ["a", "b"]})
    assert tool is None


def test_params_are_found_by_convention():
    from app.services.provenance_walker import extract_params

    assert extract_params({"align_params": {"threads": 8}}) == {"threads": 8}
    assert extract_params({"trim_params": {"q": 20}}) == {"q": 20}
    assert extract_params({}) == {}
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_provenance_facts.py -q
```

Expected: FAIL with `ImportError: cannot import name 'extract_tool_facts'`

- [ ] **Step 3: Implement the extractors**

Append to `backend/app/services/provenance_walker.py`:

```python
# Keys matching `<x>_by` whose value is not a tool name. `sra_fields_applied`
# is the live example: a list of metadata fields, not something that ran.
_NOT_TOOL_KEYS = frozenset({"sra_fields_applied", "assembly_fields_applied"})


def extract_tool_facts(facts: dict) -> tuple[str | None, str | None]:
    """The tool that produced an object and its version, read by convention.

    The `*_provenance` builders in `queue/results.py` all follow the same
    shape: `<verb>_by` names the tool, and a sibling key ending `_version`
    carries its version. Reading by convention rather than from a fixed key
    list means a builder added later still surfaces its tool here.
    """
    tool = None
    for key, value in sorted(facts.items()):
        if key in _NOT_TOOL_KEYS or not key.endswith("_by"):
            continue
        if isinstance(value, str) and value:
            tool = value
            break

    version = None
    for key, value in sorted(facts.items()):
        if key.endswith("_version") and isinstance(value, str) and value:
            version = value
            break

    return tool, version


def extract_params(facts: dict) -> dict:
    """Parameters recorded for the step that produced an object.

    Same convention: `align_params`, `trim_params`, `count_params`.
    """
    for key, value in sorted(facts.items()):
        if key.endswith("_params") and isinstance(value, dict) and value:
            return value
    return {}
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/test_provenance_facts.py -q
```

Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/provenance_walker.py backend/tests/services/test_provenance_facts.py
git commit -m "Read tool, version and params from facts by convention"
```

---

## Task 3: Spine versus supporting classification

**Files:**
- Modify: `backend/app/services/provenance_walker.py`
- Create: `backend/tests/services/test_provenance_classify.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/services/test_provenance_classify.py`:

```python
"""Which ancestors are the specimen lineage and which are materials.

A VCF's parents include the BAM and the reference. Both are real ancestry,
but a methods section renders them differently: the BAM is a step in the
story, the reference is a material the story used. Getting this wrong puts
the reference's NCBI accession at the same level as trim parameters.
"""

from beanie import PydanticObjectId

from app.models.object import ObjectRole
from app.services.provenance_walker import classify_parent

BAM = PydanticObjectId()
REF = PydanticObjectId()
GFF = PydanticObjectId()


def test_reference_named_by_facts_is_supporting():
    facts = {"reference_object_id": str(REF)}
    assert classify_parent(REF, facts=facts, role=None) == "supporting"


def test_annotation_named_by_facts_is_supporting():
    facts = {"annotation_object_id": str(GFF)}
    assert classify_parent(GFF, facts=facts, role=None) == "supporting"


def test_parent_not_named_as_material_is_spine():
    facts = {"reference_object_id": str(REF)}
    assert classify_parent(BAM, facts=facts, role=None) == "spine"


def test_falls_back_to_role_when_facts_are_silent():
    """Objects made before their step had a provenance builder have no
    `*_object_id` keys at all, so role is the only signal left."""
    assert classify_parent(REF, facts={}, role=ObjectRole.REFERENCE) == "supporting"
    assert classify_parent(BAM, facts={}, role=ObjectRole.ALIGNMENT) == "spine"


def test_unknown_role_with_silent_facts_is_spine():
    """Defaulting to spine keeps a step visible. Defaulting to supporting
    would quietly demote a real processing step into a materials list."""
    assert classify_parent(BAM, facts={}, role=None) == "spine"
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_provenance_classify.py -q
```

Expected: FAIL with `ImportError: cannot import name 'classify_parent'`

- [ ] **Step 3: Implement the classifier**

Append to `backend/app/services/provenance_walker.py`:

```python
# Fact keys naming a parent that is a material rather than a step. These are
# written by the `*_provenance` builders, which already distinguish their
# parents by role -- this reads a distinction the data already makes rather
# than inventing one.
_SUPPORTING_PARENT_KEYS = ("reference_object_id", "annotation_object_id")

# Roles that are materials when the facts do not say. Used only as a
# fallback, for objects predating their step's provenance builder.
_SUPPORTING_ROLES = frozenset({ObjectRole.REFERENCE, ObjectRole.ANNOTATION})


def classify_parent(
    parent_id: PydanticObjectId,
    *,
    facts: dict,
    role: ObjectRole | None,
) -> Literal["spine", "supporting"]:
    """Whether a parent is part of the specimen lineage or a material used.

    Facts win over role: a step that recorded `reference_object_id` is making
    an explicit claim about how that parent was used, while role is a
    property of the object in isolation.

    The default is `spine`. That direction matters: a misclassified spine
    parent appears in the materials list (visible, mildly wrong), while a
    misclassified material would drop out of the step sequence entirely.
    """
    for key in _SUPPORTING_PARENT_KEYS:
        value = facts.get(key)
        if value and str(value) == str(parent_id):
            return "supporting"

    if role is not None and role in _SUPPORTING_ROLES:
        return "supporting"

    return "spine"
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/test_provenance_classify.py -q
```

Expected: 5 passed.

If `ObjectRole.ANNOTATION` does not exist, run
`grep -n "class ObjectRole" -A 30 backend/app/models/object.py` and use the
member that names annotation files; adjust `_SUPPORTING_ROLES` accordingly.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/provenance_walker.py backend/tests/services/test_provenance_classify.py
git commit -m "Classify ancestors as specimen lineage or supporting materials"
```

---

## Task 4: The traversal

This is the only database-touching code in the feature.

**Files:**
- Modify: `backend/app/services/provenance_walker.py`
- Create: `backend/tests/services/test_provenance_walker.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/services/test_provenance_walker.py`:

```python
"""The DAG walk itself.

These tests use real DataObject documents because the walk is a database
operation and mocking Motor would test the mock. Everything downstream of
the walker is tested against hand-built chains instead, with no DB at all.
"""

import pytest
from beanie import PydanticObjectId

from app.models.object import DataObject, ObjectRole, ObjectStatus
from app.services.provenance_walker import GapKind, walk

pytestmark = pytest.mark.asyncio

OWNER = "test-profile"


async def _obj(name, *, derived_from=(), facts=None, role=None,
               produced_by_job=None, sidecar_of=None):
    obj = DataObject(
        project_id=PydanticObjectId(),
        name=name,
        owner=OWNER,
        status=ObjectStatus.READY,
        role=role,
        facts=facts or {},
        derived_from=list(derived_from),
        produced_by_job=produced_by_job,
        sidecar_of=sidecar_of,
    )
    await obj.insert()
    return obj


async def test_root_object_has_no_steps():
    reads = await _obj("reads.fastq.gz")
    chain = await walk(reads.id, owner=OWNER)

    assert chain.target.object_id == reads.id
    assert chain.target.produced_by is None
    # A root is not a gap: an uploaded file legitimately has no producing job.
    assert chain.gap_count == 0


async def test_two_step_chain_is_ordered_oldest_first():
    reads = await _obj("reads.fastq.gz")
    bam = await _obj(
        "aligned.bam",
        derived_from=[reads.id],
        role=ObjectRole.ALIGNMENT,
        facts={"aligned_by": "bwa-mem2", "aligner_version": "2.2.1"},
    )
    chain = await walk(bam.id, owner=OWNER)

    assert chain.order == (reads.id, bam.id)
    assert chain.nodes[bam.id].produced_by.tool == "bwa-mem2"
    assert chain.nodes[bam.id].produced_by.tool_version == "2.2.1"


async def test_missing_version_is_a_gap_not_a_silent_omission():
    reads = await _obj("reads.fastq.gz")
    bam = await _obj(
        "aligned.bam",
        derived_from=[reads.id],
        role=ObjectRole.ALIGNMENT,
        facts={"aligned_by": "bwa-mem2"},
    )
    chain = await walk(bam.id, owner=OWNER)

    kinds = {g.kind for g in chain.gaps}
    assert GapKind.VERSION_UNRECORDED in kinds
    # The step is still present -- a gap never removes a step.
    assert chain.nodes[bam.id].produced_by is not None


async def test_reconvergent_dag_renders_each_node_once():
    """The reference is reachable via both the BAM and the VCF."""
    ref = await _obj("ref.fasta", role=ObjectRole.REFERENCE)
    reads = await _obj("reads.fastq.gz")
    bam = await _obj(
        "aligned.bam",
        derived_from=[reads.id, ref.id],
        role=ObjectRole.ALIGNMENT,
        facts={"aligned_by": "bwa-mem2", "reference_object_id": str(ref.id)},
    )
    vcf = await _obj(
        "calls.vcf.gz",
        derived_from=[bam.id, ref.id],
        role=ObjectRole.VARIANTS,
        facts={"variants_called_by": "clair3", "reference_object_id": str(ref.id)},
    )
    chain = await walk(vcf.id, owner=OWNER)

    assert chain.order.count(ref.id) == 1
    assert chain.nodes[ref.id].kind == "supporting"
    assert chain.nodes[bam.id].kind == "spine"


async def test_sidecars_never_appear():
    ref = await _obj("ref.fasta", role=ObjectRole.REFERENCE)
    idx = await _obj("ref.fasta.fai", sidecar_of=ref.id, derived_from=[ref.id])
    reads = await _obj("reads.fastq.gz")
    bam = await _obj(
        "aligned.bam",
        derived_from=[reads.id, ref.id, idx.id],
        role=ObjectRole.ALIGNMENT,
    )
    chain = await walk(bam.id, owner=OWNER)

    assert idx.id not in chain.nodes


async def test_dangling_parent_is_a_gap_not_a_crash():
    ghost = PydanticObjectId()
    bam = await _obj("aligned.bam", derived_from=[ghost], role=ObjectRole.ALIGNMENT)
    chain = await walk(bam.id, owner=OWNER)

    assert GapKind.DANGLING_PARENT in {g.kind for g in chain.gaps}


async def test_multiple_spine_parents_record_a_branch():
    long_reads = await _obj("ont.fastq.gz")
    short_reads = await _obj("ill.fastq.gz")
    asm = await _obj(
        "polished.fasta",
        derived_from=[long_reads.id, short_reads.id],
        facts={"polished_by": "polypolish"},
    )
    chain = await walk(asm.id, owner=OWNER)

    assert chain.branches
    assert set(chain.branches[0]) == {long_reads.id, short_reads.id}


async def test_walk_is_owner_scoped():
    other = await _obj("secret.fastq.gz")
    other.owner = "someone-else"
    await other.save()

    from app.errors import NotFoundError

    # `get_object` raises NotFoundError for a wrong owner, the same error a
    # missing id raises -- so one profile cannot confirm another's id exists.
    with pytest.raises(NotFoundError):
        await walk(other.id, owner=OWNER)


async def test_failed_runs_appear_in_the_chain():
    """`records_for_object` includes failures deliberately, and provenance is
    the reader that wants them: a step that failed and was retried is the
    most informative record a methods reader can have. A chain that quietly
    showed only successes would be describing a run that never happened."""
    from app.models import JobRunTiming

    reads = await _obj("reads.fastq.gz")
    job_id = PydanticObjectId()
    bam = await _obj(
        "aligned.bam",
        derived_from=[reads.id],
        role=ObjectRole.ALIGNMENT,
        produced_by_job=job_id,
        facts={"aligned_by": "bwa-mem2", "aligner_version": "2.2.1"},
    )
    await JobRunTiming(
        job_id=str(job_id),
        object_id=str(bam.id),
        job_type="align_reads",
        outcome="failed",
        duration_ms=1000,
        input_bytes=0,
    ).insert()

    chain = await walk(bam.id, owner=OWNER)
    assert chain.nodes[bam.id].produced_by.outcome == "failed"
```

`JobRunTiming` may require fields beyond those above. Check its definition
and supply whatever else is non-optional:

```bash
grep -n "class JobRunTiming" -A 40 backend/app/models/timing.py
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_provenance_walker.py -q
```

Expected: FAIL with `ImportError: cannot import name 'walk'`

- [ ] **Step 3: Implement the traversal**

Append to `backend/app/services/provenance_walker.py`. Add these imports at
the top of the file alongside the existing ones:

```python
from collections import deque

from app.models.job import Job
from app.models.object import DataObject
from app.services import object_service, timing_service
```

Then the traversal:

```python
async def walk(
    object_id: PydanticObjectId, *, owner: str
) -> ProvenanceChain:
    """Everything that produced `object_id`, walked to the roots.

    Breadth-first up `derived_from`, memoized by object id. The memo is not
    an optimization: the DAG reconverges (a reference is reachable via both
    the alignment and the variant call) and a node must render once.

    `object_service.get_object` authorizes before anything else is read --
    ancestors are fetched by id without their own owner check, so the target
    check is what stands between a request and another profile's lineage.
    """
    target_obj = await object_service.get_object(object_id, owner=owner)

    nodes: dict[PydanticObjectId, Node] = {}
    gaps: list[Gap] = []
    branches: list[tuple[PydanticObjectId, ...]] = []
    order: list[PydanticObjectId] = []

    queue: deque[tuple[DataObject, int]] = deque([(target_obj, 0)])
    seen: set[PydanticObjectId] = {target_obj.id}
    # Every node starts as spine; a parent edge can demote it to supporting.
    kinds: dict[PydanticObjectId, Literal["spine", "supporting"]] = {
        target_obj.id: "spine"
    }

    while queue:
        obj, depth = queue.popleft()

        if depth >= MAX_DEPTH:
            gaps.append(Gap(kind=GapKind.DEPTH_EXCEEDED, object_id=obj.id))
            continue

        step, step_gaps = await _step_for(obj)
        gaps.extend(step_gaps)

        # A shared copy has its lineage deliberately cleared: `derived_from`
        # and `produced_by_job` name objects and jobs in the sender's
        # partition, which owner-scoped lookups here can never resolve.
        if obj.shared_from is not None:
            gaps.append(Gap(kind=GapKind.SHARE_BOUNDARY, object_id=obj.id))

        parent_ids: list[PydanticObjectId] = []
        spine_parents: list[PydanticObjectId] = []

        for parent_id in obj.derived_from:
            parent = await DataObject.get(parent_id)
            if parent is None:
                gaps.append(
                    Gap(
                        kind=GapKind.DANGLING_PARENT,
                        object_id=obj.id,
                        detail=str(parent_id),
                    )
                )
                continue

            # Biologically inert scaffolding: a methods paragraph mentioning
            # BAM index construction is noise. Skipped here rather than
            # filtered at render, so it never reaches the prompt.
            if parent.sidecar_of is not None:
                continue

            parent_ids.append(parent_id)
            kind = classify_parent(parent_id, facts=obj.facts, role=parent.role)
            if kind == "spine":
                spine_parents.append(parent_id)

            # Supporting wins if any edge says so: a reference reached once as
            # a material is a material, however else it is reachable.
            if parent_id not in kinds or kind == "supporting":
                kinds[parent_id] = kind

            if parent_id not in seen:
                seen.add(parent_id)
                queue.append((parent, depth + 1))

        if len(spine_parents) > 1:
            branches.append(tuple(spine_parents))

        nodes[obj.id] = Node(
            object_id=obj.id,
            name=obj.name,
            role=obj.role,
            kind=kinds.get(obj.id, "spine"),
            produced_by=step,
            parents=tuple(parent_ids),
        )
        order.append(obj.id)

    # Rebuild nodes whose kind was decided after they were appended.
    nodes = {
        oid: Node(
            object_id=n.object_id,
            name=n.name,
            role=n.role,
            kind=kinds.get(oid, n.kind),
            produced_by=n.produced_by,
            parents=n.parents,
        )
        for oid, n in nodes.items()
    }

    return ProvenanceChain(
        target=nodes[target_obj.id],
        nodes=nodes,
        # Oldest first: a methods section reads forward through time.
        order=tuple(reversed(order)),
        gaps=tuple(gaps),
        branches=tuple(branches),
    )


async def _step_for(obj: DataObject) -> tuple[Step | None, list[Gap]]:
    """The job that produced `obj`, if any, plus whatever it failed to record.

    Returns `(None, [])` for a root. A root is not a gap: an uploaded FASTQ
    legitimately has no producing job, and counting it would inflate the gap
    number with something no user can act on.
    """
    if obj.produced_by_job is None:
        return None, []

    gaps: list[Gap] = []
    job = await Job.get(obj.produced_by_job)
    job_type = job.type if job is not None else "unknown"

    tool, version = extract_tool_facts(obj.facts)
    params = extract_params(obj.facts)

    if tool is not None and version is None:
        gaps.append(Gap(kind=GapKind.VERSION_UNRECORDED, object_id=obj.id))
    if not params:
        gaps.append(Gap(kind=GapKind.PARAMS_UNRECORDED, object_id=obj.id))

    ran_at = None
    outcome = None
    rows = await timing_service.records_for_object(str(obj.id), limit=50)
    for row in rows:
        # `records_for_object` includes failures deliberately -- a step that
        # failed and was retried is exactly what a methods reader needs.
        if row.job_id == str(obj.produced_by_job):
            ran_at = row.finished_at
            outcome = row.outcome
            if tool is None:
                tool = row.tool
            if version is None:
                version = row.tool_version
            break

    step = Step(
        job_type=job_type,
        verb=_STEP_VERBS.get(job_type, GENERIC_VERB),
        tool=tool,
        tool_version=version,
        params=params,
        ran_at=ran_at,
        outcome=outcome,
        gaps=tuple(gaps),
    )
    return step, gaps
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/test_provenance_walker.py -q
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/provenance_walker.py backend/tests/services/test_provenance_walker.py
git commit -m "Walk the derived_from DAG into a ProvenanceChain"
```

---

## Task 5: The markdown renderer

Pure function. No database, no model.

**Files:**
- Create: `backend/app/services/provenance_report.py`
- Create: `backend/tests/services/test_provenance_report.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/services/test_provenance_report.py`:

```python
"""Rendering a chain as markdown.

Hand-built chains, no database: this is where the gap-honesty rules live and
they need exhaustive cheap tests. `_chain` below is the only fixture helper.
"""

from datetime import datetime

from beanie import PydanticObjectId

from app.services.provenance_report import render_markdown
from app.services.provenance_walker import (
    Gap,
    GapKind,
    Node,
    ProvenanceChain,
    Step,
)

READS = PydanticObjectId()
BAM = PydanticObjectId()


def _root(object_id=READS, name="reads.fastq.gz"):
    return Node(
        object_id=object_id,
        name=name,
        role=None,
        kind="spine",
        produced_by=None,
        parents=(),
    )


def _step_node(object_id=BAM, name="aligned.bam", step=None, parents=(READS,)):
    return Node(
        object_id=object_id,
        name=name,
        role=None,
        kind="spine",
        produced_by=step,
        parents=parents,
    )


def _chain(*nodes, gaps=(), branches=()):
    by_id = {n.object_id: n for n in nodes}
    return ProvenanceChain(
        target=nodes[-1],
        nodes=by_id,
        order=tuple(n.object_id for n in nodes),
        gaps=tuple(gaps),
        branches=tuple(branches),
    )


def test_complete_step_names_tool_and_version():
    step = Step(
        job_type="align_reads",
        verb="aligned with",
        tool="bwa-mem2",
        tool_version="2.2.1",
        ran_at=datetime(2026, 7, 14, 9, 0),
    )
    md = render_markdown(_chain(_root(), _step_node(step=step)))

    assert "aligned with bwa-mem2 2.2.1" in md
    assert "reads.fastq.gz" in md


def test_missing_version_is_stated_not_omitted():
    """The awkwardness is the point: a reader scanning for the version sees
    the question was asked and unanswered."""
    step = Step(
        job_type="align_reads",
        verb="aligned with",
        tool="bwa-mem2",
        tool_version=None,
        gaps=(Gap(kind=GapKind.VERSION_UNRECORDED, object_id=BAM),),
    )
    md = render_markdown(
        _chain(
            _root(),
            _step_node(step=step),
            gaps=(Gap(kind=GapKind.VERSION_UNRECORDED, object_id=BAM),),
        )
    )

    assert "version not recorded" in md
    assert "bwa-mem2" in md


def test_every_gap_kind_renders_a_marker():
    for kind, expected in [
        (GapKind.VERSION_UNRECORDED, "version not recorded"),
        (GapKind.PARAMS_UNRECORDED, "parameters not recorded"),
        (GapKind.SHARE_BOUNDARY, "another profile"),
        (GapKind.DANGLING_PARENT, "no longer exists"),
        (GapKind.DEPTH_EXCEEDED, "truncated"),
    ]:
        chain = _chain(_root(), gaps=(Gap(kind=kind, object_id=READS),))
        md = render_markdown(chain)
        assert expected in md, f"{kind} rendered no marker"


def test_gap_count_is_shown():
    chain = _chain(
        _root(),
        gaps=(
            Gap(kind=GapKind.VERSION_UNRECORDED, object_id=READS),
            Gap(kind=GapKind.PARAMS_UNRECORDED, object_id=READS),
        ),
    )
    assert "2 facts not recorded" in render_markdown(chain)


def test_no_gaps_says_so_positively():
    assert "All facts recorded" in render_markdown(_chain(_root()))


def test_branch_renders_as_a_visible_fork():
    other = PydanticObjectId()
    chain = _chain(
        _root(),
        _root(object_id=other, name="ill.fastq.gz"),
        _step_node(parents=(READS, other)),
        branches=((READS, other),),
    )
    md = render_markdown(chain)
    assert "two inputs" in md.lower() or "branch" in md.lower()
    assert "ill.fastq.gz" in md


def test_root_is_labelled_input_not_a_gap():
    md = render_markdown(_chain(_root()))
    assert "Input" in md
    assert "not recorded" not in md
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_provenance_report.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.provenance_report'`

- [ ] **Step 3: Implement the renderer**

Create `backend/app/services/provenance_report.py`:

```python
"""Rendering a ProvenanceChain as a methods report.

This is the deliverable, not a fallback. It works with no AI provider
configured, and it is the artifact a user cites -- the prose version in
`provenance_prompt.py` is a second rendering of exactly these facts and can
never contain one this does not.

The governing rule: never omit a step known to have run, and never assert a
fact that is not there. A gap renders in the position the fact would have
occupied, so a reader scanning for "which version" sees the question asked
and unanswered rather than seeing nothing and assuming it did not matter.
"""

from app.services.provenance_walker import (
    Gap,
    GapKind,
    Node,
    ProvenanceChain,
)

_GAP_TEXT = {
    GapKind.VERSION_UNRECORDED: "**version not recorded**",
    GapKind.PARAMS_UNRECORDED: "**parameters not recorded**",
    GapKind.SHARE_BOUNDARY: (
        "**lineage continues in another profile and is not available here**"
    ),
    GapKind.DANGLING_PARENT: "**parent object no longer exists**",
    GapKind.DEPTH_EXCEEDED: "**ancestry truncated at the depth limit**",
}


def _gaps_for(chain: ProvenanceChain, node_id) -> list[Gap]:
    return [g for g in chain.gaps if g.object_id == node_id]


def _describe_step(node: Node, gaps: list[Gap]) -> str:
    step = node.produced_by
    if step is None:
        return f"**Input:** `{node.name}`"

    kinds = {g.kind for g in gaps}
    parts = [step.verb]

    if step.tool:
        parts.append(step.tool)
    else:
        parts.append("an unrecorded tool")

    if step.tool_version:
        parts.append(step.tool_version)
    elif GapKind.VERSION_UNRECORDED in kinds:
        parts.append(f"({_GAP_TEXT[GapKind.VERSION_UNRECORDED]})")

    line = f"**{node.name}** — {' '.join(parts)}"

    if step.ran_at:
        line += f", {step.ran_at:%Y-%m-%d}"
    if step.outcome and step.outcome != "success":
        # Failures are in the chain deliberately; saying so is the point.
        line += f" — run outcome: {step.outcome}"

    if step.params:
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(step.params.items()))
        line += f"\n  - Parameters: {rendered}"
    elif GapKind.PARAMS_UNRECORDED in kinds:
        line += (
            f"\n  - {_GAP_TEXT[GapKind.PARAMS_UNRECORDED]}"
            f" (job: {step.job_type})"
        )

    return line


def render_markdown(chain: ProvenanceChain) -> str:
    lines: list[str] = ["## Provenance", ""]

    if chain.gap_count:
        lines.append(f"_{chain.gap_count} facts not recorded._")
    else:
        lines.append("_All facts recorded._")
    lines.append("")

    spine = [
        chain.nodes[oid]
        for oid in chain.order
        if chain.nodes[oid].kind == "spine"
    ]
    supporting = [
        chain.nodes[oid]
        for oid in chain.order
        if chain.nodes[oid].kind == "supporting"
    ]

    lines.append("### Steps")
    lines.append("")
    for node in spine:
        lines.append(f"- {_describe_step(node, _gaps_for(chain, node.object_id))}")

    if chain.branches:
        lines.append("")
        for branch in chain.branches:
            names = ", ".join(f"`{chain.nodes[b].name}`" for b in branch if b in chain.nodes)
            lines.append(
                f"- _This step combined two inputs (a branch in the lineage): {names}._"
            )

    if supporting:
        lines.append("")
        lines.append("### Materials")
        lines.append("")
        for node in supporting:
            lines.append(
                f"- {_describe_step(node, _gaps_for(chain, node.object_id))}"
            )

    chain_level = [g for g in chain.gaps if g.kind in (
        GapKind.SHARE_BOUNDARY, GapKind.DANGLING_PARENT, GapKind.DEPTH_EXCEEDED
    )]
    if chain_level:
        lines.append("")
        lines.append("### Limits of this record")
        lines.append("")
        for gap in chain_level:
            text = _GAP_TEXT[gap.kind]
            if gap.detail:
                text += f" (id: {gap.detail})"
            lines.append(f"- {text}")

    return "\n".join(lines)
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/test_provenance_report.py -q
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/provenance_report.py backend/tests/services/test_provenance_report.py
git commit -m "Render a provenance chain as a gap-honest markdown report"
```

---

## Task 6: The prompt builder and the containment check

The containment check is the layer that makes the anti-fabrication claim real
rather than aspirational. It fails closed.

**Files:**
- Create: `backend/app/services/provenance_prompt.py`
- Create: `backend/tests/services/test_provenance_prompt.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/services/test_provenance_prompt.py`:

```python
"""Prompt construction and the containment check.

The containment check is the only anti-fabrication layer that does not
depend on the model behaving, so it gets the most tests. It catches invented
versions and tool names -- the high-cost class. It does NOT catch invented
causal claims ("to remove adapter contamination"), and these tests do not
pretend otherwise.
"""

from beanie import PydanticObjectId

from app.services.provenance_prompt import (
    build_prompt,
    supported_tokens,
    verify_containment,
)
from app.services.provenance_walker import (
    Gap,
    GapKind,
    Node,
    ProvenanceChain,
    Step,
)

BAM = PydanticObjectId()


def _chain_with(step, gaps=()):
    node = Node(
        object_id=BAM,
        name="aligned.bam",
        role=None,
        kind="spine",
        produced_by=step,
        parents=(),
    )
    return ProvenanceChain(
        target=node,
        nodes={BAM: node},
        order=(BAM,),
        gaps=tuple(gaps),
    )


def _align_step(version="2.2.1"):
    return Step(
        job_type="align_reads",
        verb="aligned with",
        tool="bwa-mem2",
        tool_version=version,
    )


def test_prompt_contains_the_facts():
    system, user = build_prompt(_chain_with(_align_step()))
    assert "bwa-mem2" in user
    assert "2.2.1" in user


def test_prompt_forbids_inventing_facts():
    system, _ = build_prompt(_chain_with(_align_step()))
    lowered = system.lower()
    assert "do not" in lowered
    assert "invent" in lowered or "infer" in lowered


def test_gaps_are_supplied_as_content_to_state():
    """The model is told 'version not recorded' as a fact to repeat, not as
    a blank to fill."""
    chain = _chain_with(
        _align_step(version=None),
        gaps=(Gap(kind=GapKind.VERSION_UNRECORDED, object_id=BAM),),
    )
    _, user = build_prompt(chain)
    assert "version not recorded" in user


def test_supported_tokens_include_tools_and_versions():
    tokens = supported_tokens(_chain_with(_align_step()))
    assert "bwa-mem2" in tokens
    assert "2.2.1" in tokens


def test_containment_accepts_prose_using_only_supported_facts():
    chain = _chain_with(_align_step())
    prose = "Reads were aligned with bwa-mem2 2.2.1."
    assert verify_containment(prose, chain) is None


def test_containment_rejects_an_invented_version():
    """The failure this whole feature exists to prevent."""
    chain = _chain_with(_align_step())
    prose = "Reads were aligned with bwa-mem2 2.2.9."
    reason = verify_containment(prose, chain)
    assert reason is not None
    assert "2.2.9" in reason


def test_containment_rejects_an_invented_tool():
    chain = _chain_with(_align_step())
    prose = "Reads were aligned with bowtie2 2.2.1."
    assert verify_containment(prose, chain) is not None


def test_containment_rejects_a_version_filled_into_a_gap():
    chain = _chain_with(
        _align_step(version=None),
        gaps=(Gap(kind=GapKind.VERSION_UNRECORDED, object_id=BAM),),
    )
    prose = "Reads were aligned with bwa-mem2 0.7.17."
    assert verify_containment(prose, chain) is not None


def test_containment_allows_ordinary_numbers():
    """A year or a count is not a version claim; rejecting those would make
    the check unusable."""
    chain = _chain_with(_align_step())
    prose = "In 2026, reads were aligned with bwa-mem2 2.2.1 across 24 samples."
    assert verify_containment(prose, chain) is None
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_provenance_prompt.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.provenance_prompt'`

- [ ] **Step 3: Implement the prompt builder**

Create `backend/app/services/provenance_prompt.py`:

```python
"""Turning a ProvenanceChain into a prompt, and checking what comes back.

Four layers stop fabrication here. Three are structural and one depends on
the model behaving:

1. The model gets no database access and no free text -- only this chain,
   the same object the structured report rendered. There is no path by which
   it learns a fact the report does not already show.
2. Gaps are supplied as content to state, not blanks to fill.
3. `verify_containment` rejects output introducing an unsupported version or
   tool name. Deterministic, and it fails closed.
4. The system prompt constrains the task to rephrasing.

Layer 3's limit, stated plainly because someone will otherwise assume it is
total: it catches invented versions and tool names, which is the high-cost
class. It does not catch an invented causal claim -- "trimmed to remove
adapter contamination" when nothing measured contamination. That is mitigated
by layer 4 and by the structured report being the citable artifact, not
eliminated.
"""

import re

from app.services.provenance_report import render_markdown
from app.services.provenance_walker import ProvenanceChain

SYSTEM_PROMPT = """\
You rewrite a structured record of how a bioinformatics file was produced \
into a short methods paragraph suitable for a scientific manuscript.

Rules, in order of importance:

1. Use ONLY the facts given below. Do not add a tool, a version, a parameter, \
a step, or a purpose that is not in the record.
2. Where the record says a fact was not recorded, say so in the paragraph. \
Do not infer, guess, or substitute a plausible value. "aligned with bwa-mem2 \
(version not recorded)" is correct and expected.
3. Do not interpret results or claim significance. You are describing what \
was run, not what it showed.
4. Keep the order of steps exactly as given.
5. Write plainly, in past tense, one paragraph.
"""

# A version claim: two or more dot-separated numeric components. Deliberately
# not `\d+` alone -- a year, a thread count and a sample size are ordinary
# numbers, and rejecting those would make the check unusable.
_VERSION_RE = re.compile(r"\b\d+\.\d+[\w.\-]*")


def build_prompt(chain: ProvenanceChain) -> tuple[str, str]:
    """System and user prompts for one chain.

    The user prompt is the rendered markdown report verbatim. That is
    deliberate: the model sees exactly what the user sees, so anything it
    states is checkable against the same text.
    """
    return SYSTEM_PROMPT, render_markdown(chain)


def supported_tokens(chain: ProvenanceChain) -> set[str]:
    """Every tool name and version the chain actually recorded."""
    tokens: set[str] = set()
    for node in chain.nodes.values():
        step = node.produced_by
        if step is None:
            continue
        if step.tool:
            tokens.add(step.tool.lower())
        if step.tool_version:
            tokens.add(step.tool_version.lower())
        for value in step.params.values():
            tokens.add(str(value).lower())
    return tokens


def verify_containment(prose: str, chain: ProvenanceChain) -> str | None:
    """Why this prose must be rejected, or None if it is safe to show.

    Fails closed: an unrecognized version-shaped token is a rejection, not a
    warning. A methods paragraph carrying one invented version is worse than
    no paragraph at all.
    """
    supported = supported_tokens(chain)

    for match in _VERSION_RE.findall(prose):
        if match.lower().rstrip(".,;)") not in supported:
            return f"unsupported version token: {match}"

    lowered = prose.lower()
    known_tools = {
        node.produced_by.tool.lower()
        for node in chain.nodes.values()
        if node.produced_by is not None and node.produced_by.tool
    }
    # A tool named in the prose that the chain never recorded. Checked
    # against a fixed vocabulary rather than by parsing prose, because
    # extracting "the tool" from a sentence is exactly the kind of guess this
    # module exists to avoid.
    for candidate in _COMMON_TOOLS:
        if candidate in lowered and candidate not in known_tools:
            return f"unsupported tool name: {candidate}"

    return None


# Tools this app knows about, used only to catch a model naming one that did
# not run. Missing a name means a miss, never a false rejection -- which is
# the right direction for a list nobody can keep exhaustive.
_COMMON_TOOLS = frozenset(
    {
        "bwa-mem2", "bwa", "minimap2", "bowtie2", "star", "dragmap", "hisat2",
        "fastp", "cutadapt", "trimmomatic", "fastqc", "nanoplot",
        "clair3", "deepvariant", "bcftools", "freebayes", "gatk",
        "flye", "spades", "hifiasm", "canu", "raven",
        "featurecounts", "salmon", "kallisto", "htseq",
        "samtools", "polypolish", "racon", "medaka", "ragtag", "quast",
    }
)
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/test_provenance_prompt.py -q
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/provenance_prompt.py backend/tests/services/test_provenance_prompt.py
git commit -m "Build the narrative prompt and verify model output containment"
```

---

## Task 7: The `PROVENANCE_NARRATIVE` task slot

**Files:**
- Modify: `backend/app/models/ai.py:57-70`
- Create: `backend/tests/services/ai/test_provenance_slot.py`

Per CLAUDE.md: a new AI feature needs its own `TaskSlot` member. Reusing an
existing slot silently ties two unrelated features to one provider with no
way for the user to separate them.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/ai/test_provenance_slot.py`:

```python
"""The narrative feature routes through its own slot.

The settings page renders one row per TaskSlot member, so the enum is what
makes a feature independently routable.
"""

from app.models.ai import TaskSlot


def test_provenance_narrative_slot_exists():
    assert TaskSlot.PROVENANCE_NARRATIVE.value == "provenance_narrative"


def test_every_slot_has_a_label():
    for slot in TaskSlot:
        assert slot.label
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_provenance_slot.py -q
```

Expected: FAIL with `AttributeError: PROVENANCE_NARRATIVE`

- [ ] **Step 3: Add the slot**

In `backend/app/models/ai.py`, add the member to `TaskSlot`:

```python
    FILE_SUMMARY = "file_summary"
    ORGANISM_BLURB = "organism_blurb"
    PROJECT_QA = "project_qa"
    PROVENANCE_NARRATIVE = "provenance_narrative"
```

And the label to `_SLOT_LABELS`:

```python
_SLOT_LABELS = {
    TaskSlot.FILE_SUMMARY: "File summaries",
    TaskSlot.ORGANISM_BLURB: "Organism blurbs",
    TaskSlot.PROJECT_QA: "Project Q&A chat",
    TaskSlot.PROVENANCE_NARRATIVE: "Methods narratives",
}
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_provenance_slot.py -q
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/ai.py backend/tests/services/ai/test_provenance_slot.py
git commit -m "Add the PROVENANCE_NARRATIVE task slot"
```

---

## Task 8: API schemas and routes

**Files:**
- Modify: `backend/app/api/v1/schemas.py` (append after `ComputationRecord`)
- Modify: `backend/app/api/v1/objects.py` (append after `object_computations`)
- Create: `backend/tests/api/test_provenance_routes.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/api/test_provenance_routes.py`:

```python
"""The two routes.

Split deliberately: the report route never touches a provider, so a missing
or broken AI configuration cannot take down the thing users actually cite.

Uses the `client` and `two_profiles` fixtures from `tests/api/conftest.py`.
Two profiles is the minimum that proves isolation: profile A asking for its
own data succeeds whether or not the route ever applied an owner filter, so
the isolation assertion has to be B asking for A's object.
"""

import pytest
from beanie import PydanticObjectId
from httpx import AsyncClient

from app.models.object import DataObject, ObjectRole, ObjectStatus

pytestmark = pytest.mark.asyncio


async def _obj(owner, name, **kw):
    obj = DataObject(
        project_id=PydanticObjectId(),
        name=name,
        owner=owner,
        status=ObjectStatus.READY,
        **kw,
    )
    await obj.insert()
    return obj


async def test_report_route_returns_markdown_and_gap_count(
    client: AsyncClient, two_profiles
):
    owner = two_profiles["a"].owner_id()
    reads = await _obj(owner, "reads.fastq.gz")
    bam = await _obj(
        owner,
        "aligned.bam",
        role=ObjectRole.ALIGNMENT,
        derived_from=[reads.id],
        facts={"aligned_by": "bwa-mem2"},
    )

    resp = await client.get(
        f"/api/v1/objects/{bam.id}/provenance-narrative",
        headers=two_profiles["a_headers"],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "bwa-mem2" in body["markdown"]
    assert body["gap_count"] >= 1
    assert body["steps"]


async def test_report_route_works_with_no_ai_provider(
    client: AsyncClient, two_profiles
):
    """The structured half must never depend on a configured provider."""
    obj = await _obj(two_profiles["a"].owner_id(), "reads.fastq.gz")
    resp = await client.get(
        f"/api/v1/objects/{obj.id}/provenance-narrative",
        headers=two_profiles["a_headers"],
    )
    assert resp.status_code == 200


async def test_report_route_rejects_another_owner(
    client: AsyncClient, two_profiles
):
    """B asking for A's object. `get_object` raises the same NotFoundError
    for a wrong owner as for a missing id, so this is a 404 rather than a
    403 -- deliberate, so one profile cannot confirm another's id exists."""
    obj = await _obj(two_profiles["a"].owner_id(), "secret.fastq.gz")
    resp = await client.get(
        f"/api/v1/objects/{obj.id}/provenance-narrative",
        headers=two_profiles["b_headers"],
    )
    assert resp.status_code == 404


async def test_prose_route_reports_unavailable_with_no_provider(
    client: AsyncClient, two_profiles
):
    obj = await _obj(two_profiles["a"].owner_id(), "reads.fastq.gz")
    resp = await client.post(
        f"/api/v1/objects/{obj.id}/provenance-narrative/prose",
        headers=two_profiles["a_headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["prose"] is None
    assert resp.json()["unavailable_reason"]
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/api/test_provenance_routes.py -q
```

Expected: FAIL with 404s on every route.

Check the fixture names first: run
`grep -n "def client\|def owner" backend/tests/conftest.py backend/tests/api/conftest.py`
and match whatever this suite already uses. If the API tests use a different
auth fixture, use that one rather than adding a new fixture.

- [ ] **Step 3: Add the schemas**

Append to `backend/app/api/v1/schemas.py`:

```python
# --- Provenance narratives ---
class ProvenanceStepOut(BaseModel):
    """One step, flattened for the panel.

    Deliberately not the whole `Step`: `job_type` is here because the UI
    shows it when parameters are missing, but `Gap` objects are not -- the
    markdown already places each gap in the position its fact would have
    occupied, and a second parallel representation would drift.
    """

    object_id: str
    name: str
    kind: str
    verb: str | None
    tool: str | None
    tool_version: str | None
    job_type: str | None
    ran_at: datetime | None
    outcome: str | None


class ProvenanceNarrativeOut(BaseModel):
    markdown: str
    gap_count: int
    steps: list[ProvenanceStepOut]
    materials: list[ProvenanceStepOut]
    has_branches: bool


class ProvenanceProseOut(BaseModel):
    """The model-rendered half.

    `prose` is None whenever the paragraph could not be produced *or* was
    rejected by the containment check, with `unavailable_reason` saying
    which. Rejection is not an error state to retry -- it means the model
    introduced a fact the record does not support, and the structured report
    stands alone.
    """

    prose: str | None
    unavailable_reason: str | None
```

- [ ] **Step 4: Add the routes**

Append to `backend/app/api/v1/objects.py`:

```python
@router.get(
    "/{object_id}/provenance-narrative",
    response_model=ProvenanceNarrativeOut,
)
async def provenance_narrative(
    object_id: PydanticObjectId,
    owner: OwnerDep,
) -> ProvenanceNarrativeOut:
    """A methods report for one object, assembled from recorded facts only.

    No model involvement: this must work on an install with no AI provider
    configured, because it is the artifact users cite.
    """
    chain = await provenance_walker.walk(object_id, owner=owner)

    def _out(node) -> ProvenanceStepOut:
        step = node.produced_by
        return ProvenanceStepOut(
            object_id=str(node.object_id),
            name=node.name,
            kind=node.kind,
            verb=step.verb if step else None,
            tool=step.tool if step else None,
            tool_version=step.tool_version if step else None,
            job_type=step.job_type if step else None,
            ran_at=step.ran_at if step else None,
            outcome=step.outcome if step else None,
        )

    nodes = [chain.nodes[oid] for oid in chain.order]
    return ProvenanceNarrativeOut(
        markdown=provenance_report.render_markdown(chain),
        gap_count=chain.gap_count,
        steps=[_out(n) for n in nodes if n.kind == "spine"],
        materials=[_out(n) for n in nodes if n.kind == "supporting"],
        has_branches=bool(chain.branches),
    )


@router.post(
    "/{object_id}/provenance-narrative/prose",
    response_model=ProvenanceProseOut,
)
async def provenance_narrative_prose(
    object_id: PydanticObjectId,
    owner: OwnerDep,
) -> ProvenanceProseOut:
    """The same facts, rendered as prose by a model.

    Every failure mode returns 200 with `prose=None` and a reason: no
    provider configured, the call failed, or the output was rejected for
    introducing an unsupported fact. None of those is an error the caller
    should retry, and the structured report is unaffected either way.
    """
    chain = await provenance_walker.walk(object_id, owner=owner)

    provider = await ai.resolve(TaskSlot.PROVENANCE_NARRATIVE)
    if provider is None:
        return ProvenanceProseOut(
            prose=None,
            unavailable_reason="No AI provider is configured for methods narratives.",
        )

    system, user = provenance_prompt.build_prompt(chain)
    result = await ai.complete(provider, system=system, user=user)

    # `complete()` never raises and never returns None -- it returns
    # Completion | ToolCall | Failure. Checking for None here would treat
    # every failure as a success.
    if not isinstance(result, Completion):
        return ProvenanceProseOut(
            prose=None,
            unavailable_reason="The model call did not succeed.",
        )

    rejection = provenance_prompt.verify_containment(result.text, chain)
    if rejection is not None:
        log.warning(
            "provenance_prose_rejected",
            object_id=str(object_id),
            reason=rejection,
        )
        return ProvenanceProseOut(
            prose=None,
            unavailable_reason=(
                f"The generated paragraph was rejected because it introduced a "
                f"fact this record does not support ({rejection}). "
                f"The structured report above is unaffected."
            ),
        )

    return ProvenanceProseOut(prose=result.text, unavailable_reason=None)
```

Add these imports at the top of `objects.py`:

```python
from app.logging import get_logger
from app.models.ai import TaskSlot
from app.services import ai, provenance_prompt, provenance_report, provenance_walker
from app.services.ai import Completion
```

and to the existing schema import block (`objects.py:10-18`):

```python
from app.api.v1.schemas import (
    ProvenanceNarrativeOut,
    ProvenanceProseOut,
    ProvenanceStepOut,
)
```

**`objects.py` has no module logger today** — it is the one thing in this
task the file does not already have. Add one below the imports, matching how
every other module in the package does it:

```python
log = get_logger(__name__)
```

Two things already verified, so do not re-derive them: `app.services.ai`
re-exports `resolve`, `complete` and `Completion` at package level
(`services/ai/__init__.py`), and `Completion` is a frozen dataclass with
`text: str` and `model: str` (`services/ai/adapters.py:36`), so
`result.text` is correct.

- [ ] **Step 5: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/api/test_provenance_routes.py -q
```

Expected: 4 passed.

- [ ] **Step 6: Run the whole backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: all pass. Read the count, not the exit code.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/v1/schemas.py backend/app/api/v1/objects.py backend/tests/api/test_provenance_routes.py
git commit -m "Serve the provenance report and its optional prose rendering"
```

---

## Task 9: Frontend panel

No headless component testing exists in this repo and none is expected;
verification is manual in the browser (Task 10).

**Files:**
- Modify: `frontend/src/api/client.ts`
- Create: `frontend/src/components/ProvenanceNarrative.tsx`
- Modify: `frontend/src/components/DetailPanel.tsx`

- [ ] **Step 1: Add the API client functions**

In `frontend/src/api/client.ts`, next to `getObjectComputations`:

```typescript
export type ProvenanceStep = {
  object_id: string;
  name: string;
  kind: "spine" | "supporting";
  verb: string | null;
  tool: string | null;
  tool_version: string | null;
  job_type: string | null;
  ran_at: string | null;
  outcome: string | null;
};

export type ProvenanceNarrative = {
  markdown: string;
  gap_count: number;
  steps: ProvenanceStep[];
  materials: ProvenanceStep[];
  has_branches: boolean;
};

export type ProvenanceProse = {
  prose: string | null;
  unavailable_reason: string | null;
};

export async function getProvenanceNarrative(
  objectId: string,
): Promise<ProvenanceNarrative> {
  return request(`/objects/${objectId}/provenance-narrative`);
}

export async function generateProvenanceProse(
  objectId: string,
): Promise<ProvenanceProse> {
  return request(`/objects/${objectId}/provenance-narrative/prose`, {
    method: "POST",
  });
}
```

Match the existing `request` helper's signature — check how
`getObjectComputations` calls it and mirror that exactly.

- [ ] **Step 2: Create the panel component**

Create `frontend/src/components/ProvenanceNarrative.tsx`:

```tsx
/**
 * The methods report for one file.
 *
 * The structured report is the deliverable and renders unconditionally. The
 * prose button is a second rendering of the same facts, and it is fetched
 * only on click -- it costs a model call, and most opens of this panel do
 * not want one.
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import * as api from "../api/client";

export function ProvenanceNarrative({ objectId }: { objectId: string }) {
  const [copied, setCopied] = useState<"report" | "prose" | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["provenance-narrative", objectId],
    queryFn: () => api.getProvenanceNarrative(objectId),
  });

  const prose = useMutation({
    mutationFn: () => api.generateProvenanceProse(objectId),
  });

  const copy = (text: string, which: "report" | "prose") => {
    void navigator.clipboard.writeText(text);
    setCopied(which);
    setTimeout(() => setCopied(null), 2000);
  };

  if (isLoading) return <div className="section-body">Loading provenance…</div>;
  if (error || !data) {
    return <div className="section-body">Could not load provenance.</div>;
  }

  return (
    <div className="section">
      <div className="section-title">
        Provenance
        <span className="badge" style={{ marginLeft: 8 }}>
          {data.gap_count === 0
            ? "All facts recorded"
            : `${data.gap_count} facts not recorded`}
        </span>
      </div>

      <div className="section-body">
        <pre className="provenance-report">{data.markdown}</pre>

        <div className="row" style={{ gap: 8 }}>
          <button onClick={() => copy(data.markdown, "report")}>
            {copied === "report" ? "Copied" : "Copy report"}
          </button>
          <button
            onClick={() => prose.mutate()}
            disabled={prose.isPending}
          >
            {prose.isPending ? "Generating…" : "Generate prose"}
          </button>
        </div>

        {prose.data?.prose && (
          <>
            <p className="provenance-prose">{prose.data.prose}</p>
            <button onClick={() => copy(prose.data!.prose!, "prose")}>
              {copied === "prose" ? "Copied" : "Copy paragraph"}
            </button>
          </>
        )}

        {prose.data?.unavailable_reason && (
          <p className="muted">{prose.data.unavailable_reason}</p>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Mount it in the detail panel**

In `frontend/src/components/DetailPanel.tsx`, import it next to the existing
`Computations` import (line ~32):

```tsx
import { ProvenanceNarrative } from "./ProvenanceNarrative";
```

Then render it immediately after the `<Computations ... />` element around
line 824, passing the same object id that component receives.

- [ ] **Step 4: Lint and build**

```bash
cd frontend && npm run lint && npm run build
```

Expected: both clean. Fix any type errors against the real `request` helper
signature rather than casting.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/client.ts frontend/src/components/ProvenanceNarrative.tsx frontend/src/components/DetailPanel.tsx
git commit -m "Add the provenance narrative panel"
```

---

## Task 10: Verify against real data, not fixtures

**This task is not optional and cannot be satisfied by the test suite.**

CLAUDE.md records why: the Actions tab's suggestion rules passed a full green
suite while getting two things wrong that one look at a real project exposed
-- `protein.faa` counted as an alignable reference, and one assembly stored
twice counted as two. The tests were green because the fixtures already
looked the way the rules expected. Every fixture in Tasks 1-8 has the same
weakness.

The issue's own last acceptance criterion says the same thing:
*representative narrative output is manually reviewed against its underlying
facts.*

- [ ] **Step 1: Start the worktree stack**

```bash
./ops/worktree-up.sh
```

UI on 5273, API on 8100. Do not use plain `docker compose` from a worktree --
a `PreToolUse` hook blocks it, because it would silently repoint the main
5173 stack at this branch.

- [ ] **Step 2: Find a real object with a deep chain**

```bash
docker compose -p bioflow-worktree exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models.object import DataObject

async def main():
    await connect_to_mongo()
    objs = await DataObject.find(
        {'derived_from.1': {'\$exists': True}}
    ).limit(5).to_list()
    for o in objs:
        print(o.id, o.name, o.role, len(o.derived_from), sorted(o.facts)[:6])

asyncio.run(main())
"
```

If nothing comes back, run a real pipeline first (upload reads → QC → trim →
align → call variants) through the UI on 5273 and re-run this.

- [ ] **Step 3: Walk a real chain and read the output**

```bash
docker compose -p bioflow-worktree exec api python -c "
import asyncio
from beanie import PydanticObjectId
from app.db.client import connect_to_mongo
from app.services import provenance_report, provenance_walker

OBJECT_ID = 'PASTE_AN_ID_FROM_STEP_2'
OWNER = 'PASTE_ITS_OWNER'

async def main():
    await connect_to_mongo()
    chain = await provenance_walker.walk(PydanticObjectId(OBJECT_ID), owner=OWNER)
    print(provenance_report.render_markdown(chain))

asyncio.run(main())
"
```

- [ ] **Step 4: Check the output against the facts by hand**

Read the rendered report next to the object's real `facts` and its ancestors'.
Confirm each of these, and fix anything that fails before proceeding:

- Every step that genuinely ran appears. Cross-check against the object's
  History tab in the UI, which reads `records_for_object` independently.
- No sidecar (`.bai`, `.tbi`, `.fai`) appears anywhere.
- The reference appears under Materials, not as a processing step.
- Every version shown matches the `facts` value it came from — spot-check at
  least two against the database directly.
- Gaps correspond to facts that really are missing, not to facts the walker
  failed to find. **This is the most likely real bug**: an extraction path
  that silently misses a key renders as an honest-looking gap, which is
  exactly the failure mode that looks correct.

- [ ] **Step 5: Verify the panel in the browser**

Open `http://localhost:5273`, navigate to the same object, confirm the panel
matches the CLI output and that both copy buttons work.

- [ ] **Step 6: Verify the prose path end to end if a provider is configured**

Point `TaskSlot.PROVENANCE_NARRATIVE` at a provider in Settings → AI, click
"Generate prose", and read the paragraph against the report. Confirm no
version or tool appears in the paragraph that is absent from the report.

To confirm the containment check actually fires rather than merely existing,
temporarily point the slot at a model likely to embellish, or hand-test
`verify_containment` against a deliberately bad string:

```bash
docker compose -p bioflow-worktree exec api python -c "
import asyncio
from beanie import PydanticObjectId
from app.db.client import connect_to_mongo
from app.services import provenance_prompt, provenance_walker

OBJECT_ID = 'PASTE_AN_ID'
OWNER = 'PASTE_ITS_OWNER'

async def main():
    await connect_to_mongo()
    chain = await provenance_walker.walk(PydanticObjectId(OBJECT_ID), owner=OWNER)
    bad = 'Reads were aligned with bowtie2 9.9.9 and called with gatk 4.5.0.'
    print('rejection:', provenance_prompt.verify_containment(bad, chain))

asyncio.run(main())
"
```

Expected: a non-None rejection naming the unsupported token.

- [ ] **Step 7: Tear the stack down**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 8: Record what real data changed**

If Steps 4-6 exposed anything the fixtures missed, note it in the commit
message. That delta is the most valuable thing this task produces.

```bash
git commit --allow-empty -m "Verify provenance narratives against real project data"
```

---

## Task 11: Close out the backlog entry and the issue

Per CLAUDE.md: finishing the work is not finishing the entry. This has
already gone wrong three times in this repo — one stale entry advised
deleting `JobContext.extend_lease` as dead code when four handlers were
calling it.

**Files:**
- Modify: `docs/TODO.md` (remove the entry)
- Modify: `docs/TODO-done.md` (add it, in full)

- [ ] **Step 1: Move the entry**

Cut the `## More LLM usage: pipeline provenance narratives` entry from
`docs/TODO.md` (around line 701) and paste it into `docs/TODO-done.md`,
keeping the original body intact — the diagnosis explains why the code looks
the way it does.

Append ` — FIXED` to the heading and add a note directly under it covering:

- What shipped and where the code lives (the three service modules, the two
  routes, the slot, the panel).
- **What the implementation did differently from the entry.** At minimum:
  the entry frames steps as coming from facts, and the implementation
  anchors them on `produced_by_job` instead, because several appliers set a
  job while passing `facts=` with no provenance builder — a facts-first walk
  would have dropped those steps silently.
- That `JobType` does not exist (`Job.type` is a plain `str`), so the
  exhaustiveness test runs against `registry.all_handlers()` instead.
- Whatever Task 10 turned up against real data.
- What is deliberately **not** covered: project-level methods generation, and
  backfilling provenance for objects predating their step's builder.

- [ ] **Step 2: Verify no other entry references this work as pending**

```bash
grep -n -i "provenance narrative\|methods paragraph" docs/TODO.md
```

Expected: no matches remaining in `TODO.md`.

- [ ] **Step 3: Commit**

```bash
git add docs/TODO.md docs/TODO-done.md
git commit -m "Close out the provenance narrative TODO entry"
```

- [ ] **Step 4: Merge and push**

Once `./backend/run-worktree-tests.sh tests/ -q` is green and `main` is
clean, merge and push — per CLAUDE.md there is no review gate to wait on.

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Read the count. Then merge to `main` and push to `origin`.

- [ ] **Step 5: Update the issue**

Comment on [#16](https://github.com/syntheticgio/bioflow/issues/16) with what
shipped, what differed from the spec, and what was deferred. Close it.

---

## What this plan does not build

Carried from the spec so it does not get quietly re-scoped mid-implementation:

- **Project-level methods generation.** A different feature: it must decide
  what a project's terminal outputs are, deduplicate lineage across samples,
  and collapse "we did this to all 24 samples" into one sentence. It can
  build on this walker later.
- **Backfilling provenance for historical objects.** Objects predating a
  given `*_provenance` builder render `params_unrecorded` forever. That is
  the honest state; inventing facts retroactively is the one thing this
  feature must never do.
- **The TODO entry's sibling candidates** — explaining why a QC run failed a
  threshold, and prose next-step suggestions beside the Actions cards.
