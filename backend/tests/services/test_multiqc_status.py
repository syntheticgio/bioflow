"""The seven states the Project QC panel renders.

The combinations are the point. A failed run with an older report on disk
says something different from a failed run with nothing, and both differ
from a stale report -- so these are asserted as flag combinations rather
than as a single status string.
"""

from types import SimpleNamespace

import pytest

from app.config import settings
from app.queue import multiqc_handlers
from app.services.multiqc_status import MultiqcStatus


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    qc = tmp_path / "qc_reports"
    mq = tmp_path / "multiqc_reports"
    qc.mkdir()
    mq.mkdir()
    monkeypatch.setattr(type(settings), "qc_reports_dir", property(lambda s: qc))
    monkeypatch.setattr(type(settings), "multiqc_reports_dir", property(lambda s: mq))
    return SimpleNamespace(qc=qc, mq=mq)


def _obj(obj_id="abc123", facts=None):
    return SimpleNamespace(id=obj_id, name="reads", facts=facts or {})


class TestReportGeneratedAt:
    def test_none_when_no_report_exists(self, dirs):
        assert multiqc_handlers.report_generated_at("p1") is None

    def test_reads_the_report_mtime(self, dirs):
        out = dirs.mq / "p1"
        out.mkdir()
        (out / "multiqc_report.html").write_text("<html></html>")

        assert multiqc_handlers.report_generated_at("p1") is not None


class TestNewestQcOutputAt:
    def test_none_when_nothing_is_retained(self, dirs):
        assert multiqc_handlers.newest_qc_output_at([_obj()]) is None

    def test_finds_a_retained_fastp_json(self, dirs):
        obj = _obj(facts={"qc_fastp_data": "fastp/fastp.json"})
        d = dirs.qc / str(obj.id) / "fastp"
        d.mkdir(parents=True)
        (d / "fastp.json").write_text("{}")

        assert multiqc_handlers.newest_qc_output_at([obj]) is not None

    def test_finds_a_fastqc_zip_with_no_fact(self, dirs):
        """FastQC records no fact naming its zip -- staleness must see it
        anyway, or re-running QC on a FastQC-only file would leave the
        report looking current when it is not."""
        obj = _obj()
        d = dirs.qc / str(obj.id) / "fastqc"
        d.mkdir(parents=True)
        (d / "reads_fastqc.zip").write_bytes(b"PK")

        assert multiqc_handlers.newest_qc_output_at([obj]) is not None

    def test_takes_the_newest_of_several(self, dirs):
        import os

        a = _obj(obj_id="a1", facts={"qc_fastp_data": "fastp/fastp.json"})
        b = _obj(obj_id="b2", facts={"qc_fastp_data": "fastp/fastp.json"})
        for o, when in ((a, 1_000_000), (b, 2_000_000)):
            d = dirs.qc / str(o.id) / "fastp"
            d.mkdir(parents=True)
            f = d / "fastp.json"
            f.write_text("{}")
            os.utime(f, (when, when))

        assert multiqc_handlers.newest_qc_output_at([a, b]) == 2_000_000


class TestStatusShape:
    """The dataclass's own contract -- the flag combinations the panel
    renders, independent of how they are computed."""

    def test_defaults_read_as_no_report_yet(self):
        s = MultiqcStatus()
        assert s.generated_at is None
        assert not s.running and not s.failed and not s.stale

    def test_failed_and_generated_are_independent(self):
        """The s7 case: a regeneration failed and the older report is still
        there to offer. Collapsing these into one status field is what would
        lose it."""
        s = MultiqcStatus(generated_at=1.0, failed=True, failed_at=2.0)
        assert s.generated_at is not None
        assert s.failed

    def test_serializes_every_field_the_panel_reads(self):
        keys = set(MultiqcStatus().as_dict())
        assert keys == {
            "summarizable",
            "generated_at",
            "covered",
            "stale",
            "running",
            "running_since",
            "failed",
            "failed_at",
        }


class TestRouteOrdering:
    def test_status_is_declared_before_the_greedy_report_route(self):
        """`/qc/multiqc/{project_id}/{report_path:path}` matches greedily,
        including slashes, so it will swallow `.../status` if it is
        registered first -- and the failure is a confusing 404 from the
        report route rather than anything naming the real cause.

        This was written after doing exactly that: the status route was
        appended below the report route, and the docstring claiming
        otherwise was the only thing that noticed.
        """
        from app.api.v1 import pipelines

        paths = [
            r.path
            for r in pipelines.router.routes
            if "multiqc" in getattr(r, "path", "")
        ]
        status_at = paths.index("/pipelines/qc/multiqc/{project_id}/status")
        report_at = paths.index(
            "/pipelines/qc/multiqc/{project_id}/{report_path:path}"
        )
        assert status_at < report_at
