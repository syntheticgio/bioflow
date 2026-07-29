"""Guard behavior in align_reads before it reaches AlignParams.from_dict.

Full align_reads coverage (materializing a reference, invoking a real
aligner binary) belongs to an integration test, not here. This file exists
for one narrow property: `align_reads` must reject an unwired aligner
(bowtie2, HISAT2) before it ever calls `align_runner.AlignParams.from_dict`,
because that alias silently builds minimap2 params regardless of what the
payload names. See test_align_params.py::TestEnsureWired for the guard's own
unit coverage, and test_align_launch.py for the equivalent at the
pipeline_service.launch_alignment call site.
"""

import pytest

from app.errors import PermanentError
from app.queue.align_handlers import align_reads
from app.queue.registry import JobContext


def make_ctx(payload: dict) -> JobContext:
    return JobContext(job_id="job-1", payload=payload, epoch=1, attempts=0)


class TestAlignReadsRejectsUnwiredAligners:
    def test_bowtie2_is_rejected_before_any_tool_or_param_work(self):
        """No reference_object_id, no reads, no params -- if the guard did
        not fire first, this would fail on a KeyError or a missing-blob
        error deep in materialization instead of the clear guard message."""
        with pytest.raises(PermanentError, match="not wired"):
            align_reads(make_ctx({"aligner": "bowtie2"}))

    def test_hisat2_is_rejected_before_any_tool_or_param_work(self):
        with pytest.raises(PermanentError, match="not wired"):
            align_reads(make_ctx({"aligner": "hisat2"}))
