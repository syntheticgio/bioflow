"""The coverage job handler and its applier at the seam.

The runner underneath (`mosdepth_runner`) is pure functions and is tested as
such (test_mosdepth_runner.py). This file exercises the handler itself --
payload validation, blob resolution, the windows-BED it hands mosdepth -- and
the applier that merges the resulting facts onto the BAM object, mirroring
test_feature_coverage_handlers.py's split.
"""

import gzip
import json
from pathlib import Path

import pytest

from app.errors import PermanentError
from app.pipelines import tools
from app.queue import mosdepth_handlers
from app.queue.registry import JobContext

# Captured verbatim from a real mosdepth 0.3.14 run; same fixtures as
# test_mosdepth_runner.py, reused so the handler is exercised against
# genuine output rather than a shape invented for the mock.
_REAL_SUMMARY = (
    "chrom\tlength\tbases\tmean\tmin\tmax\n"
    "chrT\t900\t1350\t1.50\t0\t2\n"
    "chrT_region\t900\t1350\t1.50\t0\t2\n"
    "total\t900\t1350\t1.50\t0\t2\n"
    "total_region\t900\t1350\t1.50\t0\t2\n"
)
_REAL_REGIONS = "chrT\t0\t450\t1.85\nchrT\t450\t900\t1.07\n"
_REAL_DIST = "total\t2\t0.57\ntotal\t1\t0.74\ntotal\t0\t1.00\n"


def _ctx(payload: dict) -> JobContext:
    return JobContext(
        job_id="job-1", payload=payload, epoch=1, attempts=1, owner="local"
    )


@pytest.fixture
def mosdepth_available(monkeypatch):
    """Pin the probe so require() passes deterministically, whether or not
    the binary exists in the image the tests run in."""
    fake = tools.Tool(name="mosdepth", path="/usr/local/bin/mosdepth", version="0.3.14")
    monkeypatch.setattr(mosdepth_handlers.tools, "mosdepth", lambda: fake)
    return fake


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Send tmp/, logs/ and coverage_dir under the test's own directory so
    the handler cannot write to the host's /data. These are derived read-only
    properties; patch what they derive from."""
    monkeypatch.setattr(mosdepth_handlers.settings, "bioinfo_home", tmp_path)
    return tmp_path


def _inputs(tmp_path) -> dict:
    bam = tmp_path / "reads.bam"
    bam.write_bytes(b"not-a-real-bam")
    bai = tmp_path / "reads.bam.bai"
    bai.write_bytes(b"not-a-real-index")
    fai = tmp_path / "reference.fa.fai"
    fai.write_text("chrT\t900\t6\t60\t61\n")
    return {
        "bam_id": "bam-1",
        "bam_path": str(bam),
        "bai_path": str(bai),
        "fai_path": str(fai),
        "project_id": "proj-1",
    }


def _write_outputs(prefix: Path) -> None:
    """Lay down what a real mosdepth run leaves beside its prefix."""
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(prefix.suffix + ".mosdepth.summary.txt").write_text(
        _REAL_SUMMARY
    )
    prefix.with_suffix(prefix.suffix + ".mosdepth.global.dist.txt").write_text(
        _REAL_DIST
    )
    with gzip.open(prefix.with_suffix(prefix.suffix + ".regions.bed.gz"), "wt") as fh:
        fh.write(_REAL_REGIONS)


class TestCoverageValidation:
    def test_missing_bam_id_is_permanent(self, mosdepth_available, home):
        with pytest.raises(PermanentError, match="bam_id"):
            mosdepth_handlers.run_coverage(_ctx({}))

    def test_missing_bam_blob_is_permanent(self, mosdepth_available, home):
        with pytest.raises(PermanentError, match="bam"):
            mosdepth_handlers.run_coverage(_ctx({"bam_id": "bam-1"}))

    def test_unwindowable_reference_is_permanent(
        self, mosdepth_available, home, tmp_path, monkeypatch
    ):
        """Every contig shorter than one window yields an empty --by BED, and
        mosdepth against an empty BED writes an empty report that reads as a
        bug rather than as "this reference is too short"."""
        payload = _inputs(tmp_path)
        Path(payload["fai_path"]).write_text("tiny\t50\t6\t60\t61\n")
        with pytest.raises(PermanentError, match="window"):
            mosdepth_handlers.run_coverage(_ctx(payload))

    def test_unreadable_reference_index_is_permanent(
        self, mosdepth_available, home, tmp_path
    ):
        payload = _inputs(tmp_path)
        Path(payload["fai_path"]).write_text("\n")
        with pytest.raises(PermanentError, match="contig lengths"):
            mosdepth_handlers.run_coverage(_ctx(payload))


class TestCoverageRun:
    def test_runs_mosdepth_over_a_generated_windows_bed(
        self, mosdepth_available, home, tmp_path, monkeypatch
    ):
        payload = _inputs(tmp_path)
        calls = []

        def fake_run(ctx, cmd, **kw):
            calls.append(cmd)
            # prefix is the second-to-last argument; the BAM is last.
            _write_outputs(Path(cmd[-2]))
            return 0

        monkeypatch.setattr(mosdepth_handlers, "run_subprocess", fake_run)
        mosdepth_handlers.run_coverage(_ctx(payload))

        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "mosdepth"
        assert "--by" in cmd
        assert "--no-per-base" in cmd

        # --by must point at a real, non-empty BED the handler wrote, not at
        # a path it merely composed.
        by_arg = Path(cmd[cmd.index("--by") + 1])
        assert by_arg.exists()
        lines = by_arg.read_text().splitlines()
        assert len(lines) == 9  # 900bp // 100 = 9 windows
        assert lines[0] == "chrT\t0\t100"
        assert lines[-1] == "chrT\t800\t900"

    def test_links_the_index_beside_the_bam(
        self, mosdepth_available, home, tmp_path, monkeypatch
    ):
        """mosdepth seeks via the BAM index and exits with "index not found"
        when it is absent, so the sidecar must be linked under the name it
        looks for -- `<bam>.bai`, not the blob's own digest name."""
        payload = _inputs(tmp_path)

        def fake_run(ctx, cmd, **kw):
            bam = Path(cmd[-1])
            assert bam.exists()
            assert bam.with_name(bam.name + ".bai").exists(), "index not linked"
            _write_outputs(Path(cmd[-2]))
            return 0

        monkeypatch.setattr(mosdepth_handlers, "run_subprocess", fake_run)
        mosdepth_handlers.run_coverage(_ctx(payload))

    def test_result_carries_summary_facts(
        self, mosdepth_available, home, tmp_path, monkeypatch
    ):
        payload = _inputs(tmp_path)

        def fake_run(ctx, cmd, **kw):
            _write_outputs(Path(cmd[-2]))
            return 0

        monkeypatch.setattr(mosdepth_handlers, "run_subprocess", fake_run)
        result = mosdepth_handlers.run_coverage(_ctx(payload))

        assert result["object_id"] == "bam-1"
        assert result["project_id"] == "proj-1"
        facts = result["facts"]
        assert facts["coverage_status"] == "ok"
        assert facts["coverage_tool_version"] == "0.3.14"
        assert facts["coverage_mean_depth"] == 1.50
        assert facts["coverage_reference_length"] == 900
        assert facts["coverage_contig_count"] == 1
        assert facts["coverage_pct_at_1x"] == pytest.approx(74.0)
        assert facts["coverage_report"] == "coverage.json"

    def test_writes_the_report_under_the_bam_id(
        self, mosdepth_available, home, tmp_path, monkeypatch
    ):
        payload = _inputs(tmp_path)

        def fake_run(ctx, cmd, **kw):
            _write_outputs(Path(cmd[-2]))
            return 0

        monkeypatch.setattr(mosdepth_handlers, "run_subprocess", fake_run)
        mosdepth_handlers.run_coverage(_ctx(payload))

        report = mosdepth_handlers.settings.coverage_dir / "bam-1" / "coverage.json"
        assert report.exists()
        parsed = json.loads(report.read_text())
        assert parsed["total"]["mean"] == 1.50
        assert parsed["regions"]["chrT"][0]["depth"] == 1.85

    def test_report_is_not_written_beside_the_alignment(
        self, mosdepth_available, home, tmp_path, monkeypatch
    ):
        """The report belongs in coverage_dir, outside objects/. A prefix
        pointing into the BAM's own directory would write derived files into
        content-addressed storage."""
        payload = _inputs(tmp_path)

        def fake_run(ctx, cmd, **kw):
            _write_outputs(Path(cmd[-2]))
            return 0

        monkeypatch.setattr(mosdepth_handlers, "run_subprocess", fake_run)
        mosdepth_handlers.run_coverage(_ctx(payload))
        assert not (tmp_path / "cov.regions.bed.gz").exists()

    def test_nonzero_exit_fails_the_job(
        self, mosdepth_available, home, tmp_path, monkeypatch
    ):
        payload = _inputs(tmp_path)

        def fake_run(ctx, cmd, **kw):
            Path(kw["log_path"]).write_text("mosdepth: index not found\n")
            return 1

        monkeypatch.setattr(mosdepth_handlers, "run_subprocess", fake_run)
        with pytest.raises(Exception) as exc:
            mosdepth_handlers.run_coverage(_ctx(payload))
        assert "mosdepth" in str(exc.value)

    def test_empty_output_fails_rather_than_reporting_success(
        self, mosdepth_available, home, tmp_path, monkeypatch
    ):
        """A zero exit with no summary means mosdepth ran and produced
        nothing. Returning ok facts there would merge a row of blanks onto
        the BAM and call the job finished."""
        payload = _inputs(tmp_path)

        def fake_run(ctx, cmd, **kw):
            return 0  # writes no output files at all

        monkeypatch.setattr(mosdepth_handlers, "run_subprocess", fake_run)
        with pytest.raises(PermanentError, match="no depth summary"):
            mosdepth_handlers.run_coverage(_ctx(payload))


class TestRegionMode:
    def _regions_payload(self, tmp_path):
        payload = _inputs(tmp_path)
        bed = tmp_path / "panel.bed"
        bed.write_text("chrT\t0\t450\tgeneA\n")
        payload["regions_id"] = "bed-1"
        payload["regions_name"] = "panel.bed"
        payload["regions_path"] = str(bed)
        return payload

    def test_uses_the_target_bed_instead_of_generated_windows(
        self, mosdepth_available, home, tmp_path, monkeypatch
    ):
        payload = self._regions_payload(tmp_path)
        calls = []

        def fake_run(ctx, cmd, **kw):
            calls.append(cmd)
            _write_outputs(Path(cmd[-2]))
            return 0

        monkeypatch.setattr(mosdepth_handlers, "run_subprocess", fake_run)
        mosdepth_handlers.run_coverage(_ctx(payload))

        by_arg = Path(calls[0][calls[0].index("--by") + 1])
        assert by_arg.name == "panel.bed"
        assert by_arg.read_text() == "chrT\t0\t450\tgeneA\n"
        # The generated windows BED must not be written at all -- building it
        # anyway would be dead work whose absence nothing else would notice.
        assert not (by_arg.parent / "windows.bed").exists()

    def test_facts_record_the_mode_and_the_region_set(
        self, mosdepth_available, home, tmp_path, monkeypatch
    ):
        payload = self._regions_payload(tmp_path)

        def fake_run(ctx, cmd, **kw):
            _write_outputs(Path(cmd[-2]))
            return 0

        monkeypatch.setattr(mosdepth_handlers, "run_subprocess", fake_run)
        facts = mosdepth_handlers.run_coverage(_ctx(payload))["facts"]

        assert facts["coverage_mode"] == "regions"
        assert facts["coverage_regions_id"] == "bed-1"
        # A window count on a region run would describe a tiling that never
        # happened.
        assert "coverage_window_count" not in facts

    def test_a_short_reference_is_not_refused_in_region_mode(
        self, mosdepth_available, home, tmp_path, monkeypatch
    ):
        """The all-contigs-too-short refusal is a windowing constraint. A
        target BED names its own intervals, so a short reference is fine --
        refusing here would block the one mode that still works."""
        payload = self._regions_payload(tmp_path)
        Path(payload["fai_path"]).write_text("tiny\t50\t6\t60\t61\n")

        def fake_run(ctx, cmd, **kw):
            _write_outputs(Path(cmd[-2]))
            return 0

        monkeypatch.setattr(mosdepth_handlers, "run_subprocess", fake_run)
        facts = mosdepth_handlers.run_coverage(_ctx(payload))["facts"]
        assert facts["coverage_mode"] == "regions"


class TestRegistration:
    def test_handler_is_registered_under_its_job_type(self):
        """A handler module that handlers.py never imports registers nothing,
        and the job type fails at dispatch with "no handler"."""
        from app.queue import handlers  # noqa: F401  (import for side effects)
        from app.queue.registry import get_handler

        assert get_handler("coverage") is not None
