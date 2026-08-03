"""Matching a suggestion card to the runs that already did its work.

Table-driven for the same reason the suggestion rules are: the value is in
pinning each branch, especially the two where the data does not live where
you would expect it (the reference is an input, not a param; a trim's tool is
a run field, not a param).
"""

from datetime import datetime
from types import SimpleNamespace

from app.models import RunInputRole, RunKind, RunStatus
from app.services.prior_runs import row_for_run, run_matches_card


def _run(kind=RunKind.ALIGNMENT, params=None, tool=None, inputs=()):
    """A stand-in for PipelineRun carrying only what the matcher reads."""
    return SimpleNamespace(
        id="run1",
        kind=kind,
        params=params or {},
        tool=tool,
        inputs=list(inputs),
        outputs=[],
        created_at=datetime(2026, 8, 1, 12, 0),
    )


def _input(object_id, role):
    return SimpleNamespace(object_id=object_id, name="x", role=role)


def _align_card(aligner="bwa-mem2", reference_id="ref1"):
    return {
        "kind": "align",
        "launch": {
            "endpoint": "/pipelines/align",
            "body": {
                "object_id": "obj1",
                "reference_id": reference_id,
                "params": {"aligner": aligner},
            },
        },
    }


class TestAlignmentMatching:
    def test_same_aligner_and_reference_matches(self):
        run = _run(
            params={"aligner": "bwa-mem2", "read_group": {"ID": "x"}},
            inputs=[_input("ref1", RunInputRole.REFERENCE)],
        )
        assert run_matches_card(run, _align_card()) is True

    def test_a_different_aligner_does_not_match(self):
        run = _run(
            params={"aligner": "minimap2"},
            inputs=[_input("ref1", RunInputRole.REFERENCE)],
        )
        assert run_matches_card(run, _align_card(aligner="bwa-mem2")) is False

    def test_a_different_reference_does_not_match(self):
        """The reference is an *input* with a role, never a param.

        A matcher that only walked `run.params` would pass this test's setup
        as a match -- same aligner, same kind -- and show the user an
        alignment against a completely different genome as a prior run of
        this card.
        """
        run = _run(
            params={"aligner": "bwa-mem2"},
            inputs=[_input("OTHER_GENOME", RunInputRole.REFERENCE)],
        )
        assert run_matches_card(run, _align_card(reference_id="ref1")) is False

    def test_a_run_with_no_reference_input_does_not_match(self):
        """Absent is not equal to whatever the card asked for."""
        run = _run(params={"aligner": "bwa-mem2"}, inputs=[])
        assert run_matches_card(run, _align_card(reference_id="ref1")) is False

    def test_read_group_differences_are_ignored(self):
        """`params` holds more than parameters.

        `read_group` is built partly from the object's own name, so a
        wholesale `params ==` comparison would make almost nothing match.
        """
        run = _run(
            params={"aligner": "bwa-mem2", "read_group": {"ID": "anything"}},
            inputs=[_input("ref1", RunInputRole.REFERENCE)],
        )
        assert run_matches_card(run, _align_card()) is True


class TestTrimMatching:
    def _trim_card(self, tool="fastp"):
        return {
            "kind": "preprocess",
            "launch": {
                "endpoint": "/pipelines/trim",
                "body": {"object_id": "obj1", "tool": tool, "params": {}},
            },
        }

    def test_same_tool_matches(self):
        run = _run(kind=RunKind.TRIM, tool="fastp")
        assert run_matches_card(run, self._trim_card()) is True

    def test_a_different_tool_does_not_match(self):
        """A trim's tool lives in the run's own `tool` field, not `params`."""
        run = _run(kind=RunKind.TRIM, tool="cutadapt")
        assert run_matches_card(run, self._trim_card(tool="fastp")) is False


class TestKindGating:
    def test_a_trim_run_never_matches_an_align_card(self):
        run = _run(kind=RunKind.TRIM, tool="fastp")
        assert run_matches_card(run, _align_card()) is False

    def test_a_card_with_no_corresponding_run_kind_never_matches(self):
        """Assemble, variants and the rest have no entry yet, and a card that
        cannot name a run kind must show nothing rather than everything."""
        run = _run(kind=RunKind.ALIGNMENT)
        assert run_matches_card(run, {"kind": "assemble", "launch": None}) is False


class TestRowShape:
    def test_a_succeeded_run_carries_its_outputs(self):
        run = _run()
        run.outputs = ["out1", "out2"]
        names = {"out1": "sample_R1.fastq.gz", "out2": "sample_R2.fastq.gz"}
        row = row_for_run(run, RunStatus.SUCCEEDED, names)
        assert row["status"] == "succeeded"
        assert row["run_id"] == "run1"
        assert [o["name"] for o in row["outputs"]] == [
            "sample_R1.fastq.gz",
            "sample_R2.fastq.gz",
        ]
        assert all(o["exists"] for o in row["outputs"])

    def test_a_failed_run_has_no_outputs(self):
        """The row that motivates the feature: no file to link, so the status
        word carries it. Hiding these invites the same failed launch again."""
        run = _run()
        run.outputs = []
        row = row_for_run(run, RunStatus.FAILED, {})
        assert row["status"] == "failed"
        assert row["outputs"] == []

    def test_a_deleted_output_is_marked_rather_than_dropped(self):
        """The run still happened; only the file is gone. A dropped row would
        make a real run look like it produced nothing."""
        run = _run()
        run.outputs = ["gone"]
        row = row_for_run(run, RunStatus.SUCCEEDED, {})
        assert row["outputs"] == [
            {"object_id": "gone", "name": "(deleted)", "exists": False}
        ]
