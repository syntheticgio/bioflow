"""Anthropic differs from the OpenAI format in exactly four ways. Each gets a
test, because each is a silent 400 if wrong.
"""

import json

import pytest

from app.models.ai import FailureReason, ProviderKind
from app.services.ai import adapters
from app.services.ai.adapters import (
    ANTHROPIC_VERSION,
    AnthropicAdapter,
    Completion,
    Failure,
    OpenAICompatAdapter,
    adapter_for,
)


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


MESSAGES_OK = {"content": [{"type": "text", "text": "Escherichia coli is a bacterium."}]}


@pytest.fixture
def adapter():
    return AnthropicAdapter(base_url="https://api.anthropic.com", api_key="sk-ant-secret99")


@pytest.fixture
def capture(monkeypatch):
    seen = {}

    def _capture(request, *a, **k):
        seen["url"] = request.full_url
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen["body"] = json.loads(request.data) if request.data else None
        return _Response(MESSAGES_OK)

    monkeypatch.setattr(adapters.urllib.request, "urlopen", _capture)
    return seen


class TestWireFormat:
    def test_posts_to_v1_messages(self, adapter, capture):
        adapter.complete(system="s", user="u", model="claude-x", max_tokens=100)
        assert capture["url"] == "https://api.anthropic.com/v1/messages"

    def test_uses_x_api_key_not_authorization(self, adapter, capture):
        adapter.complete(system="s", user="u", model="claude-x", max_tokens=100)
        assert capture["headers"]["x-api-key"] == "sk-ant-secret99"
        assert "authorization" not in capture["headers"]

    def test_sends_the_version_header(self, adapter, capture):
        adapter.complete(system="s", user="u", model="claude-x", max_tokens=100)
        assert capture["headers"]["anthropic-version"] == ANTHROPIC_VERSION

    def test_system_is_top_level_not_a_message(self, adapter, capture):
        adapter.complete(system="be brief", user="hello", model="claude-x", max_tokens=100)
        assert capture["body"]["system"] == "be brief"
        assert capture["body"]["messages"] == [{"role": "user", "content": "hello"}]


class TestComplete:
    def test_parses_the_content_block(self, adapter, capture):
        result = adapter.complete(system="s", user="u", model="claude-x", max_tokens=100)
        assert isinstance(result, Completion)
        assert result.text == "Escherichia coli is a bacterium."
        assert result.model == "claude-x"

    def test_unparseable_body_is_bad_response(self, adapter, monkeypatch):
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response({"nope": 1})
        )
        result = adapter.complete(system="s", user="u", model="claude-x", max_tokens=10)
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.BAD_RESPONSE


class TestAdapterFor:
    def test_anthropic_kind_gets_the_anthropic_adapter(self):
        a = adapter_for(ProviderKind.ANTHROPIC, base_url="https://x", api_key="k")
        assert isinstance(a, AnthropicAdapter)

    def test_everything_else_gets_openai_compat(self):
        a = adapter_for(ProviderKind.OPENAI_COMPAT, base_url="https://x", api_key="k")
        assert isinstance(a, OpenAICompatAdapter)
