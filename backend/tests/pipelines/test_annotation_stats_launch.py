"""Guards on launching an annotation Results computation."""

from types import SimpleNamespace

import pytest

from app.errors import ValidationError
from app.models.object import FormatKind, ObjectStatus
from app.queue.registry import JobContext
from app.services.pipeline_service import _check_annotation_stats_callable


def _ctx(*, payload: dict, tmp_path, monkeypatch) -> JobContext:
    from app.config import settings

    monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
    return JobContext(
        job_id="job-ann-1", payload=payload, epoch=1, attempts=1, owner="local"
    )


class _Obj:
    def __init__(self, kind, status=ObjectStatus.READY):
        self.id = "000000000000000000000000"
        self.format = type("F", (), {"kind": kind})()
        self.status = status
        self.name = "ann.gff3"


class TestCallableGuard:
    @pytest.mark.parametrize("kind", [FormatKind.GFF, FormatKind.GTF, FormatKind.BED])
    def test_accepts_annotation_formats(self, kind):
        _check_annotation_stats_callable(_Obj(kind))

    @pytest.mark.parametrize("kind", [FormatKind.BAM, FormatKind.VCF, FormatKind.FASTA])
    def test_rejects_other_formats(self, kind):
        with pytest.raises(ValidationError, match="annotation"):
            _check_annotation_stats_callable(_Obj(kind))

    def test_rejects_an_object_still_ingesting(self):
        with pytest.raises(ValidationError, match="ready"):
            _check_annotation_stats_callable(_Obj(FormatKind.GFF, status=ObjectStatus.INGESTING))


def test_genbank_is_annotation_stats_callable():
    from app.services.pipeline_service import _check_annotation_stats_callable

    obj = SimpleNamespace(
        id="x",
        name="ecoli.gbff",
        status=ObjectStatus.READY,
        format=SimpleNamespace(kind=FormatKind.GENBANK),
    )
    # Does not raise.
    _check_annotation_stats_callable(obj)


def test_genbank_does_not_count_as_a_featurecounts_annotation():
    # _is_annotation gates resolve_annotation/launch_quantify, not just
    # annotation Results eligibility. featureCounts cannot read a GenBank
    # flat file and attributes_for_format has no GenBank case, so unlike
    # _check_annotation_stats_callable above, GenBank must NOT qualify here
    # -- see _is_annotation's docstring.
    from app.services.pipeline_service import _is_annotation

    obj = SimpleNamespace(
        status=ObjectStatus.READY,
        format=SimpleNamespace(kind=FormatKind.GENBANK),
        role=None,
    )
    assert _is_annotation(obj) is False


def test_handler_stores_hierarchy_facts(tmp_path, monkeypatch):
    """The integrity counts the Results view reads come from the job."""
    from app.queue import annotation_handlers

    source = tmp_path / "a.gff"
    source.write_text(
        "##gff-version 3\n"
        "chr1\t.\tgene\t100\t900\t.\t+\t.\tID=g1;Name=BRCA1\n"
        "chr1\t.\tmRNA\t100\t500\t.\t+\t.\tID=t1;Parent=g1\n"
        "chr1\t.\texon\t100\t200\t.\t+\t.\tID=e1;Parent=t1\n"
        "chr1\t.\texon\t300\t400\t.\t+\t.\tID=e2;Parent=nosuchtranscript\n"
    )

    ctx = _ctx(
        payload={
            "object_id": "507f1f77bcf86cd799439011",
            "format_kind": "gff",
            "annotation_path": str(source),
            "contig_lengths": [["chr1", 1000]],
        },
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    facts = annotation_handlers.run_annotation_stats(ctx)["facts"]

    assert facts["annotation_unresolved_count"] == 1
    assert facts["annotation_parent_status_counts"]["dangling"] == 1
    assert facts["annotation_gene_mode"] == "typed"
    assert facts["annotation_gene_count"] == 1
    assert facts["annotation_feature_count"] == 4
    # gene -> mRNA -> exon is two hops; the dangling exon's sentinel depth
    # must not be what gets reported here.
    assert facts["annotation_max_depth"] == 2
