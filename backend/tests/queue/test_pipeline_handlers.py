"""Direct tests for pipeline_handlers.py's pure helper functions.

Most of trim_reads/run_qc's behavior is covered indirectly through the
runner modules' own tests (fastp_runner, cutadapt_runner,
trimmomatic_runner) plus TestBlobExtensionHazard's source-inspection trick
in test_fastp_runner.py. This file is for the handful of validation
functions that live in pipeline_handlers.py itself and have no other home.
"""

import pytest

from app.errors import PermanentError
from app.queue import pipeline_handlers


class TestCheckTrimmomaticAdapterFile:
    @pytest.mark.parametrize(
        "adapter_file",
        [
            "NexteraPE-PE.fa",
            "TruSeq2-PE.fa",
            "TruSeq2-SE.fa",
            "TruSeq3-PE-2.fa",
            "TruSeq3-PE.fa",
            "TruSeq3-SE.fa",
        ],
    )
    def test_known_adapter_files_are_accepted(self, adapter_file):
        pipeline_handlers._check_trimmomatic_adapter_file(adapter_file)  # no raise

    def test_none_is_accepted(self):
        """None means "no ILLUMINACLIP step" -- a legitimate configuration,
        not something to reject."""
        pipeline_handlers._check_trimmomatic_adapter_file(None)

    def test_unknown_filename_is_rejected(self):
        with pytest.raises(PermanentError, match="Unknown Trimmomatic adapter file"):
            pipeline_handlers._check_trimmomatic_adapter_file("not-a-real-file.fa")

    def test_path_traversal_is_rejected(self):
        with pytest.raises(PermanentError, match="Unknown Trimmomatic adapter file"):
            pipeline_handlers._check_trimmomatic_adapter_file("../../etc/passwd")

    def test_step_injection_is_rejected(self):
        """A value crafted to smuggle an extra Trimmomatic step through the
        colon-delimited ILLUMINACLIP argument must be rejected, not merely
        an unrecognized filename -- both fail the same allowlist check."""
        with pytest.raises(PermanentError, match="Unknown Trimmomatic adapter file"):
            pipeline_handlers._check_trimmomatic_adapter_file("x.fa:2:30:10 CROP:1")


class TestRetainMultiqcInput:
    """`_retain_multiqc_input` copies a tool's machine-readable output into
    the object's report directory so MultiQC can parse it later.

    The reason this exists at all: MultiQC reads raw tool output, not
    rendered HTML, and `_run_short_read_qc` parsed fastp's JSON into facts
    and left it in the workdir, which is reaped. FastQC needed no
    equivalent -- it already writes its zip into the report directory.
    """

    def test_copies_the_source_under_the_requested_name(self, tmp_path):
        src = tmp_path / "fastp_qc.json"
        src.write_text('{"summary": {}}')
        report_dir = tmp_path / "reports"
        report_dir.mkdir()

        pipeline_handlers._retain_multiqc_input(src, report_dir, "fastp/fastp.json")

        assert (report_dir / "fastp" / "fastp.json").read_text() == '{"summary": {}}'

    def test_creates_the_intermediate_directory(self, tmp_path):
        """MultiQC keys its module detection partly on the containing
        directory, and the destination subdirectory will not exist on the
        first QC run for an object."""
        src = tmp_path / "fastp_qc.json"
        src.write_text("{}")
        report_dir = tmp_path / "reports"
        report_dir.mkdir()

        pipeline_handlers._retain_multiqc_input(src, report_dir, "fastp/fastp.json")

        assert (report_dir / "fastp").is_dir()

    def test_a_missing_source_is_not_an_error(self, tmp_path):
        """fastp writing no JSON is a degraded run, not a failed one. The
        facts it did produce are the half of the result the user cannot get
        any other way, so a missing file here must not raise."""
        report_dir = tmp_path / "reports"
        report_dir.mkdir()

        pipeline_handlers._retain_multiqc_input(
            tmp_path / "absent.json", report_dir, "fastp/fastp.json"
        )

        assert not (report_dir / "fastp" / "fastp.json").exists()

    def test_an_unwritable_destination_is_swallowed(self, tmp_path, monkeypatch):
        """Same posture `_run_fastqc` and the QUAST report copy already
        take: a copy failure costs a nicety, while raising would cost the
        facts from a job that already did the expensive work."""
        src = tmp_path / "fastp_qc.json"
        src.write_text("{}")
        report_dir = tmp_path / "reports"
        report_dir.mkdir()

        def boom(*a, **k):
            raise OSError("read-only file system")

        monkeypatch.setattr(pipeline_handlers.shutil, "copyfile", boom)

        pipeline_handlers._retain_multiqc_input(src, report_dir, "fastp/fastp.json")

    def test_returns_whether_it_retained_anything(self, tmp_path):
        """The caller records a fact only when a file actually landed, so
        the return value has to distinguish the two outcomes rather than
        being fire-and-forget."""
        src = tmp_path / "fastp_qc.json"
        src.write_text("{}")
        report_dir = tmp_path / "reports"
        report_dir.mkdir()

        assert (
            pipeline_handlers._retain_multiqc_input(
                src, report_dir, "fastp/fastp.json"
            )
            is True
        )
        assert (
            pipeline_handlers._retain_multiqc_input(
                tmp_path / "absent.json", report_dir, "fastp/fastp.json"
            )
            is False
        )
