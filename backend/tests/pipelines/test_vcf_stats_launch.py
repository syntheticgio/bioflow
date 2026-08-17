"""Preconditions for launching a Variant Results computation."""

import pytest
from app.errors import ValidationError
from app.models import FormatInfo, ObjectStatus
from app.services.pipeline_service import _check_vcf_stats_callable


class _Obj:
    def __init__(self, kind: str, status=ObjectStatus.READY):
        self.id = "abc123"
        self.name = f"sample.{kind}"
        self.status = status
        self.format = FormatInfo(kind=kind)
        self.facts: dict = {}


class TestCallable:
    def test_a_ready_vcf_is_callable(self):
        _check_vcf_stats_callable(_Obj("vcf"))

    def test_a_ready_bcf_is_callable(self):
        _check_vcf_stats_callable(_Obj("bcf"))

    def test_a_bam_is_refused(self):
        with pytest.raises(ValidationError, match="not a VCF"):
            _check_vcf_stats_callable(_Obj("bam"))

    def test_an_unready_vcf_is_refused(self):
        with pytest.raises(ValidationError, match="not ready"):
            _check_vcf_stats_callable(_Obj("vcf", status=ObjectStatus.INGESTING))
