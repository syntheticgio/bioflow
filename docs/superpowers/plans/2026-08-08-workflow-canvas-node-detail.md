# Workflow Canvas: Tool Selection, Multi-Valued Inputs, and Node Detail — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a workflow canvas node carry its own tool choice, parameters, and files — so an `align` node says *which* aligner, accepts several read files, and opens into a detail panel with a real parameter form.

**Architecture:** Ports become a function of the node (its chosen tool) rather than of its node type alone. That single change — a resolver, `ports_for(node)`, replacing direct reads of `spec.inputs`/`spec.outputs` — is what the other three features hang off. Multi-valued ports relax exactly one rule (one-wire-per-port) on both client and server. The detail panel is an HTML view swap, not an SVG transform. Binding files at input nodes fills the existing per-run `bindings` map; the definition still stores no object ids.

**Tech Stack:** Python 3.12 / FastAPI / Beanie (MongoDB) on the backend; React 18 / TypeScript / TanStack Query / hand-rolled SVG on the frontend. Backend tests with `pytest`; frontend pure-logic tests with `vitest`.

**Design note:** [docs/superpowers/specs/2026-08-08-workflow-canvas-node-detail-design.md](../specs/2026-08-08-workflow-canvas-node-detail-design.md)

---

## Before You Start

**You are working in a git worktree.** Two rules from `CLAUDE.md` matter constantly here, and both fail *silently* if ignored:

Run backend tests with the worktree runner, never `docker compose exec api`:

```bash
./backend/run-worktree-tests.sh tests/ -q
```

`docker compose exec api python -m pytest` from a worktree tests **main's** code, not yours, and reports success for a tree you did not change.

Bring up the app with the worktree script, never bare `docker compose`:

```bash
./ops/worktree-up.sh
```

That serves this branch's UI on **localhost:5273** and its API on 8100, leaving the main instance on 5173 alone. A `PreToolUse` hook blocks bare `docker compose` in a worktree for this reason.

Frontend tests:

```bash
cd frontend && npx vitest run src/lib/workflowGraph.test.ts
```

**Manual verification is the real test for anything visual.** There is no jsdom or component-testing setup in this repo and none is expected — that is why every rule worth checking automatically lives in `lib/workflowGraph.ts` (pure) rather than in the component.

---

## File Structure

**Backend — create:**

- `backend/app/pipelines/tool_choice.py` — the `ToolChoice` dataclass and the per-family option/port/schema resolvers. Separate from `node_types.py` because that file is already 789 lines and this is a distinct responsibility: `node_types.py` says what nodes exist, this says how a tool-parameterized one resolves.
- `backend/tests/pipelines/test_tool_choice.py`
- `backend/tests/services/test_workflow_multi_port.py`

**Backend — modify:**

- `backend/app/pipelines/node_types.py` — `PortSpec.multiple`, `NodeTypeSpec.tool_choice`, `ports_for()`; `align` gains a tool choice and a multi `reads` port.
- `backend/app/models/workflow.py` — `WorkflowNode.multiple` for input slots.
- `backend/app/services/workflow_service.py:96-119` — the `duplicate_wire` rule relaxes for multi ports; `_input_port`/`_spec_for` route through `ports_for`.
- `backend/app/services/workflow_binding.py:94-141` — resolve N candidates into a list for a multi port.
- `backend/app/services/workflow_orchestrator.py:140-150` — collect multi-port inputs as lists.
- `backend/app/api/v1/workflows.py:72-73,168-190` — serve `multiple` on ports and per-tool port sets.

**Frontend — create:**

- `frontend/src/components/workflow/NodeDetailPanel.tsx` — the double-click detail view.
- `frontend/src/components/workflow/ParamForm.tsx` — the generated parameter form, driven by backend field metadata.

**Frontend — modify:**

- `frontend/src/lib/workflowGraph.ts` — `portsFor()`, multi-aware `canConnect`, `edgesInvalidatedBy()`.
- `frontend/src/lib/workflowGraph.test.ts` — tests for all three.
- `frontend/src/api/types.ts:1851-1889` — `multiple` on `PortMeta`, `tool_choice` on `NodeTypeMeta`, `multiple` on `WorkflowNode`.
- `frontend/src/components/WorkflowCanvas.tsx` — project selector, node-side binding, double-click, tool-change edge dropping.
- `frontend/src/styles.css` — panel and multi-port styles.

**Why `components/workflow/` is a new directory:** `WorkflowCanvas.tsx` is 799 lines already. The panel and the form are self-contained and belong beside it rather than inside it.

---

## Task 1: `PortSpec.multiple` and the multi-port validation rule

The smallest complete slice: a port that accepts several wires, enforced on the server. No tool choice yet, no UI.

**Files:**
- Modify: `backend/app/pipelines/node_types.py:52-57` (PortSpec)
- Modify: `backend/app/services/workflow_service.py:108-119` (duplicate_wire)
- Test: `backend/tests/services/test_workflow_multi_port.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_workflow_multi_port.py`:

```python
"""Multi-valued input ports: several wires into one port.

The rule these exercise is deliberately narrow -- only the
one-wire-per-port check relaxes. Type checking still applies to every wire
independently, which is the half that would be easy to lose.
"""

from app.models.object import FormatKind, ObjectRole
from app.models.workflow import (
    PortType,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
)
from app.pipelines.node_types import NODE_TYPES, PortSpec
from app.services.workflow_service import validate_definition


def _reads_input(node_id: str) -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id,
        kind=WorkflowNodeKind.INPUT,
        label=node_id,
        accepts=PortType(format=FormatKind.FASTQ),
    )


def test_multiple_is_false_by_default():
    port = PortSpec("reads", PortType(format=FormatKind.FASTQ))
    assert port.multiple is False


def test_two_wires_into_a_multi_port_validate():
    """align.reads is multiple, so chunked read files all go in together."""
    definition = WorkflowDefinition(
        name="two reads files",
        owner="tester",
        nodes=[
            _reads_input("r1"),
            _reads_input("r2"),
            WorkflowNode(
                node_id="ref",
                kind=WorkflowNodeKind.INPUT,
                label="ref",
                accepts=PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
            WorkflowNode(
                node_id="align_1", kind=WorkflowNodeKind.ACTION, node_type="align"
            ),
        ],
        edges=[
            WorkflowEdge(from_node="r1", from_port="object", to_node="align_1", to_port="reads"),
            WorkflowEdge(from_node="r2", from_port="object", to_node="align_1", to_port="reads"),
            WorkflowEdge(from_node="ref", from_port="object", to_node="align_1", to_port="reference"),
        ],
    )
    assert validate_definition(definition) == []


def test_two_wires_into_a_scalar_port_still_fail():
    """The relaxation is per-port, not global."""
    definition = WorkflowDefinition(
        name="two references",
        owner="tester",
        nodes=[
            _reads_input("r1"),
            WorkflowNode(
                node_id="ref_a",
                kind=WorkflowNodeKind.INPUT,
                label="ref_a",
                accepts=PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
            WorkflowNode(
                node_id="ref_b",
                kind=WorkflowNodeKind.INPUT,
                label="ref_b",
                accepts=PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
            WorkflowNode(
                node_id="align_1", kind=WorkflowNodeKind.ACTION, node_type="align"
            ),
        ],
        edges=[
            WorkflowEdge(from_node="r1", from_port="object", to_node="align_1", to_port="reads"),
            WorkflowEdge(from_node="ref_a", from_port="object", to_node="align_1", to_port="reference"),
            WorkflowEdge(from_node="ref_b", from_port="object", to_node="align_1", to_port="reference"),
        ],
    )
    codes = [e.code for e in validate_definition(definition)]
    assert "duplicate_wire" in codes


def test_type_checking_still_applies_to_every_wire_of_a_multi_port():
    """A multi port is not an untyped port."""
    definition = WorkflowDefinition(
        name="a bam among the reads",
        owner="tester",
        nodes=[
            _reads_input("r1"),
            WorkflowNode(
                node_id="bam",
                kind=WorkflowNodeKind.INPUT,
                label="bam",
                accepts=PortType(format=FormatKind.BAM, role=ObjectRole.ALIGNMENT),
            ),
            WorkflowNode(
                node_id="ref",
                kind=WorkflowNodeKind.INPUT,
                label="ref",
                accepts=PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
            WorkflowNode(
                node_id="align_1", kind=WorkflowNodeKind.ACTION, node_type="align"
            ),
        ],
        edges=[
            WorkflowEdge(from_node="r1", from_port="object", to_node="align_1", to_port="reads"),
            WorkflowEdge(from_node="bam", from_port="object", to_node="align_1", to_port="reads"),
            WorkflowEdge(from_node="ref", from_port="object", to_node="align_1", to_port="reference"),
        ],
    )
    codes = [e.code for e in validate_definition(definition)]
    assert "type_mismatch" in codes


def test_align_reads_is_multiple():
    reads = next(p for p in NODE_TYPES["align"].inputs if p.name == "reads")
    assert reads.multiple is True
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_workflow_multi_port.py -q
```

Expected: FAIL — `AttributeError: 'PortSpec' object has no attribute 'multiple'`.

- [ ] **Step 3: Add `multiple` to `PortSpec`**

In `backend/app/pipelines/node_types.py`, replace the `PortSpec` dataclass (currently lines 52-56):

```python
@dataclass(frozen=True)
class PortSpec:
    name: str
    type: PortType
    required: bool = True
    # Whether this port accepts several incoming wires, collected into a list
    # for the launcher. Only the one-wire-per-port rule relaxes -- type
    # checking still applies to each wire independently, which is what keeps a
    # multi port from becoming an untyped one.
    #
    # `continuity_qc`'s hifi_bam/nano_bam and `differential_expression`'s
    # counts are the other two ports whose launchers genuinely take lists
    # today (both currently smuggle the set through `params`). They are left
    # scalar here deliberately: each needs its own decision about how the
    # per-sample design travels, and neither is what #94 asks for.
    multiple: bool = False
```

- [ ] **Step 4: Mark `align.reads` as multiple**

In the same file, in the `"align"` entry of `NODE_TYPES` (around line 314), replace the `reads` port:

```python
            # Several read files go in together -- chunked/split reads, not
            # mates. `mate` beside it stays scalar: R2 is one file with a
            # specific meaning, and collapsing the two concepts would lose it.
            PortSpec("reads", PortType(format=FormatKind.FASTQ), multiple=True),
```

- [ ] **Step 5: Relax the duplicate-wire rule**

In `backend/app/services/workflow_service.py`, replace the `duplicate_wire` block (lines 108-119):

```python
        key = (edge.to_node, edge.to_port)
        # A multi port collects several wires; every other port takes one.
        # Checked here rather than by skipping the bookkeeping entirely,
        # because `wired` is also what the required-input check below reads --
        # a multi port with one wire must still count as satisfied.
        if key in wired and not port.multiple:
            errors.append(
                ValidationError(
                    "duplicate_wire",
                    f"Port {edge.to_port!r} on {target.node_id!r} already has an input.",
                    node_id=target.node_id,
                    port=edge.to_port,
                )
            )
            continue
        wired.add(key)
```

- [ ] **Step 6: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/test_workflow_multi_port.py -q
```

Expected: PASS, 5 passed.

- [ ] **Step 7: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the count, not just the exit code. If `tests/pipelines/test_node_types.py` fails, a port-shape assertion there needs the new field — fix it rather than the production code.

- [ ] **Step 8: Commit**

```bash
git add backend/app/pipelines/node_types.py backend/app/services/workflow_service.py backend/tests/services/test_workflow_multi_port.py
git commit -m "feat(workflow): multi-valued input ports, and align.reads becomes one

Only the one-wire-per-port rule relaxes; type checking still applies per
wire. Part of #94."
```

---

## Task 2: Binding and launching a multi port

The validator accepts several wires; now the run has to actually pass them along. Without this, a two-reads graph saves and then launches with one file.

**Files:**
- Modify: `backend/app/services/workflow_binding.py:57-143`
- Modify: `backend/app/services/workflow_orchestrator.py:140-150`
- Test: `backend/tests/services/test_workflow_multi_port.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_workflow_multi_port.py`:

```python
from beanie import PydanticObjectId

from app.services.workflow_binding import OutputCandidate, bind_downstream_inputs


def test_multi_port_binds_a_list():
    """Two upstream nodes feeding one multi port produce a list, not a
    last-writer-wins scalar."""
    definition = WorkflowDefinition(
        name="chunked reads",
        owner="tester",
        nodes=[
            WorkflowNode(
                node_id="dl", kind=WorkflowNodeKind.ACTION, node_type="download_sra"
            ),
            WorkflowNode(
                node_id="align_1", kind=WorkflowNodeKind.ACTION, node_type="align"
            ),
        ],
        edges=[
            WorkflowEdge(
                from_node="dl", from_port="reads", to_node="align_1", to_port="reads"
            ),
        ],
    )
    first, second = PydanticObjectId(), PydanticObjectId()
    bound = bind_downstream_inputs(
        definition,
        "dl",
        [
            OutputCandidate(object_id=first, format=FormatKind.FASTQ, name="chunk1.fq"),
            OutputCandidate(object_id=second, format=FormatKind.FASTQ, name="chunk2.fq"),
        ],
    )
    assert bound[("align_1", "reads")] == [first, second]


def test_scalar_port_still_binds_a_bare_id():
    """The list shape is per-port, so existing consumers are untouched."""
    definition = WorkflowDefinition(
        name="one bam",
        owner="tester",
        nodes=[
            WorkflowNode(
                node_id="align_1", kind=WorkflowNodeKind.ACTION, node_type="align"
            ),
            WorkflowNode(
                node_id="stats", kind=WorkflowNodeKind.ACTION, node_type="bam_stats"
            ),
        ],
        edges=[
            WorkflowEdge(
                from_node="align_1",
                from_port="alignment",
                to_node="stats",
                to_port="alignment",
            ),
        ],
    )
    bam = PydanticObjectId()
    bound = bind_downstream_inputs(
        definition,
        "align_1",
        [
            OutputCandidate(
                object_id=bam,
                format=FormatKind.BAM,
                role=ObjectRole.ALIGNMENT,
                name="x.bam",
            )
        ],
    )
    assert bound[("stats", "alignment")] == bam
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_workflow_multi_port.py -q
```

Expected: FAIL on `test_multi_port_binds_a_list` — the value is a single `PydanticObjectId`, not a list.

- [ ] **Step 3: Widen the binding return type**

In `backend/app/services/workflow_binding.py`, change the signature and docstring of `bind_downstream_inputs` (line 57):

```python
def bind_downstream_inputs(
    definition: WorkflowDefinition,
    node_id: str,
    candidates: list[OutputCandidate],
    *,
    outputs_by_port: dict[str, PydanticObjectId] | None = None,
    paired: bool = False,
) -> dict[tuple[str, str], PydanticObjectId | list[PydanticObjectId]]:
    """Map (downstream node, input port) -> object id, for one finished node.

    A *multi* port maps to a list instead of a bare id -- every
    type-compatible candidate, in the order the source produced them. The
    union return type rather than always-a-list is deliberate: every existing
    consumer reads scalars, and making them all unwrap a one-element list to
    gain nothing would be a large diff with no behaviour in it.

    `outputs_by_port` names which produced object corresponds to which declared
    output port. It is what makes resolution by *name*: with it, a node
    producing an R1 and an R2 binds each to the right port. Without it -- the
    common single-output case, and the only shape most node types have -- the
    lone type-compatible candidate is unambiguous and is used directly.

    A candidate whose type the target port rejects is never bound. That check
    is validation rather than selection: the graph validator already rejects a
    mistyped wire at save time, so failing it here means a definition that
    predates a registry change, and binding anyway would hand a tool a file it
    cannot read.
    """
```

- [ ] **Step 4: Bind every candidate for a multi port**

In the same function, insert this branch immediately after the `if _output_port_type(source, edge.from_port) is None: continue` guard (currently line 105), *before* the `chosen: OutputCandidate | None = None` line:

```python
        # A multi port takes everything type-compatible rather than choosing.
        # Handled before the selection logic below because that logic exists
        # to resolve ambiguity -- and a multi port has none to resolve: "two
        # candidates" is the answer, not a problem.
        if port.multiple:
            matching = [c for c in candidates if port.type.accepts(c.format, c.role)]
            if matching:
                bound[(edge.to_node, edge.to_port)] = [c.object_id for c in matching]
            continue
```

- [ ] **Step 5: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/test_workflow_multi_port.py -q
```

Expected: PASS, 7 passed.

- [ ] **Step 6: Pass lists through the orchestrator**

Read `backend/app/services/workflow_orchestrator.py` around lines 140-150 first — the exact surrounding code matters. The block builds an `inputs` dict from bindings and resolved upstream outputs:

```python
                inputs[edge.to_port] = bindings[source.node_id]
            ...
            inputs[edge.to_port] = resolved[(edge.to_node, edge.to_port)]
```

Both already assign whatever the binding holds, so a list flows through unchanged. **Verify this rather than assuming it:** add a temporary print or read the surrounding lines to confirm neither branch coerces to a scalar (e.g. `[0]`, `next(iter(...))`, or a `PydanticObjectId(...)` cast). If either does, remove the coercion. If neither does, no edit is needed — say so in the commit message rather than inventing a change.

- [ ] **Step 7: Make `_launch_align` accept a list**

In `backend/app/pipelines/node_types.py`, replace `_launch_align` (lines 93-100):

```python
async def _launch_align(*, inputs: dict, params: dict, owner: str):
    # `reads` is a multi port, so it arrives as a list. The launcher itself
    # takes one object_id: extra read files are passed through params for the
    # runner to concatenate, which is what "they all go in together" means --
    # one alignment over every chunk, not one run per file.
    reads = inputs["reads"]
    if isinstance(reads, list):
        primary, extra = reads[0], reads[1:]
    else:
        primary, extra = reads, []
    return await pipeline_service.launch_alignment(
        object_id=primary,
        reference_id=inputs["reference"],
        owner=owner,
        mate_object_id=inputs.get("mate"),
        params={**params, "extra_reads": [str(o) for o in extra]} if extra else params,
    )
```

- [ ] **Step 8: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the count.

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/workflow_binding.py backend/app/pipelines/node_types.py backend/app/services/workflow_orchestrator.py
git commit -m "feat(workflow): bind and launch multi-valued ports as lists

Scalar ports keep binding bare ids -- the union return type avoids a
no-behaviour diff across every existing consumer. Part of #94."
```

> **Note for the reviewer:** `extra_reads` is consumed by nothing yet. Step 7 gets the file ids to the launcher; making the align runner actually concatenate them is Task 3.

---

## Task 3: The align runner consumes `extra_reads`

Task 2 hands the launcher a list. Without this task the extra files are silently dropped — the exact silent-skip shape `CLAUDE.md` warns about.

**Files:**
- Modify: `backend/app/services/pipeline_service.py` (`launch_alignment`)
- Test: `backend/tests/pipelines/test_align_extra_reads.py` (create)

- [ ] **Step 1: Read the current launcher**

```bash
sed -n '/^async def launch_alignment/,/^async def /p' backend/app/services/pipeline_service.py
```

Note how `object_id` reaches the job payload, and how `align_runner` receives its read path. You need the exact payload key names for the next step — do not guess them.

- [ ] **Step 2: Write the failing test**

Create `backend/tests/pipelines/test_align_extra_reads.py`. Replace `PAYLOAD_KEY` with the key you found in Step 1:

```python
"""Extra read files reach the align job's payload.

The failure this guards against is silent: a two-chunk alignment that runs
happily over chunk one and never mentions chunk two. Nothing raises, and the
BAM looks fine -- it is just missing half the reads.
"""

import pytest

from app.models.object import FormatKind
from app.services import pipeline_service


@pytest.mark.asyncio
async def test_extra_reads_reach_the_payload(monkeypatch, ready_fastq_object, ready_reference_object):
    captured = {}

    async def fake_enqueue(*args, **kwargs):
        captured.update(kwargs.get("payload", {}))
        return None

    monkeypatch.setattr(pipeline_service, "enqueue_job", fake_enqueue)

    await pipeline_service.launch_alignment(
        object_id=ready_fastq_object.id,
        reference_id=ready_reference_object.id,
        owner="tester",
        params={"extra_reads": ["deadbeefdeadbeefdeadbeef"]},
    )

    assert captured.get("extra_reads") == ["deadbeefdeadbeefdeadbeef"]
```

**Before running:** check `backend/tests/conftest.py` for the actual fixture names providing a ready FASTQ and a ready reference object, and for how other tests in `tests/pipelines/` patch the enqueue seam. Use those names — the two above are placeholders for whatever this repo already calls them, and inventing new fixtures when equivalents exist is how a suite grows a second way to do everything.

- [ ] **Step 3: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_align_extra_reads.py -q
```

Expected: FAIL — `extra_reads` absent from the payload.

- [ ] **Step 4: Thread `extra_reads` into the payload**

In `launch_alignment`, pass `extra_reads` from `params` into the job payload alongside the primary read object. Follow the surrounding style exactly — if other optional payload fields are set conditionally, set this one the same way.

- [ ] **Step 5: Consume it in the runner**

In `backend/app/pipelines/align_runner.py`, resolve each extra read id to its path and pass every read file to the aligner in one invocation. Every aligner in this registry takes multiple read files as positional arguments, so this is an argument-list change, not a concatenation step.

**Verify before writing:** confirm that claim for the specific aligners here by reading how the command is currently built. If any aligner in `REGISTRY` cannot take several read files positionally, stop and report it — that aligner needs either a concatenation step or an exclusion from the multi port, and picking one silently is the wrong call to make inside a task.

- [ ] **Step 6: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_align_extra_reads.py -q
```

Expected: PASS.

- [ ] **Step 7: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the count.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/pipelines/align_runner.py backend/tests/pipelines/test_align_extra_reads.py
git commit -m "feat(align): pass extra read files through to the aligner

Chunked reads all go into one alignment. Part of #94."
```

---

## Task 4: `ToolChoice` and `ports_for()`

The core of the feature: a node type whose ports depend on the tool the node has chosen.

**Files:**
- Create: `backend/app/pipelines/tool_choice.py`
- Create: `backend/tests/pipelines/test_tool_choice.py`
- Modify: `backend/app/pipelines/node_types.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_tool_choice.py`:

```python
"""Tool-parameterized node types.

Per CLAUDE.md's registry rules this is the third category -- keys owned by
something outside any one enum -- so the invariant runs from the registry
outward: every option a node type offers must resolve to a port set.
"""

import pytest

from app.models.object import FormatKind, ObjectRole
from app.models.workflow import WorkflowNode, WorkflowNodeKind
from app.pipelines.node_types import NODE_TYPES, ports_for


def _align_node(aligner: str | None = None) -> WorkflowNode:
    params = {"aligner": aligner} if aligner else {}
    return WorkflowNode(
        node_id="align_1",
        kind=WorkflowNodeKind.ACTION,
        node_type="align",
        params=params,
    )


def test_align_declares_a_tool_choice():
    choice = NODE_TYPES["align"].tool_choice
    assert choice is not None
    assert choice.param_key == "aligner"
    assert "minimap2" in [o.value for o in choice.options]
    assert "star" in [o.value for o in choice.options]


def test_star_gains_an_annotation_port():
    """STAR builds an annotation-aware index; the others have no such concept
    (see aligners.index_role, which raises for a non-STAR annotated index)."""
    inputs, _ = ports_for(_align_node("star"))
    annotation = next((p for p in inputs if p.name == "annotation"), None)
    assert annotation is not None
    assert annotation.type.format is FormatKind.GTF
    # Optional: STAR supports both index shapes deliberately, and a run
    # without an annotation is a normal run, not a broken one.
    assert annotation.required is False


def test_minimap2_has_no_annotation_port():
    inputs, _ = ports_for(_align_node("minimap2"))
    assert all(p.name != "annotation" for p in inputs)


def test_every_aligner_keeps_the_shared_ports():
    for option in NODE_TYPES["align"].tool_choice.options:
        inputs, outputs = ports_for(_align_node(option.value))
        names = {p.name for p in inputs}
        assert {"reads", "mate", "reference"} <= names, option.value
        assert [p.name for p in outputs] == ["alignment"], option.value


def test_reads_stays_multiple_for_every_aligner():
    for option in NODE_TYPES["align"].tool_choice.options:
        inputs, _ = ports_for(_align_node(option.value))
        reads = next(p for p in inputs if p.name == "reads")
        assert reads.multiple is True, option.value


def test_an_unset_tool_falls_back_to_the_default():
    """A node dropped from the palette and not yet touched still has ports --
    otherwise it could not be wired at all."""
    inputs, outputs = ports_for(_align_node(None))
    assert {p.name for p in inputs} >= {"reads", "reference"}
    assert outputs


def test_an_unknown_tool_falls_back_to_the_default():
    """A definition saved before an aligner was removed still opens."""
    inputs, _ = ports_for(_align_node("no-such-aligner"))
    assert {p.name for p in inputs} >= {"reads", "reference"}


def test_a_node_type_without_a_tool_choice_uses_its_static_ports():
    node = WorkflowNode(
        node_id="qc_1", kind=WorkflowNodeKind.ACTION, node_type="qc"
    )
    inputs, outputs = ports_for(node)
    assert [p.name for p in inputs] == ["reads"]
    assert outputs == ()


def test_every_option_of_every_tool_choice_resolves():
    """The exhaustiveness invariant. A tool offered in a dropdown that no
    resolver handles is a node the canvas can place and never wire."""
    for node_type, spec in NODE_TYPES.items():
        if spec.tool_choice is None:
            continue
        for option in spec.tool_choice.options:
            node = WorkflowNode(
                node_id="n",
                kind=WorkflowNodeKind.ACTION,
                node_type=node_type,
                params={spec.tool_choice.param_key: option.value},
            )
            inputs, outputs = ports_for(node)
            assert inputs or outputs, f"{node_type}/{option.value} resolves to no ports"
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tool_choice.py -q
```

Expected: FAIL — `ImportError: cannot import name 'ports_for'`.

- [ ] **Step 3: Create the tool-choice module**

Create `backend/app/pipelines/tool_choice.py`:

```python
"""Node types parameterized by a tool.

`align` is one node type, not seven, and which aligner it runs lives in
`node.params["aligner"]` -- where `launch_alignment` already reads it from.
That is the whole reason this is a params key rather than a new model field:
the launchers already work this way, and `workflow_derive` already recovers
the tool from the PipelineRun.

The port set follows from the tool. STAR is the case that forces it: it can
build an annotation-aware index (see `aligners.STAR_ANNOTATED_DIR_SUFFIX`,
and `index_role`, which raises for a non-STAR annotated index), so a STAR
node has a GTF port that a minimap2 node has no meaning for.

Kept out of `node_types.py` because that file already carries the whole
registry at 789 lines and this is a separate responsibility: that file says
what nodes exist, this says how a parameterized one resolves.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.models.object import FormatKind, ObjectRole
from app.models.workflow import PortType


@dataclass(frozen=True)
class ToolOption:
    value: str
    label: str


@dataclass(frozen=True)
class ToolChoice:
    """Which tool a node runs, and what that implies.

    `param_key` names where the choice lives in `node.params`, so this stays
    aligned with the launcher rather than duplicating it -- `align` reads
    `aligner`, `call_variants` reads `caller`.
    """

    param_key: str
    options: tuple[ToolOption, ...]
    default: str
    # (base_inputs, base_outputs, tool) -> (inputs, outputs). Takes the static
    # tuples so a resolver expresses the *difference* a tool makes rather than
    # restating every shared port -- which is what would drift.
    resolve: Callable


def _aligner_options() -> tuple[ToolOption, ...]:
    """Built from the aligner registry, not hand-listed.

    A second list of aligners here is the list nobody updates -- the exact
    failure `aligner_registry`'s docstring says it was created to end.
    """
    from app.pipelines.aligners import Aligner

    labels = {
        Aligner.MINIMAP2: "minimap2 (long reads)",
        Aligner.BWA_MEM2: "bwa-mem2 (short reads)",
        Aligner.BOWTIE2: "bowtie2 (short reads)",
        Aligner.HISAT2: "HISAT2 (spliced)",
        Aligner.STAR: "STAR (spliced, RNA-seq)",
        Aligner.WINNOWMAP: "Winnowmap (repetitive)",
    }
    return tuple(
        ToolOption(value=a.value, label=labels.get(a, a.value)) for a in Aligner
    )


def _resolve_align_ports(base_inputs, base_outputs, tool: str):
    """STAR alone gains an annotation port; every other aligner is the base set."""
    from app.pipelines.node_types import PortSpec

    if tool != "star":
        return base_inputs, base_outputs
    annotation = PortSpec(
        "annotation",
        PortType(format=FormatKind.GTF),
        # Optional: STAR supports an index with or without an annotation, and
        # both are legitimate. Required here would refuse a genomic STAR run
        # that works.
        required=False,
    )
    return (*base_inputs, annotation), base_outputs


ALIGN_TOOL_CHOICE = ToolChoice(
    param_key="aligner",
    options=_aligner_options(),
    default="minimap2",
    resolve=_resolve_align_ports,
)
```

- [ ] **Step 4: Add `tool_choice` and `ports_for` to the registry**

In `backend/app/pipelines/node_types.py`, add to the `NodeTypeSpec` dataclass, after `run_tool`:

```python
    # Set when this node type is parameterized by a tool -- which aligner,
    # which caller. The chosen tool lives in `node.params[param_key]`, and the
    # port set follows from it, so ports are resolved per *node* via
    # `ports_for` rather than read off `spec.inputs` directly. Every read of
    # `.inputs`/`.outputs` outside this module should go through `ports_for`.
    tool_choice: "ToolChoice | None" = None
```

Add the import near the top, after the `PortType` import:

```python
from app.pipelines.tool_choice import ALIGN_TOOL_CHOICE, ToolChoice
```

Add `tool_choice=ALIGN_TOOL_CHOICE,` to the `"align"` entry of `NODE_TYPES`, immediately after its `run_kind=RunKind.ALIGNMENT,` line.

Add this function at the end of the file, before `launch_function_names`:

```python
def ports_for(node) -> tuple[tuple[PortSpec, ...], tuple[PortSpec, ...]]:
    """The (inputs, outputs) for one node, given the tool it has chosen.

    Every caller that used to read `spec.inputs`/`spec.outputs` should come
    here instead: a tool-parameterized node's real port set is not on its spec.
    Node types without a `tool_choice` -- most of them -- get their static
    tuples back unchanged, so this is a safe blanket replacement.

    An unset or unrecognized tool falls back to the default rather than
    raising. A node dropped from the palette has no tool until the resolver
    supplies one, and a definition saved before an aligner was removed must
    still open -- in both cases ports that exist beat an exception.
    """
    spec = NODE_TYPES.get(node.node_type) if node.node_type else None
    if spec is None:
        return (), ()
    choice = spec.tool_choice
    if choice is None:
        return spec.inputs, spec.outputs
    tool = node.params.get(choice.param_key) or choice.default
    if tool not in {o.value for o in choice.options}:
        tool = choice.default
    return choice.resolve(spec.inputs, spec.outputs, tool)
```

- [ ] **Step 5: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tool_choice.py -q
```

Expected: PASS, 9 passed.

- [ ] **Step 6: Route the validator through `ports_for`**

In `backend/app/services/workflow_service.py`, replace `_input_port` (lines 35-36):

```python
def _input_port(node: WorkflowNode, name: str) -> PortSpec | None:
    """Looked up per *node*, not per spec: a tool-parameterized node's ports
    depend on the tool it chose."""
    inputs, _ = ports_for(node)
    return next((p for p in inputs if p.name == name), None)
```

Update its two call sites to pass the node instead of the spec — `_input_port(target, edge.to_port)` at line 96, and the required-input loop at lines 149-150:

```python
    for node in definition.nodes:
        spec = _spec_for(node)
        if spec is None:
            continue
        inputs, _ = ports_for(node)
        for port in inputs:
            if port.required and (node.node_id, port.name) not in wired:
```

Replace the body of `_output_type` (lines 49-51) with:

```python
    _, outputs = ports_for(node)
    port = next((p for p in outputs if p.name == port_name), None)
    return port.type if port else None
```

Add the import: `from app.pipelines.node_types import NODE_TYPES, NodeTypeSpec, PortSpec, ports_for`.

- [ ] **Step 7: Route the binder through `ports_for`**

In `backend/app/services/workflow_binding.py`, replace the target-port lookup (lines 94-99):

```python
        target_inputs, _ = ports_for(target)
        port = next((p for p in target_inputs if p.name == edge.to_port), None)
        if port is None:
            continue
```

and the mate lookup inside the `paired` branch (lines 120-122):

```python
            mate_port = next(
                (p for p in target_inputs if p.name == "mate"), None
            )
```

and `_output_port_type`'s body (lines 50-54):

```python
    _, outputs = ports_for(node)
    port = next((p for p in outputs if p.name == port_name), None)
    return port.type if port else None
```

Import `ports_for` from `app.pipelines.node_types`. Remove the now-unused `target_spec` lookup if nothing else in the loop reads it — check before deleting.

- [ ] **Step 8: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the count against the pre-task baseline.

- [ ] **Step 9: Commit**

```bash
git add backend/app/pipelines/tool_choice.py backend/app/pipelines/node_types.py backend/app/services/workflow_service.py backend/app/services/workflow_binding.py backend/tests/pipelines/test_tool_choice.py
git commit -m "feat(workflow): ports resolve per node from its chosen tool

STAR gains an annotation port; every other aligner keeps the base set.
Validator and binder both route through ports_for. Part of #94."
```

---

## Task 5: Serve tool choices and per-tool ports from the API

**Files:**
- Modify: `backend/app/api/v1/workflows.py:64-80,168-190`
- Test: `backend/tests/api/test_workflow_node_types.py` (create or extend)

- [ ] **Step 1: Read the current endpoint**

```bash
sed -n '60,95p;165,195p' backend/app/api/v1/workflows.py
```

Note the exact `PortOut` and `NodeTypeOut` field names and the `ports()` helper.

- [ ] **Step 2: Write the failing test**

Create `backend/tests/api/test_workflow_node_types.py` (if it exists, append these). Match the async client fixture other tests in `tests/api/` use — check one first:

```python
"""The canvas builds its palette and its node shapes from this endpoint.

Per-tool port sets are served alongside the default so changing an aligner
re-shapes a node locally, with no round trip -- the canvas has to redraw on
a dropdown change, and a fetch per keystroke would show ports lagging the
selection.
"""

import pytest


@pytest.mark.asyncio
async def test_align_serves_its_tool_choice(client):
    response = await client.get("/api/v1/workflows/node-types")
    assert response.status_code == 200
    align = next(n for n in response.json() if n["node_type"] == "align")

    choice = align["tool_choice"]
    assert choice["param_key"] == "aligner"
    assert choice["default"] == "minimap2"
    values = [o["value"] for o in choice["options"]]
    assert "star" in values and "minimap2" in values


@pytest.mark.asyncio
async def test_align_serves_per_tool_ports(client):
    response = await client.get("/api/v1/workflows/node-types")
    align = next(n for n in response.json() if n["node_type"] == "align")

    star = align["ports_by_tool"]["star"]
    assert any(p["name"] == "annotation" for p in star["inputs"])

    minimap2 = align["ports_by_tool"]["minimap2"]
    assert all(p["name"] != "annotation" for p in minimap2["inputs"])


@pytest.mark.asyncio
async def test_ports_carry_multiple(client):
    response = await client.get("/api/v1/workflows/node-types")
    align = next(n for n in response.json() if n["node_type"] == "align")
    reads = next(p for p in align["inputs"] if p["name"] == "reads")
    assert reads["multiple"] is True


@pytest.mark.asyncio
async def test_a_node_type_without_a_tool_choice_serves_null(client):
    response = await client.get("/api/v1/workflows/node-types")
    qc = next(n for n in response.json() if n["node_type"] == "qc")
    assert qc["tool_choice"] is None
    assert qc["ports_by_tool"] == {}
```

- [ ] **Step 3: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/api/test_workflow_node_types.py -q
```

Expected: FAIL — `KeyError: 'tool_choice'`.

- [ ] **Step 4: Extend the response models**

In `backend/app/api/v1/workflows.py`, add `multiple` to `PortOut`:

```python
class PortOut(BaseModel):
    name: str
    type: PortTypeOut
    required: bool
    # Whether this port takes several wires. The canvas needs it to know when
    # to allow a second connection rather than refusing it.
    multiple: bool = False
```

Add above `NodeTypeOut`:

```python
class ToolOptionOut(BaseModel):
    value: str
    label: str


class ToolChoiceOut(BaseModel):
    param_key: str
    options: list[ToolOptionOut]
    default: str


class PortSetOut(BaseModel):
    inputs: list[PortOut]
    outputs: list[PortOut]
```

And to `NodeTypeOut` (line 72-73 area):

```python
    inputs: list[PortOut]
    outputs: list[PortOut]
    # None for the node types that run exactly one tool -- most of them.
    tool_choice: ToolChoiceOut | None = None
    # Every option's port set, keyed by tool value. Empty when there is no
    # choice. Served eagerly so the canvas re-shapes a node on a dropdown
    # change without a round trip; the payload is small (six aligners, four
    # ports each) and a fetch-per-change would show ports lagging the
    # selection.
    ports_by_tool: dict[str, PortSetOut] = {}
```

- [ ] **Step 5: Populate them in the endpoint**

In the `/node-types` handler (around line 168-190), extend the per-spec construction. Keep the existing `ports()` helper and add `multiple=p.multiple` to whatever it builds:

```python
        choice = spec.tool_choice
        by_tool: dict[str, PortSetOut] = {}
        if choice is not None:
            for option in choice.options:
                probe = WorkflowNode(
                    node_id="probe",
                    kind=WorkflowNodeKind.ACTION,
                    node_type=node_type,
                    params={choice.param_key: option.value},
                )
                tool_inputs, tool_outputs = ports_for(probe)
                by_tool[option.value] = PortSetOut(
                    inputs=ports(tool_inputs), outputs=ports(tool_outputs)
                )
```

and pass into `NodeTypeOut`:

```python
            tool_choice=(
                ToolChoiceOut(
                    param_key=choice.param_key,
                    options=[
                        ToolOptionOut(value=o.value, label=o.label)
                        for o in choice.options
                    ],
                    default=choice.default,
                )
                if choice
                else None
            ),
            ports_by_tool=by_tool,
```

The top-level `inputs`/`outputs` stay the default set — build them from a probe node with no tool set, so they match what a freshly-dropped node has.

- [ ] **Step 6: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/api/test_workflow_node_types.py -q
```

Expected: PASS, 4 passed.

- [ ] **Step 7: Add a parameter-schema endpoint**

The detail panel needs the field metadata `AlignDialog` already renders. Add to the same router:

```python
@router.get("/tool-schema/{node_type}/{tool}")
async def tool_schema(node_type: str, tool: str) -> dict:
    """The parameter form for one tool of one node type.

    Served rather than duplicated in the frontend for the reason
    `aligner_registry`'s docstring gives: a second copy of the field list is
    the copy nobody updates.
    """
    spec = NODE_TYPES.get(node_type)
    if spec is None or spec.tool_choice is None:
        raise NotFoundError(f"No tool-parameterized node type {node_type!r}.")
    if node_type == "align":
        from app.pipelines.aligner_registry import schema_for
        from app.pipelines.aligners import Aligner

        try:
            return schema_for(Aligner(tool))
        except ValueError as exc:
            raise NotFoundError(f"No aligner {tool!r}.") from exc
    raise NotFoundError(f"No schema for {node_type!r}.")
```

Declare it before `/{definition_id}`, for the reason the file's own comment at line 219 gives about `/node-types`.

- [ ] **Step 8: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the count.

- [ ] **Step 9: Commit**

```bash
git add backend/app/api/v1/workflows.py backend/tests/api/test_workflow_node_types.py
git commit -m "feat(api): serve tool choices, per-tool ports, and tool schemas

Part of #94."
```

---

## Task 6: Frontend graph rules — `portsFor`, multi-aware `canConnect`, `edgesInvalidatedBy`

The client mirror of Tasks 1 and 4. Pure, so it is the one part of the UI with tests.

**Files:**
- Modify: `frontend/src/api/types.ts:1851-1889`
- Modify: `frontend/src/lib/workflowGraph.ts`
- Test: `frontend/src/lib/workflowGraph.test.ts`

- [ ] **Step 1: Extend the API types**

In `frontend/src/api/types.ts`, replace the `PortMeta`/`NodeTypeMeta` block (lines 1856-1870):

```ts
export interface PortMeta {
  name: string;
  type: PortType;
  required: boolean;
  /** Whether this port takes several wires, collected into a list for the
   *  launcher. Only the one-wire-per-port rule relaxes -- type checking still
   *  applies to each wire. */
  multiple?: boolean;
}

export interface ToolOption {
  value: string;
  label: string;
}

export interface ToolChoice {
  /** Where the chosen tool lives in `node.params`. */
  param_key: string;
  options: ToolOption[];
  default: string;
}

export interface PortSet {
  inputs: PortMeta[];
  outputs: PortMeta[];
}

/** One entry of the canvas palette, served by `/workflows/node-types`.
 *  Generated from the backend registry rather than hand-listed here, so a tool
 *  added there reaches the canvas without a second edit. */
export interface NodeTypeMeta {
  node_type: string;
  label: string;
  /** The default port set -- what a freshly-dropped node has. */
  inputs: PortMeta[];
  outputs: PortMeta[];
  /** Null for node types that run exactly one tool. */
  tool_choice?: ToolChoice | null;
  /** Every option's ports, keyed by tool value. Lets the canvas re-shape a
   *  node on a dropdown change with no round trip. */
  ports_by_tool?: Record<string, PortSet>;
}
```

And add to `WorkflowNode` (after `accepts`):

```ts
  /** INPUT only. A slot that binds several files, whose single outgoing wire
   *  carries the set. May only feed a multi port. */
  multiple?: boolean;
```

- [ ] **Step 2: Write the failing tests**

Append to `frontend/src/lib/workflowGraph.test.ts`. Check the file's existing imports and test helpers first and reuse them — it already builds nodes and catalogs, and a second set of helpers beside them is noise:

```ts
describe("portsFor", () => {
  it("returns the default set for a node with no tool chosen", () => {
    const node = actionNode("align_1", "align");
    const { inputs } = portsFor(node, catalog);
    expect(inputs.map((p) => p.name)).toContain("reads");
  });

  it("returns the tool's set when one is chosen", () => {
    const node = { ...actionNode("align_1", "align"), params: { aligner: "star" } };
    const { inputs } = portsFor(node, catalog);
    expect(inputs.map((p) => p.name)).toContain("annotation");
  });

  it("falls back to the default for an unknown tool", () => {
    const node = { ...actionNode("align_1", "align"), params: { aligner: "nope" } };
    const { inputs } = portsFor(node, catalog);
    expect(inputs.map((p) => p.name)).toContain("reads");
    expect(inputs.map((p) => p.name)).not.toContain("annotation");
  });

  it("returns the static set for a node type with no tool choice", () => {
    const node = actionNode("qc_1", "qc");
    const { inputs } = portsFor(node, catalog);
    expect(inputs.map((p) => p.name)).toEqual(["reads"]);
  });
});

describe("canConnect with multi ports", () => {
  it("accepts a second wire into a multi port", () => {
    const nodes = [inputNode("r1"), inputNode("r2"), actionNode("align_1", "align")];
    const edges = [
      { from_node: "r1", from_port: "object", to_node: "align_1", to_port: "reads" },
    ];
    const verdict = canConnect(nodes, edges, catalog, {
      from_node: "r2",
      from_port: "object",
      to_node: "align_1",
      to_port: "reads",
    });
    expect(verdict.ok).toBe(true);
  });

  it("still refuses a second wire into a scalar port", () => {
    const nodes = [refNode("ref_a"), refNode("ref_b"), actionNode("align_1", "align")];
    const edges = [
      { from_node: "ref_a", from_port: "object", to_node: "align_1", to_port: "reference" },
    ];
    const verdict = canConnect(nodes, edges, catalog, {
      from_node: "ref_b",
      from_port: "object",
      to_node: "align_1",
      to_port: "reference",
    });
    expect(verdict.ok).toBe(false);
    expect(verdict.reason).toMatch(/already has an input/);
  });

  it("type-checks every wire of a multi port", () => {
    const nodes = [inputNode("r1"), bamNode("bam"), actionNode("align_1", "align")];
    const edges = [
      { from_node: "r1", from_port: "object", to_node: "align_1", to_port: "reads" },
    ];
    const verdict = canConnect(nodes, edges, catalog, {
      from_node: "bam",
      from_port: "object",
      to_node: "align_1",
      to_port: "reads",
    });
    expect(verdict.ok).toBe(false);
    expect(verdict.reason).toMatch(/does not accept/);
  });

  it("refuses a multi slot feeding a scalar port", () => {
    const slot = { ...inputNode("many"), multiple: true };
    const nodes = [slot, actionNode("qc_1", "qc")];
    const verdict = canConnect(nodes, [], catalog, {
      from_node: "many",
      from_port: "object",
      to_node: "qc_1",
      to_port: "reads",
    });
    expect(verdict.ok).toBe(false);
    expect(verdict.reason).toMatch(/one file/);
  });

  it("allows a multi slot feeding a multi port", () => {
    const slot = { ...inputNode("many"), multiple: true };
    const nodes = [slot, actionNode("align_1", "align")];
    const verdict = canConnect(nodes, [], catalog, {
      from_node: "many",
      from_port: "object",
      to_node: "align_1",
      to_port: "reads",
    });
    expect(verdict.ok).toBe(true);
  });
});

describe("edgesInvalidatedBy", () => {
  it("drops a wire into a port the new tool does not have", () => {
    const node = { ...actionNode("align_1", "align"), params: { aligner: "star" } };
    const nodes = [gtfNode("gtf"), node];
    const edges = [
      { from_node: "gtf", from_port: "object", to_node: "align_1", to_port: "annotation" },
    ];
    const dropped = edgesInvalidatedBy(nodes, edges, catalog, "align_1", "minimap2");
    expect(dropped.map((e) => e.to_port)).toEqual(["annotation"]);
  });

  it("keeps wires into ports both tools share", () => {
    const node = { ...actionNode("align_1", "align"), params: { aligner: "star" } };
    const nodes = [inputNode("r1"), node];
    const edges = [
      { from_node: "r1", from_port: "object", to_node: "align_1", to_port: "reads" },
    ];
    const dropped = edgesInvalidatedBy(nodes, edges, catalog, "align_1", "minimap2");
    expect(dropped).toEqual([]);
  });

  it("leaves other nodes' wires alone", () => {
    const align = { ...actionNode("align_1", "align"), params: { aligner: "star" } };
    const nodes = [inputNode("r1"), align, actionNode("qc_1", "qc")];
    const edges = [
      { from_node: "r1", from_port: "object", to_node: "qc_1", to_port: "reads" },
    ];
    const dropped = edgesInvalidatedBy(nodes, edges, catalog, "align_1", "minimap2");
    expect(dropped).toEqual([]);
  });
});
```

You will need `catalog` to carry `ports_by_tool` for `align` (with `star` having an `annotation` GTF input and `minimap2` not) and `reads` marked `multiple`. Extend the existing catalog fixture rather than making a second one.

- [ ] **Step 3: Run to verify they fail**

```bash
cd frontend && npx vitest run src/lib/workflowGraph.test.ts
```

Expected: FAIL — `portsFor is not defined`.

- [ ] **Step 4: Implement `portsFor`**

In `frontend/src/lib/workflowGraph.ts`, add after `portAccepts`:

```ts
/** The ports one node actually has, given the tool it has chosen.
 *
 * The mirror of `node_types.ports_for`. Every read of a node's ports goes
 * through here rather than through `catalog[node_type].inputs`: for a
 * tool-parameterized node those static lists are only the *default* shape, and
 * reading them directly draws a STAR node without its annotation port.
 *
 * An unset or unrecognized tool falls back to the default, so a
 * freshly-dropped node is wirable and a definition saved before an aligner was
 * removed still opens.
 */
export function portsFor(
  node: WorkflowNode,
  catalog: Record<string, NodeTypeMeta>,
): { inputs: PortMeta[]; outputs: PortMeta[] } {
  if (node.kind === "input") {
    return {
      inputs: [],
      outputs: node.accepts
        ? [{ name: "object", type: node.accepts, required: true, multiple: node.multiple }]
        : [],
    };
  }
  const meta = node.node_type ? catalog[node.node_type] : undefined;
  if (!meta) return { inputs: [], outputs: [] };
  const choice = meta.tool_choice;
  if (!choice) return { inputs: meta.inputs, outputs: meta.outputs };
  const chosen = node.params?.[choice.param_key];
  const tool = typeof chosen === "string" ? chosen : choice.default;
  const set = meta.ports_by_tool?.[tool] ?? meta.ports_by_tool?.[choice.default];
  return set ? { inputs: set.inputs, outputs: set.outputs } : { inputs: meta.inputs, outputs: meta.outputs };
}
```

- [ ] **Step 5: Route `outputType`/`inputPort` through it and make `canConnect` multi-aware**

Replace `outputType` and `inputPort` (lines 46-67):

```ts
function outputType(
  node: WorkflowNode,
  portName: string,
  catalog: Record<string, NodeTypeMeta>,
): PortType | null {
  const { outputs } = portsFor(node, catalog);
  return outputs.find((p) => p.name === portName)?.type ?? null;
}

function inputPort(
  node: WorkflowNode,
  portName: string,
  catalog: Record<string, NodeTypeMeta>,
): PortMeta | null {
  if (node.kind === "input") return null; // nothing flows into a slot
  const { inputs } = portsFor(node, catalog);
  return inputs.find((p) => p.name === portName) ?? null;
}
```

Note `inputPort` now returns the whole `PortMeta`, not just its type — `canConnect` needs `multiple`. Update its use in `canConnect` (lines 128-149):

```ts
  const accepted = inputPort(target, candidate.to_port, catalog);
  if (!accepted) {
    return { ok: false, reason: `No input port ${candidate.to_port}.` };
  }

  // One wire per input port -- unless the port collects several. Fan-*out* is
  // always fine (a trimmed FASTQ feeding both an aligner and a QC node); what
  // this governs is fan-*in*, which only a multi port has a meaning for.
  const occupied = edges.some(
    (e) => e.to_node === candidate.to_node && e.to_port === candidate.to_port,
  );
  if (occupied && !accepted.multiple) {
    return { ok: false, reason: `${candidate.to_port} already has an input.` };
  }

  // A slot holding several files may only feed a port that takes several.
  // Refused here rather than at launch, where the user has long forgotten
  // what they wired -- and silently sending one of N files is worse than
  // either.
  if (source.kind === "input" && source.multiple && !accepted.multiple) {
    return {
      ok: false,
      reason: `${candidate.to_port} takes one file, and ${source.label ?? source.node_id} holds several.`,
    };
  }

  if (!portAccepts(accepted.type, produced)) {
    const role = produced.role ?? "any";
    return {
      ok: false,
      reason: `${candidate.to_port} does not accept ${produced.format}/${role}.`,
    };
  }
```

- [ ] **Step 6: Implement `edgesInvalidatedBy`**

Add after `canConnect`:

```ts
/** Which wires stop making sense if `nodeId` switches to `tool`.
 *
 * Returned rather than applied, so the caller can both remove them and *say*
 * which it removed. A wire vanishing with no explanation is the version of
 * this that gets reported as a bug.
 *
 * The generalization of the rule `updateInput` already applies to input slots:
 * a slot whose type changed can no longer feed what it fed. Same reasoning,
 * now that an action node's ports can change too.
 */
export function edgesInvalidatedBy(
  nodes: WorkflowNode[],
  edges: WorkflowEdge[],
  catalog: Record<string, NodeTypeMeta>,
  nodeId: string,
  tool: string,
): WorkflowEdge[] {
  const node = nodes.find((n) => n.node_id === nodeId);
  if (!node) return [];
  const meta = node.node_type ? catalog[node.node_type] : undefined;
  const choice = meta?.tool_choice;
  if (!choice) return [];

  const after = portsFor(
    { ...node, params: { ...node.params, [choice.param_key]: tool } },
    catalog,
  );
  const byId = new Map(nodes.map((n) => [n.node_id, n]));

  return edges.filter((edge) => {
    if (edge.to_node === nodeId) {
      const port = after.inputs.find((p) => p.name === edge.to_port);
      if (!port) return true;
      const source = byId.get(edge.from_node);
      const produced = source ? outputType(source, edge.from_port, catalog) : null;
      return !produced || !portAccepts(port.type, produced);
    }
    if (edge.from_node === nodeId) {
      const port = after.outputs.find((p) => p.name === edge.from_port);
      if (!port) return true;
      const target = byId.get(edge.to_node);
      const accepted = target ? inputPort(target, edge.to_port, catalog) : null;
      return !accepted || !portAccepts(accepted.type, port.type);
    }
    return false;
  });
}
```

- [ ] **Step 7: Update `nodeHeight` for per-node ports**

`nodeHeight` takes a `NodeTypeMeta` and so cannot see a node's chosen tool — a STAR node would be drawn too short for its ports. Replace it:

```ts
/** The height a node needs to hold its ports without them overflowing.
 *
 * Takes the node, not its type: a STAR node has one more port than a
 * minimap2 node of the same type, and sizing from the type alone draws the
 * annotation port outside the box.
 */
export function nodeHeight(
  node: WorkflowNode,
  catalog: Record<string, NodeTypeMeta>,
): number {
  const { inputs, outputs } = portsFor(node, catalog);
  const ports = Math.max(inputs.length, outputs.length, 1);
  return NODE_HEADER + PORT_SPACING * (ports + 1);
}
```

Its one call site in `WorkflowCanvas.tsx:693` becomes `nodeHeight(node, catalog)` — Task 7 covers the component, but make this call-site edit now so the build stays green.

- [ ] **Step 8: Run to verify they pass**

```bash
cd frontend && npx vitest run src/lib/workflowGraph.test.ts
```

Expected: PASS, all tests including the pre-existing ones.

- [ ] **Step 9: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors. `inputPort`'s changed return type will surface any call site you missed.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/lib/workflowGraph.ts frontend/src/lib/workflowGraph.test.ts frontend/src/api/types.ts frontend/src/components/WorkflowCanvas.tsx
git commit -m "feat(frontend): per-node port resolution and multi-port wiring rules

portsFor mirrors the backend's ports_for; canConnect relaxes fan-in for
multi ports only; edgesInvalidatedBy reports what a tool change breaks.
Part of #94."
```

---

## Task 7: Tool selector on the canvas node

**Files:**
- Modify: `frontend/src/components/WorkflowCanvas.tsx`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Drop new nodes with the default tool**

In `addActionNode` (line 164), seed the tool so the node is immediately wirable:

```tsx
  function addActionNode(meta: NodeTypeMeta) {
    // Seeded with the default tool rather than left unset: a node with no
    // tool has no ports, and a node you cannot wire until you have visited a
    // dropdown reads as broken.
    const params = meta.tool_choice
      ? { [meta.tool_choice.param_key]: meta.tool_choice.default }
      : {};
    setNodes((current) => [
      ...current,
      {
        node_id: freshNodeId(meta.node_type),
        kind: "action",
        node_type: meta.node_type,
        params,
        continue_on_failure: false,
        position: nextFreeSlot(current),
      },
    ]);
  }
```

- [ ] **Step 2: Add the tool-change handler**

Add beside `updateInput`:

```tsx
  /** Change a node's tool, dropping the wires that stop making sense.
   *
   * The removal is reported rather than silent: a wire disappearing with no
   * explanation is what gets filed as a bug.
   */
  function changeTool(nodeId: string, tool: string) {
    const node = nodes.find((n) => n.node_id === nodeId);
    const meta = node?.node_type ? catalog[node.node_type] : undefined;
    const choice = meta?.tool_choice;
    if (!node || !choice) return;

    const dropped = edgesInvalidatedBy(nodes, edges, catalog, nodeId, tool);
    setNodes((current) =>
      current.map((n) =>
        n.node_id === nodeId
          ? { ...n, params: { ...n.params, [choice.param_key]: tool } }
          : n,
      ),
    );
    if (dropped.length > 0) {
      const keys = new Set(dropped.map(edgeKey));
      setEdges((current) => current.filter((e) => !keys.has(edgeKey(e))));
      setNotice(
        `Switched to ${tool}; removed ${dropped.length} wire(s) it has no port for: ${dropped
          .map((e) => `${e.to_node}.${e.to_port}`)
          .join(", ")}.`,
      );
    }
  }
```

Import `edgesInvalidatedBy` and `portsFor` from `../lib/workflowGraph`.

- [ ] **Step 3: Render ports from `portsFor`**

In the node-rendering block (lines 685-694), replace the port derivation:

```tsx
            const { inputs, outputs } = portsFor(node, catalog);
            const position = node.position ?? { x: 0, y: 0 };
            const height = node.kind === "input" ? 54 : nodeHeight(node, catalog);
```

Delete the old `inputs`/`outputs`/`meta`-based lines this replaces. `meta` is still needed for the label — keep that lookup.

Do the same in the edge-rendering block (lines 646-649), which derives `fromPorts`/`toPorts` from the catalog directly:

```tsx
            const fromPorts = portsFor(from, catalog).outputs.map((p) => p.name);
            const toPorts = portsFor(to, catalog).inputs.map((p) => p.name);
```

- [ ] **Step 4: Show the tool on the node and in the inspector**

Under the node label `<text>` (line 723), add a second line naming the tool:

```tsx
                {(() => {
                  const choice = meta?.tool_choice;
                  if (!choice) return null;
                  const tool = node.params?.[choice.param_key];
                  return (
                    <text
                      className="workflow-node-tool"
                      x={position.x + 10}
                      y={position.y + 34}
                    >
                      {typeof tool === "string" ? tool : choice.default}
                    </text>
                  );
                })()}
```

Add a selector to the sidebar inspector. Beside the existing `selectedInput` block, add:

```tsx
  const selectedAction = useMemo(
    () => nodes.find((n) => n.node_id === selected && n.kind === "action") ?? null,
    [nodes, selected],
  );
```

and render, before the `<h4>Tools</h4>` heading:

```tsx
          {selectedAction && (() => {
            const meta = selectedAction.node_type ? catalog[selectedAction.node_type] : undefined;
            const choice = meta?.tool_choice;
            if (!choice) return null;
            const current = selectedAction.params?.[choice.param_key];
            return (
              <div className="workflow-inspector">
                <h4>{meta?.label}</h4>
                <label>
                  <span>Tool</span>
                  <select
                    value={typeof current === "string" ? current : choice.default}
                    onChange={(e) => changeTool(selectedAction.node_id, e.target.value)}
                  >
                    {choice.options.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            );
          })()}
```

- [ ] **Step 5: Style the tool line**

In `frontend/src/styles.css`, beside the existing `.workflow-node-label` rule:

```css
/* The tool a node runs, under its label. Smaller and dimmer than the label:
   it qualifies the node rather than naming it. */
.workflow-node-tool {
  font-size: 11px;
  fill: var(--muted);
  pointer-events: none;
}
```

Check the actual variable name used for muted text in this file — `var(--muted)` is a guess; match what `.workflow-port-label` or `.muted` already uses.

- [ ] **Step 6: Typecheck and build**

```bash
cd frontend && npx tsc --noEmit && npx vite build
```

Expected: no errors.

- [ ] **Step 7: Verify in the browser**

```bash
./ops/worktree-up.sh
```

At **localhost:5273**, on the Workflows canvas:
1. Add an "Align to reference" node — it shows "minimap2" under its label, with three ports.
2. Select it, change the tool to STAR — a fourth port, `annotation`, appears.
3. Wire an input into `annotation`, then switch back to minimap2 — the wire disappears and a notice names it.
4. Add two FASTQ input nodes and wire both into the same `reads` port — both connect.
5. Wire two references into `reference` — the second is refused with "already has an input".

- [ ] **Step 8: Commit**

```bash
git add frontend/src/components/WorkflowCanvas.tsx frontend/src/styles.css
git commit -m "feat(frontend): choose a node's tool on the canvas

Nodes drop with the registry default, show their tool, and re-shape their
ports on a change -- reporting any wires that drop. Part of #94."
```

---

## Task 8: The node detail panel

**Files:**
- Create: `frontend/src/components/workflow/ParamForm.tsx`
- Create: `frontend/src/components/workflow/NodeDetailPanel.tsx`
- Modify: `frontend/src/components/WorkflowCanvas.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Add the schema API client method**

In `frontend/src/api/client.ts`, beside the other workflow methods:

```ts
  toolSchema: (nodeType: string, tool: string) =>
    request<{ aligner: string; fields: ParamField[] }>(
      `/workflows/tool-schema/${nodeType}/${tool}`,
    ),
```

Match the file's existing request helper and naming exactly — read a neighbouring method first. Add the `ParamField` type to `types.ts`, mirroring `aligner_registry.ParamField`:

```ts
/** One input in a generated parameter form. Mirrors
 *  `aligner_registry.ParamField` -- served rather than duplicated so a knob
 *  added there reaches the form without a second edit. */
export interface ParamField {
  key: string;
  label: string;
  kind: "int" | "bool" | "select" | "text";
  default: unknown;
  help: string;
  group: "biology" | "performance";
  min?: number | null;
  max?: number | null;
  choices?: { value: string; label: string }[];
}
```

- [ ] **Step 2: Write `ParamForm`**

Create `frontend/src/components/workflow/ParamForm.tsx`:

```tsx
/**
 * A parameter form generated from backend field metadata.
 *
 * The fields come from `aligner_registry`, which is also what `AlignDialog`
 * renders from -- a second hand-written copy here is the copy that goes stale
 * the first time a knob is added.
 *
 * Grouping is not decoration: a generated form is otherwise an
 * undifferentiated pile of inputs, and biology-vs-performance is roughly how
 * AlignDialog was already organized by hand.
 */

import { useState } from "react";
import type { ParamField } from "../../api/types";

interface Props {
  fields: ParamField[];
  values: Record<string, unknown>;
  onChange: (key: string, value: unknown) => void;
}

function Field({ field, value, onChange }: {
  field: ParamField;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const current = value ?? field.default;
  return (
    <label className="param-field">
      <span className="param-label">
        {field.label}
        <em className="param-help">{field.help}</em>
      </span>
      {field.kind === "bool" ? (
        <input
          type="checkbox"
          checked={Boolean(current)}
          onChange={(e) => onChange(e.target.checked)}
        />
      ) : field.kind === "select" ? (
        <select value={String(current ?? "")} onChange={(e) => onChange(e.target.value)}>
          {(field.choices ?? []).map((choice) => (
            <option key={choice.value} value={choice.value}>
              {choice.label}
            </option>
          ))}
        </select>
      ) : field.kind === "int" ? (
        <input
          type="number"
          value={current === null || current === undefined ? "" : String(current)}
          min={field.min ?? undefined}
          max={field.max ?? undefined}
          // Empty means "leave it to the tool's own default", which is not the
          // same as zero -- coercing a cleared box to 0 would silently set a
          // thread count of nothing.
          onChange={(e) =>
            onChange(e.target.value === "" ? undefined : Number(e.target.value))
          }
        />
      ) : (
        <input
          type="text"
          value={String(current ?? "")}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </label>
  );
}

export function ParamForm({ fields, values, onChange }: Props) {
  const [showAdvanced, setShowAdvanced] = useState(false);
  const biology = fields.filter((f) => f.group === "biology");
  const performance = fields.filter((f) => f.group === "performance");

  return (
    <div className="param-form">
      {biology.map((field) => (
        <Field
          key={field.key}
          field={field}
          value={values[field.key]}
          onChange={(value) => onChange(field.key, value)}
        />
      ))}
      {performance.length > 0 && (
        <>
          <button
            type="button"
            className="btn subtle"
            onClick={() => setShowAdvanced((v) => !v)}
          >
            {showAdvanced ? "Hide" : "Show"} performance settings
          </button>
          {showAdvanced &&
            performance.map((field) => (
              <Field
                key={field.key}
                field={field}
                value={values[field.key]}
                onChange={(value) => onChange(field.key, value)}
              />
            ))}
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Write `NodeDetailPanel`**

Create `frontend/src/components/workflow/NodeDetailPanel.tsx`:

```tsx
/**
 * One node, in full: its tool, its parameters, and what is wired into it.
 *
 * A panel rather than an SVG camera zoom, which is what the issue asked for
 * literally. The substance of this screen is a form, and forms are HTML -- a
 * real zoom would mean `foreignObject` or form controls hand-built in SVG,
 * both worse than the animation is good. Animating out of the node's position
 * keeps what the zoom was *for*: knowing which node you opened.
 *
 * Wiring is shown read-only. Rewiring is a canvas gesture, and a second way to
 * do it here would be a second set of rules to keep in step with
 * `canConnect`.
 */

import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { NodeTypeMeta, WorkflowEdge, WorkflowNode } from "../../api/types";
import { portsFor } from "../../lib/workflowGraph";
import { ParamForm } from "./ParamForm";

interface Props {
  node: WorkflowNode;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  catalog: Record<string, NodeTypeMeta>;
  onClose: () => void;
  onChangeTool: (nodeId: string, tool: string) => void;
  onChangeParam: (nodeId: string, key: string, value: unknown) => void;
  onChangeLabel: (nodeId: string, label: string) => void;
  onToggleContinue: (nodeId: string, value: boolean) => void;
}

export function NodeDetailPanel({
  node,
  nodes,
  edges,
  catalog,
  onClose,
  onChangeTool,
  onChangeParam,
  onChangeLabel,
  onToggleContinue,
}: Props) {
  const meta = node.node_type ? catalog[node.node_type] : undefined;
  const choice = meta?.tool_choice;
  const chosen = choice ? node.params?.[choice.param_key] : undefined;
  const tool = typeof chosen === "string" ? chosen : choice?.default;
  const { inputs, outputs } = portsFor(node, catalog);
  const nameOf = (id: string) =>
    nodes.find((n) => n.node_id === id)?.label ?? id;

  const schema = useQuery({
    queryKey: ["tool-schema", node.node_type, tool],
    queryFn: () => api.toolSchema(node.node_type!, tool!),
    enabled: Boolean(node.node_type && tool && choice),
  });

  return (
    <div className="node-detail">
      <div className="node-detail-header">
        <button className="btn" onClick={onClose}>
          ← Back to graph
        </button>
        <h2>{meta?.label ?? node.label ?? node.node_id}</h2>
      </div>

      <section>
        <label>
          <span>Label</span>
          <input
            value={node.label ?? ""}
            placeholder={node.node_id}
            onChange={(e) => onChangeLabel(node.node_id, e.target.value)}
          />
        </label>

        {choice && (
          <label>
            <span>Tool</span>
            <select
              value={tool}
              onChange={(e) => onChangeTool(node.node_id, e.target.value)}
            >
              {choice.options.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        )}

        <label className="checkbox">
          <input
            type="checkbox"
            checked={node.continue_on_failure}
            onChange={(e) => onToggleContinue(node.node_id, e.target.checked)}
          />
          <span>
            Carry on if this step fails
            <em>
              For steps whose failure is survivable -- QC and stats. Everything
              downstream of a load-bearing step is skipped when it fails.
            </em>
          </span>
        </label>
      </section>

      {choice && (
        <section>
          <h3>Parameters</h3>
          {schema.isLoading && <p className="muted">Loading…</p>}
          {schema.data && (
            <ParamForm
              fields={schema.data.fields}
              values={node.params ?? {}}
              onChange={(key, value) => onChangeParam(node.node_id, key, value)}
            />
          )}
        </section>
      )}

      <section>
        <h3>Inputs</h3>
        <ul className="node-detail-ports">
          {inputs.map((port) => {
            const wired = edges.filter(
              (e) => e.to_node === node.node_id && e.to_port === port.name,
            );
            return (
              <li key={port.name}>
                <strong>{port.name}</strong>
                <em>
                  {port.type.format}
                  {port.type.role ? `/${port.type.role}` : ""}
                  {port.required ? "" : " (optional)"}
                  {port.multiple ? " (several)" : ""}
                </em>
                <span>
                  {wired.length === 0
                    ? "not connected"
                    : wired.map((e) => nameOf(e.from_node)).join(", ")}
                </span>
              </li>
            );
          })}
          {inputs.length === 0 && <li className="muted">No inputs.</li>}
        </ul>

        <h3>Outputs</h3>
        <ul className="node-detail-ports">
          {outputs.map((port) => {
            const wired = edges.filter(
              (e) => e.from_node === node.node_id && e.from_port === port.name,
            );
            return (
              <li key={port.name}>
                <strong>{port.name}</strong>
                <em>
                  {port.type.format}
                  {port.type.role ? `/${port.type.role}` : ""}
                </em>
                <span>
                  {wired.length === 0
                    ? "not connected"
                    : wired.map((e) => nameOf(e.to_node)).join(", ")}
                </span>
              </li>
            );
          })}
          {outputs.length === 0 && <li className="muted">No outputs.</li>}
        </ul>
      </section>
    </div>
  );
}
```

- [ ] **Step 4: Wire double-click into the canvas**

In `WorkflowCanvas.tsx`, add state and handlers:

```tsx
  const [detailNodeId, setDetailNodeId] = useState<string | null>(null);

  const detailNode = useMemo(
    () => nodes.find((n) => n.node_id === detailNodeId) ?? null,
    [nodes, detailNodeId],
  );

  function updateNode(nodeId: string, patch: Partial<WorkflowNode>) {
    setNodes((current) =>
      current.map((n) => (n.node_id === nodeId ? { ...n, ...patch } : n)),
    );
  }

  function setParam(nodeId: string, key: string, value: unknown) {
    setNodes((current) =>
      current.map((n) =>
        n.node_id === nodeId ? { ...n, params: { ...n.params, [key]: value } } : n,
      ),
    );
  }
```

Add `onDoubleClick` to the node `<rect>` (beside its `onMouseDown` at line 711):

```tsx
                  onDoubleClick={(e) => {
                    e.stopPropagation();
                    setDetailNodeId(node.node_id);
                  }}
```

Render the panel in place of the graph body — replace the `<div className="workflow-body">` opening with a conditional:

```tsx
      {detailNode ? (
        <NodeDetailPanel
          node={detailNode}
          nodes={nodes}
          edges={edges}
          catalog={catalog}
          onClose={() => setDetailNodeId(null)}
          onChangeTool={changeTool}
          onChangeParam={setParam}
          onChangeLabel={(id, label) => updateNode(id, { label })}
          onToggleContinue={(id, value) =>
            updateNode(id, { continue_on_failure: value })
          }
        />
      ) : (
        <div className="workflow-body">
          {/* ...existing palette and svg... */}
        </div>
      )}
```

Add an Escape handler so the panel closes from the keyboard:

```tsx
  useEffect(() => {
    if (!detailNodeId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDetailNodeId(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [detailNodeId]);
```

Import `useEffect` and `NodeDetailPanel`.

- [ ] **Step 5: Style the panel**

Add to `frontend/src/styles.css`. Match the surrounding file's conventions for spacing and colour variables — read a neighbouring block first rather than inventing names:

```css
/* The node detail view. Animates out of the canvas so it reads as opening
   *this* node rather than navigating somewhere else. */
.node-detail {
  padding: 20px 28px;
  overflow-y: auto;
  animation: node-detail-in 140ms ease-out;
}

@keyframes node-detail-in {
  from {
    opacity: 0;
    transform: scale(0.97);
  }
  to {
    opacity: 1;
    transform: scale(1);
  }
}

.node-detail-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.node-detail-ports {
  list-style: none;
  padding: 0;
}

.node-detail-ports li {
  display: grid;
  grid-template-columns: 140px 1fr 1fr;
  gap: 8px;
  padding: 6px 0;
}

.param-field {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: 12px;
  padding: 8px 0;
}

.param-help {
  display: block;
  font-size: 11px;
  opacity: 0.75;
}
```

- [ ] **Step 6: Typecheck and build**

```bash
cd frontend && npx tsc --noEmit && npx vite build
```

Expected: no errors.

- [ ] **Step 7: Verify in the browser**

At **localhost:5273**:
1. Double-click an align node — the panel opens with the tool selector and a parameter form.
2. Change a parameter (e.g. threads), go back, re-open — the value persisted.
3. Switch the tool to STAR inside the panel — the form re-loads with STAR's fields.
4. Check the Inputs list names the upstream nodes wired into each port, and says "not connected" for the rest.
5. Press Escape — the panel closes back to the graph.
6. Toggle "Carry on if this step fails", save the workflow, re-open it — the toggle survived.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/components/workflow frontend/src/components/WorkflowCanvas.tsx frontend/src/api/client.ts frontend/src/api/types.ts frontend/src/styles.css
git commit -m "feat(frontend): node detail panel with a generated parameter form

Double-click opens a node in full: tool, parameters from the backend field
metadata, wiring, and continue_on_failure -- which no UI could set before.
Part of #94."
```

---

## Task 9: Binding files at the input node

**Files:**
- Modify: `frontend/src/components/WorkflowCanvas.tsx`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Move the project selector into the toolbar**

`projectId` state already exists (line 105) but is only reachable inside the launch dialog. Move the selector into the toolbar so it governs the whole canvas, and make the projects query unconditional:

```tsx
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api.listProjects(),
  });

  const projectObjects = useQuery({
    queryKey: ["objects", projectId],
    queryFn: () => api.listObjects(projectId!),
    enabled: Boolean(projectId),
  });
```

Note both lose their `enabled: launching` gate — file selection on the canvas needs them before the dialog opens.

In the toolbar, after the name input:

```tsx
        <select
          className="workflow-project"
          value={projectId ?? ""}
          onChange={(e) => {
            setProjectId(e.target.value || null);
            // Bindings name objects in the old project, so they cannot
            // survive a project change -- keeping them would submit ids the
            // new project does not contain.
            setBindings({});
          }}
          aria-label="Project"
        >
          <option value="">Choose a project…</option>
          {(projects.data ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
```

- [ ] **Step 2: Widen `bindings` to hold several ids**

A multi slot binds N files. Change the state type:

```tsx
  // One object id per slot, or several for a slot marked `multiple`. The
  // union rather than always-an-array keeps the scalar path -- every existing
  // read and the launch payload -- unchanged.
  const [bindings, setBindings] = useState<Record<string, string | string[]>>({});
```

Update `unbound` to treat an empty array as unbound:

```tsx
  const unbound = inputNodes.filter((n) => {
    const bound = bindings[n.node_id];
    return !bound || (Array.isArray(bound) && bound.length === 0);
  });
```

- [ ] **Step 3: Render the binding selector on the input node**

Input nodes are SVG `<rect>`s, and a `<select>` cannot live inside one. Use `<foreignObject>` — the one place it is warranted, since this is a control rather than a form layout:

In the node-rendering block, inside the `<g>` for a node where `node.kind === "input"`:

```tsx
                {node.kind === "input" && (
                  <foreignObject
                    x={position.x + 8}
                    y={position.y + 26}
                    width={NODE_WIDTH - 16}
                    height={24}
                  >
                    <select
                      className="node-binding"
                      value={
                        Array.isArray(bindings[node.node_id])
                          ? ""
                          : ((bindings[node.node_id] as string) ?? "")
                      }
                      disabled={!projectId}
                      onClick={(e) => e.stopPropagation()}
                      onMouseDown={(e) => e.stopPropagation()}
                      onChange={(e) => {
                        const value = e.target.value;
                        setBindings((current) => ({
                          ...current,
                          [node.node_id]: node.multiple
                            ? [
                                ...((current[node.node_id] as string[]) ?? []),
                                value,
                              ].filter(Boolean)
                            : value,
                        }));
                      }}
                    >
                      <option value="">
                        {!projectId
                          ? "Choose a project first"
                          : node.multiple
                            ? "Add a file…"
                            : "Choose a file…"}
                      </option>
                      {bindableObjects(projectObjects.data ?? [], node.accepts).map(
                        (object) => (
                          <option key={object.id} value={object.id}>
                            {object.name}
                          </option>
                        ),
                      )}
                    </select>
                  </foreignObject>
                )}
```

The `stopPropagation` on mousedown matters: without it, clicking the dropdown starts a node drag.

For a multi slot, show what is bound underneath, each removable:

```tsx
                {node.kind === "input" && node.multiple && (
                  <foreignObject
                    x={position.x + 8}
                    y={position.y + 52}
                    width={NODE_WIDTH - 16}
                    height={Math.max(
                      ((bindings[node.node_id] as string[]) ?? []).length * 18,
                      1,
                    )}
                  >
                    <ul className="node-binding-list">
                      {((bindings[node.node_id] as string[]) ?? []).map((id) => (
                        <li key={id}>
                          <span>
                            {(projectObjects.data ?? []).find((o) => o.id === id)?.name ??
                              id}
                          </span>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              setBindings((current) => ({
                                ...current,
                                [node.node_id]: (
                                  (current[node.node_id] as string[]) ?? []
                                ).filter((x) => x !== id),
                              }));
                            }}
                          >
                            ×
                          </button>
                        </li>
                      ))}
                    </ul>
                  </foreignObject>
                )}
```

Input nodes need to be taller to hold this — replace the fixed `54`:

```tsx
            const height =
              node.kind === "input"
                ? 54 +
                  (node.multiple
                    ? 20 + ((bindings[node.node_id] as string[]) ?? []).length * 18
                    : 0)
                : nodeHeight(node, catalog);
```

- [ ] **Step 4: Add a "several files" toggle to the input inspector**

In the `selectedInput` inspector block, after the Accepts selector:

```tsx
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={Boolean(selectedInput.multiple)}
                  onChange={(e) => {
                    updateInput(selectedInput.node_id, { multiple: e.target.checked });
                    // The slot's shape changed, so what it can feed changed
                    // with it -- a set cannot flow into a scalar port. Same
                    // reasoning as the `accepts` branch of updateInput.
                    setEdges((current) =>
                      current.filter((e2) => e2.from_node !== selectedInput.node_id),
                    );
                    setBindings((current) => ({ ...current, [selectedInput.node_id]: e.target.checked ? [] : "" }));
                  }}
                />
                <span>
                  Several files
                  <em>
                    Read files split into chunks that all go into one run. Not
                    mates -- R2 belongs on its own slot.
                  </em>
                </span>
              </label>
```

- [ ] **Step 5: Reduce the Run dialog to a confirmation**

The dialog's per-input selectors stay as a fallback, but the project row is now redundant. Replace it with a read-only line, and let the dialog open pre-bound:

```tsx
            <p className="muted">
              {projectId
                ? `Running in ${(projects.data ?? []).find((p) => p.id === projectId)?.name ?? "this project"}.`
                : "Choose a project in the toolbar first."}
            </p>
```

Leave the per-input `<select>`s in place — they already read and write the same `bindings` map, so a slot bound on the canvas shows up bound here. For a multi slot, show the count rather than a single select:

```tsx
              {node.multiple ? (
                <span className="muted">
                  {((bindings[node.node_id] as string[]) ?? []).length} file(s) selected on the canvas
                </span>
              ) : (
                /* ...existing select... */
              )}
```

- [ ] **Step 6: Send multi bindings to the launcher**

Check what `api.launchWorkflow` sends and what the backend's launch endpoint expects for `bindings`. `WorkflowRun.bindings` is a `list[WorkflowBinding]` keyed by `node_id`, so N rows sharing a `node_id` is already representable — but the request model and the resolver that reads it may assume one row per node.

Read `backend/app/api/v1/workflows.py`'s launch handler and `workflow_orchestrator`'s binding lookup (around line 147, `bindings[source.node_id]`). If that lookup builds a `dict[str, ObjectId]`, widen it to hold a list when several rows share a node_id:

```python
    # A multi slot contributes several rows under one node_id. dict-by-node_id
    # would keep only the last, silently launching with one file of N.
    bindings: dict[str, PydanticObjectId | list[PydanticObjectId]] = {}
    for binding in run.bindings:
        existing = bindings.get(binding.node_id)
        if existing is None:
            bindings[binding.node_id] = binding.object_id
        elif isinstance(existing, list):
            existing.append(binding.object_id)
        else:
            bindings[binding.node_id] = [existing, binding.object_id]
```

Write a backend test for this in `tests/services/test_workflow_multi_port.py` before changing it, following Task 1's pattern.

- [ ] **Step 7: Style the node-side controls**

```css
/* Bind a file on the node itself. `foreignObject` is warranted here -- a
   native select cannot live in an SVG rect, and this is a control rather
   than a layout. */
.node-binding {
  width: 100%;
  font-size: 11px;
}

.node-binding-list {
  list-style: none;
  margin: 0;
  padding: 0;
  font-size: 11px;
}

.node-binding-list li {
  display: flex;
  justify-content: space-between;
  gap: 4px;
}
```

- [ ] **Step 8: Typecheck, build, and run both suites**

```bash
cd frontend && npx tsc --noEmit && npx vite build
```

```bash
./backend/run-worktree-tests.sh tests/ -q
```

```bash
cd frontend && npx vitest run
```

Expected: all green. Read the counts.

- [ ] **Step 9: Verify in the browser**

At **localhost:5273**:
1. Choose a project in the toolbar — input nodes' dropdowns populate with matching files only (a reference slot must not offer `protein.faa`).
2. Bind a file on an input node — the node shows the filename.
3. Mark a slot "Several files", add two FASTQs — both list on the node with × buttons.
4. Wire that slot into an align node's `reads` port — it connects. Wire it into a QC node's `reads` port — refused, with the "takes one file" reason.
5. Click Run — the dialog shows everything already bound; launch it.
6. Watch the run in Activity: the align job's command should name both read files.
7. Switch projects — bindings clear.

**Per `CLAUDE.md`, check the binding filter against real objects, not just the UI.** The suggestion-rules failure that rule exists for was exactly this shape — rules that passed green tests while miscounting real files:

```bash
docker compose exec api python -c "..."
```

(from the main checkout, against a real project — confirm that `bindableObjects`' backend equivalent excludes sidecars and non-ready objects for a real project's file list.)

- [ ] **Step 10: Commit**

```bash
git add frontend/src/components/WorkflowCanvas.tsx frontend/src/styles.css backend/app/services/workflow_orchestrator.py backend/tests/services/test_workflow_multi_port.py
git commit -m "feat(workflow): bind files at the input node

A project selector on the toolbar, file pickers on input nodes, and slots
that hold several files. Bindings stay per-run -- the definition still
stores no object ids. Part of #94."
```

---

## Task 10: Close out and merge

- [ ] **Step 1: Run everything one more time**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

```bash
cd frontend && npx vitest run && npx tsc --noEmit && npx vite build
```

Expected: green. **Read the test counts** — `CLAUDE.md` is explicit that "green" means the count, not the exit code of whatever ran last.

- [ ] **Step 2: Check for a TODO entry this closes**

```bash
grep -in "canvas\|workflow\|aligner\|multi" docs/TODO.md
```

If an entry describes any of this work, append ` — FIXED` to its heading, write a note saying what shipped and where the code lives, record what the implementation did differently from this plan, and **move the whole entry to `docs/TODO-done.md`**. If nothing matches, skip — do not invent an entry.

- [ ] **Step 3: Record the implementation deltas in the design note**

Append a short "What shipped differently" section to
`docs/superpowers/specs/2026-08-08-workflow-canvas-node-detail-design.md`. Every plan executed in this repo has departed from itself somewhere, and per `CLAUDE.md` that delta is the most valuable sentence in the record. Likely candidates: whether `extra_reads` needed a runner change or the aligners took multiple files natively, and whether the orchestrator's binding dict needed widening.

- [ ] **Step 4: Merge to main and push**

`main` is this project's dev trunk. Per `CLAUDE.md`, once the suite is green and `main` is clean, merge and push without asking.

```bash
git checkout main && git pull && git merge --no-ff claude/issue-94-brainstorm-bfbd56
```

If `main` moved, **re-run the backend suite after merging** rather than assuming the earlier green still holds.

```bash
./backend/run-worktree-tests.sh tests/ -q
```

```bash
git push origin main
```

- [ ] **Step 5: Restore the main stack**

If anything repointed the 5173 instance at this worktree, put it back from the main checkout root:

```bash
docker compose up -d --build api web worker
```

Confirm with:

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

The source path must be the main checkout, not a path under `.claude/worktrees/`.

- [ ] **Step 6: Update the issue**

```bash
gh issue comment 94 --body "Implemented and merged to main..."
```

Say what shipped against each of the four asks in the issue, and note what was deliberately left out (`continuity_qc`/`differential_expression` multi ports, `AlignDialog` sharing the generated form, saved binding defaults). Close the issue if all four asks are covered.

---

## Self-Review Notes

**Spec coverage.** §1 tool selection → Tasks 4, 5, 7. §2 multi ports and multi slots → Tasks 1, 2, 3, 6, 9. §3 detail view → Task 8. §4 node-side binding → Task 9. Testing section → the test steps throughout, plus Task 10's final runs.

**Two places this plan deliberately says "read first, then write":** Task 2 Step 6 (the orchestrator may already pass lists through untouched) and Task 3 Step 5 (whether the aligners take several read files positionally). Both are claims I could not verify without running the code, and inventing a change where none is needed is worse than an explicit check. Task 3 Step 5 says to *stop and report* if an aligner cannot take several files — that is a real decision, not a task-level one.

**Known ordering constraint.** Task 3 must not be skipped. Task 2 gets extra read files as far as the launcher and no further; without Task 3 they are silently dropped, which is precisely the silent-skip failure `CLAUDE.md`'s registry section warns about.
