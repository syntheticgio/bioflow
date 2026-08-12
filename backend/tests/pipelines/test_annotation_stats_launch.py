"""Guards on launching an annotation Results computation."""

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
