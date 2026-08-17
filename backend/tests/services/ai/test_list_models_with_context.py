"""list_models_with_context(): a second method alongside list_models(),
capturing /v1/models' context_length field for compaction's use -- added as
a new method rather than changing list_models()'s existing return shape,
since list_models() already has callers that only want the id list.
"""

import json

from app.services.ai import adapters
from app.services.ai.adapters import AnthropicAdapter, Failure, OpenAICompatAdapter


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestOpenAiCompat:
    def test_captures_context_length_when_present(self, monkeypatch):
        adapter = OpenAICompatAdapter(base_url="http://x", api_key=None)
        payload = {
            "data": [
                {"id": "model-a", "context_length": 32000},
                {"id": "model-b"},
            ]
        }
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )

        result = adapter.list_models_with_context()

        assert result == {"model-a": 32000, "model-b": None}

    def test_propagates_failure(self, monkeypatch):
        adapter = OpenAICompatAdapter(base_url="http://x", api_key=None)

        def raise_it(*a, **k):
            import urllib.error

            raise urllib.error.HTTPError("http://x", 500, "err", {}, None)

        monkeypatch.setattr(adapters.urllib.request, "urlopen", raise_it)

        result = adapter.list_models_with_context()

        assert isinstance(result, Failure)

    def test_empty_data_is_an_empty_dict_not_a_failure(self, monkeypatch):
        adapter = OpenAICompatAdapter(base_url="http://x", api_key=None)
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response({"data": []})
        )

        assert adapter.list_models_with_context() == {}


class TestAnthropic:
    def test_returns_none_for_every_model(self, monkeypatch):
        """Anthropic's /v1/models has never been observed to carry
        context_length -- every model maps to None, not omitted."""
        adapter = AnthropicAdapter(base_url="https://api.anthropic.com", api_key="k")
        payload = {"data": [{"id": "claude-x"}, {"id": "claude-y"}]}
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )

        result = adapter.list_models_with_context()

        assert result == {"claude-x": None, "claude-y": None}

    def test_propagates_failure(self, monkeypatch):
        adapter = AnthropicAdapter(base_url="https://api.anthropic.com", api_key="k")

        def raise_it(*a, **k):
            import urllib.error

            raise urllib.error.HTTPError("http://x", 401, "err", {}, None)

        monkeypatch.setattr(adapters.urllib.request, "urlopen", raise_it)

        result = adapter.list_models_with_context()

        assert isinstance(result, Failure)
