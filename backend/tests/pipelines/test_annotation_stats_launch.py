"""Guards on launching an annotation Results computation."""

from types import SimpleNamespace

import pytest

from app.errors import ValidationError
from app.models.object import FormatKind, ObjectStatus
from app.services.pipeline_service import _check_annotation_stats_callable


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
    )
    assert _is_annotation(obj) is False
