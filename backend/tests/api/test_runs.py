"""RunOut's field mapping.

RunOut.of() enumerates every field on PipelineRun by hand rather than
delegating to a generic serializer, so a field added to the model is silently
dropped from the API until someone adds it here too -- as happened with
`tool`. Exercised as a pure function over a fake stand-in, with no database
involved: PipelineRun is a Beanie Document and cannot be constructed without
ODM initialization, so a lightweight fake (matching the FakeObject pattern
in tests/pipelines/test_launch_rules.py) stands in for it here -- only the
mapping itself is under test, not persistence.
"""

from app.api.v1.runs import RunOut
from app.models import RunInputRole
from beanie import PydanticObjectId


class FakeInput:
    def __init__(self, object_id, name, role):
        self.object_id = object_id
        self.name = name
        self.role = role


class FakeRun:
    """Enough of a PipelineRun for RunOut.of() to map."""

    def __init__(
        self,
        *,
        kind="trim",
        tool=None,
        params=None,
        inputs=None,
        outputs=None,
    ):
        self.id = PydanticObjectId()
        self.kind = type("K", (), {"value": kind})()
        self.project_id = PydanticObjectId()
        self.label = "Trim test.fastq.gz"
        self.inputs = inputs or []
        self.params = params or {}
        self.tool = tool
        self.outputs = outputs or []
        self.created_at = "2026-07-28T00:00:00Z"
        self.updated_at = "2026-07-28T00:00:00Z"


class TestRunOutTool:
    def test_carries_the_tool_that_ran_a_trim(self):
        run = FakeRun(tool="cutadapt")
        assert RunOut.of(run, "succeeded").tool == "cutadapt"

    def test_defaults_to_none_for_a_run_with_no_tool(self):
        """Alignment runs leave PipelineRun.tool unset -- they name their
        tool inside params["aligner"] instead. RunOut must not invent one."""
        run = FakeRun(kind="alignment", tool=None, params={"aligner": "minimap2"})
        assert RunOut.of(run, "succeeded").tool is None


class TestRunOutFieldMapping:
    def test_inputs_and_params_still_round_trip(self):
        """A regression guard for RunOut.of() itself: adding `tool` must not
        disturb the fields that were already being mapped correctly."""
        object_id = PydanticObjectId()
        run = FakeRun(
            inputs=[FakeInput(object_id, "reads.fastq.gz", RunInputRole.READS)],
            params={"threads": 4},
        )
        out = RunOut.of(run, "running")
        assert out.status == "running"
        assert out.params == {"threads": 4}
        assert out.inputs == [
            {"object_id": str(object_id), "name": "reads.fastq.gz", "role": "reads"}
        ]
