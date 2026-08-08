# Reusable user-defined pipeline DAGs

Design note for [#18](https://github.com/syntheticgio/bioflow/issues/18) (epic)
and its first slice [#20](https://github.com/syntheticgio/bioflow/issues/20).

Status: design agreed 2026-08-07. Not implemented.

## What this is

A ComfyUI-style canvas for composing BioFlow's existing pipeline actions into a
saved, reusable graph. The user drags typed nodes, wires outputs to inputs, binds
real files to the graph's declared inputs, and runs it. A saved graph is
project-independent: it describes *what to do*, never *which files*.

The reference point is deliberate. ComfyUI's usability comes from typed ports --
a node declares named inputs and outputs with types, and the canvas refuses a
wire whose types disagree. That constraint is what makes a builder something
other than a way to author broken pipelines, and BioFlow already owns the
vocabulary to express it (§3).

## What exists today, and why none of it is this

Three mechanisms are easy to mistake for a workflow graph. None is one.

- **`Job.depends_on`** (`models/job.py:206`) is real scheduling: a job stays
  `BLOCKED` until every id in the list has *succeeded*, and
  `queue/queue.py:_release_dependents` unblocks or cancels dependents as each
  finishes. But it is built fresh at launch time by `pipeline_service` and
  discarded; nothing persists the shape, and it cannot be authored or reused.
- **`PipelineRun`** (`models/run.py`) is explicitly *not* a graph. Its module
  docstring states the test: "whether it describes a user's request or the
  machine's plan; only the former belongs." It records one launch.
- **`RunJob`** links jobs to runs many-to-many, because a deduplicated
  `build_index` can serve two runs. That shape recurs here one level up (§4).

The gap this epic fills is a *persisted, instantiable* graph. Nothing today has
one.

## 1. Decisions

Recorded with their alternatives, because the rejected options are the ones
someone will re-propose later.

### 1.1 A node is a launch, expandable to its jobs

A canvas node corresponds to a **launch** -- the same altitude as an Actions tab
click -- not to a single job or handler.

"Align" is one node. Running it fans out internally into the index build, the
alignment, the BAM index and the header parses that `pipeline_service` already
creates. A node can be *expanded* in the UI to reveal those jobs read-only, which
is where live per-job progress renders.

Rejected: **node = one job**. It offers maximum ComfyUI fidelity and a 1:1 map
onto the queue, but it exposes plumbing the current design deliberately
auto-attaches (BAM indexing, header parsing), and lets a user author a graph that
forgets the index. Authoring at launch altitude inherits, for free, everything
`pipeline_service` knows about validating inputs, picking tools, deduplicating
index builds, and attaching follow-up jobs.

The cost is real and accepted: **a workflow can only compose actions BioFlow
already has a launch path for.** Arbitrary tool wiring is out of scope.

### 1.2 Inputs are explicit nodes

A saved definition names its inputs with dedicated `INPUT` nodes carrying a label
and an accepted type. The workflow's parameters are exactly its input nodes.

Rejected: **free ports at the edge** (any unwired input becomes a parameter).
This is the more faithful ComfyUI analogy, but it has nowhere to put a *name*, and
deriving a definition from a completed run (§7) needs one --  `RunInput` already
carries `object_id`, `name`, and `role`, which maps onto an input node almost
exactly. Explicit nodes also let one input feed several downstream nodes, which
the free-port form can only express by duplicating a binding.

Rejected: **storing bound object ids in the definition**. A definition carrying
real object ids goes stale when files are deleted, and would make every saved
graph implicitly project-scoped.

Ceremony mitigation: leaving an input port unwired at save time auto-creates its
`INPUT` node. The free-port *gesture* still works; it just lands in the explicit
model.

### 1.3 Failure fails the branch, not the workflow

Descendants of a failed node are cancelled. Independent branches run to
completion. The workflow ends `PARTIAL` -- a status `RunStatus` already defines
for exactly this shape.

A per-node `continue_on_failure` flag lets a node's dependents proceed anyway.
This mirrors `OPTIONAL_ROLES` in `run.py`, which already encodes "this failing
does not fail the run" for ingests and QC.

Rejected: **fail-fast**, today's `depends_on` behavior lifted whole. Cancelling a
running assembly because an unrelated QC node failed defeats the point of a graph
whose branches are independent.

**This requires a queue change.** `_release_dependents` currently calls
`_fail_blocked_job` on any dependent with a failed dependency, unconditionally --
there is no per-job "continue anyway" and no notion of branch scope. See §8.3.

### 1.4 Retry is in place, per node

A failed node is retried individually: a *new* job and a *new* `PipelineRun` are
enqueued, the node instance re-points at them, and the attempt counter
increments. Succeeded nodes keep their original links and are not re-executed. On
success, downstream nodes launch and the workflow continues from where it
stopped. "Retry all failed" is this operation applied to a set.

Node-level *job* retry (`max_attempts`, backoff, `DEAD`) is unchanged and still
the queue's business. This decision concerns what happens after a workflow has
already come to rest.

Rejected: **re-run the whole workflow**. Content-addressed dedup would spare an
unchanged `build_index`, but dedup is per-job identity, not per-node -- a
six-hour assembly upstream of the failure would genuinely re-run.

This decision is the reason node instances are separate documents (§4): a `DEAD`
job cannot be un-deaded, so retry *must* be able to re-point a node at new work
while its siblings keep pointing at their original jobs.

### 1.5 A workflow run is a parent over `PipelineRun`s

`WorkflowRun` is a new document. Each node execution creates an ordinary
`PipelineRun`, exactly as an Actions tab click does, plus a link row recording
"this run is node X of workflow run Y". **`PipelineRun` is unchanged.**

The payoff is that the activity view, provenance panel, and prior-run suggestion
lookups keep working on workflow-produced runs with no changes -- they see
ordinary runs, because that is what they are.

Rejected: **adding `workflow_run_id`/`node_id` to `PipelineRun`**. Fewer
documents, but it puts graph structure onto the one model whose docstring exists
to keep graph structure off it. Once it carries ordering, the docstring's test
stops being enforceable and the next change adds more.

Rejected: **one document for the whole workflow**, replacing `PipelineRun` for
workflow-launched work. Forks every consumer of `PipelineRun` into two code
paths.

## 2. Concepts

| Concept | Status | Role |
|---|---|---|
| `Job` | exists | queue unit; retries, leases, progress |
| `PipelineRun` | exists, **unchanged** | one launch = one user intent |
| `WorkflowDefinition` | new | the saved graph; no project, no file references |
| `WorkflowRun` | new | one execution of a definition |
| `WorkflowNodeRun` | new | one node's execution within a run |

## 3. Port types

`PortType` is `(FormatKind, ObjectRole | None)`, reusing the two enums in
`models/object.py` rather than inventing a parallel vocabulary.

This is what makes wire validation more than cosmetic. `ObjectRole` exists
precisely to record intent the bytes cannot carry -- its `PROTEIN` member is
commented as "the role that matters most: a protein FASTA and a reference genome
are both `FormatKind.FASTA`, and only this keeps one out of the aligner's
reference picker." A canvas that refuses to wire a protein FASTA into an
alignment reference port is enforcing the rule the Actions tab already enforces,
expressed once more.

A `None` role means the port accepts any role for that format.

## 4. Models

### `WorkflowDefinition`

```
WorkflowDefinition(TimestampedDocument)
    name: str
    description: str
    nodes: list[WorkflowNode]
    edges: list[WorkflowEdge]
    version: int
```

```
WorkflowNode
    node_id: str                    # stable within the graph, survives edits
    kind: WorkflowNodeKind          # INPUT | ACTION
    node_type: str | None           # ACTION only; key into NODE_TYPES (§5)
    params: dict                    # tool choice, presets; node-local
    continue_on_failure: bool = False
    position: {x: float, y: float}  # canvas layout
    label: str | None               # INPUT only -- "tumor reads"
    accepts: PortType | None        # INPUT only
```

```
WorkflowEdge
    from_node: str
    from_port: str
    to_node: str
    to_port: str
```

Ports are **not** stored on the node. They are declared by the node-type registry
(§5) and looked up by `node_type`, so a definition saved today does not keep
stale ports after a tool gains an input.

`version` increments on edit; a `WorkflowRun` pins the version it ran, so a
historical run stays readable after its definition changes. Same reasoning that
made `PipelineRun` denormalize its input names rather than look them up.

### `WorkflowRun`

```
WorkflowRun(TimestampedDocument)
    definition_id: PydanticObjectId
    definition_version: int
    project_id: PydanticObjectId
    label: str
    bindings: list[WorkflowBinding]   # input node_id -> object_id + name
```

Status is **derived from node states, never stored**, following `RunStatus`'s
docstring: a stored status is a second source of truth that drifts the first time
a write is lost.

`bindings` denormalizes the bound object's name alongside its id, for the reason
`RunInput` does -- a run must stay readable after its inputs are deleted.

### `WorkflowNodeRun`

```
WorkflowNodeRun(TimestampedDocument)
    workflow_run_id: PydanticObjectId
    node_id: str
    run_id: PydanticObjectId | None   # the PipelineRun this node produced
    attempt: int
    state: NodeRunState
    outputs: list[PydanticObjectId]
```

A separate document rather than an array on `WorkflowRun`, for the reason §1.4
forces: retry enqueues new work and re-points one node while its siblings keep
their original links, and `attempt` makes that history legible instead of
overwriting it. This is `RunJob`'s link-collection shape one level up, and the
same many-to-many pressure will apply once dedup means one `build_index` serves
two nodes.

Indexes: `by_workflow_run`, and unique on `(workflow_run_id, node_id, attempt)`
so a retry cannot double-insert and double-count in the derived status -- the
same guard `RunJob.uniq_run_job` provides.

## 5. The node-type registry

The load-bearing new component, and the one most likely to fail silently.

```python
NODE_TYPES: dict[str, NodeTypeSpec]

NodeTypeSpec:
    label: str
    run_kind: RunKind | None           # None for launches that create no run
    inputs: tuple[PortSpec, ...]       # name, PortType, required
    outputs: tuple[PortSpec, ...]
    launch: Callable                   # adapts (bound inputs, params) -> launch_*
```

Every canvas capability reads from here: which nodes exist, which ports they
expose, which wires validate, and how a node launches.

### Why it is keyed by its own string, not by `RunKind`

Keying on `RunKind` was the obvious choice and is wrong. Measured on `main` at
design time:

- **24 `launch_*` functions** exist across `services/` (23 in
  `pipeline_service.py`, plus `ncbi_assembly_service.launch_download`).
- **Only 9 create a `PipelineRun`.** `grep -n "RunKind\."` finds exactly nine
  call sites across all services.
- The other 15 -- `launch_qc`, `launch_bam_stats`, `launch_vcf_stats`,
  `launch_completeness`, `launch_misassembly_qc`, `launch_assembly_error_qc`,
  `launch_qv_qc`, `launch_continuity_qc`, `launch_build_index`, the three AI
  summary launches, and others -- enqueue jobs with no run record.
- `RunKind.REFERENCE_ASSEMBLY` has **no** launcher creating it, matching the
  unlinked `POLISH`/`SCAFFOLD` roles that `run.py`'s own comments flag as
  [#23](https://github.com/syntheticgio/bioflow/issues/23).

Keying on `RunKind` would therefore make most QC nodes unrepresentable --
precisely the nodes a user most wants as `continue_on_failure` leaves. The
registry gets its own key space, and `run_kind` is an optional *attribute* of a
spec rather than its identity.

### Exhaustiveness

Per CLAUDE.md's rules for hand-maintained registries keyed by an enum, this is
the third category: the keys belong to a set (`launch_*` functions) outside any
single enum, so full derivation is impossible and forcing it would be wrong.

The checkable invariant is the other direction. The registry carries a companion
`frozenset` of deliberately-excluded launches, each with a comment saying why
(e.g. `launch_summary` is an AI annotation, not a pipeline step;
`launch_build_index` is auto-attached by `launch_alignment` and should not be a
separate node), and a test asserts:

```
{every launch_* in services} == set(NODE_TYPES keys) | EXCLUDED_LAUNCHES
```

This is the `FORMAT_DERIVED_ROLES` / `COMPONENT_ORDER` pattern the codebase
already uses and tests. Without it, adding a launch function produces a tool that
silently cannot appear on the canvas -- the same failure shape as the STAR
`_SIDECAR_ROLES` bug, which cost a `build_index` job its eight index files while
the full suite stayed green.

Per CLAUDE.md, the registry must also be checked against real objects, not only
fixtures: a `docker compose exec api python -c "..."` pass confirming each spec's
declared ports match what its launcher actually accepts.

### The adapter problem

There is no `launch(kind, inputs, params)` to call. Each launcher has its own
hand-written keyword signature -- `launch_alignment` alone spans
`pipeline_service.py:1412` to ~1690, with reference checks, index dedup,
read-group defaults, chemistry resolution and platform presets inside it. Each
`NodeTypeSpec.launch` is a small hand-written adapter from the generic
`(bound_inputs, params)` shape onto one specific signature.

This is the bulk of the registry's implementation cost and should be estimated as
such.

## 6. Execution

On launch: validate bindings against port types, create the `WorkflowRun` and one
`WorkflowNodeRun` per node, then launch every node whose inputs are all
satisfied (initially, those fed only by `INPUT` nodes).

Nodes launch **progressively, as they become runnable** -- not all at once behind
`depends_on`.

This is the significant departure from how the queue works today, and it is
stated here rather than left to be discovered: **`depends_on` cannot express a
workflow edge.** It links job *ids* known at enqueue time, and a downstream
node's job does not exist yet -- it cannot be created until its input objects
exist, because the launchers validate their inputs. Workflow edges are resolved
by the orchestrator; `depends_on` continues to handle intra-launch ordering,
untouched.

A completion hook fires when a node's `PipelineRun` reaches a terminal state. It:

1. resolves the finished node's output objects,
2. binds them to downstream ports by `FormatKind`/`ObjectRole`, which the ingest
   path already sets,
3. launches any node whose inputs are now all satisfied.

Where a node produces several objects matching one port type, the `NodeTypeSpec`
output declaration names which output feeds which port -- the resolution is by
declared output name, with type as validation, not by type alone.

## 7. Deriving a definition from a run

A convenience that populates the canvas; it introduces no new persistence.

Read a selected set of `PipelineRun`s, map each to its node type, create an
`INPUT` node per `RunInput`, and infer edges where one run's output object id
appears in another's inputs. The result is an *unsaved* canvas the user edits and
saves.

`RunInput` already carrying `object_id`, `name`, and `role` is what makes the
input-node derivation nearly mechanical.

Runs whose launcher is in `EXCLUDED_LAUNCHES` or has no `NODE_TYPES` entry are
reported as skipped rather than silently dropped.

## 8. Decomposition into child issues

### 8.1 Models, registry, validation (#20)

`WorkflowDefinition`, `WorkflowRun`, `WorkflowNodeRun`; `NODE_TYPES` with its
exhaustiveness test and real-object check; port-type wire validation; definition
CRUD. No execution.

### 8.2 Orchestration

Completion hook, output→port resolution, progressive launch, retry-in-place,
cancellation, derived status.

### 8.3 Queue failure semantics

Branch-scoped failure propagation and `continue_on_failure` in
`queue/queue.py:_release_dependents`. Small, isolated, independently testable,
and a prerequisite for §1.3. Worth landing before 8.2.

### 8.4 Canvas UI

Graph editor, palette generated from the registry, live wire validation against
port types, binding dialog filtered by accepted type, save/load.

### 8.5 Activity presentation

Workflow-level progress derived from node states; expanded per-node job view;
consumes `run_ids` from the `job.progress` event added by
[#24](https://github.com/syntheticgio/bioflow/issues/24).

## 9. Boundary with #6 / #24

This epic **aggregates** progress and owns workflow-level state. It does not
invent per-job progress transport.

[#24](https://github.com/syntheticgio/bioflow/issues/24) added `run_ids:
list[str]` to the `job.progress` SSE event for this epic to consume. It is a
*list*, deliberately: a job can belong to more than one run, which is why `Job`
has no `run_id` field. Aggregation here must handle a job reporting into several
runs -- and, once workflows exist, into several workflow runs.

## 10. Risks

Two, both structural rather than incidental.

**The registry is a hand-maintained adapter over 24 differently-shaped launch
functions.** Its exhaustiveness test is the only thing standing between a new
tool and silent absence from the canvas. It is not optional.

**Progressive launch is new orchestration, not a reuse of `depends_on`.** The
completion hook is the workflow engine, and its recovery behaviour -- what
happens when the process dies between a node finishing and its successor
launching -- needs the same reconciler treatment `reconcile_queue` gives the
queue. A workflow run stuck with a finished node and an unlaunched successor is
the failure mode to design against in 8.2.
