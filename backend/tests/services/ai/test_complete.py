"""The call itself: pick an adapter, run it, record what happened.

This is where the "never raise" invariant meets the new "leave a trace"
behaviour, so both are asserted here.
"""

import sys

import pytest
import pytest_asyncio

from app.models.ai import AiProvider, FailureReason, ProviderKind
from app.services.ai import crypto, provider_service
from app.services.ai.adapters import Completion, Failure
from app.services.ai.router import ResolvedProvider

# `app.services.ai/__init__.py` re-exports a function named `complete`, which
# shadows the submodule of the same name as an attribute on the package -- so
# `from app.services.ai import complete` (or `import ...complete as x`)
# resolves to the function, not the module `_run` lives on. Pull the real
# submodule out of `sys.modules`, where `import` always registers it under
# its full dotted path regardless of what the package's own `__init__.py`
# rebinds its attribute to.
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


async def _resolved(model: str = "m", cache: list[str] | None = None) -> ResolvedProvider:
    p = await provider_service.create(
        name="P", kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x:1", model=model, api_key=None,
    )
    if cache:
        p.models_cache = cache
        await p.save()
    return ResolvedProvider(
        provider_id=str(p.id), name="P", kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x:1", api_key=None, model=model,
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
