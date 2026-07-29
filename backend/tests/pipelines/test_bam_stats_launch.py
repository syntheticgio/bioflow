"""BAM Results launch rules: what may be computed and what blocks it.

Mirrors test_align_launch.py's FakeObject approach -- pure decisions, no
database or HTTP. launch_bam_stats itself (the async DB-touching wrapper) is
exercised through the running app in the manual verification pass, since this
service module has no established pattern for a database-backed launch test.
"""

import pytest

from app.errors import ValidationError
from app.models import FormatKind, ObjectStatus
from app.services import pipeline_service


class FakeObject:
    def __init__(
        self,
        name="aligned.bam",
        *,
        kind=FormatKind.BAM,
        status=ObjectStatus.READY,
        facts=None,
    ):
        self.name = name
        self.format = type("F", (), {"kind": kind})()
        self.status = status
        self.facts = facts or {}
        self.id = name


class TestBamStatsCallable:
    def test_accepts_a_ready_sorted_bam(self):
        pipeline_service._check_bam_stats_callable(
            FakeObject(facts={"sort_order": "coordinate"})
        )

    @pytest.mark.parametrize(
        "status",
        [ObjectStatus.UPLOADING, ObjectStatus.HASHING, ObjectStatus.INGESTING,
         ObjectStatus.ERROR, ObjectStatus.MISSING],
    )
    def test_rejects_a_file_that_is_not_ready(self, status):
        with pytest.raises(ValidationError, match="not ready"):
            pipeline_service._check_bam_stats_callable(
                FakeObject(status=status, facts={"sort_order": "coordinate"})
            )

    @pytest.mark.parametrize("kind", [FormatKind.FASTQ, FormatKind.FASTA, FormatKind.VCF])
    def test_rejects_anything_that_is_not_bam(self, kind):
        with pytest.raises(ValidationError, match="not a BAM alignment"):
            pipeline_service._check_bam_stats_callable(
                FakeObject(kind=kind, facts={"sort_order": "coordinate"})
            )

    def test_rejects_a_bam_that_is_not_coordinate_sorted(self):
        with pytest.raises(ValidationError, match="not coordinate-sorted"):
            pipeline_service._check_bam_stats_callable(
                FakeObject(facts={"sort_order": "queryname"})
            )

    def test_rejects_a_bam_with_no_recorded_sort_order(self):
        """Absent sort_order is treated the same as 'not coordinate-sorted' --
        the header parse records sort_order whenever the BAM declares one, so
        a missing value means it never declared coordinate order."""
        with pytest.raises(ValidationError, match="not coordinate-sorted"):
            pipeline_service._check_bam_stats_callable(FakeObject(facts={}))
