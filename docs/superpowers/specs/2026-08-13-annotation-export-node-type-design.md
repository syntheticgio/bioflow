# Canvas node type for annotation subset export

Design for [#371](https://github.com/syntheticgio/bioflow/issues/371).
Follow-up to [#358](https://github.com/syntheticgio/bioflow/issues/358)
(annotation subset export), where putting these on the canvas was explicitly
out of scope.

## Problem

Two launchers sit in `EXCLUDED_LAUNCHES` in
`backend/app/pipelines/node_types.py` rather than having a real
`NodeTypeSpec`, each carrying a `TODO(#371)`:

- `pipeline_service.launch_annotation_stats`
- `pipeline_service.launch_annotation_export`

The issue treats these as two independent decisions. They are not. Export
raises `PermanentError("this annotation has no computed results; compute them
before exporting")` when `features.db` is absent
(`backend/app/queue/annotation_handlers.py:307`), and only
`launch_annotation_stats` creates that database. Whether stats deserves a node
type is therefore the same question as whether an export node can stand alone.

## Decisions

### D1: `launch_annotation_stats` gets no node type

It stays in `EXCLUDED_LAUNCHES`. Three independent reasons:

1. It produces facts merged onto the object plus a SQLite sidecar — no output
   object, so no output port a downstream node could consume. Same class as
   `launch_gc_tracks`, `launch_meryl_analysis`, and `launch_annotate_genome`,
   all excluded for this reason already.
2. It accepts four formats (`_ANNOTATION_STATS_FORMATS`: GFF, GTF, BED,
   GenBank). A single `PortSpec` cannot represent that the way `vcf_stats` and
   `bam_stats` do for their one format each.
3. It already runs automatically at ingest (`backend/app/queue/results.py`,
   gated by `should_auto_analyze_annotation`). A canvas node would be a second
   trigger for something that has usually already happened.

**Source:** the issue's first scope question, answered "no".

The existing exclusion comment stays; its `TODO(#371)` line is replaced with a
settled decision and a pointer to this document.

### D2: `launch_annotation_export` gets a node type, `annotation_export`

It derives a real object (a filtered `ANNOTATION` subset), which is what
distinguishes it from every launcher in D1's class.

### D3: The stats dependency is auto-ensured, not wired

The export node's launch adapter ensures the results sidecar exists, enqueuing
`launch_annotation_stats` first when it does not and chaining the export behind
it.

This follows the precedent the registry already sets for
`launch_build_index`: `launch_alignment` calls
`_enqueue_build_index`/`build_index` itself when a reference is unindexed, and
`build_index` is excluded from the canvas precisely so a user cannot build a
graph that indexes twice or not at all.

**Alternatives considered:**

- *Stats as a real node with the feature DB as an output port, wired into
  export.* The honest dependency graph, and the only option that answers D1
  "yes". Rejected: it requires an output port for something that is not a
  `DataObject`, a departure from how every read-only launcher is modelled.
- *Export fails at runtime with the handler's existing message.* Rejected: it
  permits a graph that is guaranteed to fail, which is what the canvas's type
  system exists to prevent.

**Known cost, recorded deliberately:** a user reading the canvas cannot see
that a stats computation may run. This is the one place the design trades graph
truthfulness for usability. If the canvas later grows a way to display implicit
steps, this node should be the first to adopt it.

## Requirements

### Ports

**AE-1.** The `annotation_export` node type MUST declare exactly one input
port named `annotation`, required.

**AE-2.** The `annotation` port MUST accept objects of format GFF, GTF, and
BED.

**AE-3.** The `annotation` port MUST reject objects of format GenBank.

*Rationale for AE-3:* `export_annotation_subset` refuses GenBank outright
(`annotation_handlers.py:294`) because its features span several lines and its
segment rows correspond to no single line. Encoding the refusal in the port
means the canvas declines the wire at design time rather than failing the job
at runtime. Note this makes export's accepted set narrower than stats'.

**AE-4.** The node type MUST declare exactly one output port named `subset`
with `ObjectRole.ANNOTATION`.

**AE-5.** The `subset` port MUST declare the same three-format set as
`annotation`, not a single fixed format.

*Rationale for AE-5:* the subset is written in the source file's own syntax, so
the node does not always produce one format.

**AE-6.** `run_kind` MUST be `None`.

*Rationale:* the launcher creates no `PipelineRun`, consistent with the other
stats-and-export-class launchers.

### PortType extension

**AE-7.** `PortType` MUST be able to express a port accepting more than one
`FormatKind`, and `PortType.accepts` MUST return true for any format in that
set.

*Source:* `PortType.format` is a single `FormatKind` and `accepts` is an
equality check (`backend/app/models/workflow.py:56`). Every port declared today
is single-format; `quantify`'s annotation port sidesteps the question by
accepting GTF only. AE-2 is the first genuine union, so this is a new
capability rather than a use of an existing one.

The extension MUST be backwards compatible: every existing single-format
`PortSpec` declaration keeps working unchanged. Widening the port to accept
GenBank in order to avoid this extension is rejected — it would trade a
design-time refusal for a runtime failure, contradicting AE-3.

### Parameters

**AE-8.** The node type MUST expose exactly these seven filter parameters:

| key | kind | notes |
|---|---|---|
| `contig` | text | |
| `start_min` | int | empty means no lower bound |
| `start_max` | int | empty means no upper bound |
| `feature_type` | text | e.g. `gene`, `exon` |
| `biotype` | text | e.g. `protein_coding` |
| `name_query` | text | substring match |
| `strand` | select (`+` / `-` / unset) | |

**AE-9.** The node type MUST NOT expose `top_level_only` or `parent_status`.

*Rationale:* the handler force-sets `top_level_only=False` regardless of input
(`annotation_handlers.py:322`), so a control for it would do nothing.
`parent_status` expresses the Results table's "Unresolved" view and is
meaningless without that table in front of the user.

**AE-10.** All seven filter fields MUST carry `group: "filters"`.

**AE-11.** The node type MUST expose an `output_name` text parameter, which is
not a filter but names the derived object.

**AE-12.** When `output_name` is blank, the launch adapter MUST derive a name
from the source object's name plus a suffix.

*Rationale:* the launcher requires `output_name`; without a default, a node
left alone fails on a missing argument rather than doing the obvious thing.

**AE-13.** A node with all seven filters unset MUST be launchable, exporting
every feature.

*Rationale:* "export everything" is a valid subset request, and an empty
`FeatureFilters` is what the handler already builds from an empty filter dict.

**AE-14.** Filter parameters MUST be declared statically on the `NodeTypeSpec`
rather than fetched from an endpoint.

*Rationale:* unlike aligner parameters, they do not vary by a chosen tool.

The one runtime failure that remains is "no features matched the requested
filters" (`annotation_handlers.py:329`). Whether a filter matches anything is
not knowable at design time, so this is not preventable by typing.

### Frontend

**AE-15.** `NodeTypeSpec` MUST carry a static parameter-field declaration,
serialized into the node-type catalog the frontend already fetches.

**AE-16.** `NodeDetailPanel` MUST render a parameter form for a node type that
declares static fields.

*Source:* today the Parameters section is gated `{choice && ...}` with
`enabled: node.node_type === "align"`
(`frontend/src/components/workflow/NodeDetailPanel.tsx:54,109`). A node with
parameters but no `ToolChoice` renders no form at all. There is exactly one
parameter form in the canvas today and it only ever serves aligner parameters,
so this is not a matter of reusing a general mechanism — the general mechanism
does not exist.

**AE-17.** The aligner's existing dynamic `alignerSchema` fetch MUST keep
working unchanged.

*Rationale:* two sources of fields, one form. Migrating the aligner onto a
general per-node-type schema endpoint is the clean end state and is explicitly
out of scope here.

**AE-18.** `ParamForm` MUST render a field whose `group` it does not
recognize, rather than omitting it.

*Source:* `ParamForm` filters to `biology` and `performance`
(`frontend/src/components/workflow/ParamForm.tsx:74-75`); a field in any other
group vanishes with no error. This is a live trap that would have silently
swallowed all seven filter fields. Adding `filters` as a known group is
necessary but not sufficient — the fallback is what stops the next group from
disappearing.

**AE-19.** Unrecognized-group fields MUST render after `biology` and before the
performance disclosure, always visible rather than behind the toggle.

*Rationale:* filters are the point of this node, not advanced tuning.

No new `ParamForm` field kinds are needed: `text`, `int`, and `select` cover
all seven. The existing empty-means-`undefined` handling for `int`
(`ParamForm.tsx:57`) is already correct for optional bounds — a cleared box
means "no bound", not zero.

## Testing

**AE-20.** The full `TestExhaustiveness` class in
`backend/tests/pipelines/test_node_types.py` MUST pass.

*Rationale, per CLAUDE.md:* #355 landed a `NodeTypeSpec` and an exclusion for
the same launcher in two separate commits, satisfying
`test_every_launch_function_is_classified` while silently failing
`test_no_launcher_is_both_used_and_excluded` in the same class. Running only
the test a bug report names is how that stayed red.

**AE-21.** `launch_annotation_export` MUST be removed from `EXCLUDED_LAUNCHES`
in the same commit that adds its `NODE_TYPES` entry.

**AE-22.** A test MUST assert the `annotation` port accepts GFF, GTF, and BED
and rejects GenBank.

*Style reference:* `test_align_declares_a_reference_port_that_rejects_protein`
— assert the refusal, which is the direction that fails when the seam breaks.

**AE-23.** A test MUST assert the launch adapter enqueues stats first when the
sidecar is absent, and does not when it is present.

**AE-24.** `ParamForm`'s unknown-group rendering (AE-18) is verified manually
in the browser.

*Rationale:* this repo has no headless frontend test setup — no
jsdom/testing-library, zero `.test.tsx` files — and CLAUDE.md states none is
expected. Manual verification at the worktree stack's UI is the actual
verification step for anything UI-facing.

**AE-25.** The backend suite MUST be run with
`./backend/run-worktree-tests.sh tests/ -q`, not `docker compose exec api`.

*Rationale, per CLAUDE.md:* from a worktree, `docker compose exec api` silently
tests main's code, because the `api` container bind-mounts the main repo's
`backend/`.

**AE-26.** The canvas node MUST be exercised manually against a real
annotation on the worktree stack (`./ops/worktree-up.sh`, UI on 5273):
placed, wired, filtered, and launched, producing a derived object.

*Rationale, per CLAUDE.md:* a rule checked only against hand-built fixtures is
how the Actions tab's suggestion rules passed a green suite while being wrong
about real objects.

**AE-27.** The worktree stack MUST be brought down with
`./ops/worktree-up.sh --down` once verification is complete.

## Out of scope

- A node type for `launch_annotation_stats` (D1).
- Migrating the aligner's parameter form onto a general per-node-type schema
  endpoint (AE-17).
- Displaying auto-attached implicit steps on the canvas (D3's recorded cost).
- Contig autocomplete or a locus-window composite control. The seven fields are
  plain inputs; richer controls are a later refinement if asked for.
