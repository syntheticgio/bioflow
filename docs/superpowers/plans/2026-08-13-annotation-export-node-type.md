# Annotation Export Canvas Node Type Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `annotation_export` canvas node type so a workflow can express "export a filtered subset of an annotation" as a graph step.

**Architecture:** A new `NodeTypeSpec` in the hand-maintained `NODE_TYPES` registry, whose launch adapter auto-ensures the annotation results sidecar exists (mirroring how `launch_alignment` auto-attaches `build_index`). Two supporting changes make the node expressible at all: `PortType` gains multi-format acceptance so the port can take GFF/GTF/BED while rejecting GenBank, and the canvas parameter form learns to render statically-declared fields rather than only fetched aligner schemas.

**Tech Stack:** Python 3 / FastAPI / Beanie (backend), React / TypeScript (frontend), pytest.

**Spec:** [`docs/superpowers/specs/2026-08-13-annotation-export-node-type-design.md`](../specs/2026-08-13-annotation-export-node-type-design.md)

---

## Before you start

**This plan cannot be implemented in the `issue-217-brainstorm-27a8c6` worktree.** That worktree contains only `docs/` — no `backend/`, no `frontend/`. Work from the main checkout at `/Users/syntheticgio/Programming/local-bio-pipeliner`, or create a fresh worktree from this branch.

**Branch:** `docs/371-annotation-export-node-type` holds the spec and this plan. Implementation touches backend and frontend, so create a feature branch off it:

```bash
git checkout docs/371-annotation-export-node-type
git checkout -b feat/371-annotation-export-node-type
```

**Running tests.** From the main checkout:

```bash
docker compose exec api python -m pytest backend/tests/pipelines/test_node_types.py -q
```

From a worktree, use `./backend/run-worktree-tests.sh tests/ -q` instead — `docker compose exec api` silently tests main's code because the `api` container bind-mounts the main repo's `backend/`.

**A note on the registry.** `NODE_TYPES` and `EXCLUDED_LAUNCHES` in `backend/app/pipelines/node_types.py` form a *partition*: every `launch_*` must be in exactly one. Task 4 adds the spec and removes the exclusion **in the same commit** — splitting them across commits satisfies one exhaustiveness test while silently failing another (this is what went wrong on #355).

---

## File Structure

**Backend — modify:**
- `backend/app/models/workflow.py` — `PortType` gains multi-format acceptance (Task 1)
- `backend/app/pipelines/aligner_registry.py` — `ParamField.group` gains `"filters"` (Task 2)
- `backend/app/pipelines/node_types.py` — `NodeTypeSpec.param_fields`, the launch adapter, the spec, the exclusion removal (Tasks 3–5)
- `backend/app/api/v1/workflows.py` — serialize `param_fields` into the node-type catalog (Task 6)

**Backend — test:**
- `backend/tests/models/test_workflow_port_type.py` — create (Task 1)
- `backend/tests/pipelines/test_node_types.py` — modify (Tasks 4, 5)
- `backend/tests/api/test_workflows_node_types.py` — modify or create (Task 6)

**Frontend — modify:**
- `frontend/src/api/types.ts` — `PortType.formats`, `ParamFieldMeta.group`, `NodeTypeMeta.param_fields` (Task 7)
- `frontend/src/lib/workflowGraph.ts` — `portAccepts` mirrors the backend rule (Task 7)
- `frontend/src/components/workflow/ParamForm.tsx` — unknown-group fallback (Task 8)
- `frontend/src/components/workflow/NodeDetailPanel.tsx` — render static fields (Task 9)

---

## Task 1: `PortType` accepts several formats

Implements **AE-7**. Every port declared today is single-format and `PortType.accepts` is an equality check, so a GFF/GTF/BED port is not expressible. This must be backwards compatible: ~30 existing single-format `PortSpec` declarations keep working unchanged.

**Files:**
- Modify: `backend/app/models/workflow.py:37-60`
- Test: `backend/tests/models/test_workflow_port_type.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/models/test_workflow_port_type.py`:

```python
"""PortType's format matching, including the multi-format case.

The single-format path is the one ~30 existing PortSpec declarations use, so
it is tested here explicitly rather than assumed: a change to `formats` that
broke `format` would break every port on the canvas.
"""

from app.models.object import FormatKind, ObjectRole
from app.models.workflow import PortType


class TestSingleFormat:
    def test_accepts_its_own_format(self):
        port = PortType(format=FormatKind.BAM)
        assert port.accepts(FormatKind.BAM, None)

    def test_rejects_another_format(self):
        port = PortType(format=FormatKind.BAM)
        assert not port.accepts(FormatKind.VCF, None)

    def test_required_role_is_not_satisfied_by_an_absent_one(self):
        """The rule that stops a protein FASTA reaching a reference port."""
        port = PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE)
        assert not port.accepts(FormatKind.FASTA, None)
        assert not port.accepts(FormatKind.FASTA, ObjectRole.PROTEIN)
        assert port.accepts(FormatKind.FASTA, ObjectRole.REFERENCE)


class TestMultiFormat:
    def test_accepts_every_declared_format(self):
        port = PortType(formats=(FormatKind.GFF, FormatKind.GTF, FormatKind.BED))
        assert port.accepts(FormatKind.GFF, None)
        assert port.accepts(FormatKind.GTF, None)
        assert port.accepts(FormatKind.BED, None)

    def test_rejects_a_format_not_declared(self):
        port = PortType(formats=(FormatKind.GFF, FormatKind.GTF, FormatKind.BED))
        assert not port.accepts(FormatKind.GENBANK, None)

    def test_role_rule_still_applies_across_formats(self):
        port = PortType(
            formats=(FormatKind.GFF, FormatKind.GTF),
            role=ObjectRole.ANNOTATION,
        )
        assert port.accepts(FormatKind.GFF, ObjectRole.ANNOTATION)
        assert not port.accepts(FormatKind.GFF, None)

    def test_single_format_is_readable_as_a_set(self):
        """`accepted_formats` is what serialization and the frontend read, so
        it must be populated for single-format ports too."""
        port = PortType(format=FormatKind.BAM)
        assert port.accepted_formats == (FormatKind.BAM,)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker compose exec api python -m pytest backend/tests/models/test_workflow_port_type.py -q`

Expected: FAIL — `TestMultiFormat` errors with a Pydantic validation error on the unexpected `formats` keyword, and `test_single_format_is_readable_as_a_set` fails with `AttributeError: 'PortType' object has no attribute 'accepted_formats'`. `TestSingleFormat` should PASS already.

- [ ] **Step 3: Implement multi-format acceptance**

In `backend/app/models/workflow.py`, replace the `PortType` class (currently lines 37–60) with:

```python
class PortType(BaseModel):
    """What may flow down a wire.

    Reuses the two enums that already describe a file rather than inventing a
    parallel vocabulary. `role=None` means "any role for this format", which is
    the honest type for a port like QC's that genuinely does not care.

    A port names either one format (`format`, how nearly every port is
    declared) or several (`formats`). The pair exists because annotation
    export accepts GFF/GTF/BED while refusing GenBank, whose features span
    several lines -- a refusal worth making at design time on the canvas
    rather than at runtime in the handler. Read `accepted_formats`, never
    either field directly: it is the one place that knows both spellings.
    """

    format: FormatKind | None = None
    formats: tuple[FormatKind, ...] | None = None
    role: ObjectRole | None = None

    @model_validator(mode="after")
    def _exactly_one_spelling(self) -> "PortType":
        if (self.format is None) == (self.formats is None):
            raise ValueError("PortType needs exactly one of `format` or `formats`")
        if self.formats is not None and not self.formats:
            raise ValueError("PortType `formats` cannot be empty")
        return self

    @property
    def accepted_formats(self) -> tuple[FormatKind, ...]:
        """Every format this port accepts, however it was declared."""
        if self.formats is not None:
            return self.formats
        assert self.format is not None  # guaranteed by _exactly_one_spelling
        return (self.format,)

    def accepts(self, format: FormatKind, role: ObjectRole | None) -> bool:
        """Whether an object of this format/role may connect here.

        A required role is not satisfied by an absent one. An object with no
        role has not declared its intent, and treating that as a match is
        exactly the guess `ObjectRole` exists to prevent -- it is how a
        protein FASTA reaches an aligner's reference port.
        """
        if format not in self.accepted_formats:
            return False
        if self.role is None:
            return True
        return self.role == role
```

Add `model_validator` to the Pydantic import at the top of the file (it currently imports `BaseModel`; make it `from pydantic import BaseModel, model_validator`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `docker compose exec api python -m pytest backend/tests/models/test_workflow_port_type.py -q`

Expected: PASS, 8 tests.

- [ ] **Step 5: Run the full workflow and canvas suites for regressions**

`format` became optional, so anything constructing or reading a `PortType` is worth re-checking.

Run: `docker compose exec api python -m pytest backend/tests/pipelines/test_node_types.py backend/tests/models/ backend/tests/api/ -q`

Expected: PASS. If something reads `port.type.format` directly and now gets `None`, change it to `port.type.accepted_formats` — that is the field's whole purpose.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/workflow.py backend/tests/models/test_workflow_port_type.py
git commit -m "feat(models): let a canvas port accept several formats

Annotation export takes GFF/GTF/BED but must refuse GenBank, whose features
span several lines and whose segment rows correspond to no single line. Every
port declared until now names one format and accepts is an equality check, so
that port was not expressible -- the alternative was widening it to GenBank
and failing in the handler instead of on the canvas.

Read accepted_formats rather than either field: it is the one place that
knows both spellings, and single-format ports keep working untouched."
```

---

## Task 2: `ParamField.group` gains `"filters"`

Implements part of **AE-10**. The `group` literal is shared by the aligner registry and the canvas form.

**Files:**
- Modify: `backend/app/pipelines/aligner_registry.py:44`

- [ ] **Step 1: Widen the literal**

In `backend/app/pipelines/aligner_registry.py`, in the `ParamField` dataclass, change:

```python
    group: Literal["biology", "performance"] = "biology"
```

to:

```python
    # "filters" is the annotation-export node's group -- fields that select
    # which features to export, which are neither biology knobs nor
    # performance tuning. ParamForm renders any unrecognized group rather
    # than dropping it, so a fourth value here is not a silent failure.
    group: Literal["biology", "performance", "filters"] = "biology"
```

Also update the class docstring's `group` paragraph to mention the third value:

```python
    `group` is what keeps a generated form from becoming an undifferentiated
    pile of inputs: biology fields render in the dialog body, performance
    fields under the advanced disclosure -- which is roughly how AlignDialog
    was already organized by hand. `filters` is the annotation-export node's,
    rendered always-visible like biology.
```

- [ ] **Step 2: Verify nothing broke**

Run: `docker compose exec api python -m pytest backend/tests/pipelines/ -q`

Expected: PASS. This is a widening, so no existing value becomes invalid.

- [ ] **Step 3: Commit**

```bash
git add backend/app/pipelines/aligner_registry.py
git commit -m "feat(pipelines): allow a filters group on a generated param field

The annotation export node's seven fields select which features to export --
neither biology nor performance, the only two groups the literal allowed."
```

---

## Task 3: `NodeTypeSpec` carries static param fields

Implements **AE-14** and **AE-15**. Aligner parameters are fetched per-tool; filter parameters never vary, so they are declared on the spec.

**Files:**
- Modify: `backend/app/pipelines/node_types.py` (the `NodeTypeSpec` dataclass, currently ending ~line 101)

- [ ] **Step 1: Add the field**

In `backend/app/pipelines/node_types.py`, add to the `NodeTypeSpec` dataclass, after `tool_choice`:

```python
    # Parameter fields shown on the node itself, declared statically because
    # they do not vary by a chosen tool -- unlike an aligner's knobs, which
    # are fetched per-tool from aligner_registry. A node type may have these,
    # a tool_choice, both, or neither; the detail panel renders whichever it
    # finds.
    param_fields: tuple["ParamField", ...] = ()
```

Add the import near the top of the file, alongside the existing `tool_choice` import:

```python
from app.pipelines.aligner_registry import ParamField
```

If that import is circular (`aligner_registry` importing from `node_types`), instead import it under `TYPE_CHECKING` and quote the annotation as already written above. Check with:

```bash
docker compose exec api python -c "import app.pipelines.node_types"
```

- [ ] **Step 2: Verify the module still imports**

Run: `docker compose exec api python -c "import app.pipelines.node_types; print('ok')"`

Expected: `ok`

- [ ] **Step 3: Verify existing specs are unaffected**

Run: `docker compose exec api python -m pytest backend/tests/pipelines/test_node_types.py -q`

Expected: PASS. `param_fields` defaults to empty, so every existing spec is unchanged.

- [ ] **Step 4: Commit**

```bash
git add backend/app/pipelines/node_types.py
git commit -m "feat(pipelines): let a node type declare static parameter fields

Aligner knobs are fetched per-tool because they vary by tool. Annotation
export's filters do not vary at all, so they belong on the spec rather than
behind a schema endpoint."
```

---

## Task 4: The `annotation_export` node type

Implements **AE-1** through **AE-6**, **AE-8** through **AE-11**, **AE-21**, **AE-22**. The spec entry and the exclusion removal land together — see the partition note at the top of this plan.

**Files:**
- Modify: `backend/app/pipelines/node_types.py` (launch adapter, `NODE_TYPES` entry, `EXCLUDED_LAUNCHES` removal)
- Test: `backend/tests/pipelines/test_node_types.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/pipelines/test_node_types.py`, inside `class TestSpecs`:

```python
    def test_annotation_export_port_accepts_gff_gtf_bed_and_rejects_genbank(self):
        """The concrete rule this port's multi-format type exists for.

        GenBank is refused because its features span several lines and its
        segment rows correspond to no single line, so the export handler
        cannot subset it. Refusing on the canvas beats failing in the job.
        """
        spec = NODE_TYPES["annotation_export"]
        port = next(p for p in spec.inputs if p.name == "annotation")
        assert port.type.accepts(FormatKind.GFF, ObjectRole.ANNOTATION)
        assert port.type.accepts(FormatKind.GTF, ObjectRole.ANNOTATION)
        assert port.type.accepts(FormatKind.BED, ObjectRole.ANNOTATION)
        assert not port.type.accepts(FormatKind.GENBANK, ObjectRole.ANNOTATION)

    def test_annotation_export_declares_an_annotation_output(self):
        spec = NODE_TYPES["annotation_export"]
        assert [p.name for p in spec.outputs] == ["subset"]
        assert spec.outputs[0].type.role is ObjectRole.ANNOTATION

    def test_annotation_export_output_matches_its_input_formats(self):
        """The subset is written in the source file's own syntax, so the
        output is the same three-format set rather than one fixed format."""
        spec = NODE_TYPES["annotation_export"]
        source = next(p for p in spec.inputs if p.name == "annotation")
        assert spec.outputs[0].type.accepted_formats == source.type.accepted_formats

    def test_annotation_export_creates_no_pipeline_run(self):
        assert NODE_TYPES["annotation_export"].run_kind is None

    def test_annotation_export_declares_its_filter_fields(self):
        """Seven filters plus output_name. top_level_only and parent_status
        are deliberately absent: the handler force-sets the first, and the
        second is an artifact of the Results table's Unresolved view."""
        spec = NODE_TYPES["annotation_export"]
        keys = [f.key for f in spec.param_fields]
        assert keys == [
            "contig",
            "start_min",
            "start_max",
            "feature_type",
            "biotype",
            "name_query",
            "strand",
            "output_name",
        ]
        assert "top_level_only" not in keys
        assert "parent_status" not in keys

    def test_annotation_export_filter_fields_are_grouped_as_filters(self):
        spec = NODE_TYPES["annotation_export"]
        filters = [f for f in spec.param_fields if f.key != "output_name"]
        assert all(f.group == "filters" for f in filters)
```

Ensure the test file imports `FormatKind` and `ObjectRole` — it already imports them for `test_align_declares_a_reference_port_that_rejects_protein`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker compose exec api python -m pytest backend/tests/pipelines/test_node_types.py -q`

Expected: FAIL — six new tests error with `KeyError: 'annotation_export'`. `test_every_launch_function_is_classified` still PASSES (the launcher is still excluded).

- [ ] **Step 3: Write the launch adapter**

In `backend/app/pipelines/node_types.py`, add alongside the other `_launch_*` adapters:

```python
async def _launch_annotation_export(*, inputs: dict, params: dict, owner: str):
    """Export a filtered subset, computing the results sidecar first if needed.

    `export_annotation_subset` refuses to run without `features.db`, and only
    `launch_annotation_stats` writes one. Rather than let a user build a graph
    that fails for want of a precomputed sidecar, this ensures it the way
    `launch_alignment` auto-attaches `build_index` for an unindexed reference
    -- which is why `launch_build_index` is itself off the canvas.

    The cost, recorded rather than hidden: a stats job may run without
    appearing as a node. See the design doc for #371.
    """
    from app.services import pipeline_service

    object_id = inputs["annotation"]

    filters = {
        key: params.get(key)
        for key in (
            "contig",
            "start_min",
            "start_max",
            "feature_type",
            "biotype",
            "name_query",
            "strand",
        )
        if params.get(key) not in (None, "")
    }

    await pipeline_service.ensure_annotation_stats(object_id=object_id, owner=owner)

    return await pipeline_service.launch_annotation_export(
        object_id=object_id,
        owner=owner,
        filters=filters,
        output_name=params.get("output_name") or "",
    )
```

- [ ] **Step 4: Write the node type spec**

Add to `NODE_TYPES` in `backend/app/pipelines/node_types.py`, keeping the dict's existing ordering style:

```python
    "annotation_export": NodeTypeSpec(
        label="Export annotation subset",
        launch_name="pipeline_service.launch_annotation_export",
        run_kind=None,  # Derives an object but records no PipelineRun.
        launch=_launch_annotation_export,
        inputs=(
            PortSpec(
                "annotation",
                # GenBank is excluded deliberately -- see the port test.
                PortType(
                    formats=(FormatKind.GFF, FormatKind.GTF, FormatKind.BED),
                    role=ObjectRole.ANNOTATION,
                ),
            ),
        ),
        outputs=(
            PortSpec(
                "subset",
                PortType(
                    formats=(FormatKind.GFF, FormatKind.GTF, FormatKind.BED),
                    role=ObjectRole.ANNOTATION,
                ),
            ),
        ),
        param_fields=(
            ParamField(
                key="contig",
                label="Contig",
                kind="text",
                default=None,
                help="Only features on this sequence. Blank means every contig.",
                group="filters",
            ),
            ParamField(
                key="start_min",
                label="Start at",
                kind="int",
                default=None,
                help="Only features starting at or after this coordinate.",
                group="filters",
            ),
            ParamField(
                key="start_max",
                label="End before",
                kind="int",
                default=None,
                help="Only features starting at or before this coordinate.",
                group="filters",
            ),
            ParamField(
                key="feature_type",
                label="Feature type",
                kind="text",
                default=None,
                help="gene, exon, CDS, and so on. Blank means every type.",
                group="filters",
            ),
            ParamField(
                key="biotype",
                label="Biotype",
                kind="text",
                default=None,
                help="protein_coding, rRNA, and so on.",
                group="filters",
            ),
            ParamField(
                key="name_query",
                label="Name contains",
                kind="text",
                default=None,
                help="Substring match against the feature's name.",
                group="filters",
            ),
            ParamField(
                key="strand",
                label="Strand",
                kind="select",
                default=None,
                help="Blank means both strands.",
                group="filters",
                choices=(
                    Choice(value="", label="Both"),
                    Choice(value="+", label="Forward (+)"),
                    Choice(value="-", label="Reverse (-)"),
                ),
            ),
            ParamField(
                key="output_name",
                label="Name the result",
                kind="text",
                default=None,
                help="Blank names it after the source annotation.",
                group="filters",
            ),
        ),
    ),
```

Import `Choice` alongside `ParamField` from `app.pipelines.aligner_registry`.

- [ ] **Step 5: Remove the exclusion — same commit**

In `EXCLUDED_LAUNCHES`, delete this block entirely (the comment and the string):

```python
        # User-triggered from the Results tab's feature table, with an
        # arbitrary filter chosen interactively rather than wired from an
        # upstream node's output -- not a graph step in the sense the other
        # entries here are. It does produce a derived object (unlike its
        # siblings above), which is exactly why a real canvas node type is
        # worth designing properly rather than shoehorning in now: filters
        # have no fixed port shape to express as PortSpec inputs.
        # TODO(#371): design and add a canvas node type.
        "pipeline_service.launch_annotation_export",
```

- [ ] **Step 6: Settle the stats exclusion comment**

Still in `EXCLUDED_LAUNCHES`, replace the `launch_annotation_stats` entry's `TODO(#371)` line. Change:

```python
        # TODO(#371): a canvas node type may still be worth adding so a
        # workflow can express "compute annotation stats" as a graph step.
        "pipeline_service.launch_annotation_stats",
```

to:

```python
        # Settled in #371: no node type. Beyond producing no object, it
        # accepts four formats where a PortSpec names one set, and it already
        # runs at ingest -- a node would be a second trigger for something
        # that has usually happened. The export node ensures it on demand.
        "pipeline_service.launch_annotation_stats",
```

- [ ] **Step 7: Run the whole exhaustiveness class, not just one test**

Run: `docker compose exec api python -m pytest backend/tests/pipelines/test_node_types.py -q`

Expected: PASS, including all three of `test_every_launch_function_is_classified`, `test_exclusions_are_real_functions`, and `test_no_launcher_is_both_used_and_excluded`. If only the first passes, the exclusion removal in Step 5 was missed.

- [ ] **Step 8: Commit**

```bash
git add backend/app/pipelines/node_types.py backend/tests/pipelines/test_node_types.py
git commit -m "feat(pipelines): add a canvas node type for annotation subset export

Closes the half of #371 that had a derived object to wire. The port takes
GFF/GTF/BED and refuses GenBank, whose features span several lines -- the
export handler already refuses it, and refusing on the canvas turns a failed
job into an unwireable edge.

The spec entry and the EXCLUDED_LAUNCHES removal are one commit on purpose:
they are a partition, and splitting them satisfies one exhaustiveness test
while failing another, which is how #355 stayed red.

Annotation stats stays excluded, with its TODO replaced by the decision."
```

---

## Task 5: The launch adapter ensures the sidecar

Implements **AE-12**, **AE-13**, **AE-23**. Task 4 called `pipeline_service.ensure_annotation_stats`, which does not exist yet.

**Files:**
- Modify: `backend/app/services/pipeline_service.py` (near `launch_annotation_export`, ~line 2580)
- Test: `backend/tests/pipelines/test_node_types.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/pipelines/test_node_types.py` a new class:

```python
class TestAnnotationExportLaunch:
    """The adapter's sidecar handling -- the design's one implicit step."""

    @pytest.mark.asyncio
    async def test_computes_stats_first_when_the_sidecar_is_absent(self, monkeypatch):
        calls = []

        async def fake_ensure(*, object_id, owner):
            calls.append(("ensure", str(object_id)))

        async def fake_export(*, object_id, owner, filters, output_name):
            calls.append(("export", str(object_id)))
            return {"job_id": "j1"}

        monkeypatch.setattr(
            pipeline_service, "ensure_annotation_stats", fake_ensure
        )
        monkeypatch.setattr(
            pipeline_service, "launch_annotation_export", fake_export
        )

        spec = NODE_TYPES["annotation_export"]
        await spec.launch(
            inputs={"annotation": "64b7f0000000000000000001"},
            params={},
            owner="tester",
        )

        assert calls == [
            ("ensure", "64b7f0000000000000000001"),
            ("export", "64b7f0000000000000000001"),
        ]

    @pytest.mark.asyncio
    async def test_passes_only_the_filters_that_were_set(self, monkeypatch):
        """An empty box means "no bound", not a filter on empty string."""
        seen = {}

        async def fake_ensure(*, object_id, owner):
            return None

        async def fake_export(*, object_id, owner, filters, output_name):
            seen.update(filters=filters, output_name=output_name)
            return {"job_id": "j1"}

        monkeypatch.setattr(
            pipeline_service, "ensure_annotation_stats", fake_ensure
        )
        monkeypatch.setattr(
            pipeline_service, "launch_annotation_export", fake_export
        )

        spec = NODE_TYPES["annotation_export"]
        await spec.launch(
            inputs={"annotation": "64b7f0000000000000000001"},
            params={"contig": "chr1", "feature_type": "", "strand": None},
            owner="tester",
        )

        assert seen["filters"] == {"contig": "chr1"}

    @pytest.mark.asyncio
    async def test_no_filters_at_all_is_launchable(self, monkeypatch):
        """Exporting everything is a valid request."""
        seen = {}

        async def fake_ensure(*, object_id, owner):
            return None

        async def fake_export(*, object_id, owner, filters, output_name):
            seen.update(filters=filters)
            return {"job_id": "j1"}

        monkeypatch.setattr(
            pipeline_service, "ensure_annotation_stats", fake_ensure
        )
        monkeypatch.setattr(
            pipeline_service, "launch_annotation_export", fake_export
        )

        spec = NODE_TYPES["annotation_export"]
        await spec.launch(
            inputs={"annotation": "64b7f0000000000000000001"},
            params={},
            owner="tester",
        )

        assert seen["filters"] == {}
```

Add these imports to the test file if absent:

```python
import pytest
from app.services import pipeline_service
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker compose exec api python -m pytest backend/tests/pipelines/test_node_types.py::TestAnnotationExportLaunch -q`

Expected: FAIL — `AttributeError: module 'app.services.pipeline_service' has no attribute 'ensure_annotation_stats'` on the monkeypatch.

- [ ] **Step 3: Implement `ensure_annotation_stats`**

In `backend/app/services/pipeline_service.py`, add immediately before `launch_annotation_export` (~line 2580):

```python
async def ensure_annotation_stats(
    *, object_id: PydanticObjectId, owner: str
) -> None:
    """Compute the results sidecar if this annotation has none.

    `export_annotation_subset` raises PermanentError without `features.db`,
    and only `launch_annotation_stats` writes one. The canvas export node
    calls this first so a graph cannot fail purely for want of a precomputed
    sidecar -- the same auto-attach `launch_alignment` does for an unindexed
    reference.

    A no-op when the database is already on disk, which is the common case:
    ingest analyzes annotations automatically.
    """
    db_path = settings.annotation_stats_dir / str(object_id) / "features.db"
    if db_path.exists():
        return

    await launch_annotation_stats(object_id=object_id, owner=owner)
```

- [ ] **Step 4: Default the output name in `launch_annotation_export`**

Implements AE-12. In `launch_annotation_export`, after the `_check_annotation_stats_callable(ann)` line, add:

```python
    # A node left with the name box blank still has to produce a sensibly
    # named object rather than failing on a missing argument.
    if not output_name:
        output_name = f"{ann.name} (subset)"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `docker compose exec api python -m pytest backend/tests/pipelines/test_node_types.py -q`

Expected: PASS, including the three new `TestAnnotationExportLaunch` tests.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/pipelines/test_node_types.py
git commit -m "feat(pipelines): ensure the annotation sidecar before exporting a subset

export_annotation_subset refuses to run without features.db and only the
stats launcher writes one, so a canvas export node would otherwise be
buildable and guaranteed to fail. Ensured the way launch_alignment attaches
build_index, and a no-op in the common case where ingest already analyzed
the annotation.

A blank name box now yields '<source> (subset)' rather than a missing
argument."
```

---

## Task 6: Serve `param_fields` in the node-type catalog

Implements the serialization half of **AE-15**.

**Files:**
- Modify: `backend/app/api/v1/workflows.py:90-96` (`NodeTypeOut`) and `:201-262` (`list_node_types`)
- Test: `backend/tests/api/test_workflows_node_types.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/api/test_workflows_node_types.py` (create the file if absent, following the auth/fixture pattern of a neighbouring API test module):

```python
@pytest.mark.asyncio
async def test_node_type_catalog_serves_static_param_fields(client):
    """The canvas cannot render a form for fields it is never sent."""
    resp = await client.get("/api/v1/workflows/node-types")
    assert resp.status_code == 200

    by_type = {n["node_type"]: n for n in resp.json()}
    export = by_type["annotation_export"]

    keys = [f["key"] for f in export["param_fields"]]
    assert keys == [
        "contig",
        "start_min",
        "start_max",
        "feature_type",
        "biotype",
        "name_query",
        "strand",
        "output_name",
    ]
    assert all(f["group"] == "filters" for f in export["param_fields"][:7])


@pytest.mark.asyncio
async def test_node_types_without_static_fields_serve_an_empty_list(client):
    resp = await client.get("/api/v1/workflows/node-types")
    by_type = {n["node_type"]: n for n in resp.json()}
    assert by_type["qc"]["param_fields"] == []


@pytest.mark.asyncio
async def test_multi_format_port_serves_every_accepted_format(client):
    """The frontend mirrors the accept rule, so it needs the whole set."""
    resp = await client.get("/api/v1/workflows/node-types")
    by_type = {n["node_type"]: n for n in resp.json()}
    port = next(
        p for p in by_type["annotation_export"]["inputs"] if p["name"] == "annotation"
    )
    assert set(port["type"]["formats"]) == {"gff", "gtf", "bed"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `docker compose exec api python -m pytest backend/tests/api/test_workflows_node_types.py -q`

Expected: FAIL with `KeyError: 'param_fields'`.

- [ ] **Step 3: Add the field to `NodeTypeOut`**

In `backend/app/api/v1/workflows.py`, add to `NodeTypeOut`:

```python
    # Parameter fields declared on the spec itself, for node types whose
    # parameters do not vary by tool. Empty for most. The aligner's per-tool
    # schema is still fetched separately from /pipelines/aligner-schema.
    param_fields: list[dict] = []
```

- [ ] **Step 4: Populate it in `list_node_types`**

In the `result.append(NodeTypeOut(...))` call, add:

```python
                param_fields=[asdict(f) for f in spec.param_fields],
```

Add `from dataclasses import asdict` to the module imports. This mirrors `aligner_registry.schema_for`, which uses `asdict` for the same reason: a field added to `ParamField` reaches the form without a second edit here.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `docker compose exec api python -m pytest backend/tests/api/ -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/workflows.py backend/tests/api/test_workflows_node_types.py
git commit -m "feat(api): serve a node type's static parameter fields

The canvas cannot render a form for fields it is never sent. asdict rather
than a hand-written projection, matching aligner_registry.schema_for: a field
added to ParamField should reach the form without a second edit here."
```

---

## Task 7: Frontend types and the mirrored accept rule

Implements the frontend half of **AE-7**. `portAccepts` in `workflowGraph.ts` duplicates the backend's format check; without this the canvas refuses valid GFF/GTF/BED wires even though the backend accepts them.

**Files:**
- Modify: `frontend/src/api/types.ts:975-985, 2349-2352, 2384-2395`
- Modify: `frontend/src/lib/workflowGraph.ts:41-45`

- [ ] **Step 1: Update `PortType`**

In `frontend/src/api/types.ts`, replace the `PortType` interface:

```typescript
export interface PortType {
  /** Set when the port names exactly one format -- how nearly every port is
   *  declared. Null when it names several, in which case read `formats`. */
  format: string | null;
  /** Set when the port accepts several formats (annotation export takes
   *  GFF/GTF/BED but refuses GenBank). Null for single-format ports. */
  formats?: string[] | null;
  role: string | null;
}
```

- [ ] **Step 2: Update `ParamFieldMeta.group`**

In the same file, change:

```typescript
  group: "biology" | "performance";
```

to:

```typescript
  group: "biology" | "performance" | "filters";
```

- [ ] **Step 3: Add `param_fields` to `NodeTypeMeta`**

Add to the `NodeTypeMeta` interface:

```typescript
  /** Fields declared on the spec, for node types whose parameters do not
   *  vary by tool. Empty for most. The aligner's per-tool schema is fetched
   *  separately. */
  param_fields?: ParamFieldMeta[];
```

- [ ] **Step 4: Mirror the accept rule**

In `frontend/src/lib/workflowGraph.ts`, replace `portAccepts`:

```typescript
/** Every format a port accepts, however it was declared. The mirror of the
 *  backend's `PortType.accepted_formats` -- read this, never either field. */
function acceptedFormats(port: PortType): string[] {
  if (port.formats && port.formats.length > 0) return port.formats;
  return port.format ? [port.format] : [];
}

/** Whether an object of this format/role may enter a port of this type.
 *  The mirror of `PortType.accepts` on the backend: a required role is not
 *  satisfied by an absent one, which is what stops a protein FASTA reaching an
 *  aligner's reference port. */
export function portAccepts(port: PortType, produced: PortType): boolean {
  const producedFormats = acceptedFormats(produced);
  const accepted = acceptedFormats(port);
  if (!producedFormats.some((f) => accepted.includes(f))) return false;
  if (port.role === null || port.role === undefined) return true;
  return port.role === produced.role;
}
```

- [ ] **Step 5: Fix the rejection message**

At `frontend/src/lib/workflowGraph.ts:189`, the message reads `${produced.format}`, now possibly null. Change it to:

```typescript
      reason: `${candidate.to_port} does not accept ${acceptedFormats(produced).join("/") || "unknown"}/${role}.`,
```

- [ ] **Step 6: Typecheck**

Run: `cd frontend && npx tsc --noEmit`

Expected: no errors. If other call sites read `.format` directly and now fail on `string | null`, route them through `acceptedFormats` — export it if needed.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/lib/workflowGraph.ts
git commit -m "feat(frontend): mirror multi-format port acceptance on the canvas

portAccepts duplicates the backend's format check, so a port accepting
GFF/GTF/BED would have been refused on the canvas even though the API
accepts it -- the wire would simply never connect, with no error saying why."
```

---

## Task 8: `ParamForm` renders unrecognized groups

Implements **AE-18** and **AE-19**. `ParamForm` currently filters to `biology` and `performance`; a field in any other group vanishes with no error. This would have silently swallowed all seven filter fields.

**Files:**
- Modify: `frontend/src/components/workflow/ParamForm.tsx:72-108`

- [ ] **Step 1: Add the fallback**

Replace the body of `ParamForm` in `frontend/src/components/workflow/ParamForm.tsx`:

```typescript
export function ParamForm({ fields, values, onChange }: Props) {
  const [showAdvanced, setShowAdvanced] = useState(false);
  const biology = fields.filter((f) => f.group === "biology");
  const performance = fields.filter((f) => f.group === "performance");
  // Everything else, rather than nothing. A field in an unrecognized group
  // used to disappear with no error -- which would have swallowed the
  // annotation export node's seven filters in full. Rendering it in the wrong
  // place is a bug someone can see; omitting it is one nobody can.
  const other = fields.filter(
    (f) => f.group !== "biology" && f.group !== "performance",
  );

  const render = (field: ParamFieldMeta) => (
    <Field
      key={field.key}
      field={field}
      value={values[field.key]}
      onChange={(value) => onChange(field.key, value)}
    />
  );

  return (
    <div className="param-form">
      {biology.map(render)}
      {other.map(render)}
      {performance.length > 0 && (
        <>
          <button
            type="button"
            className="btn subtle"
            onClick={() => setShowAdvanced((v) => !v)}
          >
            {showAdvanced ? "Hide" : "Show"} performance settings
          </button>
          {showAdvanced && performance.map(render)}
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Typecheck**

Run: `cd frontend && npx tsc --noEmit`

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/workflow/ParamForm.tsx
git commit -m "fix(frontend): render param fields in unrecognized groups, not nothing

The form filtered to biology and performance and dropped anything else with
no error -- a field in a third group simply did not exist on screen. Found
while adding the annotation export node, whose seven filters would all have
vanished silently."
```

---

## Task 9: Render static fields in the node detail panel

Implements **AE-16** and **AE-17**. The Parameters section is currently gated on a `ToolChoice` and an `align`-only query.

**Files:**
- Modify: `frontend/src/components/workflow/NodeDetailPanel.tsx:44-56, 109-121`

- [ ] **Step 1: Resolve fields from two sources**

In `frontend/src/components/workflow/NodeDetailPanel.tsx`, after the existing `schema` query (~line 56), add:

```typescript
  // Two sources, one form: an aligner's knobs are fetched per-tool, while a
  // node type whose parameters never vary declares them on its spec. A node
  // may have either, and until now only the fetched kind rendered at all.
  const staticFields = meta?.param_fields ?? [];
  const fields = schema.data?.fields ?? staticFields;
```

- [ ] **Step 2: Ungate the Parameters section**

Replace the `{choice && (...)}` block (currently lines 109–121) with:

```typescript
      {fields.length > 0 && (
        <section>
          <h3>Parameters</h3>
          {choice && schema.isLoading && <p className="muted">Loading…</p>}
          <ParamForm
            fields={fields}
            values={node.params ?? {}}
            onChange={(key, value) => onChangeParam(node.node_id, key, value)}
          />
        </section>
      )}
```

- [ ] **Step 3: Typecheck**

Run: `cd frontend && npx tsc --noEmit`

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/workflow/NodeDetailPanel.tsx
git commit -m "feat(frontend): render parameters for nodes without a tool choice

The Parameters section was gated on a ToolChoice and an align-only query, so
a node with parameters but one tool rendered no form at all. Static fields
from the spec and the aligner's fetched schema now feed one form."
```

---

## Task 10: Verify against the real app

Implements **AE-24**, **AE-26**, **AE-27**. This repo has no headless frontend test setup, so the browser is the verification step — and per CLAUDE.md, a rule checked only against hand-built fixtures is how the Actions tab's suggestion rules passed a green suite while being wrong about real objects.

**Files:** none — verification only.

- [ ] **Step 1: Run the full backend suite**

From the main checkout:

```bash
docker compose exec api python -m pytest backend/tests/ -q
```

From a worktree:

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the count, not just the exit code.

- [ ] **Step 2: Bring up the stack**

From a worktree: `./ops/worktree-up.sh` (UI on 5273). From the main checkout: `docker compose up -d --build api web worker`  (UI on 5173).

Note `worker` does not hot-reload. `pipeline_service.py` changed, so restart it:

```bash
docker compose restart worker
```

- [ ] **Step 3: Confirm the node appears and its port types are enforced**

In the workflow canvas:

1. The palette shows **Export annotation subset**.
2. Dropping it and opening the detail panel shows a **Parameters** section with all eight fields — the AE-18 regression check, since these are the first `filters`-group fields ever rendered.
3. Wiring a GFF, GTF, or BED annotation into `annotation` connects.
4. Wiring a **GenBank** annotation is refused, with the rejection message naming the accepted formats.

- [ ] **Step 4: Run it against a real annotation**

Set `feature_type` to `gene` (or a type the annotation actually has), leave the rest blank, and run the workflow. Confirm:

- The job completes and a derived `ANNOTATION` object appears, named `<source> (subset)` since the name box was blank.
- Running against an annotation with **no** computed results still succeeds — the stats job runs first. Check the worker log for the `run_annotation_stats` job preceding `export_annotation_subset`.

- [ ] **Step 5: Confirm the aligner form still works**

The `align` node's Parameters section must still render its fetched per-tool fields, and changing the aligner dropdown must still re-fetch. This is AE-17 — the regression Task 9 could plausibly have caused.

- [ ] **Step 6: Tear down any stack you started**

```bash
./ops/worktree-up.sh --down
```

Leave the shared main stack on 5173 running. Per CLAUDE.md, orphaned worktree stacks wipe each other's test databases and cost an afternoon to trace.

- [ ] **Step 7: Update the issue and open the PR**

Comment on [#371](https://github.com/syntheticgio/bioflow/issues/371) recording that annotation stats was deliberately left off the canvas and why, so the decision is findable from the issue rather than only in the spec.

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --title "feat(pipelines): canvas node type for annotation subset export" --body "$(cat <<'EOF'
Adds an `annotation_export` canvas node so a workflow can express "export a
filtered subset of an annotation" as a graph step.

**Why the stats half of the issue is a "no".** #371 asks about two launchers.
The code makes them one question: `export_annotation_subset` refuses to run
without `features.db`, and only the stats launcher writes one. Stats stays
excluded (no output object, four input formats, already runs at ingest) and
the export node ensures the sidecar itself, the way `launch_alignment`
attaches `build_index`.

**Two things this needed beyond a registry entry.** `PortType` could only name
one format, so a port taking GFF/GTF/BED while refusing GenBank was not
expressible. And `ParamForm` silently dropped any field outside the `biology`
and `performance` groups -- which would have swallowed all seven filter fields
without a word.

Design: `docs/superpowers/specs/2026-08-13-annotation-export-node-type-design.md`

Closes #371

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Label the PR `type:feature` and `area:pipelines` — `.github/release.yml` categorizes by label, not by the title's prefix.

- [ ] **Step 8: Watch CI and fix what it finds**

`gh pr create` returns before any check runs. Poll until every check reports pass or fail:

```bash
gh pr checks <N>
```

```bash
gh pr view <N> --json mergeable,mergeStateStatus
```

CI runs `ruff check`, which catches import-order rules (`I001`) the local suite never invokes — this exact thing failed on #217/#314 with a green local run. Apply the minimal fix ruff suggests, push, and re-poll. Only report the PR URL once checks are green and `mergeable` is clean.

---

## Notes for the implementer

**On the partition.** If `test_every_launch_function_is_classified` passes but `test_no_launcher_is_both_used_and_excluded` fails, the `EXCLUDED_LAUNCHES` removal in Task 4 Step 5 was skipped. Run the whole `TestExhaustiveness` class every time, not the single test a failure names.

**On `PortType.format` becoming optional.** Task 1 makes it nullable. Anything reading `port.type.format` directly now risks `None`. The fix is always `accepted_formats` (backend) or `acceptedFormats` (frontend) — never re-widen a port to dodge the type error.

**On the implicit stats step.** It is deliberate and its cost is recorded in the spec: a user cannot see on the canvas that a stats job may run. Do not "fix" this by adding a stats node — that is the alternative the design rejected. If it becomes a real complaint, the answer is a way to display implicit steps, and this node adopts it first.

**On out-of-scope findings.** Per CLAUDE.md, file a GitHub issue yourself for unrelated problems you notice (with `type:`/`area:`/`status:` labels) and keep going. Do not fold them into this branch.
