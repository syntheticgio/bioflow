# Workflow canvas: tool selection, multi-valued inputs, and node detail

Design note for [#94](https://github.com/syntheticgio/bioflow/issues/94).

Follows on from
[2026-08-07-workflow-dag-design.md](2026-08-07-workflow-dag-design.md), which
established the definition/run/node-instance model this builds on. Nothing here
changes that split: a definition still holds no object ids, and binding still
happens per run.

## The problem

The canvas built for #79 works, and using it on a real graph exposed four gaps.
All four are the same shape -- the canvas models a node as less than the launcher
behind it actually is.

1. **Files can only be bound in the Run dialog.** An input node on the canvas
   shows a type ("Reads (FASTQ)") and nothing else. Which file it will carry is
   invisible until launch, so a graph reads as a shape rather than a plan.
2. **An aligner takes exactly one reads file.** Reads split across several
   files -- not mates, just chunks that all go in together -- have nowhere to go.
   `PortSpec` is scalar and `canConnect` refuses a second wire into an occupied
   port.
3. **The tool is not part of the node.** `align` renders one generic "Align to
   reference" box; which aligner runs is chosen in `AlignDialog` at launch time
   and is not in the graph at all. Two graphs that differ only in aligner are
   indistinguishable on the canvas.
4. **A node's parameters are unreachable.** `node.params` exists in the model and
   no UI writes to it. So is `continue_on_failure`, which the orchestrator
   honours and nothing can set.

## What this changes

Four changes, in dependency order. (1) is the foundation -- ports become a
function of the node rather than of its type alone -- and (3) is the surface
that makes (1) usable.

### 1. Tool selection on the node

`NodeTypeSpec` gains an optional `tool_choice`, declaring that a node type is
parameterized by a tool:

- which `params` key carries the choice (`aligner`, `caller`, `assembler`),
- the available options and the default,
- how a chosen tool maps to a port set and to a parameter schema.

**The chosen tool lives in `node.params[<key>]`.** This is not a new convention:
`_launch_variant_calling` already reads `params.get("caller")`, and
`launch_alignment` already takes its aligner through `params`. So no launcher
signature changes, and `workflow_derive` keeps working -- a run's tool is already
recorded on the `PipelineRun`, which is how `run_tool` tells the three
`REFERENCE_ASSEMBLY` node types apart today.

**Ports become resolvable per node.** `NodeTypeSpec.inputs`/`outputs` are static
tuples read by `node_type` alone. They gain a resolver, `ports_for(node)`, which
returns the port set given the node's chosen tool and falls back to the static
tuples for the node types with no tool choice -- which is most of them. The
node-type API grows per-tool port sets alongside the default set, so the canvas
can re-shape a node locally without a round trip.

**Changing the tool drops invalidated edges.** After a re-shape, every edge
touching the node is re-checked: a wire into a port that no longer exists, or
that no longer type-checks, is removed. This is `updateInput`'s existing rule for
input slots (`WorkflowCanvas.tsx`, the `if (patch.accepts)` branch) generalized
to action nodes. It runs on both sides -- the client so the user sees it
immediately, the server because `validate_definition` must reject a graph whose
edges contradict its nodes however it was constructed.

**Why one palette entry per capability, not per tool.** The palette lists "Align
to reference", and the node drops with the registry's default aligner already
selected so it is immediately wirable. The alternative -- "Align (minimap2)",
"Align (STAR)", "Align (bwa-mem2)" as separate node types -- keeps ports static
and needs none of the re-shaping machinery above, but the palette grows with
every tool (there are already seven aligners) and swapping a tool means deleting
and rewiring the node. Dropping with a default rather than unset means a
freshly-placed node is never in a state where it has no ports to wire.

### 2. Multi-valued ports and multi-file input slots

Two changes meeting in the middle. Both are needed: several *nodes* feeding one
port and one *slot* holding several files are different situations.

**Multi-valued ports.** `PortSpec` gains `multiple: bool = False`. A multi port
accepts N incoming edges. Only the "this port is already taken" rule in
`canConnect` relaxes -- type-checking is unchanged and applies to each wire
independently. At launch, `workflow_binding` collects the port's upstream outputs
into a list for the launcher.

**Multi-file input slots.** An input node gains `multiple: bool`. The slot binds
N objects and its single outgoing wire carries the set. `WorkflowRun.bindings` is
already a `list[WorkflowBinding]` keyed by `node_id`, so N rows sharing a
`node_id` expresses this with no schema change -- only the one-row-per-node
assumption in the binding resolver has to go.

**Where they meet.** A multi slot may only wire into a multi port; the reverse
would silently drop files. That is refused at connect time with a reason, not at
launch, where the user has forgotten what they wired. Several separate nodes into
one multi port is unrestricted.

**Validation.** A multi port with zero wires is unbound and fails exactly as a
required scalar port does. A multi slot holding one file is legal and behaves as
a scalar one.

**Scope.** `align.reads` is marked `multiple` here, because it is what #94 asks
for. `continuity_qc`'s `hifi_bam`/`nano_bam` and `differential_expression`'s
`counts` are the other two ports whose launchers genuinely take lists today --
both currently smuggle the set through `params`, and both are noted as scalar
approximations in `node_types.py`. They are left as follow-ups: each needs its
own decision about how the per-sample design travels, and neither is what this
issue is about. The `multiple` field is what unblocks them.

### 3. The node detail view

Double-clicking a node replaces the canvas body with a full-width panel for that
node, animating out from the node's canvas position. Escape or a back control
returns. Canvas state is untouched -- this is a view swap, not navigation.

The panel shows:

- **Identity.** Type, and an editable label.
- **Tool selector**, when the node type has one. Changing it here is the same
  operation as changing it on the canvas: ports re-shape, invalidated edges go,
  and the panel reports which wires were removed rather than discarding them
  silently.
- **Parameter form**, generated from the selected tool's field metadata --
  `aligner_registry.schema_for()` for aligners, and the same shape for other tool
  families. Fields group as `AlignDialog` already groups them: biology in the
  body, performance under an advanced disclosure. Values write to `node.params`.
- **Ports and their wiring.** Each input with its type and upstream source (or
  "not connected"); each output with its downstream consumers; multi ports list
  every wire. Read-only in v1 -- rewiring stays a canvas gesture.
- **`continue_on_failure`**, which the orchestrator honours and no UI can
  currently set.

**Why a panel and not an SVG camera transform.** The issue asks to "zoom us in to
just that node", and a literal camera zoom on the SVG is the direct reading. But
the substance of this screen is a form, and forms are HTML -- a real zoom would
mean `foreignObject` or form controls hand-built in SVG, both worse than the
animation is good. Animating the panel out of the node's position preserves what
the zoom was for: knowing which node you opened.

**Not in scope: refactoring `AlignDialog`.** The generated form here is the
component `AlignDialog` should eventually render from, but folding it in widens
the change without serving #94. `AlignDialog` works.

### 4. Binding files at the input node

The toolbar gains a **project selector**. It is what makes file selection
possible; with no project chosen, input nodes say so rather than showing an empty
list.

With a project chosen, each input node renders its binding inline: a selector over
that project's objects matching the slot's `accepts` type -- the filtering
`bindableObjects` already does for the Run dialog. Multi slots take several files.
The node shows the chosen filename rather than only its type, so a bound graph
reads as a concrete plan.

**These are per-run bindings.** They fill the same `bindings` map the Run dialog
builds. The definition still holds no object ids and stays reusable across
projects. Switching project clears them, for the reason the Run dialog already
clears them: they name objects the new project does not contain.

**The Run dialog stays**, reduced to a confirmation -- what is bound, what is
still unbound, and launch. When everything is bound on the canvas it is one
click. It remains a complete alternative path for anyone who would rather bind
there.

**Deliberately not saving bindings onto the definition.** Bindings live in canvas
state and are lost on reload, as today. Persisting them as defaults is the change
that would make a saved definition project-specific, which is the property the
schema was built to avoid. Nothing here forecloses an opt-in "save as defaults"
later.

## Testing

The repo has no component-testing setup for the frontend (no jsdom, zero
`.test.tsx`), so anything that must be checked automatically has to be pure and
live outside the component -- the rule `lib/workflowGraph.ts` already follows.

**Pure and unit-tested** (`lib/workflowGraph.ts` and its Python mirror):

- `canConnect` with multi ports: a second wire into a multi port is accepted, a
  second wire into a scalar port is still refused, and type-checking applies
  per-wire regardless.
- A multi slot into a scalar port is refused, with a reason naming the reason.
- The edge-invalidation rule: given a node, a new tool, and an edge set, which
  edges survive. Ports that vanish, ports that change type, ports that are
  untouched.
- `ports_for(node)`: the tool-parameterized case, the static fallback, and an
  unknown tool value.

**Backend** (`pytest`, run from a worktree via `./backend/run-worktree-tests.sh`):

- `validate_definition` rejects a graph whose edges contradict its nodes' ports
  after a tool change -- the server-side half of the invalidation rule.
- `workflow_binding` collects N upstream outputs into a list for a multi port,
  and N binding rows for one multi slot.
- The `node_types.py` exhaustiveness test extends to `tool_choice`: a
  tool-parameterized node type must resolve ports for every option it offers.
  Per CLAUDE.md's registry rules this is the third category -- keys owned outside
  any single enum -- so the invariant runs from the registry outward.

**Manual, in the browser** (`./ops/worktree-up.sh`, UI on 5273): everything
visual. Dropping a node and changing its tool, watching invalidated wires
disappear, the detail panel's form writing through to a launch, binding files on
input nodes, and a real multi-reads alignment running end to end.

Per CLAUDE.md, the multi-file and tool-selection paths get checked against a real
project's objects and not only against fixtures -- the suggestion-rules failure
that rule was written for was exactly a case of hand-built objects matching what
the code expected.

## Out of scope

- **[#93](https://github.com/syntheticgio/bioflow/issues/93)**, folding the
  Workflows section into Running. A separate change to the activity view,
  touching none of this.
- `continuity_qc` and `differential_expression` migrating to multi ports (above).
- `AlignDialog` rendering the shared generated form (above).
- Saving bindings as definition defaults (above).

## What shipped differently

Implemented across ten reviewed tasks (`docs/superpowers/plans/2026-08-08-workflow-canvas-node-detail.md`). Every deviation below was a real finding made while building, not a plan error caught in advance.

- **Two aligners genuinely cannot take several read files positionally, and neither can any of the other four.** The plan assumed this might be simple; reading `align_runner.py` directly showed every one of the six aligners' argument builders takes exactly `r1`/`r2`, with no shared multi-file convention (bowtie2/HISAT2 support comma-lists in their real CLIs, but this runner never builds that syntax; the other four have no equivalent at all). The only approach that works uniformly is concatenating extra read files into the primary before alignment, gzip-aware, in the queue handler (`align_handlers.py`, not `align_runner.py`, which has no filesystem access). This became Task 3.
- **`workflow_orchestrator._bound_inputs` had a real, silent overwrite bug**, found while implementing Task 2: two edges into one multi port used plain `inputs[edge.to_port] = value` assignment, so the second edge clobbered the first. Fixed in the same task, verified by reverting it and watching the new test fail with the exact overwrite symptom.
- **Task 4's `ports_for()` migration missed a live bug in `workflow_derive.py`**, caught by spec review rather than by the original plan: `_port_for_role`/`_accepts_for` took a bare `node_type: str`, so a real historical STAR-alignment run's `annotation`-role input silently failed to redraw as an edge when a graph was derived from run history. Fixed with a minimal `_NodeRef(node_type, params)` shim rather than threading a full `WorkflowNode` through call sites that never needed the rest of it.
- **A second migration gap was found and deliberately left open, tracked instead of fixed**: `workflow_orchestrator.py`'s `_is_multi_port` and the missing-required-input check in `_advance` still read the static spec, not `ports_for()`. Safe today only because STAR's `annotation` port is optional and no tool-added port is `multiple` yet — tracked in `docs/TODO.md` rather than expanded into Task 4's scope.
- **Task 7 found and fixed a rendering bug beyond its own remit**: the node- and edge-rendering blocks in `WorkflowCanvas.tsx` still derived ports from the static catalog even after `nodeHeight` (Task 6) had switched to `portsFor` — a STAR node's box was sized for four ports but only drew three dots. Both blocks now call `portsFor`.
- **The tool-name label's fixed vertical offset collided with the first port dot** for every aligner except the one the reviewer manually checked (and, on closer arithmetic, for that one too, at the boundary). Fixed by computing the label's position from the same `nodePortPosition` math `nodeHeight` already uses, so it never re-breaks at a different port count.
- **Task 8 skipped a whole new API endpoint and type it didn't need**: the plan called for a new `/workflows/tool-schema/{node_type}/{tool}` client method and `ParamField` type, but `api.alignerSchema`/`AlignerSchema`/`ParamFieldMeta` already existed for `AlignDialog` and call the identical backend function. The node detail panel reuses them directly; Task 5's new endpoint on the backend ended up serving the same data as this pre-existing one, which is a minor duplication left as-is rather than reworked mid-plan.
- **Task 9 found the same overwrite-shaped bug as Task 2, one layer up**: `_advance`'s `bindings = {b.node_id: b.object_id for b in run.bindings}` kept only the last `WorkflowBinding` row when several shared a `node_id` — exactly the shape a multi-file binding produces. Fixed with a scalar-then-list accumulator matching `_bound_inputs`'s existing convention; `_bound_inputs` itself needed no changes, since its list-flattening logic was already shaped as "however many ids this value holds," not "however many edges produced it."
- **STAR's `annotation` port is real and correctly wired end to end, but not yet reachable by any real user**: `WorkflowCanvas.tsx`'s `ACCEPT_CHOICES` list (what an input slot can be configured to accept) has no `gtf` entry, only `gff` — a distinct `FormatKind`. Tracked in `docs/TODO.md` as a one-line fix, deliberately not made part of this plan.
- **Two data-loss-shaped bugs (`_bound_inputs` in Task 2, `_advance`'s binding dict in Task 9) were each found and fixed inside the task that introduced their triggering condition, not in a later audit.** Both were the same shape — a plain dict assignment silently overwriting on a duplicate key — surfacing in two different functions because multi-valued ports and multi-valued bindings were built in separate tasks. Worth a note for whoever next adds a third "several of these share one key" concept to this graph model: grep for bare `dict[...] = value` assignments keyed by node/port before assuming the existing ones already accumulate.
