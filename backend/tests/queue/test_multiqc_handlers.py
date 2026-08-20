"""Staging and gating for the project-scoped MultiQC report.

The staging half is where the real behaviour is: MultiQC derives its sample
names from the paths it scans, so what this code copies and what it calls
the copies is what a user ends up reading in the report.
"""

from types import SimpleNamespace

import pytest
from beanie import PydanticObjectId

from app.config import settings
from app.queue import multiqc_handlers


def _obj(*, name=None, facts=None, obj_id=None):
    """A stand-in for DataObject carrying only what staging reads.

    A real DataObject needs a live Mongo connection to construct; staging
    touches `id`, `name` and `facts` and nothing else, so this keeps these
    as pure filesystem tests.
    """
    return SimpleNamespace(
        id=obj_id or PydanticObjectId(),
        name=name,
        facts=facts or {},
    )


@pytest.fixture
def qc_reports(tmp_path, monkeypatch):
    """Point qc_reports_dir at a tmp dir.

    Note this is exactly the isolation the fixtures in test_share_reports.py
    lack -- they write into the live directory, which left 16 orphaned dirs
    on a dev box and is filed as its own issue.
    """
    root = tmp_path / "qc_reports"
    root.mkdir()
    monkeypatch.setattr(type(settings), "qc_reports_dir", property(lambda self: root))
    return root


def _write_fastp(qc_reports, obj):
    d = qc_reports / str(obj.id) / "fastp"
    d.mkdir(parents=True, exist_ok=True)
    (d / "fastp.json").write_text('{"summary": {}}')


def _write_fastqc(qc_reports, obj, stem="reads_R1"):
    d = qc_reports / str(obj.id) / "fastqc"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}_fastqc.zip").write_bytes(b"PK\x03\x04stub")


def _write_quast(qc_reports, obj):
    d = qc_reports / str(obj.id) / "quast"
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.tsv").write_text("Assembly\tassembly\n")


def _write_samtools_stats(qc_reports, obj):
    d = qc_reports / str(obj.id) / "samtools"
    d.mkdir(parents=True, exist_ok=True)
    (d / "stats.txt").write_text("# This file was produced by samtools stats\n")


class TestStageMultiqcInputs:
    def test_counts_objects_that_contributed(self, tmp_path, qc_reports):
        a = _obj(facts={"qc_fastp_data": "fastp/fastp.json"})
        b = _obj(facts={"qc_fastp_data": "fastp/fastp.json"})
        _write_fastp(qc_reports, a)
        _write_fastp(qc_reports, b)

        stage = tmp_path / "stage"
        stage.mkdir()

        assert multiqc_handlers.stage_multiqc_inputs([a, b], stage) == 2

    def test_stages_fastqc_zip_without_any_fact(self, tmp_path, qc_reports):
        """FastQC predates retention -- `_run_fastqc` writes its zip as a
        side effect of running, and records no fact naming it. Presence on
        disk is the only signal, so a glob rather than a fact lookup."""
        a = _obj()
        _write_fastqc(qc_reports, a)

        stage = tmp_path / "stage"
        stage.mkdir()

        assert multiqc_handlers.stage_multiqc_inputs([a], stage) == 1
        assert list(stage.rglob("*_fastqc.zip"))

    def test_object_with_no_report_dir_contributes_nothing(self, tmp_path, qc_reports):
        stage = tmp_path / "stage"
        stage.mkdir()
        assert multiqc_handlers.stage_multiqc_inputs([_obj()], stage) == 0

    def test_a_fact_naming_a_missing_file_is_skipped(self, tmp_path, qc_reports):
        """The fact says retained and the file is gone -- an offloaded
        object, or a report dir cleaned up by hand. That costs one sample's
        row, not the whole report."""
        a = _obj(facts={"qc_fastp_data": "fastp/fastp.json"})
        (qc_reports / str(a.id)).mkdir(parents=True)

        stage = tmp_path / "stage"
        stage.mkdir()

        assert multiqc_handlers.stage_multiqc_inputs([a], stage) == 0

    def test_a_file_on_disk_without_its_fact_is_not_staged(self, tmp_path, qc_reports):
        """Deliberate: the fact is the record that this application wrote
        the file. Globbing for fastp.json instead would let a stale copy, or
        a future tool's output, silently widen what the report covers."""
        a = _obj(facts={})
        _write_fastp(qc_reports, a)

        stage = tmp_path / "stage"
        stage.mkdir()

        assert multiqc_handlers.stage_multiqc_inputs([a], stage) == 0

    def test_each_object_lands_in_its_own_subdirectory(self, tmp_path, qc_reports):
        """Two objects both contributing `fastp.json` must not collide --
        without a per-object directory the second copy overwrites the first
        and the report silently loses a sample."""
        a = _obj(name="sample_A", facts={"qc_fastp_data": "fastp/fastp.json"})
        b = _obj(name="sample_B", facts={"qc_fastp_data": "fastp/fastp.json"})
        _write_fastp(qc_reports, a)
        _write_fastp(qc_reports, b)

        stage = tmp_path / "stage"
        stage.mkdir()
        multiqc_handlers.stage_multiqc_inputs([a, b], stage)

        assert len(list(stage.rglob("fastp.json"))) == 2

    def test_one_unreadable_input_does_not_lose_the_other_object(
        self, tmp_path, qc_reports, monkeypatch
    ):
        """Best-effort per file: a sample whose copy fails drops out of the
        report, and every other sample still appears."""
        a = _obj(facts={"qc_fastp_data": "fastp/fastp.json"})
        b = _obj(facts={"qc_fastp_data": "fastp/fastp.json"})
        _write_fastp(qc_reports, a)
        _write_fastp(qc_reports, b)

        real = multiqc_handlers.shutil.copyfile
        seen: list[str] = []

        def flaky(src, dest, *a_, **k):
            seen.append(str(src))
            if len(seen) == 1:
                raise OSError("input/output error")
            return real(src, dest)

        monkeypatch.setattr(multiqc_handlers.shutil, "copyfile", flaky)

        stage = tmp_path / "stage"
        stage.mkdir()

        assert multiqc_handlers.stage_multiqc_inputs([a, b], stage) == 1


class TestRetainedFactFilesCoverage:
    """Confirms the entries #702 added to RETAINED_FACT_FILES actually
    stage -- not just that the mechanism works generically (the fastp-only
    tests above already prove that), but that these specific tool/path
    pairs are wired correctly. A typo in either half of a
    (fact_key, rel) tuple would silently stage nothing for that tool."""

    def test_stages_a_retained_quast_report_tsv(self, tmp_path, qc_reports):
        a = _obj(facts={"assembly_misassembly_data": "quast/report.tsv"})
        _write_quast(qc_reports, a)

        stage = tmp_path / "stage"
        stage.mkdir()

        assert multiqc_handlers.stage_multiqc_inputs([a], stage) == 1
        assert list(stage.rglob("report.tsv"))

    def test_stages_retained_samtools_stats(self, tmp_path, qc_reports):
        a = _obj(facts={"bam_stats_data": "samtools/stats.txt"})
        _write_samtools_stats(qc_reports, a)

        stage = tmp_path / "stage"
        stage.mkdir()

        assert multiqc_handlers.stage_multiqc_inputs([a], stage) == 1
        assert list(stage.rglob("stats.txt"))

    def test_a_project_with_one_of_each_tool_summarizes(self, tmp_path, qc_reports):
        """The scenario #702 exists for: a project with an aligned BAM, an
        assembly QC'd against a reference, and QC'd reads all count toward
        the same aggregate, even though only one of the three (fastp) was
        wired before this issue."""
        reads = _obj(facts={"qc_fastp_data": "fastp/fastp.json"})
        bam = _obj(facts={"bam_stats_data": "samtools/stats.txt"})
        assembly = _obj(facts={"assembly_misassembly_data": "quast/report.tsv"})
        _write_fastp(qc_reports, reads)
        _write_samtools_stats(qc_reports, bam)
        _write_quast(qc_reports, assembly)

        assert multiqc_handlers.count_summarizable([reads, bam, assembly]) == 3


class TestObjectLabel:
    def test_uses_the_object_name_so_the_report_is_readable(self):
        """This string becomes the sample name a user reads in MultiQC's
        leftmost column."""
        obj = _obj(name="liver_rep1.fastq.gz")
        assert multiqc_handlers._object_label(obj).startswith("liver_rep1.fastq.gz")

    def test_appends_the_id_so_two_same_named_objects_stay_apart(self):
        obj = _obj(name="reads")
        assert str(obj.id) in multiqc_handlers._object_label(obj)

    def test_falls_back_to_the_id_when_there_is_no_name(self):
        obj = _obj(name=None)
        assert multiqc_handlers._object_label(obj) == str(obj.id)

    def test_strips_path_separators_out_of_a_name(self):
        """The label becomes a directory name. A name carrying a separator
        would otherwise write outside the staging directory."""
        obj = _obj(name="../../etc/passwd")
        label = multiqc_handlers._object_label(obj)
        assert "/" not in label
        assert ".." not in label

    def test_strips_shell_and_quote_characters(self):
        """The label reaches MultiQC as part of a path and reaches the
        report as displayed text."""
        obj = _obj(name='a"; rm -rf /; echo "b')
        label = multiqc_handlers._object_label(obj)
        assert all(c not in label for c in '"; ')


class TestGate:
    def test_requires_at_least_two_contributing_objects(self):
        """One sample with two tools is not an aggregate -- the per-object
        QC tab already shows that better."""
        assert multiqc_handlers.MIN_CONTRIBUTING_OBJECTS == 2


class TestRegistration:
    def test_the_handler_is_registered_under_its_job_name(self):
        """handlers.py must import this module or registry.load_handlers()
        never sees the @handler decorator, and the job enqueues into a
        queue nothing can run."""
        from app.queue import handlers  # noqa: F401
        from app.queue.registry import _HANDLERS

        assert "multiqc_report" in _HANDLERS
