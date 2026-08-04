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
