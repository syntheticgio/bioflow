"""Matching a suggestion card to the runs that already did its work.

Table-driven for the same reason the suggestion rules are: the value is in
pinning each branch, especially the two where the data does not live where
you would expect it (the reference is an input, not a param; a trim's tool is
a run field, not a param).
"""

from datetime import datetime
from types import SimpleNamespace

from app.models import RunInputRole, RunKind, RunStatus
from app.services.prior_runs import run_matches_card


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
