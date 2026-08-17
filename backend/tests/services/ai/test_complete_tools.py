"""Threading `tools`/`history` through `complete()` and `complete_sync()`,
and the new `ToolCall` outcome neither function previously had to handle.
"""

import sys

import pytest
import pytest_asyncio

# See test_complete.py for why the submodule must be pulled from sys.modules
# rather than imported as `app.services.ai.complete`.
import app.services.ai.complete  # noqa: F401,E402
from app.models.ai import AiProvider, ProviderKind
from app.services.ai import crypto, provider_service
from app.services.ai.adapters import Completion, ConversationTurn, ToolCall, ToolSpec
from app.services.ai.router import ResolvedProvider

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


async def _resolved(model: str = "m") -> ResolvedProvider:
    p = await provider_service.create(
        name="P", kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x:1", model=model, api_key=None,
    )
    return ResolvedProvider(
        provider_id=str(p.id), name="P", kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x:1", api_key=None, model=model, models_cache=[],
    )


SPEC = ToolSpec(name="x", description="d", parameters={})


class TestCompletePassesToolsAndHistory:
    async def test_passes_tools_through_to_the_adapter(self, monkeypatch):
        captured = {}

        def fake_run(provider, **kwargs):
            captured.update(kwargs)
            return Completion("ok", "m")

        monkeypatch.setattr(complete_mod, "_run", fake_run)
        provider = await _resolved()

        await complete_mod.complete(provider, system="s", user="u", tools=[SPEC])

        assert captured["tools"] == [SPEC]

    async def test_passes_history_through_to_the_adapter(self, monkeypatch):
        captured = {}
        history = [ConversationTurn(role="user", content="hi")]

        def fake_run(provider, **kwargs):
            captured.update(kwargs)
            return Completion("ok", "m")

        monkeypatch.setattr(complete_mod, "_run", fake_run)
        provider = await _resolved()

        await complete_mod.complete(provider, system="s", user="", history=history)

        assert captured["history"] == history

    async def test_returns_a_toolcall_and_still_records_success(self, monkeypatch):
        """A ToolCall is not a Failure -- the round trip succeeded even
        though no final text came back yet, so success is recorded."""
        monkeypatch.setattr(
            complete_mod, "_run", lambda p, **k: ToolCall("id1", "x", {"a": 1})
        )
        provider = await _resolved()

        result = await complete_mod.complete(provider, system="s", user="u", tools=[SPEC])

        assert isinstance(result, ToolCall)
        stored = await provider_service.get(provider.provider_id)
        assert stored.status == "ok"

    async def test_toolcall_does_not_trip_failure_recording(self, monkeypatch):
        monkeypatch.setattr(
            complete_mod, "_run", lambda p, **k: ToolCall("id1", "x", {})
        )
        provider = await _resolved()

        await complete_mod.complete(provider, system="s", user="u", tools=[SPEC])

        stored = await provider_service.get(provider.provider_id)
        assert stored.status != "failed"


class TestCompleteSyncPassesToolsAndHistory:
    async def test_passes_tools_through(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            complete_mod, "_run", lambda p, **k: captured.update(k) or Completion("ok", "m")
        )
        provider = await _resolved()

        complete_mod.complete_sync(provider, system="s", user="u", tools=[SPEC])

        assert captured["tools"] == [SPEC]

    async def test_passes_history_through(self, monkeypatch):
        captured = {}
        history = [ConversationTurn(role="user", content="hi")]
        monkeypatch.setattr(
            complete_mod, "_run", lambda p, **k: captured.update(k) or Completion("ok", "m")
        )
        provider = await _resolved()

        complete_mod.complete_sync(provider, system="s", user="", history=history)

        assert captured["history"] == history

    async def test_returns_a_toolcall_without_touching_the_database(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("complete_sync must not record to the database")

        monkeypatch.setattr(provider_service, "record_failure", boom)
        monkeypatch.setattr(provider_service, "record_success", boom)
        monkeypatch.setattr(
            complete_mod, "_run", lambda p, **k: ToolCall("id1", "x", {})
        )
        provider = await _resolved()

        result = complete_mod.complete_sync(provider, system="s", user="u", tools=[SPEC])

        assert isinstance(result, ToolCall)
