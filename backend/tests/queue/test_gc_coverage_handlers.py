"""The gc_bias job handler and its applier at the seam.

Mirrors test_mosdepth_handlers.py's split: this file exercises the handler
itself -- payload validation, the join/aggregate call into the pure
gc_coverage module -- and the applier that merges the resulting facts onto
the BAM object.
"""

import pytest

from app.errors import PermanentError
from app.queue.gc_coverage_handlers import compute_gc_bias
from app.queue.registry import JobContext


def _ctx(payload: dict) -> JobContext:
    return JobContext(
        job_id="job1", payload=payload, epoch=0, attempts=1, owner="test-owner",
    )


def test_compute_gc_bias_returns_curve_facts():
    payload = {
        "bam_id": "abc123",
        "project_id": "proj1",
        "gc_contigs": [
            {
                "name": "c1", "length": 20, "window_bases": 10,
                "gc": [30.0, 70.0], "skew": [0.0, 0.0],
            },
        ],
        "depth_regions": {
            "c1": [
                {"start": 0, "end": 10, "depth": 5.0, "name": None},
                {"start": 10, "end": 20, "depth": 15.0, "name": None},
            ],
        },
    }
    result = compute_gc_bias(_ctx(payload))
    assert result["object_id"] == "abc123"
    assert result["project_id"] == "proj1"
    assert result["job_id"] == "job1"
    assert result["facts"]["gc_bias_status"] == "ok"
    assert result["facts"]["gc_bias_curve"] == [
        {"gc_min": 30.0, "gc_max": 35.0, "mean_depth": 5.0, "window_count": 1},
        {"gc_min": 70.0, "gc_max": 75.0, "mean_depth": 15.0, "window_count": 1},
    ]
    assert result["facts"]["gc_bias_partial"] is False
    assert "gc_bias_computed_at" in result["facts"]


def test_compute_gc_bias_requires_bam_id():
    with pytest.raises(PermanentError):
        compute_gc_bias(_ctx({}))


def test_compute_gc_bias_carries_the_partial_flag_from_the_payload():
    """Important #1 finding: gc_tracks truncates to MAX_STORED_CONTIGS and
    sets gc_tracks_partial on its own fact; launch_gc_bias forwards that as
    the payload's gc_tracks_partial key, and the handler must re-emit it as
    gc_bias_partial so the chart can warn the curve covers only a fraction
    of a fragmented assembly."""
    payload = {
        "bam_id": "abc123",
        "gc_contigs": [
            {"name": "c1", "length": 20, "window_bases": 10, "gc": [30.0, 70.0]},
        ],
        "depth_regions": {
            "c1": [
                {"start": 0, "end": 10, "depth": 5.0},
                {"start": 10, "end": 20, "depth": 15.0},
            ],
        },
        "gc_tracks_partial": True,
    }
    result = compute_gc_bias(_ctx(payload))
    assert result["facts"]["gc_bias_partial"] is True


def test_compute_gc_bias_partial_flag_absent_from_payload_defaults_false():
    payload = {
        "bam_id": "abc123",
        "gc_contigs": [
            {"name": "c1", "length": 20, "window_bases": 10, "gc": [30.0, 70.0]},
        ],
        "depth_regions": {
            "c1": [
                {"start": 0, "end": 10, "depth": 5.0},
                {"start": 10, "end": 20, "depth": 15.0},
            ],
        },
    }
    result = compute_gc_bias(_ctx(payload))
    assert result["facts"]["gc_bias_partial"] is False


def test_compute_gc_bias_reports_empty_status_for_an_all_n_reference():
    """Important #2 finding: an empty curve with status 'ok' is
    indistinguishable in BamResults.tsx from a job that never ran (an empty
    array is truthy in JS). All windows scoring gc=None is a legitimate
    all-N reference case, so it must report 'empty', not 'ok'."""
    payload = {
        "bam_id": "abc123",
        "gc_contigs": [
            {"name": "c1", "length": 20, "window_bases": 10, "gc": [None, None]},
        ],
        "depth_regions": {
            "c1": [
                {"start": 0, "end": 10, "depth": 5.0},
                {"start": 10, "end": 20, "depth": 15.0},
            ],
        },
    }
    result = compute_gc_bias(_ctx(payload))
    assert result["facts"]["gc_bias_status"] == "empty"
    assert result["facts"]["gc_bias_curve"] == []


class TestRegistration:
    def test_handler_is_registered_under_its_job_type(self):
        """A handler module that handlers.py never imports registers nothing,
        and the job type fails at dispatch with "no handler"."""
        from app.queue import handlers  # noqa: F401  (import for side effects)
        from app.queue.registry import get_handler

        assert get_handler("gc_bias") is not None


pytestmark_apply = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


class TestApplyGcBias:
    """The applier that merges a gc_bias run's facts onto the BAM.

    Mirrors TestApplyCoverage in test_mosdepth_handlers.py, including its
    no-Redis fixture.
    """

    pytestmark = pytestmark_apply

    @pytest.fixture(autouse=True)
    def _no_queue(self, monkeypatch):
        from app.services import object_service

        async def _skip_ingest(obj, **kwargs):
            return ""

        async def _skip_enqueue(*args, **kwargs):
            return None

        monkeypatch.setattr(object_service, "enqueue_ingest", _skip_ingest)
        monkeypatch.setattr("app.queue.queue.enqueue", _skip_enqueue)

    async def _bam(self, name):
        from app.config import settings
        from app.services import object_service, project_service

        project = await project_service.create_project(name=name, owner="local")
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        scratch = settings.tmp_dir / f"{name}.bam"
        scratch.write_bytes(b"fake-bam-bytes")
        return await object_service.ingest_local_file(
            owner="local",
            project_id=project.id,
            path=scratch,
            name="aligned.bam",
        )

    async def test_merges_facts_onto_the_stored_object(self):
        from app.queue import results

        bam = await self._bam("gc-bias-apply")
        await results._apply_gc_bias(
            {
                "object_id": str(bam.id),
                "facts": {
                    "gc_bias_status": "ok",
                    "gc_bias_curve": [
                        {
                            "gc_min": 30.0, "gc_max": 35.0,
                            "mean_depth": 5.0, "window_count": 1,
                        },
                    ],
                    "gc_bias_computed_at": "2026-08-20T00:00:00+00:00",
                },
            },
            owner="local",
        )
        refreshed = await results.DataObject.get(bam.id)
        assert refreshed.facts["gc_bias_status"] == "ok"
        assert refreshed.facts["gc_bias_curve"] == [
            {"gc_min": 30.0, "gc_max": 35.0, "mean_depth": 5.0, "window_count": 1},
        ]

    async def test_does_nothing_when_the_object_is_missing(self):
        from beanie import PydanticObjectId

        from app.queue import results

        await results._apply_gc_bias(
            {"object_id": str(PydanticObjectId()), "facts": {"gc_bias_status": "ok"}},
            owner="local",
        )

    async def test_does_nothing_without_facts_or_object_id(self):
        from app.queue import results

        await results._apply_gc_bias({}, owner="local")
        await results._apply_gc_bias({"object_id": "x"}, owner="local")
        await results._apply_gc_bias({"facts": {"a": 1}}, owner="local")
