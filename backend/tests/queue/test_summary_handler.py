"""The summary job's failure style.

A summary is additive. The handler therefore has to distinguish a *skip* -- no
server, nothing worth saying -- from a failure, and report the skips as success
so the activity view does not fill with red rows for a feature the user may
never have opted into.
"""

import pytest
from app.errors import PermanentError
from app.models.ai import FailureReason, ProviderKind
from app.queue import summary_handlers
from app.queue.registry import JobContext
from app.queue.summary_handlers import summarize_object
from app.services import summary_prompt
from app.services.ai.adapters import Completion, Failure
from app.services.ai.router import ResolvedProvider


def _ctx(payload: dict) -> JobContext:
    return JobContext(job_id="job-1", payload=payload, epoch=1, attempts=1, owner="local")


def _payload(**overrides) -> dict:
    base = {
        "object_id": "obj-1",
        "name": "reads.fastq.gz",
        "format_kind": "fastq",
        "organism": "Homo sapiens",
        "facts": {
            "qc_tool": "fastp",
            "qc_before_filtering": {"total_reads": 1000, "q30_rate": 0.93},
        },
        "metadata": {},
        "facts_fingerprint": "abc123",
    }
    base.update(overrides)
    return base


def _fake_provider():
    """A resolved provider that no test actually calls out to."""
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
        monkeypatch.setattr(summary_handlers, "_resolve_sync", lambda: None)
        result = summarize_object(_ctx(_payload()))
        assert result["skipped"] == "no_provider"
        assert "summary" not in result

    def test_a_file_with_nothing_to_say_is_skipped_before_the_model_is_called(
        self, monkeypatch
    ):
        monkeypatch.setattr(summary_handlers, "_resolve_sync", lambda: _fake_provider())

        def must_not_run(*a, **k):
            raise AssertionError("the model must not be called with no prompt")

        monkeypatch.setattr(summary_handlers, "_complete", must_not_run)
        result = summarize_object(_ctx(_payload(facts={}, metadata={})))
        assert result["skipped"] == "insufficient_data"

    def test_a_model_that_returns_nothing_is_skipped_rather_than_stored(self, monkeypatch):
        monkeypatch.setattr(summary_handlers, "_resolve_sync", lambda: _fake_provider())
        monkeypatch.setattr(
            summary_handlers, "_complete", lambda p, **kw: Failure(FailureReason.BAD_RESPONSE)
        )
        result = summarize_object(_ctx(_payload()))
        assert result["skipped"] == "bad_response"


class TestSuccess:
    def test_a_generated_summary_carries_its_model_and_fingerprint(self, monkeypatch):
        monkeypatch.setattr(summary_handlers, "_resolve_sync", lambda: _fake_provider())
        monkeypatch.setattr(
            summary_handlers,
            "_complete",
            lambda p, **kw: Completion("The reads look usable.", "test-model"),
        )
        result = summarize_object(_ctx(_payload()))
        assert result["summary"] == "The reads look usable."
        assert result["model"] == "test-model"
        # Without this the UI cannot tell a current summary from a stale one.
        assert result["facts_fingerprint"] == "abc123"

    def test_the_organism_reaches_the_prompt(self, monkeypatch):
        """The whole premise of the feature: species is what turns a number
        into a judgement."""
        seen = {}
        monkeypatch.setattr(summary_handlers, "_resolve_sync", lambda: _fake_provider())

        def capture(p, *, system, user, **kw):
            seen["user"] = user
            return Completion("ok", "test-model")

        monkeypatch.setattr(summary_handlers, "_complete", capture)
        summarize_object(_ctx(_payload()))
        assert "Homo sapiens" in seen["user"]
        assert summary_prompt.SYSTEM_PROMPT  # sanity: a system prompt exists


class TestPayloadValidation:
    def test_a_payload_with_no_object_is_permanently_bad(self):
        """Unlike the soft skips above: retrying cannot fix a job that does not
        say what it is about."""
        with pytest.raises(PermanentError):
            summarize_object(_ctx({"name": "x"}))


class TestFailureReasons:
    def test_a_failure_is_reported_in_the_result(self, monkeypatch):
        """The new behaviour: a summary that did not appear says why, instead
        of being a silent no-op."""
        monkeypatch.setattr(summary_handlers, "_resolve_sync", lambda: _fake_provider())
        monkeypatch.setattr(
            summary_handlers,
            "_complete",
            lambda p, **kw: Failure(FailureReason.INVALID_KEY),
        )
        result = summarize_object(_ctx(_payload()))
        assert result["skipped"] == "invalid_key"

    def test_nothing_configured_is_reported_distinctly(self, monkeypatch):
        monkeypatch.setattr(summary_handlers, "_resolve_sync", lambda: None)
        result = summarize_object(_ctx(_payload()))
        assert result["skipped"] == "no_provider"
