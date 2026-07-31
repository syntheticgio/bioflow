# Variant Annotation Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make variant consequence annotation reachable — a launchable run that produces an annotated VCF, an Actions card that names the missing input when it cannot run, and the gene/consequence/AA columns in the variants table.

**Architecture:** A `resolve_annotation_inputs` service function answers "can this VCF be annotated, and with what" for both the card and the launcher, so they cannot disagree. A queue handler mirrors `run_vcf_stats`. The Actions card reports the specific missing input. The frontend adds three columns and a consequence filter.

**Tech Stack:** Python 3.12 + FastAPI + Motor/Mongo + Redis queue, pytest inside the `api` container. React 18 + TypeScript + vitest.

**Spec:** `docs/superpowers/specs/2026-07-31-variant-annotation-design.md`

**Predecessor:** `docs/superpowers/plans/2026-07-31-variant-annotation.md` — already merged. `csq_parse.py`, `csq_runner.py`, `tools.bcftools_csq()`, and the four index columns all exist and are tested. This plan makes them reachable.

**Run backend tests with:** `docker compose exec -T api python -m pytest tests/ -q` from the main repo root.

**After any change under `app/queue/`:** `docker compose restart worker`, or the job silently runs the old in-memory code. This plan adds a queue handler, so this applies to every manual test.

---

## What is already true

Verified against the live database before writing this plan — do not re-derive:

- **A VCF knows its reference.** `derived_from` on `DRR1066343.bcftools.vcf.gz` is `[<bam_id>, <reference_id>]`. The reference is already there; no new metadata is needed.
- **A GFF3 knows its reference** through `facts.ncbi_assembly_accession`, which matches the reference's own. Confirmed: both the T. brucei GFF3 and its FASTA carry `GCF_000002445.2`.
- **`bcftools csq` works and is probed.** `tools.bcftools_csq()` returns `True 1.21 None` on this image.
- **The index already has the columns.** `gene`, `consequence`, `aa_change`, `aa_pos`, plus a `consequence` filter on `VariantFilters`, and `QUERY_FORMAT` already emits `%INFO/BCSQ` at field 6.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `backend/app/services/pipeline_service.py` | Modify | `resolve_annotation_inputs()` + `launch_annotation()` |
| `backend/tests/services/test_annotation_inputs.py` | Create | Resolution rules, including every unavailable reason |
| `backend/app/pipelines/csq_runner.py` | Modify | `annotated_name()` — output naming |
| `backend/tests/pipelines/test_csq_runner.py` | Modify | Naming tests |
| `backend/app/queue/variant_handlers.py` | Modify | `annotate_variants` handler |
| `backend/tests/pipelines/test_annotate_launch.py` | Create | Payload and command wiring |
| `backend/app/services/suggestion_service.py` | Modify | `build_annotate_card()` + wire into `suggestions_for` |
| `backend/tests/services/test_suggestion_service.py` | Modify | Card availability and reason text |
| `backend/app/api/v1/pipelines.py` | Modify | `POST /annotate` route |
| `frontend/src/api/types.ts` | Modify | Three fields on `VariantRow` |
| `frontend/src/components/VariantTable.tsx` | Modify | Three columns + consequence filter |

---

### Task 1: Resolving annotation inputs

The one place that answers "can this VCF be annotated". Both the card and the launcher call it, so they cannot disagree about why something is unavailable — the drift the `VariantFilters` docstring warns about, in a different guise.

**Files:**
- Modify: `backend/app/services/pipeline_service.py`
- Test: `backend/tests/services/test_annotation_inputs.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/services/test_annotation_inputs.py`. Read `backend/tests/services/test_suggestion_service.py` first for how this repo builds `DataObject` fixtures, and follow that style rather than inventing another.

```python
"""Which VCFs can be annotated, and why not when they cannot.

Every unavailable reason is asserted, because the reason *is* the feature: a
card that says "cannot annotate" tells the user nothing, and all three real
projects are blocked on a different input.
"""

import pytest

from app.services import pipeline_service


class TestResolveAnnotationInputs:
    async def test_resolves_when_reference_and_annotation_are_present(
        self, annotatable_vcf
    ):
        got = await pipeline_service.resolve_annotation_inputs(annotatable_vcf)
        assert got.ok
        assert got.reference is not None
        assert got.annotation is not None
        assert got.reason is None

    # The real T. brucei case: reference and GFF3 both present, nothing called.
    async def test_unavailable_when_the_vcf_has_no_variants(self, empty_vcf):
        got = await pipeline_service.resolve_annotation_inputs(empty_vcf)
        assert not got.ok
        assert "no variants" in got.reason.lower()

    # The real yeast case.
    async def test_unavailable_when_no_annotation_accompanies_the_reference(
        self, vcf_without_gff
    ):
        got = await pipeline_service.resolve_annotation_inputs(vcf_without_gff)
        assert not got.ok
        assert "annotation" in got.reason.lower()
        # Names the action, not just the absence.
        assert "ncbi" in got.reason.lower()

    async def test_unavailable_when_the_reference_is_not_in_the_project(
        self, vcf_without_reference
    ):
        got = await pipeline_service.resolve_annotation_inputs(vcf_without_reference)
        assert not got.ok
        assert "reference" in got.reason.lower()

    # A GFF3 for a different assembly must not be paired with this reference:
    # csq would run and annotate nothing, which reads as success.
    async def test_ignores_an_annotation_for_a_different_assembly(
        self, vcf_with_mismatched_gff
    ):
        got = await pipeline_service.resolve_annotation_inputs(
            vcf_with_mismatched_gff
        )
        assert not got.ok
        assert "annotation" in got.reason.lower()
```

You must also write the five fixtures. Build them from real shapes:

- a VCF has `derived_from = [bam_id, reference_id]`, `role = ObjectRole.VARIANTS`, and `facts["vcf_stats_summary"]["variants"]` for the count;
- a reference has `role = ObjectRole.REFERENCE` and `facts["ncbi_assembly_accession"]`;
- a GFF3 has `role = ObjectRole.ANNOTATION` and the same `ncbi_assembly_accession`.

Use whatever async-mongo fixture the existing service tests use; do not stand up a second harness.

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec -T api python -m pytest tests/services/test_annotation_inputs.py -q`
Expected: `AttributeError: module 'app.services.pipeline_service' has no attribute 'resolve_annotation_inputs'`.

- [ ] **Step 3: Implement**

Add to `backend/app/services/pipeline_service.py`:

```python
@dataclass(frozen=True)
class AnnotationInputs:
    """What a VCF needs to be annotated, or why it cannot be.

    One result type for both the Actions card and the launcher. They asked the
    same question separately once before -- the align card and
    `resolve_reference` -- and disagreed about which references counted, which
    is how a project with one usable genome ended up refusing to align beside
    a card saying it could.
    """

    ok: bool
    reference: DataObject | None = None
    annotation: DataObject | None = None
    reason: str | None = None


async def resolve_annotation_inputs(vcf: DataObject) -> AnnotationInputs:
    """Find the reference and GFF3 for a VCF, or say what is missing.

    The reason text names the missing input and, where there is one, the
    action: "no annotation, download it from NCBI" is something a user can do,
    "cannot annotate" is not. All three projects on this machine are blocked on
    a different one of these, so every branch is reachable in practice.
    """
    variants = 0
    summary = vcf.facts.get("vcf_stats_summary") or {}
    if isinstance(summary, dict):
        variants = summary.get("variants") or 0
    if not variants:
        return AnnotationInputs(
            ok=False,
            reason=(
                "This VCF has no called variants to annotate. Compute its "
                "results first, or call variants against a reference."
            ),
        )

    reference = None
    for parent_id in vcf.derived_from or []:
        parent = await DataObject.get(parent_id)
        if parent is not None and parent.role is ObjectRole.REFERENCE:
            reference = parent
            break

    if reference is None:
        return AnnotationInputs(
            ok=False,
            reason=(
                "The reference this VCF was called against isn't in this "
                "project, so there is nothing to read genes from."
            ),
        )

    accession = reference.facts.get("ncbi_assembly_accession")
    candidates = await object_service.list_objects(
        vcf.project_id, limit=500, status=ObjectStatus.READY
    )
    annotation = None
    for obj in candidates:
        if obj.role is not ObjectRole.ANNOTATION:
            continue
        # Match on assembly accession rather than taking any GFF3 in the
        # project. An annotation for a different assembly parses fine and
        # annotates nothing, which reads as a successful run producing an
        # empty column -- worse than refusing.
        if accession and obj.facts.get("ncbi_assembly_accession") != accession:
            continue
        annotation = obj
        break

    if annotation is None:
        return AnnotationInputs(
            ok=False,
            reference=reference,
            reason=(
                "No annotation (GFF3) for this reference. Download it from "
                "NCBI alongside the genome — the assembly download offers it."
            ),
        )

    return AnnotationInputs(ok=True, reference=reference, annotation=annotation)
```

Add `from dataclasses import dataclass` to the imports if absent, and check that `DataObject`, `ObjectRole`, `ObjectStatus` and `object_service` are already imported in this module — they are used elsewhere in it, but confirm rather than assume.

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec -T api python -m pytest tests/services/test_annotation_inputs.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/services/test_annotation_inputs.py
git commit -m "feat: resolve the reference and annotation behind a VCF"
```

---

### Task 2: The annotation queue handler

**Files:**
- Modify: `backend/app/queue/variant_handlers.py`
- Test: `backend/tests/pipelines/test_annotate_launch.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_annotate_launch.py`:

```python
"""Wiring for the annotation run.

The handler itself shells out and is verified manually against real data;
what is worth testing here is that the payload carries every input the command
needs, since a missing one fails thirty seconds into a job rather than at
launch.
"""

import pytest

from app.errors import PermanentError
from app.queue import variant_handlers


class TestAnnotateVariantsPayload:
    def test_requires_an_object_id(self):
        ctx = _ctx(payload={})
        with pytest.raises(PermanentError, match="object_id"):
            variant_handlers.annotate_variants(ctx)

    def test_requires_a_reference(self):
        ctx = _ctx(payload={"object_id": "abc"})
        with pytest.raises(PermanentError, match="reference"):
            variant_handlers.annotate_variants(ctx)

    def test_requires_an_annotation(self):
        ctx = _ctx(payload={"object_id": "abc", "reference_sha256": "d" * 64})
        with pytest.raises(PermanentError, match="annotation"):
            variant_handlers.annotate_variants(ctx)
```

Write `_ctx` as a small helper building a `JobContext` double. Read `backend/tests/pipelines/test_vcf_stats_launch.py` first — it already constructs one, and this should reuse that approach rather than inventing a second.

- [ ] **Step 2: Run tests to verify they fail**

Expected: `AttributeError: module 'app.queue.variant_handlers' has no attribute 'annotate_variants'`.

- [ ] **Step 3: Add `annotated_name` to `csq_runner.py`**

The existing `_rename_output` cannot be reused — see the comment in the handler below for why. Add this to `backend/app/pipelines/csq_runner.py`, and a test for it in the existing `backend/tests/pipelines/test_csq_runner.py`:

```python
# Suffixes a VCF may arrive with, longest first so `.vcf.gz` is stripped whole
# rather than leaving a stray `.vcf`.
_VCF_SUFFIXES = (".vcf.gz", ".vcf", ".bcf")


def annotated_name(vcf_name: str) -> str:
    """The output name for an annotated copy of `vcf_name`.

    Not `variant_runner.output_name`, which takes `Path(name).stem` because
    its input is a BAM. A `.vcf.gz` has a double extension, so the stem keeps
    the inner `.vcf` and the result is `foo.bcftools.vcf.csq.vcf.gz`.
    """
    for suffix in _VCF_SUFFIXES:
        if vcf_name.endswith(suffix):
            return f"{vcf_name[: -len(suffix)]}.csq.vcf.gz"
    return f"{vcf_name}.csq.vcf.gz"
```

Tests, appended to `test_csq_runner.py`:

```python
class TestAnnotatedName:
    # The trap: Path("x.bcftools.vcf.gz").stem is "x.bcftools.vcf", so the
    # BAM-oriented helper would produce "x.bcftools.vcf.csq.vcf.gz".
    def test_strips_a_double_extension_whole(self):
        assert (
            csq_runner.annotated_name("DRR1066343.bcftools.vcf.gz")
            == "DRR1066343.bcftools.csq.vcf.gz"
        )

    def test_handles_a_plain_vcf(self):
        assert csq_runner.annotated_name("sample.vcf") == "sample.csq.vcf.gz"

    def test_handles_a_bcf(self):
        assert csq_runner.annotated_name("x.bcf") == "x.csq.vcf.gz"

    def test_falls_back_on_an_unrecognised_name(self):
        assert csq_runner.annotated_name("weird") == "weird.csq.vcf.gz"
```

Run: `docker compose exec -T api python -m pytest tests/pipelines/test_csq_runner.py -q` — expect 4 new tests passing alongside the existing 8.

- [ ] **Step 4: Implement the handler**

Add to `backend/app/queue/variant_handlers.py`, below `run_vcf_stats`. Add `csq_runner` to the existing `from app.pipelines import ...` line.

```python
@handler(
    "annotate_variants",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
)
def annotate_variants(ctx: JobContext) -> dict:
    """Add consequence annotations to a VCF with `bcftools csq`.

    Produces a new VCF object rather than mutating the input: the original is
    what the caller actually emitted, and an annotation run is a derivation of
    it like every other step here.

    GFF3 parse warnings are expected on real NCBI annotations -- the T. brucei
    file emits three kinds -- and are logged rather than failed on. See
    `csq_runner.is_benign_gff_warning`.
    """
    bcftools = tools.require(tools.bcftools_csq())

    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("annotate_variants requires an 'object_id'")
    if not (ctx.payload.get("reference_sha256") or ctx.payload.get("reference_path")):
        raise PermanentError("annotate_variants requires a reference")
    if not (
        ctx.payload.get("annotation_sha256") or ctx.payload.get("annotation_path")
    ):
        raise PermanentError("annotate_variants requires an annotation (GFF3)")

    work = _prepare_workdir(ctx, "annotate")

    vcf = _named_link(work, _resolve_blob(ctx.payload, "vcf"), ctx.payload.get("vcf_name"))
    reference = _named_link(
        work, _resolve_blob(ctx.payload, "reference"), ctx.payload.get("reference_name")
    )
    annotation = _named_link(
        work,
        _resolve_blob(ctx.payload, "annotation"),
        ctx.payload.get("annotation_name"),
    )

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # csq needs a .fai beside the reference, exactly as the aligners do.
    ctx.progress(phase="index", pct=0.1, message="indexing the reference")
    code = run_subprocess(
        ctx,
        [tools.require(tools.samtools()).path, "faidx", str(reference)],
        log_path=str(log_path),
    )
    if code != 0:
        raise _failure(code, log_path, "samtools faidx")

    ctx.progress(phase="annotate", pct=0.3, message="calling consequences")
    out = work / "annotated.vcf.gz"
    code = run_subprocess(
        ctx,
        csq_runner.build_csq_command(
            bcftools_path=bcftools.path,
            vcf=vcf,
            reference=reference,
            annotation=annotation,
            out=out,
        ),
        log_path=str(log_path),
    )
    if code != 0:
        raise _failure(code, log_path, "bcftools csq")

    # Not `_rename_output`: it calls `variant_runner.output_name`, which takes
    # `Path(name).stem` on the assumption its input is a BAM. Handed a
    # `.vcf.gz` that yields `DRR1066343.bcftools.vcf.csq.vcf.gz` -- the stem of
    # a double extension keeps the inner `.vcf`. Verified, not guessed.
    final = out.parent / csq_runner.annotated_name(vcf.name)
    if final != out:
        out.rename(final)
    produced = final
    index = _index_vcf(ctx, produced, log_path)

    return {
        "produced": str(produced),
        "index": str(index),
        "tool": "bcftools csq",
        "tool_version": bcftools.version,
    }
```

`_rename_output` and `_index_vcf` already exist in this file and are used by `call_variants` — read their signatures before calling them and adapt if they differ from the above.

- [ ] **Step 5: Add the launcher**

Add to `backend/app/services/pipeline_service.py`, modelled on `launch_vcf_stats`:

```python
async def launch_annotation(*, object_id: PydanticObjectId):
    """Queue a consequence-annotation run for a VCF.

    Resolution goes through `resolve_annotation_inputs` rather than repeating
    the rules, so a launch cannot succeed where the card said it could not, or
    the reverse.
    """
    from app.queue import queue

    tools.require(tools.bcftools_csq())

    vcf = await DataObject.get(object_id)
    if vcf is None:
        raise NotFoundError(f"Object not found: {object_id}")

    inputs = await resolve_annotation_inputs(vcf)
    if not inputs.ok:
        raise ValidationError(inputs.reason)

    payload: dict = {
        "object_id": str(vcf.id),
        "project_id": str(vcf.project_id),
        "vcf_name": vcf.name,
        "reference_name": inputs.reference.name,
        "annotation_name": inputs.annotation.name,
    }
    for key, obj in (
        ("vcf", vcf),
        ("reference", inputs.reference),
        ("annotation", inputs.annotation),
    ):
        digest, path = await _resolve_readable(obj)
        if digest:
            payload[f"{key}_sha256"] = digest
        if path:
            payload[f"{key}_path"] = path

    return await queue.enqueue(
        "annotate_variants",
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
        max_attempts=2,
        dedup_key=f"annotate:{vcf.id}",
        project_id=vcf.project_id,
        object_id=vcf.id,
    )
```

- [ ] **Step 6: Add the route**

In `backend/app/api/v1/pipelines.py`, beside the `/vcfstats` route. Add an `AnnotateRequest` model matching how `VcfStatsRequest` is declared in this file:

```python
@router.post("/annotate", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_annotate(body: AnnotateRequest) -> JobOut:
    """Queue consequence annotation for a called VCF."""
    job = await pipeline_service.launch_annotation(object_id=body.object_id)
    return JobOut.model_validate(job.model_dump())
```

- [ ] **Step 7: Run the tests and restart the worker**

```bash
docker compose exec -T api python -m pytest tests/ -q
docker compose restart worker
```

Expected: no failures. The restart matters — the handler is new, and the worker holds the old module list until restarted.

Confirm it registered:

```bash
docker compose logs worker 2>&1 | grep -i handlers_loaded | tail -1
```

Expected: `annotate_variants` appears in the list.

- [ ] **Step 8: Commit**

```bash
git add backend/app/pipelines/csq_runner.py backend/tests/pipelines/test_csq_runner.py backend/app/queue/variant_handlers.py backend/app/services/pipeline_service.py backend/app/api/v1/pipelines.py backend/tests/pipelines/test_annotate_launch.py
git commit -m "feat: add the variant annotation run"
```

---

### Task 3: The Actions card

**Files:**
- Modify: `backend/app/services/suggestion_service.py`
- Test: `backend/tests/services/test_suggestion_service.py`

Per CLAUDE.md, registering a tool without this leaves a dead card — a rule that can never pick the new tool means it will never be suggested however cleanly it installs.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/services/test_suggestion_service.py`, following its existing fixture style:

```python
class TestAnnotateCard:
    """Per CLAUDE.md, assert the *unavailable* direction hardest: the image
    ships bcftools 1.21, so an availability assertion passes whether or not
    the patch worked."""

    def test_no_card_on_a_non_vcf(self, bam_object):
        assert suggestion_service.build_annotate_card(bam_object, None) is None

    def test_available_when_inputs_resolve(self, vcf_object, resolved_inputs):
        card = suggestion_service.build_annotate_card(vcf_object, resolved_inputs)
        assert card.status is CardStatus.AVAILABLE

    def test_unavailable_reason_comes_from_the_resolver(self, vcf_object):
        inputs = pipeline_service.AnnotationInputs(
            ok=False, reason="No annotation (GFF3) for this reference."
        )
        card = suggestion_service.build_annotate_card(vcf_object, inputs)
        assert card.status is CardStatus.UNAVAILABLE
        assert card.reason == "No annotation (GFF3) for this reference."

    def test_unavailable_when_csq_is_missing(self, vcf_object, resolved_inputs, monkeypatch):
        monkeypatch.setattr(
            tools,
            "bcftools_csq",
            lambda: tools.Tool(
                name="bcftools csq", path=None, version=None, error="too old"
            ),
        )
        card = suggestion_service.build_annotate_card(vcf_object, resolved_inputs)
        assert card.status is CardStatus.UNAVAILABLE
        assert "csq" in card.reason.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Expected: `AttributeError: ... has no attribute 'build_annotate_card'`.

- [ ] **Step 3: Implement the card**

Add to `backend/app/services/suggestion_service.py`, following the shape of `build_variants_card`:

```python
def build_annotate_card(obj, inputs) -> SuggestionCard | None:
    """Consequence annotation for a called VCF.

    The reason text is the resolver's, not this card's. Two places deciding
    why something is unavailable drift, and the card is the one the user
    reads -- so it must say exactly what the launcher would enforce.
    """
    if obj.format.kind not in (FormatKind.VCF, FormatKind.BCF):
        return None

    csq = tools.bcftools_csq()
    if not csq.available:
        return SuggestionCard(
            kind="annotate",
            category="ANNOTATE",
            title="Annotate variants",
            description="Add gene and protein consequences to these variants.",
            status=CardStatus.UNAVAILABLE,
            reason=csq.error or "bcftools csq is unavailable.",
        )

    if inputs is None or not inputs.ok:
        return SuggestionCard(
            kind="annotate",
            category="ANNOTATE",
            title="Annotate variants",
            description="Add gene and protein consequences to these variants.",
            status=CardStatus.UNAVAILABLE,
            reason=(inputs.reason if inputs else "Inputs could not be resolved."),
        )

    return SuggestionCard(
        kind="annotate",
        category="ANNOTATE",
        title="Annotate variants",
        description=(
            f"Read genes from {inputs.annotation.name} and record what each "
            f"variant does to a protein."
        ),
        status=CardStatus.AVAILABLE,
        # The key is `body`, not `payload`, and it must be the *complete* JSON
        # body for the endpoint -- the frontend posts it verbatim and adds
        # nothing, because the launch endpoints do not share a request shape.
        # An available card without a usable body renders as a button that
        # does nothing.
        launch={
            "endpoint": "/api/v1/pipelines/annotate",
            "body": {"object_id": str(obj.id)},
        },
    )
```

`SuggestionCard`'s fields are `kind`, `category`, `title`, `description`, `why`, `status`, `reason`, `launch` — `why` is optional and unused here. Match the existing cards for anything not shown above.

- [ ] **Step 4: Wire it into `suggestions_for`**

The resolution is async, so it must be awaited in `suggestions_for` and passed in — the builders are deliberately synchronous and pure. Beside the existing `chemistry` block:

```python
    annotation_inputs = None
    if obj.format.kind in (FormatKind.VCF, FormatKind.BCF):
        # VCF only, and awaited here rather than inside the builder: it walks
        # provenance to the reference and lists the project for a GFF3, which
        # keeps the builders uniformly synchronous.
        annotation_inputs = await pipeline_service.resolve_annotation_inputs(obj)
```

and add to the `builders` tuple, after `variants`:

```python
        ("annotate", lambda: build_annotate_card(obj, annotation_inputs)),
```

- [ ] **Step 5: Run the tests**

```bash
docker compose exec -T api python -m pytest tests/ -q
```

- [ ] **Step 6: Check the card against the real database**

CLAUDE.md is explicit that a green suite is not enough here — the suggestion rules once passed fully green while being wrong about real files.

```bash
docker compose exec -T api python -c "
import asyncio
from app.db.client import connect_to_mongo, get_db
from app.models.object import DataObject
from app.services import suggestion_service
async def m():
    await connect_to_mongo()
    db = get_db()
    async for doc in db.objects.find({'role': 'variants'}):
        obj = DataObject.model_validate(doc)
        for card in await suggestion_service.suggestions_for(obj):
            if card['kind'] == 'annotate':
                print(obj.name, '->', card['status'], '|', card.get('reason'))
asyncio.run(m())
"
```

Expected, from the real data on this machine: the two T. brucei VCFs report no variants to annotate; the yeast VCF reports the missing GFF3. Any card claiming *available* here is wrong, because no project currently has all three inputs — if you see one, stop and investigate rather than proceeding.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat: add the annotate card, naming the missing input"
```

---

### Task 4: Consequence columns in the variants table

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/components/VariantTable.tsx`

Frontend tests run on the host: `cd frontend && npx vitest run`. There is no headless component-testing setup in this repo and none is expected — verification is manual, in Task 5.

- [ ] **Step 1: Add the fields to `VariantRow`**

In `frontend/src/api/types.ts`, extend the existing interface:

```ts
export interface VariantRow {
  chrom: string;
  pos: number;
  ref: string;
  alt: string;
  qual: number | null;
  filter: string;
  dp: number | null;
  gt: string;
  /** Present only on an annotated VCF; null on every row of an un-annotated
   *  one, which is the common case. */
  gene: string | null;
  consequence: string | null;
  aa_change: string | null;
  aa_pos: number | null;
}
```

- [ ] **Step 2: Add the columns**

In `frontend/src/components/VariantTable.tsx`, add three header cells after `<th>Alt</th>`:

```tsx
                <th>Gene</th>
                <th>Consequence</th>
                <th>AA change</th>
```

and the matching body cells after the Alt `<td>`:

```tsx
                  {/* Empty on every row of an un-annotated VCF, which is the
                      common case -- so this renders as an ordinary dash
                      rather than anything that suggests something failed. */}
                  <td className="mono">{row.gene ?? "—"}</td>
                  <td>{row.consequence ?? "—"}</td>
                  <td className="mono">{row.aa_change ?? "—"}</td>
```

- [ ] **Step 3: Add the consequence filter**

The existing filters (contig, filter, type, min QUAL) each have a control and a piece of query state. Add a consequence dropdown beside them, following that pattern exactly.

Populate its options from the rows the server returns rather than a hardcoded list — a hardcoded vocabulary would silently omit any consequence type bcftools emits that was not anticipated, which is the failure the parser's unknown-rank fallback already guards against on the backend.

Wire it into the query key and the request parameters the same way `filterValue` is, and reset the page to 0 on change alongside the other filters.

- [ ] **Step 4: Verify it compiles and tests pass**

```bash
cd frontend && npx tsc --noEmit && npx vitest run
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/components/VariantTable.tsx
git commit -m "feat: show gene, consequence and amino-acid change per variant"
```

---

### Task 5: Merge to main and verify end to end

**Files:** none. Manual verification is the actual check for UI work in this repo, and the annotation run has never executed through the queue.

- [ ] **Step 1: Merge and rebuild from the main repo root**

Never from a worktree — the Compose bind mounts are relative paths, and running from one silently repoints the shared stack.

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && git merge <branch> --no-edit && docker compose up -d --build api web worker
```

- [ ] **Step 2: Confirm the stack is on main and the handler registered**

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' | grep -c worktrees
docker compose logs worker 2>&1 | grep -i handlers_loaded | tail -1
```

Expected: `0` worktree paths, and `annotate_variants` in the handler list.

- [ ] **Step 3: Give the yeast project its GFF3**

No project currently has all three inputs. The yeast project has 6,641 called variants and no annotation, so it is one download away from being the end-to-end case.

At localhost:5173, open the yeast project → *More ways to add files* → *Download from NCBI…* → `GCF_000146045.2` → download, keeping the annotation. Wait for it to reach READY.

- [ ] **Step 4: Run the annotation**

On the yeast VCF's Actions tab, the annotate card should now read *available*. Launch it, and watch the job in the Activity view.

- [ ] **Step 5: Confirm the numbers against the command line**

The design measured 4,152 annotated of 6,641 on exactly this data. The run should reproduce it:

```bash
docker compose exec -T api python -c "
import sqlite3, glob
for p in glob.glob('/data/vcf_stats/*/variants.db'):
    c = sqlite3.connect(p)
    total = c.execute('select count(*) from variants').fetchone()[0]
    ann = c.execute('select count(*) from variants where consequence is not null').fetchone()[0]
    if ann:
        print(p, 'total', total, 'annotated', ann)
        for row in c.execute('select consequence, count(*) from variants where consequence is not null group by 1 order by 2 desc limit 5'):
            print('   ', row)
"
```

Expected: roughly 4,060 rows carrying a consequence (4,152 minus the 92 bare `@` pointers, which are references to another record rather than annotations of their own), with `synonymous` and `missense` the top two.

- [ ] **Step 6: Check the four UI behaviours**

1. The annotated VCF's Results tab shows Gene / Consequence / AA change populated.
2. The consequence dropdown filters, and the row count changes with it.
3. An **un-annotated** VCF still renders, with dashes in those three columns and no error.
4. The Context button from the previous feature still opens the Sequence Viewer.

- [ ] **Step 7: Commit any fixes**

---

## Notes for the implementer

**Restart the worker after every queue change.** `worker` does not hot-reload. A job that appears to run with your fix but is silently executing the old code reads as "the fix didn't work".

**Do not duplicate the resolution rules.** The card and the launcher both call `resolve_annotation_inputs`. If you find yourself writing "does this VCF have a GFF3" a second time, that is the drift this design exists to prevent.

**Do not build iCn3D.** It is unblocked by this work but out of scope. The spec's follow-on section covers it.

**An empty consequence column is the normal case**, not a bug. Most VCFs here are un-annotated and must look ordinary.
