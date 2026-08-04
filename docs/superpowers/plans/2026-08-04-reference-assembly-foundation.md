# Reference Assembly Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the shared pipeline vocabulary and validation helpers that let Pilon, RagTag, and iVar plug into BioFlow as reference-based assembly workflows.

**Architecture:** Introduce `reference_assembly` as a distinct pipeline/run family, separate from de novo assembly and assembly QC. Add reusable validators for assembly FASTA inputs and BAM provenance so future tool launches can reject wrong input combinations before queueing.

**Tech Stack:** Python 3.13, FastAPI service layer, Beanie/Pydantic models, pytest, TypeScript API mirrors, React tool metadata views.

---

## File Structure

- Modify `backend/app/models/run.py`: add `RunKind.REFERENCE_ASSEMBLY` and input roles for draft assemblies and primers.
- Modify `backend/app/pipelines/tools.py`: add `PipelineType.REFERENCE_ASSEMBLY`.
- Create `backend/app/services/reference_assembly.py`: focused helper module for draft/reference validation and BAM target provenance.
- Create `backend/tests/services/test_reference_assembly_foundation.py`: unit tests for validators and provenance matching.
- Modify `frontend/src/api/types.ts`: mirror new enum values that cross the API.
- Modify `frontend/src/components/PipelineToolSelector.tsx`: label `reference_assembly` in the tool picker.
- Modify `frontend/src/components/HelpSoftware.tsx`: add `Reference assembly` section title and ordering.
- Modify `docs/superpowers/specs/2026-08-04-reference-assembly-foundation-design.md`: add a short implementation note only if implementation finds a material design adjustment.

## Task 1: Backend Run And Pipeline Vocabulary

**Files:**
- Modify: `backend/app/models/run.py`
- Modify: `backend/app/pipelines/tools.py`
- Test: `backend/tests/services/test_reference_assembly_foundation.py`

- [ ] **Step 1: Write the failing enum test**

Create `backend/tests/services/test_reference_assembly_foundation.py` with:

```python
from app.models import RunInputRole, RunKind
from app.pipelines.tools import PipelineType


class TestReferenceAssemblyVocabulary:
    def test_reference_assembly_has_its_own_pipeline_family(self):
        assert PipelineType.REFERENCE_ASSEMBLY.value == "reference_assembly"

    def test_reference_assembly_has_its_own_run_kind(self):
        assert RunKind.REFERENCE_ASSEMBLY.value == "reference_assembly"

    def test_run_input_roles_cover_future_tool_shapes(self):
        assert RunInputRole.DRAFT_ASSEMBLY.value == "draft_assembly"
        assert RunInputRole.PRIMERS.value == "primers"
        assert RunInputRole.REFERENCE.value == "reference"
        assert RunInputRole.ALIGNMENT.value == "alignment"
```

- [ ] **Step 2: Run the test to verify it fails**

Run from the main checkout root:

```bash
docker compose exec api python -m pytest tests/services/test_reference_assembly_foundation.py::TestReferenceAssemblyVocabulary -q
```

Expected: FAIL with an `AttributeError` naming `REFERENCE_ASSEMBLY` or `DRAFT_ASSEMBLY`.

- [ ] **Step 3: Add backend enum values**

In `backend/app/models/run.py`, update `RunKind`:

```python
class RunKind(StrEnum):
    ALIGNMENT = "alignment"
    TRIM = "trim"
    SRA_DOWNLOAD = "sra_download"
    VARIANT_CALLING = "variant_calling"
    ASSEMBLY_DOWNLOAD = "assembly_download"
    UNIPROT_DOWNLOAD = "uniprot_download"
    QUANTIFY = "quantify"
    DIFFERENTIAL_EXPRESSION = "differential_expression"
    ASSEMBLY = "assembly"
    # Reference-guided assembly work such as Pilon polishing, RagTag
    # scaffolding, and iVar consensus. Separate from ASSEMBLY, which is de
    # novo, and from assembly QC, which scores rather than produces assemblies.
    REFERENCE_ASSEMBLY = "reference_assembly"
```

In the same file, update `RunInputRole`:

```python
class RunInputRole(StrEnum):
    READS = "reads"
    MATE = "mate"
    REFERENCE = "reference"
    ALIGNMENT = "alignment"
    ANNOTATION = "annotation"
    COUNTS = "counts"
    # The assembly being polished or scaffolded by a reference-based assembly
    # workflow. Separate from REFERENCE because RagTag consumes both.
    DRAFT_ASSEMBLY = "draft_assembly"
    # Primer BED input for iVar-style amplicon workflows once that tool lands.
    PRIMERS = "primers"
```

In `backend/app/pipelines/tools.py`, update `PipelineType`:

```python
class PipelineType(StrEnum):
    TRIM = "trim"
    ALIGN = "align"
    QC = "qc"
    UTILITY = "utility"
    DOWNLOAD = "download"
    VARIANT = "variant"
    EXPRESSION = "expression"
    ASSEMBLE = "assemble"
    ASSEMBLY_QC = "assembly_qc"
    # Tools that improve, scaffold, polish, or produce an assembly using a
    # reference, draft assembly, or alignment. Kept separate from ASSEMBLE and
    # ASSEMBLY_QC so future Pilon/RagTag/iVar metadata is grouped honestly.
    REFERENCE_ASSEMBLY = "reference_assembly"
```

- [ ] **Step 4: Run the enum test to verify it passes**

```bash
docker compose exec api python -m pytest tests/services/test_reference_assembly_foundation.py::TestReferenceAssemblyVocabulary -q
```

Expected: PASS with `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/run.py backend/app/pipelines/tools.py backend/tests/services/test_reference_assembly_foundation.py
git commit -m "Add reference assembly pipeline vocabulary"
```

## Task 2: Assembly FASTA Validators

**Files:**
- Create: `backend/app/services/reference_assembly.py`
- Modify: `backend/tests/services/test_reference_assembly_foundation.py`

- [ ] **Step 1: Add failing validator tests**

Append to `backend/tests/services/test_reference_assembly_foundation.py`:

```python
from types import SimpleNamespace

import pytest
from beanie import PydanticObjectId

from app.errors import ValidationError
from app.models import FormatKind, ObjectRole, ObjectStatus
from app.services import reference_assembly


def _object(
    *,
    name="assembly.fasta",
    kind=FormatKind.FASTA,
    role=ObjectRole.REFERENCE,
    status=ObjectStatus.READY,
    project_id=None,
    derived_from=None,
):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name=name,
        format=SimpleNamespace(kind=kind),
        role=role,
        status=status,
        project_id=project_id or PydanticObjectId(),
        derived_from=derived_from or [],
    )


class TestAssemblyValidators:
    def test_draft_assembly_accepts_ready_reference_fasta(self):
        obj = _object(role=ObjectRole.REFERENCE)

        assert reference_assembly.check_draft_assembly(obj) is obj

    def test_draft_assembly_accepts_uploaded_fasta_with_no_role(self):
        obj = _object(role=None)

        assert reference_assembly.check_draft_assembly(obj) is obj

    def test_draft_assembly_rejects_not_ready(self):
        obj = _object(status=ObjectStatus.HASHING)

        with pytest.raises(ValidationError, match="not ready"):
            reference_assembly.check_draft_assembly(obj)

    def test_draft_assembly_rejects_non_fasta(self):
        obj = _object(kind=FormatKind.FASTQ)

        with pytest.raises(ValidationError, match="not a FASTA"):
            reference_assembly.check_draft_assembly(obj)

    def test_draft_assembly_rejects_protein_fasta(self):
        obj = _object(role=ObjectRole.PROTEIN)

        with pytest.raises(ValidationError, match="protein"):
            reference_assembly.check_draft_assembly(obj)

    def test_draft_assembly_rejects_transcript_fasta(self):
        obj = _object(role=ObjectRole.TRANSCRIPT)

        with pytest.raises(ValidationError, match="transcript"):
            reference_assembly.check_draft_assembly(obj)

    def test_reference_assembly_accepts_reference_fasta(self):
        obj = _object(role=ObjectRole.REFERENCE)

        assert reference_assembly.check_reference_assembly(obj) is obj

    def test_reference_assembly_rejects_unset_role(self):
        obj = _object(role=None)

        with pytest.raises(ValidationError, match="not marked as a reference"):
            reference_assembly.check_reference_assembly(obj)
```

- [ ] **Step 2: Run the validator tests to verify they fail**

```bash
docker compose exec api python -m pytest tests/services/test_reference_assembly_foundation.py::TestAssemblyValidators -q
```

Expected: FAIL with an import error for `app.services.reference_assembly` or missing validator functions.

- [ ] **Step 3: Implement assembly validators**

Create `backend/app/services/reference_assembly.py`:

```python
"""Shared validation for reference-based assembly workflows.

These helpers are foundation code for Pilon, RagTag and iVar. They validate
object shape and provenance before a future tool-specific launch queues any
long-running work.
"""

from app.errors import ValidationError
from app.models import DataObject, FormatKind, ObjectRole, ObjectStatus

ASSEMBLY_EXCLUDED_ROLES = {ObjectRole.PROTEIN, ObjectRole.TRANSCRIPT}


def _role_name(obj: DataObject) -> str:
    return obj.role.value if obj.role else "unassigned"


def _check_ready_fasta(obj: DataObject, *, purpose: str) -> None:
    if obj.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{obj.name!r} is not ready for {purpose} (status={obj.status.value})",
            details={"object_id": str(obj.id), "status": obj.status.value},
        )
    if obj.format.kind is not FormatKind.FASTA:
        raise ValidationError(
            f"{obj.name!r} is {obj.format.kind.value}, not a FASTA assembly",
            details={"object_id": str(obj.id), "kind": obj.format.kind.value},
        )
    if obj.role in ASSEMBLY_EXCLUDED_ROLES:
        raise ValidationError(
            f"{obj.name!r} is a {_role_name(obj)} FASTA, not a genome assembly",
            details={"object_id": str(obj.id), "role": obj.role.value},
        )


def check_draft_assembly(obj: DataObject) -> DataObject:
    """Validate an assembly that a tool will polish or scaffold.

    Uploaded assemblies may have no role, so this checks shape rather than
    provenance. Protein and transcript FASTA are explicitly excluded because
    their bytes are FASTA while their biological meaning is not an assembly.
    """
    _check_ready_fasta(obj, purpose="reference-based assembly")
    return obj


def check_reference_assembly(obj: DataObject) -> DataObject:
    """Validate a trusted reference assembly input.

    Unlike draft assemblies, references must carry ObjectRole.REFERENCE so a
    generic uploaded FASTA is not silently treated as the authoritative target
    for scaffolding or consensus.
    """
    _check_ready_fasta(obj, purpose="reference-based assembly")
    if obj.role is not ObjectRole.REFERENCE:
        raise ValidationError(
            f"{obj.name!r} is not marked as a reference assembly",
            details={"object_id": str(obj.id), "role": _role_name(obj)},
        )
    return obj
```

- [ ] **Step 4: Run validator tests to verify they pass**

```bash
docker compose exec api python -m pytest tests/services/test_reference_assembly_foundation.py::TestAssemblyValidators -q
```

Expected: PASS with `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/reference_assembly.py backend/tests/services/test_reference_assembly_foundation.py
git commit -m "Add reference assembly input validators"
```

## Task 3: BAM Alignment Target Provenance

**Files:**
- Modify: `backend/app/services/reference_assembly.py`
- Modify: `backend/tests/services/test_reference_assembly_foundation.py`

- [ ] **Step 1: Add failing BAM provenance tests**

Append to `backend/tests/services/test_reference_assembly_foundation.py`:

```python
class TestAlignmentTargetProvenance:
    def test_alignment_target_finds_single_reference_parent(self):
        target = _object(name="draft.fasta", role=ObjectRole.REFERENCE)
        bam = _object(
            name="reads.bam",
            kind=FormatKind.BAM,
            role=ObjectRole.ALIGNMENT,
            derived_from=[target.id],
        )
        objects = {target.id: target}

        assert reference_assembly.alignment_target_for_bam(
            bam, object_lookup=objects.get
        ) is target

    def test_alignment_target_rejects_non_bam(self):
        obj = _object(kind=FormatKind.FASTA)

        with pytest.raises(ValidationError, match="not an alignment"):
            reference_assembly.alignment_target_for_bam(obj, object_lookup={}.get)

    def test_alignment_target_rejects_bam_with_no_target(self):
        bam = _object(
            name="reads.bam",
            kind=FormatKind.BAM,
            role=ObjectRole.ALIGNMENT,
            derived_from=[],
        )

        with pytest.raises(ValidationError, match="no recorded alignment target"):
            reference_assembly.alignment_target_for_bam(bam, object_lookup={}.get)

    def test_alignment_target_rejects_ambiguous_targets(self):
        target_a = _object(name="a.fasta", role=ObjectRole.REFERENCE)
        target_b = _object(name="b.fasta", role=ObjectRole.REFERENCE)
        bam = _object(
            name="reads.bam",
            kind=FormatKind.BAM,
            role=ObjectRole.ALIGNMENT,
            derived_from=[target_a.id, target_b.id],
        )
        objects = {target_a.id: target_a, target_b.id: target_b}

        with pytest.raises(ValidationError, match="ambiguous alignment target"):
            reference_assembly.alignment_target_for_bam(
                bam, object_lookup=objects.get
            )

    def test_check_bam_aligned_to_accepts_matching_target(self):
        target = _object(name="draft.fasta", role=ObjectRole.REFERENCE)
        bam = _object(
            name="reads.bam",
            kind=FormatKind.BAM,
            role=ObjectRole.ALIGNMENT,
            derived_from=[target.id],
        )
        objects = {target.id: target}

        assert reference_assembly.check_bam_aligned_to(
            bam, target, object_lookup=objects.get
        ) is bam

    def test_check_bam_aligned_to_rejects_mismatch(self):
        target = _object(name="draft.fasta", role=ObjectRole.REFERENCE)
        other = _object(name="other.fasta", role=ObjectRole.REFERENCE)
        bam = _object(
            name="reads.bam",
            kind=FormatKind.BAM,
            role=ObjectRole.ALIGNMENT,
            derived_from=[other.id],
        )
        objects = {other.id: other}

        with pytest.raises(ValidationError, match="aligned to 'other.fasta'"):
            reference_assembly.check_bam_aligned_to(
                bam, target, object_lookup=objects.get
            )
```

- [ ] **Step 2: Run BAM provenance tests to verify they fail**

```bash
docker compose exec api python -m pytest tests/services/test_reference_assembly_foundation.py::TestAlignmentTargetProvenance -q
```

Expected: FAIL with missing `alignment_target_for_bam` or `check_bam_aligned_to`.

- [ ] **Step 3: Implement BAM provenance helpers**

Append to `backend/app/services/reference_assembly.py`:

```python
ALIGNMENT_KINDS = {FormatKind.BAM, FormatKind.SAM, FormatKind.CRAM}


def _is_assembly_like(obj: DataObject) -> bool:
    return obj.format.kind is FormatKind.FASTA and obj.role not in ASSEMBLY_EXCLUDED_ROLES


def alignment_target_for_bam(
    bam: DataObject, *, object_lookup
) -> DataObject:
    """Return the single assembly/reference this alignment was made against.

    `object_lookup` is injected so tests can use an in-memory mapping and
    future service callers can pass an owner-scoped lookup. The function never
    guesses from filenames.
    """
    if bam.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{bam.name!r} is not ready (status={bam.status.value})",
            details={"object_id": str(bam.id), "status": bam.status.value},
        )
    if bam.format.kind not in ALIGNMENT_KINDS:
        raise ValidationError(
            f"{bam.name!r} is {bam.format.kind.value}, not an alignment",
            details={"object_id": str(bam.id), "kind": bam.format.kind.value},
        )

    targets = []
    for parent_id in bam.derived_from or []:
        parent = object_lookup(parent_id)
        if parent is not None and _is_assembly_like(parent):
            targets.append(parent)

    if not targets:
        raise ValidationError(
            f"{bam.name!r} has no recorded alignment target",
            details={"object_id": str(bam.id)},
        )
    if len(targets) > 1:
        raise ValidationError(
            f"{bam.name!r} has an ambiguous alignment target",
            details={
                "object_id": str(bam.id),
                "targets": [str(target.id) for target in targets],
            },
        )
    return targets[0]


def check_bam_aligned_to(
    bam: DataObject, target: DataObject, *, object_lookup
) -> DataObject:
    """Validate that a BAM/CRAM was aligned to the selected assembly target."""
    resolved = alignment_target_for_bam(bam, object_lookup=object_lookup)
    if resolved.id != target.id:
        raise ValidationError(
            f"{bam.name!r} is aligned to {resolved.name!r}, not {target.name!r}",
            details={
                "bam_id": str(bam.id),
                "target_id": str(target.id),
                "resolved_target_id": str(resolved.id),
            },
        )
    return bam
```

- [ ] **Step 4: Run BAM provenance tests to verify they pass**

```bash
docker compose exec api python -m pytest tests/services/test_reference_assembly_foundation.py::TestAlignmentTargetProvenance -q
```

Expected: PASS with `6 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/reference_assembly.py backend/tests/services/test_reference_assembly_foundation.py
git commit -m "Add alignment target validation"
```

## Task 4: Owner-Scoped Async Lookup Wrapper

**Files:**
- Modify: `backend/app/services/reference_assembly.py`
- Modify: `backend/tests/services/test_reference_assembly_foundation.py`

- [ ] **Step 1: Add failing service-wrapper tests**

Append to `backend/tests/services/test_reference_assembly_foundation.py`:

```python
from unittest.mock import AsyncMock, patch


class TestOwnerScopedAlignmentValidation:
    pytestmark = pytest.mark.asyncio

    async def test_resolve_alignment_target_uses_owner_scoped_get_object(self):
        target = _object(name="draft.fasta", role=ObjectRole.REFERENCE)
        bam = _object(
            name="reads.bam",
            kind=FormatKind.BAM,
            role=ObjectRole.ALIGNMENT,
            derived_from=[target.id],
        )

        async def _get_object(object_id, *, owner):
            assert object_id == target.id
            assert owner == "local"
            return target

        with patch(
            "app.services.object_service.get_object", AsyncMock(side_effect=_get_object)
        ):
            resolved = await reference_assembly.resolve_alignment_target_for_bam(
                bam, owner="local"
            )

        assert resolved is target

    async def test_validate_bam_aligned_to_uses_owner_scoped_lookup(self):
        target = _object(name="draft.fasta", role=ObjectRole.REFERENCE)
        bam = _object(
            name="reads.bam",
            kind=FormatKind.BAM,
            role=ObjectRole.ALIGNMENT,
            derived_from=[target.id],
        )

        async def _get_object(object_id, *, owner):
            assert owner == "local"
            return target

        with patch(
            "app.services.object_service.get_object", AsyncMock(side_effect=_get_object)
        ):
            resolved = await reference_assembly.validate_bam_aligned_to(
                bam, target, owner="local"
            )

        assert resolved is bam
```

- [ ] **Step 2: Run wrapper tests to verify they fail**

```bash
docker compose exec api python -m pytest tests/services/test_reference_assembly_foundation.py::TestOwnerScopedAlignmentValidation -q
```

Expected: FAIL with missing `resolve_alignment_target_for_bam` or `validate_bam_aligned_to`.

- [ ] **Step 3: Implement async wrappers**

Append to `backend/app/services/reference_assembly.py`:

```python
async def resolve_alignment_target_for_bam(
    bam: DataObject, *, owner: str
) -> DataObject:
    """Owner-scoped service wrapper around alignment_target_for_bam."""
    from app.services import object_service

    cache: dict = {}

    async def _load(parent_id):
        if parent_id not in cache:
            cache[parent_id] = await object_service.get_object(parent_id, owner=owner)
        return cache[parent_id]

    parents = {}
    for parent_id in bam.derived_from or []:
        parents[parent_id] = await _load(parent_id)

    return alignment_target_for_bam(bam, object_lookup=parents.get)


async def validate_bam_aligned_to(
    bam: DataObject, target: DataObject, *, owner: str
) -> DataObject:
    """Owner-scoped validation for future Pilon and iVar launch paths."""
    from app.services import object_service

    parents = {}
    for parent_id in bam.derived_from or []:
        parents[parent_id] = await object_service.get_object(parent_id, owner=owner)

    return check_bam_aligned_to(bam, target, object_lookup=parents.get)
```

- [ ] **Step 4: Run wrapper tests to verify they pass**

```bash
docker compose exec api python -m pytest tests/services/test_reference_assembly_foundation.py::TestOwnerScopedAlignmentValidation -q
```

Expected: PASS with `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/reference_assembly.py backend/tests/services/test_reference_assembly_foundation.py
git commit -m "Add owner scoped alignment validation"
```

## Task 5: TypeScript Mirrors And Software Help Ordering

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/components/PipelineToolSelector.tsx`
- Modify: `frontend/src/components/HelpSoftware.tsx`

- [ ] **Step 1: Update `PipelineType` mirror**

In `frontend/src/api/types.ts`, update `PipelineType`:

```ts
export type PipelineType =
  | "trim"
  | "align"
  | "qc"
  | "utility"
  | "download"
  | "variant"
  | "expression"
  | "assemble"
  | "assembly_qc"
  | "reference_assembly";
```

- [ ] **Step 2: Update run kind and input role mirrors**

In `frontend/src/api/types.ts`, update `RunKind` and `RunInputRole`:

```ts
export type RunKind =
  | "alignment"
  | "trim"
  | "sra_download"
  | "variant_calling"
  | "assembly_download"
  | "uniprot_download"
  | "quantify"
  | "differential_expression"
  | "assembly"
  | "reference_assembly";

export type RunInputRole =
  | "reads"
  | "mate"
  | "reference"
  | "alignment"
  | "annotation"
  | "counts"
  | "draft_assembly"
  | "primers";
```

- [ ] **Step 3: Update tool picker label**

In `frontend/src/components/PipelineToolSelector.tsx`, update `PIPELINE_LABEL`:

```ts
const PIPELINE_LABEL: Record<PipelineType, string> = {
  trim: "a trimmer",
  align: "an aligner",
  qc: "a QC tool",
  utility: "a tool",
  download: "a download tool",
  variant: "a variant caller",
  expression: "an expression tool",
  assemble: "an assembler",
  assembly_qc: "an assembly QC tool",
  reference_assembly: "a reference assembly tool",
};
```

- [ ] **Step 4: Update Software help section labels**

In `frontend/src/components/HelpSoftware.tsx`, update `TITLES` and `ORDER`:

```ts
const TITLES: Record<string, string> = {
  qc: "Quality control",
  trim: "Trimming",
  assemble: "Assembly",
  reference_assembly: "Reference assembly",
  assembly_qc: "Assembly QC",
  align: "Alignment",
  expression: "Expression",
  variant: "Variant calling",
  download: "Data retrieval",
  utility: "Utilities",
};

const ORDER: PipelineType[] = [
  "qc", "trim", "assemble", "reference_assembly", "assembly_qc", "align",
  "expression", "variant", "download", "utility",
];
```

- [ ] **Step 5: Run frontend typecheck**

```bash
docker compose exec web npm run typecheck
```

Expected: PASS. If the repo does not define `typecheck`, run:

```bash
docker compose exec web npm run build
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/components/PipelineToolSelector.tsx frontend/src/components/HelpSoftware.tsx
git commit -m "Mirror reference assembly types in frontend"
```

## Task 6: Backend Test Sweep And Spec Delta

**Files:**
- Modify: `docs/superpowers/specs/2026-08-04-reference-assembly-foundation-design.md` only if the implementation intentionally differs from the spec.

- [ ] **Step 1: Run focused backend tests**

```bash
docker compose exec api python -m pytest tests/services/test_reference_assembly_foundation.py -q
```

Expected: PASS with all tests in the file passing.

- [ ] **Step 2: Run related service tests**

```bash
docker compose exec api python -m pytest tests/services/test_completeness_launch.py tests/services/test_prior_runs.py -q
```

Expected: PASS. These guard the adjacent assembly FASTA validation and run-history code paths.

- [ ] **Step 3: Run the full backend suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: PASS. Read the final count and confirm there are zero failures, zero errors, and zero unexpected skips.

- [ ] **Step 4: Update the design spec if implementation changed it**

If the implemented helper names differ from the design, append a concrete
`Implementation Delta` section to
`docs/superpowers/specs/2026-08-04-reference-assembly-foundation-design.md`.
For example, if `alignment_target_for_bam` was renamed to
`resolve_alignment_target`, write:

```markdown
## Implementation Delta

The implementation used `resolve_alignment_target` instead of
`alignment_target_for_bam` because the final helper accepts BAM, SAM, and CRAM
inputs. The behavior is otherwise unchanged: reference-based assembly remains a
foundation-only slice with no generic launch endpoint or queue handler.
```

If helper names match the design, do not edit the spec.

- [ ] **Step 5: Commit any spec delta**

If Step 4 edited the spec:

```bash
git add docs/superpowers/specs/2026-08-04-reference-assembly-foundation-design.md
git commit -m "Document reference assembly implementation delta"
```

If Step 4 made no edit, skip this commit step.

## Task 7: Final Verification And Merge Readiness

**Files:**
- Review only: git status and commit log.

- [ ] **Step 1: Verify only intended files changed**

```bash
git status --short
```

Expected: The only untracked file may be the pre-existing `docs/superpowers/specs/2026-08-03-de-and-variant-ai-summaries-design.md`. No modified tracked files should remain.

- [ ] **Step 2: Inspect branch commits**

```bash
git log --oneline main..HEAD
```

Expected: Commits include the design commit and the implementation commits from this plan.

- [ ] **Step 3: If all tests are green, merge according to repo instructions**

From `codex/reference-assembly-foundation`, run:

```bash
git switch main
git status --short
git merge --no-ff codex/reference-assembly-foundation -m "Merge reference assembly foundation"
git push origin main
```

Expected: `main` is clean before merge except the ignored pre-existing untracked spec, merge succeeds, and push to `origin/main` succeeds.
