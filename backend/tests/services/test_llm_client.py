"""The client's one real contract: a down server is not an error.

The model server is a separate process on the host that most of the time is
probably not running. Every one of these tests pins the same promise from a
different angle -- when it is absent or misbehaving, the client returns None and
the rest of the app carries on unchanged.
"""

import json
import urllib.error
from io import BytesIO

import pytest

from app.services import llm_client


class _Response:
    """Minimal stand-in for what urlopen yields as a context manager."""

    def __init__(self, payload, status=200):
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestAvailability:
    def test_a_healthy_server_is_available(self, monkeypatch):
        monkeypatch.setattr(
            llm_client.urllib.request,
            "urlopen",
            lambda *a, **k: _Response({"status": "ok"}),
        )
        assert llm_client.is_available() is True

    def test_a_refused_connection_reports_unavailable_rather_than_raising(self, monkeypatch):
        def refuse(*a, **k):
            raise OSError("Connection refused")

        monkeypatch.setattr(llm_client.urllib.request, "urlopen", refuse)
        assert llm_client.is_available() is False

    def test_a_server_that_answers_but_is_not_ok_is_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            llm_client.urllib.request,
            "urlopen",
            lambda *a, **k: _Response({"status": "loading"}),
        )
        assert llm_client.is_available() is False


class TestModelSelection:
    def test_configuration_wins_over_discovery(self, monkeypatch):
        monkeypatch.setattr(llm_client.settings, "llm_model", "pinned-model")
        assert llm_client.default_model() == "pinned-model"

    def test_discovery_prefers_a_model_already_resident(self, monkeypatch):
        """Asking for one the server must load from disk turns a few-second
        call into a slow one."""
        monkeypatch.setattr(llm_client.settings, "llm_model", "")
        monkeypatch.setattr(
            llm_client.urllib.request,
            "urlopen",
            lambda *a, **k: _Response(
                {"data": [{"id": "cold", "loaded": False}, {"id": "warm", "loaded": True}]}
            ),
        )
        assert llm_client.default_model() == "warm"

    def test_no_reachable_server_yields_no_model(self, monkeypatch):
        monkeypatch.setattr(llm_client.settings, "llm_model", "")

        def refuse(*a, **k):
            raise OSError("unreachable")

        monkeypatch.setattr(llm_client.urllib.request, "urlopen", refuse)
        assert llm_client.default_model() is None


class TestCompletion:
    def test_a_normal_completion_returns_text_and_the_model_that_wrote_it(self, monkeypatch):
        monkeypatch.setattr(llm_client.settings, "llm_model", "test-model")
        monkeypatch.setattr(
            llm_client.urllib.request,
            "urlopen",
            lambda *a, **k: _Response(
                {"choices": [{"message": {"content": "  The reads look fine.  "}}]}
            ),
        )
        result = llm_client.complete(system="s", user="u")
        assert result == ("The reads look fine.", "test-model")

    def test_an_http_error_yields_none(self, monkeypatch):
        monkeypatch.setattr(llm_client.settings, "llm_model", "test-model")

        def fail(*a, **k):
            raise urllib.error.HTTPError(
                "http://x", 400, "Bad Request", {}, BytesIO(b'{"error":"too long"}')
            )

        monkeypatch.setattr(llm_client.urllib.request, "urlopen", fail)
        assert llm_client.complete(system="s", user="u") is None

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"choices": []},
            {"choices": [{"message": {}}]},
            {"choices": [{"message": {"content": "   "}}]},
        ],
        ids=["empty", "no-choices", "no-content", "blank-content"],
    )
    def test_an_unusable_response_yields_none_rather_than_a_broken_summary(
        self, monkeypatch, payload
    ):
        monkeypatch.setattr(llm_client.settings, "llm_model", "test-model")
        monkeypatch.setattr(
            llm_client.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        assert llm_client.complete(system="s", user="u") is None
