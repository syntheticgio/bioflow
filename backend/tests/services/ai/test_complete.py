"""The call itself: pick an adapter, run it, record what happened.

This is where the "never raise" invariant meets the new "leave a trace"
behaviour, so both are asserted here.
"""

import sys

import pytest
import pytest_asyncio

from app.models.ai import AiProvider, FailureReason, ProviderKind
from app.services.ai import crypto, provider_service, redaction
from app.services.ai.adapters import Completion, Failure
from app.services.ai.router import ResolvedProvider

# `app.services.ai/__init__.py` re-exports a function named `complete`, which
# shadows the submodule of the same name as an attribute on the package -- so
# `import app.services.ai.complete as complete_mod` resolves to the function
# too: that form still imports `app.services.ai` first (running its
# `__init__.py`, which rebinds the `complete` attribute), then does the
# equivalent of `complete_mod = app.services.ai.complete`, an attribute
# lookup on the now-rebound package -- not a `sys.modules` lookup. Pull the
# real submodule out of `sys.modules` instead, where `import` always
# registers it under its full dotted path regardless of what `__init__.py`
# does to the package's own attributes.
import app.services.ai.complete  # noqa: F401,E402 - registers the submodule below

complete_mod = sys.modules["app.services.ai.complete"]

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto.settings, "bioinfo_home", tmp_path)
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean():
    await AiProvider.find_all().delete()


async def _resolved(
    model: str = "m",
    cache: list[str] | None = None,
    api_key: str | None = None,
) -> ResolvedProvider:
    p = await provider_service.create(
        name="P", kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x:1", model=model, api_key=api_key,
    )
    if cache:
        p.models_cache = cache
        await p.save()
    return ResolvedProvider(
        provider_id=str(p.id), name="P", kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x:1", api_key=api_key, model=model,
        models_cache=cache or [],
    )


class TestComplete:
    async def test_returns_the_completion(self, monkeypatch):
        provider = await _resolved()
        monkeypatch.setattr(
            complete_mod, "_run", lambda p, **kw: Completion("text", "m")
        )
        result = await complete_mod.complete(provider, system="s", user="u")
        assert isinstance(result, Completion)
        assert result.text == "text"

    async def test_success_marks_the_provider_ok(self, monkeypatch):
        provider = await _resolved()
        monkeypatch.setattr(
            complete_mod, "_run", lambda p, **kw: Completion("text", "m")
        )
        await complete_mod.complete(provider, system="s", user="u")
        stored = await provider_service.get(provider.provider_id)
        assert stored.status == "ok"

    async def test_failure_records_the_reason_on_the_provider(self, monkeypatch):
        """The badge has to go red from a real job, not only from a manual
        fetch -- a key that expires between tests is the whole reason."""
        provider = await _resolved()
        monkeypatch.setattr(
            complete_mod, "_run", lambda p, **kw: Failure(FailureReason.INVALID_KEY)
        )
        result = await complete_mod.complete(provider, system="s", user="u")

        assert isinstance(result, Failure)
        stored = await provider_service.get(provider.provider_id)
        assert stored.status == "failed"
        assert stored.status_reason == FailureReason.INVALID_KEY

    async def test_never_raises(self, monkeypatch):
        """The invariant the whole package is built around."""

        def explode(p, **kw):
            raise RuntimeError("adapter blew up")

        provider = await _resolved()
        monkeypatch.setattr(complete_mod, "_run", explode)
        result = await complete_mod.complete(provider, system="s", user="u")
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.BAD_RESPONSE

    async def test_falls_back_to_the_first_cached_model(self, monkeypatch):
        """An unset model with a cached list is recoverable -- picking the
        first is better than refusing to run."""
        seen = {}

        def capture(p, **kw):
            seen["model"] = kw["model"]
            return Completion("t", kw["model"])

        provider = await _resolved(model="", cache=["cached-first", "other"])
        monkeypatch.setattr(complete_mod, "_run", capture)
        await complete_mod.complete(provider, system="s", user="u")
        assert seen["model"] == "cached-first"

    async def test_no_model_and_no_cache_is_model_not_found(self, monkeypatch):
        provider = await _resolved(model="", cache=[])
        result = await complete_mod.complete(provider, system="s", user="u")
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.MODEL_NOT_FOUND

    async def test_max_tokens_defaults_to_the_setting(self, monkeypatch):
        from app.config import settings

        seen = {}

        def capture(p, **kw):
            seen["max_tokens"] = kw["max_tokens"]
            return Completion("t", "m")

        provider = await _resolved()
        monkeypatch.setattr(complete_mod, "_run", capture)
        await complete_mod.complete(provider, system="s", user="u")
        assert seen["max_tokens"] == settings.llm_max_tokens

    async def test_max_tokens_can_be_overridden(self, monkeypatch):
        seen = {}

        def capture(p, **kw):
            seen["max_tokens"] = kw["max_tokens"]
            return Completion("t", "m")

        provider = await _resolved()
        monkeypatch.setattr(complete_mod, "_run", capture)
        await complete_mod.complete(provider, system="s", user="u", max_tokens=250)
        assert seen["max_tokens"] == 250


class TestCompleteSync:
    async def test_returns_the_completion(self, monkeypatch):
        provider = await _resolved()
        monkeypatch.setattr(
            complete_mod, "_run", lambda p, **kw: Completion("text", "m")
        )
        result = complete_mod.complete_sync(provider, system="s", user="u")
        assert isinstance(result, Completion)
        assert result.text == "text"

    async def test_never_raises(self, monkeypatch):
        """The invariant the whole package is built around -- this is the
        function a worker-thread queue handler will call directly."""

        def explode(p, **kw):
            raise RuntimeError("adapter blew up")

        provider = await _resolved()
        monkeypatch.setattr(complete_mod, "_run", explode)
        result = complete_mod.complete_sync(provider, system="s", user="u")
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.BAD_RESPONSE

    async def test_no_model_and_no_cache_is_model_not_found(self, monkeypatch):
        provider = await _resolved(model="", cache=[])
        result = complete_mod.complete_sync(provider, system="s", user="u")
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.MODEL_NOT_FOUND

    async def test_does_not_touch_the_database(self, monkeypatch):
        """complete_sync() is meant to run off the event loop in a worker
        thread, so it must never call the async DB-recording helpers --
        doing so from a worker thread is exactly the kind of unhandled
        failure this coverage exists to catch before Task 14 lands."""

        def boom(*args, **kwargs):
            raise AssertionError("complete_sync must not record to the database")

        monkeypatch.setattr(provider_service, "record_failure", boom)
        monkeypatch.setattr(provider_service, "record_success", boom)

        provider = await _resolved()

        monkeypatch.setattr(
            complete_mod, "_run", lambda p, **kw: Completion("text", "m")
        )
        success_result = complete_mod.complete_sync(provider, system="s", user="u")
        assert isinstance(success_result, Completion)

        monkeypatch.setattr(
            complete_mod, "_run", lambda p, **kw: Failure(FailureReason.INVALID_KEY)
        )
        failure_result = complete_mod.complete_sync(provider, system="s", user="u")
        assert isinstance(failure_result, Failure)


KEY = "sk-test-abcdef0123456789"


def _explode_echoing_the_key(prefix: str = "", suffix: str = ""):
    """An adapter crash whose message embeds the key -- a client library
    reprs the request, and the Authorization header comes with it."""

    def explode(p, **kw):
        raise RuntimeError(f"{prefix}POST failed: Authorization: Bearer {KEY}{suffix}")

    return explode


class TestCrashLogsDoNotLeakTheKey:
    """`str(e)` on an adapter crash is unscrubbed input. A provider client
    that echoes the request into its exception message puts the key in the
    log, which is the artifact people paste into bug reports.
    """

    async def test_complete_scrubs_the_log(self, monkeypatch, capsys):
        provider = await _resolved(api_key=KEY)
        monkeypatch.setattr(complete_mod, "_run", _explode_echoing_the_key())
        await complete_mod.complete(provider, system="s", user="u")

        # structlog prints straight to stdout in this codebase's config;
        # caplog cannot see it (see test_subprocess.py's note on this).
        out = capsys.readouterr().out
        assert "ai_call_crashed" in out
        assert KEY not in out
        assert redaction.REDACTED in out

    async def test_complete_scrubs_the_returned_failure(self, monkeypatch):
        """The detail reaches `record_failure` and the settings page, which
        outlives the log line."""
        provider = await _resolved(api_key=KEY)
        monkeypatch.setattr(complete_mod, "_run", _explode_echoing_the_key())
        result = await complete_mod.complete(provider, system="s", user="u")

        assert isinstance(result, Failure)
        assert KEY not in result.detail
        assert redaction.REDACTED in result.detail

    async def test_complete_sync_scrubs_the_log(self, monkeypatch, capsys):
        provider = await _resolved(api_key=KEY)
        monkeypatch.setattr(complete_mod, "_run", _explode_echoing_the_key())
        complete_mod.complete_sync(provider, system="s", user="u")

        out = capsys.readouterr().out
        assert "ai_call_crashed" in out
        assert KEY not in out
        assert redaction.REDACTED in out

    async def test_complete_sync_scrubs_the_returned_failure(self, monkeypatch):
        """Thread handlers return this detail in their result payload, where
        `results.py` persists it."""
        provider = await _resolved(api_key=KEY)
        monkeypatch.setattr(complete_mod, "_run", _explode_echoing_the_key())
        result = complete_mod.complete_sync(provider, system="s", user="u")

        assert isinstance(result, Failure)
        assert KEY not in result.detail
        assert redaction.REDACTED in result.detail

    async def test_a_key_straddling_the_truncation_boundary_is_scrubbed(
        self, monkeypatch, capsys
    ):
        """Truncating before scrubbing would slice the key in half and leave a
        prefix that `replace` no longer matches -- a partial key in the log.
        This is what pins the scrub-then-truncate order.
        """
        # Land the key across MAX_BODY_CHARS so a leading slice would split it.
        padding = "x" * (redaction.MAX_BODY_CHARS - len(KEY) // 2)
        provider = await _resolved(api_key=KEY)
        crash = _explode_echoing_the_key(prefix=padding)
        monkeypatch.setattr(complete_mod, "_run", crash)
        result = await complete_mod.complete(provider, system="s", user="u")

        out = capsys.readouterr().out
        # No fragment of the key survives, in the log or the stored detail.
        head = KEY[: len(KEY) // 2]
        assert head not in out
        assert head not in result.detail
