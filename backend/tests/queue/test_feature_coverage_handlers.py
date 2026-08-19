"""The feature_coverage job handler and its applier at the seam.

The runner underneath (`feature_coverage_runner`) is pure functions and is
tested as such (test_feature_coverage_runner.py). This file exercises the
handler itself -- payload validation, blob resolution, the sort-then-coverage
subprocess sequence -- and the applier that merges the resulting facts onto
the BAM object, mirroring test_expression_handlers.py's split for `quantify`
and test_results_owner.py's pattern for a facts-merging applier.
"""

from pathlib import Path

import pytest
from beanie import PydanticObjectId

from app.errors import PermanentError
from app.pipelines import tools
from app.queue import feature_coverage_handlers, results
from app.queue.registry import JobContext

# Captured verbatim from `bedtools coverage -sorted -g ref.genome -a ann.gff -b
# reads.bam` (bedtools v2.31.1) -- same fixture Task 5's
# test_parse_coverage_matches_real_bedtools_output uses, reused here so this
# handler test is validated against the same genuine-output shape.
_REAL_COVERAGE_TSV = (
    "chrT\tRefSeq\tgene\t1\t200\t.\t+\t.\tID=gene-abcA;Name=abcA\t"
    "4\t200\t200\t1.0000000\n"
    "chrT\tRefSeq\tgene\t300\t500\t.\t-\t.\tID=gene-abcB;Name=abcB\t"
    "2\t100\t201\t0.4975124\n"
    "chrT\tRefSeq\tgene\t700\t900\t.\t+\t.\tID=gene-abcC;Name=abcC\t"
    "0\t0\t201\t0.0000000\n"
)


def _ctx(payload: dict) -> JobContext:
    return JobContext(job_id="job-1", payload=payload, epoch=1, attempts=1, owner="local")


def _fake_tool(name: str, version: str) -> tools.Tool:
    return tools.Tool(name=name, path=f"/usr/bin/{name}", version=version)


@pytest.fixture
def bedtools_available(monkeypatch):
    """Pin the bedtools probe so require() passes deterministically, whether
    or not the binary exists in this image."""
    fake = _fake_tool("bedtools", "2.31.1")
    monkeypatch.setattr(feature_coverage_handlers.tools, "bedtools", lambda: fake)
    return fake


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Send tmp/, logs/, and feature_coverage_dir under the test's own
    directory, so the handler cannot write to the host's /data. These are
    derived read-only properties; patch what they derive from."""
    monkeypatch.setattr(feature_coverage_handlers.settings, "bioinfo_home", tmp_path)
    return tmp_path


def _inputs(tmp_path) -> dict:
    bam = tmp_path / "reads.bam"
    bam.write_bytes(b"not-a-real-bam")
    annotation = tmp_path / "ann.gff"
    annotation.write_text("chrT\tRefSeq\tgene\t1\t200\t.\t+\t.\tID=gene-abcA\n")
    fai = tmp_path / "reference.fa.fai"
    fai.write_text("chrT\t900\t6\t60\t61\n")
    return {
        "bam_id": "bam-1",
        "bam_path": str(bam),
        "annotation_id": "ann-1",
        "annotation_path": str(annotation),
        "annotation_format": "gff",
        "fai_path": str(fai),
        "project_id": "proj-1",
    }


class TestFeatureCoverageValidation:
    def test_missing_bam_id_is_permanent(self, bedtools_available, home):
        with pytest.raises(PermanentError, match="bam_id"):
            feature_coverage_handlers.run_feature_coverage(_ctx({}))

    def test_missing_annotation_id_is_permanent(self, bedtools_available, home):
        with pytest.raises(PermanentError, match="annotation_id"):
            feature_coverage_handlers.run_feature_coverage(_ctx({"bam_id": "bam-1"}))

    def test_bad_annotation_format_is_permanent(self, bedtools_available, home):
        with pytest.raises(PermanentError, match="annotation_format"):
            feature_coverage_handlers.run_feature_coverage(
                _ctx(
                    {
                        "bam_id": "bam-1",
                        "annotation_id": "ann-1",
                        "annotation_format": "vcf",
                    }
                )
            )

    def test_missing_bam_blob_is_permanent(self, bedtools_available, home):
        with pytest.raises(PermanentError, match="bam"):
            feature_coverage_handlers.run_feature_coverage(
                _ctx(
                    {
                        "bam_id": "bam-1",
                        "annotation_id": "ann-1",
                        "annotation_format": "gff",
                    }
                )
            )


class TestFeatureCoverageRun:
    def test_sorts_annotation_then_runs_coverage(
        self, bedtools_available, home, tmp_path, monkeypatch
    ):
        payload = _inputs(tmp_path)
        calls = []

        def fake_run(ctx, cmd, **kw):
            calls.append((cmd, kw.get("log_path")))
            if cmd[:2] == ["bedtools", "sort"]:
                Path(kw["log_path"]).write_text(
                    "chrT\tRefSeq\tgene\t1\t200\t.\t+\t.\tID=gene-abcA\n"
                )
            else:
                Path(kw["log_path"]).write_text(_REAL_COVERAGE_TSV)
            return 0

        monkeypatch.setattr(feature_coverage_handlers, "run_subprocess", fake_run)
        feature_coverage_handlers.run_feature_coverage(_ctx(payload))

        assert len(calls) == 2
        sort_cmd, _ = calls[0]
        coverage_cmd, _ = calls[1]

        assert sort_cmd[0] == "bedtools"
        assert sort_cmd[1] == "sort"
        assert "-faidx" in sort_cmd

        assert coverage_cmd[0] == "bedtools"
        assert coverage_cmd[1] == "coverage"
        assert "-sorted" in coverage_cmd
        assert "-g" in coverage_cmd
        # The coverage step's -a must be the *sorted* annotation, not the
        # original symlinked file, or -sorted's contig-order assumption
        # (build_command's whole reason to exist, per Task 5's docstring)
        # is violated the moment an unsorted annotation slips through.
        a_arg = coverage_cmd[coverage_cmd.index("-a") + 1]
        assert "sorted" in Path(a_arg).name

    def test_result_carries_summary_facts(
        self, bedtools_available, home, tmp_path, monkeypatch
    ):
        payload = _inputs(tmp_path)

        def fake_run(ctx, cmd, **kw):
            if cmd[:2] == ["bedtools", "sort"]:
                Path(kw["log_path"]).write_text("")
            else:
                Path(kw["log_path"]).write_text(_REAL_COVERAGE_TSV)
            return 0

        monkeypatch.setattr(feature_coverage_handlers, "run_subprocess", fake_run)
        result = feature_coverage_handlers.run_feature_coverage(_ctx(payload))

        assert result["object_id"] == "bam-1"
        assert result["project_id"] == "proj-1"
        assert result["job_id"] == "job-1"
        facts = result["facts"]
        assert facts["feature_coverage_status"] == "ok"
        assert facts["feature_coverage_tool_version"] == "2.31.1"
        assert facts["feature_coverage_feature_count"] == 3
        assert facts["feature_coverage_zero_features"] == 1
        assert facts["feature_coverage_median_breadth"] == 0.4975124
        assert facts["feature_coverage_annotation_id"] == "ann-1"
        assert facts["feature_coverage_report"] == "coverage.json"

        report_path = (
            feature_coverage_handlers.settings.feature_coverage_dir
            / "bam-1"
            / "coverage.json"
        )
        assert report_path.exists()

    def test_sort_nonzero_exit_raises_failure(
        self, bedtools_available, home, tmp_path, monkeypatch
    ):
        payload = _inputs(tmp_path)
        monkeypatch.setattr(
            feature_coverage_handlers, "run_subprocess", lambda *a, **k: 1
        )
        with pytest.raises(PermanentError, match="bedtools sort exited 1"):
            feature_coverage_handlers.run_feature_coverage(_ctx(payload))

    def test_coverage_nonzero_exit_raises_failure(
        self, bedtools_available, home, tmp_path, monkeypatch
    ):
        payload = _inputs(tmp_path)

        def fake_run(ctx, cmd, **kw):
            if cmd[:2] == ["bedtools", "sort"]:
                Path(kw["log_path"]).write_text("")
                return 0
            return 1

        monkeypatch.setattr(feature_coverage_handlers, "run_subprocess", fake_run)
        with pytest.raises(PermanentError, match="bedtools coverage exited 1"):
            feature_coverage_handlers.run_feature_coverage(_ctx(payload))


pytestmark_apply = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


class TestApplyFeatureCoverage:
    pytestmark = pytestmark_apply

    @pytest.fixture(autouse=True)
    def _no_queue(self, monkeypatch):
        """Stub the enqueue `ingest_local_file` triggers so this test does
        not need a live Redis -- mirrors test_results_owner.py's fixture of
        the same name and rationale."""
        from app.services import object_service

        async def _skip_ingest(obj, **kwargs):
            return ""

        async def _skip_enqueue(*args, **kwargs):
            return None

        monkeypatch.setattr(object_service, "enqueue_ingest", _skip_ingest)
        monkeypatch.setattr("app.queue.queue.enqueue", _skip_enqueue)

    async def test_merges_facts_onto_the_stored_object(self):
        from app.services import object_service, project_service

        owner = "local"
        project = await project_service.create_project(
            name="feature-coverage-apply", owner=owner
        )
        from app.config import settings

        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        scratch = settings.tmp_dir / "feature-coverage-apply.bam"
        scratch.write_bytes(b"fake-bam-bytes")
        bam = await object_service.ingest_local_file(
            owner=owner,
            project_id=project.id,
            path=scratch,
            name="aligned.bam",
        )

        facts = {
            "feature_coverage_status": "ok",
            "feature_coverage_tool_version": "2.31.1",
            "feature_coverage_feature_count": 3,
            "feature_coverage_zero_features": 1,
            "feature_coverage_median_breadth": 0.4975124,
            "feature_coverage_annotation_id": "ann-1",
            "feature_coverage_report": "coverage.json",
        }
        await results._apply_feature_coverage(
            {"object_id": str(bam.id), "facts": facts}, owner=owner
        )

        refreshed = await results.DataObject.get(bam.id)
        assert refreshed.facts["feature_coverage_feature_count"] == 3
        assert refreshed.facts["feature_coverage_median_breadth"] == 0.4975124
        assert refreshed.facts["feature_coverage_annotation_id"] == "ann-1"

    async def test_does_nothing_when_the_object_is_missing(self):
        # A random valid ObjectId with nothing stored under it: the applier
        # must log and return rather than raise.
        missing_id = PydanticObjectId()
        await results._apply_feature_coverage(
            {"object_id": str(missing_id), "facts": {"feature_coverage_status": "ok"}},
            owner="local",
        )

    async def test_does_nothing_without_facts_or_object_id(self):
        await results._apply_feature_coverage({}, owner="local")
        await results._apply_feature_coverage({"object_id": "x"}, owner="local")
        await results._apply_feature_coverage({"facts": {"a": 1}}, owner="local")
