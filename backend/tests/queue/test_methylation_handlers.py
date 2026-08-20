"""The methylation job handler at the seam.

The runner underneath (`modkit_runner`) is pure functions and is tested as
such (test_modkit_runner.py). This file exercises the handler itself --
payload validation, blob resolution, the command it hands modkit, and K3's
enforcement (a pileup producing zero rows fails the job) -- mirroring
test_mosdepth_handlers.py's split.
"""

from pathlib import Path

import pytest

from app.errors import PermanentError
from app.pipelines import tools
from app.queue import methylation_handlers
from app.queue.registry import JobContext

# A single-row bedMethyl, 18 columns (9 tab-delimited, 9 space-delimited).
_ONE_ROW_BEDMETHYL = "chr1\t1000\t1001\tm\t20\t+\t1000\t1001\t255,0,0\t20 75.0 15 5 0 0 0 0 0\n"


def _ctx(payload: dict) -> JobContext:
    return JobContext(
        job_id="job-1", payload=payload, epoch=1, attempts=1, owner="local"
    )


@pytest.fixture
def modkit_available(monkeypatch):
    """Pin the probe so require() passes deterministically, whether or not
    the binary exists in the image the tests run in."""
    fake = tools.Tool(name="modkit", path="/usr/local/bin/modkit", version="0.6.4")
    monkeypatch.setattr(methylation_handlers.tools, "modkit", lambda: fake)
    return fake


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Send tmp/, logs/ and methylation_dir under the test's own directory
    so the handler cannot write to the host's /data. These are derived
    read-only properties; patch what they derive from."""
    monkeypatch.setattr(methylation_handlers.settings, "bioinfo_home", tmp_path)
    return tmp_path


def _inputs(tmp_path) -> dict:
    bam = tmp_path / "reads.bam"
    bam.write_bytes(b"not-a-real-bam")
    bai = tmp_path / "reads.bam.bai"
    bai.write_bytes(b"not-a-real-index")
    return {
        "bam_id": "bam-1",
        "bam_path": str(bam),
        "bai_path": str(bai),
        "project_id": "proj-1",
    }


class TestMethylationValidation:
    def test_missing_bam_id_is_permanent(self, modkit_available, home):
        with pytest.raises(PermanentError, match="bam_id"):
            methylation_handlers.run_methylation(_ctx({}))

    def test_missing_bam_blob_is_permanent(self, modkit_available, home):
        with pytest.raises(PermanentError, match="bam"):
            methylation_handlers.run_methylation(_ctx({"bam_id": "bam-1"}))


class TestMethylationZeroRowsFails:
    """K3: a pileup that runs cleanly but calls no sites is a failure, not a
    silent empty success. Written before the happy-path test, per the plan."""

    def test_empty_pileup_output_raises_permanent_error(
        self, modkit_available, home, tmp_path, monkeypatch
    ):
        payload = _inputs(tmp_path)

        def fake_run(ctx, cmd, **kw):
            # modkit ran, exited 0, but the output file has no rows -- an
            # aligned region with no modifiable bases, say. Output path is
            # the 4th positional argument (index 3) -- see
            # build_pileup_command's argument order.
            Path(cmd[3]).write_text("")
            return 0

        monkeypatch.setattr(methylation_handlers, "run_subprocess", fake_run)
        with pytest.raises(PermanentError, match="no base-modification calls"):
            methylation_handlers.run_methylation(_ctx(payload))

    def test_missing_pileup_output_also_raises_permanent_error(
        self, modkit_available, home, tmp_path, monkeypatch
    ):
        payload = _inputs(tmp_path)

        def fake_run(ctx, cmd, **kw):
            # modkit exits 0 but never wrote the output file at all.
            return 0

        monkeypatch.setattr(methylation_handlers, "run_subprocess", fake_run)
        with pytest.raises(PermanentError, match="no base-modification calls"):
            methylation_handlers.run_methylation(_ctx(payload))


class TestMethylationRun:
    def test_runs_modkit_pileup_over_the_linked_bam(
        self, modkit_available, home, tmp_path, monkeypatch
    ):
        payload = _inputs(tmp_path)
        calls = []

        def fake_run(ctx, cmd, **kw):
            calls.append(cmd)
            Path(cmd[3]).write_text(_ONE_ROW_BEDMETHYL)
            return 0

        monkeypatch.setattr(methylation_handlers, "run_subprocess", fake_run)
        methylation_handlers.run_methylation(_ctx(payload))

        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "modkit"
        assert cmd[1] == "pileup"
        assert "--ref" not in cmd
        assert "--cpg" not in cmd

    def test_links_the_index_beside_the_bam(
        self, modkit_available, home, tmp_path, monkeypatch
    ):
        """modkit seeks via the BAM index the same way mosdepth does, and
        exits with an index-not-found error when it is absent, so the
        sidecar must be linked under the name it looks for."""
        payload = _inputs(tmp_path)

        def fake_run(ctx, cmd, **kw):
            bam = Path(cmd[2])
            assert bam.exists()
            assert bam.with_name(bam.name + ".bai").exists(), "index not linked"
            Path(cmd[3]).write_text(_ONE_ROW_BEDMETHYL)
            return 0

        # bam is the 3rd positional argument (index 2), output bed the 4th
        # (index 3) -- see build_pileup_command's argument order.
        monkeypatch.setattr(methylation_handlers, "run_subprocess", fake_run)
        methylation_handlers.run_methylation(_ctx(payload))

    def test_result_carries_summary_facts_and_output(
        self, modkit_available, home, tmp_path, monkeypatch
    ):
        payload = _inputs(tmp_path)

        def fake_run(ctx, cmd, **kw):
            Path(cmd[3]).write_text(_ONE_ROW_BEDMETHYL)
            return 0

        monkeypatch.setattr(methylation_handlers, "run_subprocess", fake_run)
        result = methylation_handlers.run_methylation(_ctx(payload))

        assert result["object_id"] == "bam-1"
        assert result["project_id"] == "proj-1"
        facts = result["facts"]
        assert facts["methylation_status"] == "ok"
        assert facts["methylation_tool_version"] == "0.6.4"
        assert facts["methylation_site_count"] == 1
        assert facts["methylation_mean_pct"] == pytest.approx(75.0)
        assert facts["methylation_by_code"]["m"]["sites"] == 1
        assert facts["methylation_report"] == "methylation.json"
        # _inputs() sets no bam_name, so the handler falls back to its
        # documented default "aligned.bam".
        assert result["output"]["name"] == "aligned.bam.methylation.bed"
        assert Path(result["output"]["tmp_path"]).exists()

    def test_writes_the_report_under_the_bam_id(
        self, modkit_available, home, tmp_path, monkeypatch
    ):
        payload = _inputs(tmp_path)

        def fake_run(ctx, cmd, **kw):
            Path(cmd[3]).write_text(_ONE_ROW_BEDMETHYL)
            return 0

        monkeypatch.setattr(methylation_handlers, "run_subprocess", fake_run)
        methylation_handlers.run_methylation(_ctx(payload))

        report = home / "methylation" / "bam-1" / "methylation.json"
        assert report.exists()

    def test_non_zero_exit_is_a_failure(
        self, modkit_available, home, tmp_path, monkeypatch
    ):
        payload = _inputs(tmp_path)

        def fake_run(ctx, cmd, **kw):
            return 1

        monkeypatch.setattr(methylation_handlers, "run_subprocess", fake_run)
        with pytest.raises(Exception, match="modkit"):
            methylation_handlers.run_methylation(_ctx(payload))
