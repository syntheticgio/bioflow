"""The variant summary job's failure style. Mirrors test_de_summary_handler.py."""

import pytest

from app.errors import PermanentError
from app.models.ai import FailureReason, ProviderKind
from app.queue import variant_summary_handlers
from app.queue.registry import JobContext
from app.queue.variant_summary_handlers import summarize_variant_results
from app.services import variant_summary_prompt
from app.services.ai.adapters import Completion, Failure
from app.services.ai.router import ResolvedProvider


def _ctx(payload: dict) -> JobContext:
    return JobContext(job_id="job-1", payload=payload, epoch=1, attempts=1, owner="local")


def _payload(**overrides) -> dict:
    base = {
        "object_id": "obj-1",
        "facts": {
            "vcf_stats_summary": {"variants": 48213, "ti_tv_ratio": 2.14},
        },
        "top_variants": [
            {"gene": "BRCA1", "position": "chr17:43094464", "consequence": "stop_gained"}
        ],
        "facts_fingerprint": "abc123",
    }
    base.update(overrides)
    return base


def _fake_provider():
    return ResolvedProvider(
        provider_id="000000000000000000000000",
        name="Test",
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x:1",
        api_key=None,
        model="test-model",
        models_cache=[],
    )


class TestSkips:
    def test_no_provider_is_a_success_with_a_reason_not_a_failure(self, monkeypatch):
        monkeypatch.setattr(variant_summary_handlers, "_resolve_sync", lambda: None)
        result = summarize_variant_results(_ctx(_payload()))
        assert result["skipped"] == "no_provider"

    def test_a_result_with_nothing_to_say_is_skipped_before_the_model_is_called(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            variant_summary_handlers, "_resolve_sync", lambda: _fake_provider()
        )

        def must_not_run(*a, **k):
            raise AssertionError("the model must not be called with no prompt")

        monkeypatch.setattr(variant_summary_handlers, "_complete", must_not_run)
        result = summarize_variant_results(
            _ctx(
                _payload(
                    facts={"vcf_stats_summary": {"variants": 0}}, top_variants=[]
                )
            )
        )
        assert result["skipped"] == "insufficient_data"


class TestSuccess:
    def test_a_generated_summary_carries_its_model_and_fingerprint(self, monkeypatch):
        monkeypatch.setattr(
            variant_summary_handlers, "_resolve_sync", lambda: _fake_provider()
        )
        monkeypatch.setattr(
            variant_summary_handlers,
            "_complete",
            lambda p, **kw: Completion("48,213 variants were called.", "test-model"),
        )
        result = summarize_variant_results(_ctx(_payload()))
        assert result["summary"] == "48,213 variants were called."
        assert result["model"] == "test-model"
        assert result["facts_fingerprint"] == "abc123"


class TestPayloadValidation:
    def test_a_payload_with_no_object_is_permanently_bad(self):
        with pytest.raises(PermanentError):
            summarize_variant_results(_ctx({"facts": {}}))


class TestFailureReasons:
    def test_a_failure_is_reported_in_the_result(self, monkeypatch):
        monkeypatch.setattr(
            variant_summary_handlers, "_resolve_sync", lambda: _fake_provider()
        )
        monkeypatch.setattr(
            variant_summary_handlers,
            "_complete",
            lambda p, **kw: Failure(FailureReason.INVALID_KEY),
        )
        result = summarize_variant_results(_ctx(_payload()))
        assert result["skipped"] == "invalid_key"
