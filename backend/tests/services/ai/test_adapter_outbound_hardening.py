"""Bounds on what an attacker-controlled base_url can do to the api process (#872).

A provider's base_url is user-settable by design -- pointing it at your own
local model server is the feature. That makes the *response* untrusted input,
and these are the two ways it could reach past the adapter: following a
redirect out of the validated scheme, and reading an unbounded body into
memory.
"""

import json
import urllib.error
import urllib.request

import pytest

from app.models.ai import FailureReason
from app.services.ai import adapters
from app.services.ai.adapters import Failure, OpenAICompatAdapter


@pytest.fixture
def adapter():
    return OpenAICompatAdapter(base_url="http://model:1234", api_key="sk-secret-123")


class _Response:
    """A stub that honours read(amount), the way a real response does."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self, amount=None):
        return self._body if amount is None else self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestRedirectsAreNotFollowed:
    def test_the_opener_carries_the_no_redirect_handler(self):
        """The property, asserted on the opener itself.

        urlopen() uses the *global* opener and takes no handler argument, so a
        future edit that "simplifies" _urlopen back to urlopen would silently
        restore redirect-following with nothing else failing. This is what
        notices.
        """
        assert any(
            isinstance(h, adapters._NoRedirects) for h in adapters._opener.handlers
        )

    def test_a_redirect_becomes_a_failure_not_a_second_request(self, adapter, monkeypatch):
        """A validated https host answering 302 with file:// or an internal
        address must not be followed -- the key rides along in the header."""
        calls: list[str] = []

        def _raise_redirect(request, timeout):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(
                request.full_url, 302, "Found",
                {"Location": "file:///etc/passwd"}, None,
            )

        monkeypatch.setattr(adapters, "_urlopen", _raise_redirect)

        result = adapter.list_models()

        assert isinstance(result, Failure)
        assert result.reason is FailureReason.UNREACHABLE
        # One request, to the configured host. The redirect target was never
        # fetched.
        assert calls == ["http://model:1234/v1/models"]

    def test_the_key_is_not_in_the_reported_detail(self, adapter, monkeypatch):
        """A hostile upstream controls the error body, which is reflected back
        through the settings endpoints -- so it must not echo the key."""

        def _raise(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 500, "Server Error", {},
                _Body(b"your key sk-secret-123 is bad"),
            )

        monkeypatch.setattr(adapters, "_urlopen", _raise)
        result = adapter.list_models()
        assert isinstance(result, Failure)
        assert "sk-secret-123" not in (result.detail or "")


class _Body:
    """HTTPError treats its `fp` as a real file and closes it on cleanup, so
    this needs close() as well as read()."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self, amount=None):
        return self._data

    def close(self):
        pass


class TestResponseSizeIsBounded:
    def test_an_oversized_body_is_a_bad_response(self, adapter, monkeypatch):
        """Without the cap, response.read() buffers whatever the upstream sends
        into the api process."""
        huge = b"x" * (adapters._MAX_RESPONSE_BYTES + 10)
        monkeypatch.setattr(adapters, "_urlopen", lambda r, timeout: _Response(huge))

        result = adapter.list_models()

        assert isinstance(result, Failure)
        assert result.reason is FailureReason.BAD_RESPONSE

    def test_a_body_exactly_at_the_cap_is_still_parsed(self, adapter, monkeypatch):
        """The cap must reject *over* the limit, not at it -- an off-by-one here
        would fail a legitimate large model list rather than a hostile one."""
        payload = {"data": [{"id": "alpha"}]}
        body = json.dumps(payload).encode()
        padding = b" " * (adapters._MAX_RESPONSE_BYTES - len(body))
        monkeypatch.setattr(
            adapters, "_urlopen", lambda r, timeout: _Response(body + padding)
        )

        assert adapter.list_models() == ["alpha"]

    def test_an_ordinary_response_is_unaffected(self, adapter, monkeypatch):
        payload = {"data": [{"id": "zeta"}, {"id": "alpha"}]}
        monkeypatch.setattr(
            adapters,
            "_urlopen",
            lambda r, timeout: _Response(json.dumps(payload).encode()),
        )
        assert adapter.list_models() == ["alpha", "zeta"]
