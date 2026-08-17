"""Which objects auto-analyze at ingest.

The sidecar case is the one that matters. On a real database, every object
whose format.kind is BED was a .fai or a STAR .ann index misdetected as BED
-- 8 of them, all carrying sidecar_of. Without that exclusion every
reference ingest queues 8 jobs that each write a database of garbage
intervals, and no test that only checks real annotations would notice.
"""

from types import SimpleNamespace

import pytest
from app.models.object import FormatKind
from app.services.pipeline_service import should_auto_analyze_annotation


def _obj(kind=FormatKind.GFF, sidecar_of=None, facts=None):
    return SimpleNamespace(kind=kind, sidecar_of=sidecar_of, facts=facts or {})


class TestFormat:
    @pytest.mark.parametrize(
        "kind",
        [FormatKind.GFF, FormatKind.GTF, FormatKind.BED, FormatKind.GENBANK],
    )
    def test_every_annotation_format_qualifies(self, kind):
        o = _obj(kind=kind)
        assert should_auto_analyze_annotation(
            kind=o.kind, sidecar_of=o.sidecar_of, facts=o.facts
        ) is True

    @pytest.mark.parametrize(
        "kind", [FormatKind.BAM, FormatKind.VCF, FormatKind.FASTA, FormatKind.FASTQ]
    )
    def test_non_annotation_formats_do_not(self, kind):
        assert should_auto_analyze_annotation(
            kind=kind, sidecar_of=None, facts={}
        ) is False


class TestSidecarGuard:
    def test_a_sidecar_never_qualifies(self):
        # A .fai misdetected as BED. This is the direction that fails when
        # the guard breaks.
        assert should_auto_analyze_annotation(
            kind=FormatKind.BED, sidecar_of="507f1f77bcf86cd799439011", facts={}
        ) is False

    @pytest.mark.parametrize(
        "kind",
        [FormatKind.GFF, FormatKind.GTF, FormatKind.BED, FormatKind.GENBANK],
    )
    def test_the_guard_applies_to_every_format(self, kind):
        assert should_auto_analyze_annotation(
            kind=kind, sidecar_of="507f1f77bcf86cd799439011", facts={}
        ) is False


class TestAlreadyAnalyzed:
    def test_a_never_analyzed_annotation_qualifies(self):
        assert should_auto_analyze_annotation(
            kind=FormatKind.GFF, sidecar_of=None, facts={}
        ) is True

    def test_one_analyzed_without_a_reference_qualifies_for_repair(self):
        assert should_auto_analyze_annotation(
            kind=FormatKind.GFF,
            sidecar_of=None,
            facts={
                "annotation_stats_status": "ok",
                "annotation_contig_lengths_known": False,
            },
        ) is True

    def test_a_fully_analyzed_annotation_does_not(self):
        assert should_auto_analyze_annotation(
            kind=FormatKind.GFF,
            sidecar_of=None,
            facts={
                "annotation_stats_status": "ok",
                "annotation_contig_lengths_known": True,
            },
        ) is False
