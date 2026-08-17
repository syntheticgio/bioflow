"""The OpenAI-compatible adapter, against a stubbed urlopen.

No network and no database: this is a request-builder and a response-parser,
and both are worth testing in isolation from either.
"""

import json
import urllib.error

import pytest
from app.models.ai import FailureReason
from app.services.ai import adapters
from app.services.ai.adapters import Completion, Failure, OpenAICompatAdapter


class _Response:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code: int, body: str = "{}"):
    def raise_it(*a, **k):
        raise urllib.error.HTTPError("http://x", code, "err", {}, None)

    return raise_it


@pytest.fixture
def adapter():
    return OpenAICompatAdapter(base_url="http://model:1234", api_key="sk-test123456")


CHAT_OK = {"choices": [{"message": {"content": "The reads look usable."}}]}


class TestComplete:
    def test_returns_the_text_and_model(self, adapter, monkeypatch):
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(CHAT_OK)
        )
        result = adapter.complete(system="s", user="u", model="m", max_tokens=100)
        assert isinstance(result, Completion)
        assert result.text == "The reads look usable."
        assert result.model == "m"

    def test_prefers_the_servers_own_model_claim(self, adapter, monkeypatch):
        """A local server can keep serving a previously-loaded model while
        echoing back a different requested one -- report what actually
        answered, not what was asked for."""
        payload = {
            "model": "mlx-community/other-model",
            "choices": [{"message": {"content": "ok"}}],
        }
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        result = adapter.complete(system="s", user="u", model="requested", max_tokens=10)
        assert isinstance(result, Completion)
        assert result.model == "mlx-community/other-model"

    def test_falls_back_to_the_requested_model_when_absent(self, adapter, monkeypatch):
        """Some servers omit the `model` field; the requested name then stands."""
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(CHAT_OK)
        )
        result = adapter.complete(system="s", user="u", model="m", max_tokens=100)
        assert isinstance(result, Completion)
        assert result.model == "m"

    def test_sends_a_bearer_header(self, adapter, monkeypatch):
        seen = {}

        def capture(request, *a, **k):
            seen["auth"] = request.get_header("Authorization")
            seen["body"] = json.loads(request.data)
            return _Response(CHAT_OK)

        monkeypatch.setattr(adapters.urllib.request, "urlopen", capture)
        adapter.complete(system="s", user="u", model="m", max_tokens=100)
        assert seen["auth"] == "Bearer sk-test123456"

    def test_sends_system_as_a_message(self, adapter, monkeypatch):
        """The OpenAI shape. Contrast with the Anthropic adapter, where the
        system prompt is a top-level field."""
        seen = {}

        def capture(request, *a, **k):
            seen["body"] = json.loads(request.data)
            return _Response(CHAT_OK)

        monkeypatch.setattr(adapters.urllib.request, "urlopen", capture)
        adapter.complete(system="be brief", user="hello", model="m", max_tokens=100)
        assert seen["body"]["messages"] == [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hello"},
        ]

    def test_omits_the_auth_header_when_keyless(self, monkeypatch):
        """A local server given `Authorization: Bearer None` can 400."""
        seen = {}

        def capture(request, *a, **k):
            seen["auth"] = request.get_header("Authorization")
            return _Response(CHAT_OK)

        monkeypatch.setattr(adapters.urllib.request, "urlopen", capture)
        OpenAICompatAdapter(base_url="http://m:1", api_key=None).complete(
            system="s", user="u", model="m", max_tokens=10
        )
        assert seen["auth"] is None

    @pytest.mark.parametrize(
        "code,reason",
        [
            (401, FailureReason.INVALID_KEY),
            (403, FailureReason.INVALID_KEY),
            (429, FailureReason.RATE_LIMITED),
            (404, FailureReason.MODEL_NOT_FOUND),
            (500, FailureReason.UNREACHABLE),
        ],
    )
    def test_maps_http_status_to_a_reason(self, adapter, monkeypatch, code, reason):
        monkeypatch.setattr(adapters.urllib.request, "urlopen", _http_error(code))
        result = adapter.complete(system="s", user="u", model="m", max_tokens=10)
        assert isinstance(result, Failure)
        assert result.reason == reason

    def test_connection_refused_is_unreachable(self, adapter, monkeypatch):
        def refuse(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(adapters.urllib.request, "urlopen", refuse)
        result = adapter.complete(system="s", user="u", model="m", max_tokens=10)
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.UNREACHABLE

    def test_unparseable_200_is_bad_response(self, adapter, monkeypatch):
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response({"weird": 1})
        )
        result = adapter.complete(system="s", user="u", model="m", max_tokens=10)
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.BAD_RESPONSE

    def test_empty_text_is_bad_response(self, adapter, monkeypatch):
        payload = {"choices": [{"message": {"content": "   "}}]}
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        result = adapter.complete(system="s", user="u", model="m", max_tokens=10)
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.BAD_RESPONSE

    def test_empty_content_with_length_finish_names_the_budget(self, adapter, monkeypatch):
        """A reasoning model can burn its whole token budget on
        reasoning_content and return content: ''. finish_reason: length is the
        tell -- the message should name the exhausted budget so the user knows
        to raise llm_max_tokens or pick a non-reasoning model."""
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": "let me think about the reads...",
                    },
                    "finish_reason": "length",
                }
            ]
        }
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        result = adapter.complete(system="s", user="u", model="m", max_tokens=16)
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.BAD_RESPONSE
        assert result.detail is not None
        assert "budget" in result.detail.lower()
        assert "max_tokens=16" in result.detail

    def test_empty_content_with_reasoning_names_the_reasoning(self, adapter, monkeypatch):
        """reasoning_content present but content empty and finish_reason not
        'length' -- still no text, but the detail should say the model only
        reasoned, not that it returned garbage."""
        payload = {
            "choices": [
                {
                    "message": {
                        "content": " ",
                        "reasoning_content": "thinking...",
                    },
                    "finish_reason": "stop",
                }
            ]
        }
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        result = adapter.complete(system="s", user="u", model="m", max_tokens=400)
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.BAD_RESPONSE
        assert result.detail is not None
        assert "reasoning" in result.detail.lower()

    def test_the_key_is_scrubbed_from_the_error_detail(self, adapter, monkeypatch):
        """Providers echo the key back. The detail is stored, so this matters."""

        def raise_with_key(*a, **k):
            raise urllib.error.HTTPError(
                "http://x", 401, "err", {}, _BodyIO(b"bad key sk-test123456")
            )

        monkeypatch.setattr(adapters.urllib.request, "urlopen", raise_with_key)
        result = adapter.complete(system="s", user="u", model="m", max_tokens=10)
        assert "sk-test123456" not in (result.detail or "")


class _BodyIO:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data


class TestListModels:
    def test_returns_sorted_ids(self, adapter, monkeypatch):
        payload = {"data": [{"id": "zeta"}, {"id": "alpha"}]}
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        assert adapter.list_models() == ["alpha", "zeta"]

    def test_puts_loaded_models_first(self, adapter, monkeypatch):
        """LM Studio reports which model is resident. Asking for one it would
        have to load from disk turns a few-second call into a slow one, so a
        resident model is the better default -- opportunistic, never required."""
        payload = {"data": [{"id": "alpha"}, {"id": "zeta", "loaded": True}]}
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        assert adapter.list_models() == ["zeta", "alpha"]

    def test_maps_401_to_invalid_key(self, adapter, monkeypatch):
        monkeypatch.setattr(adapters.urllib.request, "urlopen", _http_error(401))
        result = adapter.list_models()
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.INVALID_KEY

    def test_empty_list_is_not_a_failure(self, adapter, monkeypatch):
        """A reachable server with no models loaded is configured correctly and
        merely empty -- the key is valid, which is what the fetch proves."""
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response({"data": []})
        )
        assert adapter.list_models() == []

    def test_non_dict_json_body_is_bad_response(self, adapter, monkeypatch):
        """A malformed server -- e.g. a nonstandard local server returning a
        bare JSON array from /v1/models -- must not raise AttributeError out
        of list_models() when the code calls result.get("data")."""
        monkeypatch.setattr(
            adapters.urllib.request,
            "urlopen",
            lambda *a, **k: _Response(["not", "a", "dict"]),
        )
        result = adapter.list_models()
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.BAD_RESPONSE
