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
