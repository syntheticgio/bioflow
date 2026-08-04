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
