"""Tool-calling in the OpenAI-compatible adapter: sending `tools`, and
parsing a `tool_calls` response instead of plain text.
"""

import json

import pytest

from app.models.ai import FailureReason
from app.services.ai import adapters
from app.services.ai.adapters import Completion, Failure, OpenAICompatAdapter, ToolCall, ToolSpec


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def adapter():
    return OpenAICompatAdapter(base_url="http://model:1234", api_key=None)


@pytest.fixture
def capture(monkeypatch):
    seen = {}

    def _capture(request, *a, **k):
        seen["body"] = json.loads(request.data)
        return _Response(seen.get("_respond_with", {"choices": [{"message": {"content": "hi"}}]}))

    monkeypatch.setattr(adapters.urllib.request, "urlopen", _capture)
    return seen


SEARCH_TOOL = ToolSpec(
    name="search_objects", description="Search files.", parameters={"type": "object"}
)


class TestSendingTools:
    def test_sends_tools_array_in_body(self, adapter, capture):
        adapter.complete(system="s", user="u", model="m", max_tokens=100, tools=[SEARCH_TOOL])
        assert capture["body"]["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "search_objects",
                    "description": "Search files.",
                    "parameters": {"type": "object"},
                },
            }
        ]

    def test_omits_the_field_entirely_when_no_tools_passed(self, adapter, capture):
        """Some servers reject an empty tools array, so the field must be
        absent -- not present-and-empty -- on every existing call site."""
        adapter.complete(system="s", user="u", model="m", max_tokens=100)
        assert "tools" not in capture["body"]

    def test_omits_the_field_when_tools_is_an_empty_list(self, adapter, capture):
        adapter.complete(system="s", user="u", model="m", max_tokens=100, tools=[])
        assert "tools" not in capture["body"]


class TestParsingToolCalls:
    def test_tool_calls_response_returns_a_toolcall(self, adapter, monkeypatch):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "search_objects",
                                    "arguments": '{"kinds": ["fastq"]}',
                                },
                            }
                        ],
                    }
                }
            ]
        }
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        result = adapter.complete(
            system="s", user="u", model="m", max_tokens=100, tools=[SEARCH_TOOL]
        )
        assert isinstance(result, ToolCall)
        assert result.id == "call_1"
        assert result.name == "search_objects"
        assert result.arguments == {"kinds": ["fastq"]}

    def test_multiple_tool_calls_takes_the_first_and_logs_the_rest(
        self, adapter, monkeypatch, capsys
    ):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "search_objects", "arguments": "{}"},
                            },
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {"name": "list_jobs", "arguments": "{}"},
                            },
                        ],
                    }
                }
            ]
        }
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        result = adapter.complete(
            system="s", user="u", model="m", max_tokens=100, tools=[SEARCH_TOOL]
        )
        assert result.id == "call_1"
        # structlog prints directly to stdout in this codebase's config;
        # caplog cannot see it (see test_subprocess.py's note on this).
        assert "ai_multi_tool_call_dropped" in capsys.readouterr().out

    def test_malformed_arguments_json_is_a_bad_response(self, adapter, monkeypatch):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "x", "arguments": "not json"},
                            }
                        ],
                    }
                }
            ]
        }
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        result = adapter.complete(
            system="s", user="u", model="m", max_tokens=100, tools=[SEARCH_TOOL]
        )
        assert isinstance(result, Failure)
        assert result.reason is FailureReason.BAD_RESPONSE

    def test_plain_text_response_with_tools_offered_still_returns_completion(
        self, adapter, monkeypatch
    ):
        payload = {"choices": [{"message": {"content": "there are 3 files"}}]}
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        result = adapter.complete(
            system="s", user="u", model="m", max_tokens=100, tools=[SEARCH_TOOL]
        )
        assert isinstance(result, Completion)
        assert result.text == "there are 3 files"

    def test_empty_tool_calls_list_falls_through_to_text(self, adapter, monkeypatch):
        """A response can carry `tool_calls: []` rather than omitting the key."""
        payload = {"choices": [{"message": {"content": "an answer", "tool_calls": []}}]}
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        result = adapter.complete(
            system="s", user="u", model="m", max_tokens=100, tools=[SEARCH_TOOL]
        )
        assert isinstance(result, Completion)
        assert result.text == "an answer"
