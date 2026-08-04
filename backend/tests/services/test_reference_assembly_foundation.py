from types import SimpleNamespace

import pytest
from beanie import PydanticObjectId

from app.errors import ValidationError
from app.models import RunInputRole, RunKind
from app.models import FormatKind, ObjectRole, ObjectStatus
from app.pipelines.tools import PipelineType
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
