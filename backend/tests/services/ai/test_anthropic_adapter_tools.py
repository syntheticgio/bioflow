"""Tool-calling in the Anthropic adapter: `tools` uses `input_schema` rather
than OpenAI's `function.parameters`, and a tool call arrives as a `tool_use`
content block rather than a `tool_calls` array.
"""

import json

import pytest

from app.models.ai import FailureReason
from app.services.ai import adapters
from app.services.ai.adapters import AnthropicAdapter, Completion, Failure, ToolCall, ToolSpec


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
    return AnthropicAdapter(base_url="https://api.anthropic.com", api_key="sk-ant-secret99")


@pytest.fixture
def capture(monkeypatch):
    seen = {}

    def _capture(request, *a, **k):
        seen["body"] = json.loads(request.data) if request.data else None
        return _Response({"content": [{"type": "text", "text": "hi"}]})

    monkeypatch.setattr(adapters.urllib.request, "urlopen", _capture)
    return seen


SEARCH_TOOL = ToolSpec(
    name="search_objects", description="Search files.", parameters={"type": "object"}
)


class TestSendingTools:
    def test_sends_tools_as_input_schema_not_parameters(self, adapter, capture):
        adapter.complete(
            system="s", user="u", model="claude-x", max_tokens=100, tools=[SEARCH_TOOL]
        )
        assert capture["body"]["tools"] == [
            {
                "name": "search_objects",
                "description": "Search files.",
                "input_schema": {"type": "object"},
            }
        ]

    def test_omits_the_field_entirely_when_no_tools_passed(self, adapter, capture):
        adapter.complete(system="s", user="u", model="claude-x", max_tokens=100)
        assert "tools" not in capture["body"]

    def test_omits_the_field_when_tools_is_an_empty_list(self, adapter, capture):
        adapter.complete(system="s", user="u", model="claude-x", max_tokens=100, tools=[])
        assert "tools" not in capture["body"]


class TestParsingToolUse:
    def test_tool_use_content_block_returns_a_toolcall(self, adapter, monkeypatch):
        payload = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "list_jobs",
                    "input": {"state": "running"},
                }
            ]
        }
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        result = adapter.complete(
            system="s", user="u", model="claude-x", max_tokens=100, tools=[SEARCH_TOOL]
        )
        assert isinstance(result, ToolCall)
        assert result.id == "toolu_1"
        assert result.name == "list_jobs"
        assert result.arguments == {"state": "running"}

    def test_input_is_already_a_dict_not_a_json_string(self, adapter, monkeypatch):
        """The one place the two wire formats genuinely diverge: OpenAI's
        `arguments` is a JSON-encoded string, Anthropic's `input` is already
        parsed. Passing a dict straight through (no json.loads) is correct."""
        payload = {"content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {"a": 1}}]}
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        result = adapter.complete(
            system="s", user="u", model="claude-x", max_tokens=100, tools=[SEARCH_TOOL]
        )
        assert result.arguments == {"a": 1}

    def test_multiple_tool_use_blocks_takes_the_first_and_logs_the_rest(
        self, adapter, monkeypatch, capsys
    ):
        payload = {
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "a", "input": {}},
                {"type": "tool_use", "id": "toolu_2", "name": "b", "input": {}},
            ]
        }
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        result = adapter.complete(
            system="s", user="u", model="claude-x", max_tokens=100, tools=[SEARCH_TOOL]
        )
        assert result.id == "toolu_1"
        assert "ai_multi_tool_call_dropped" in capsys.readouterr().out

    def test_text_block_only_still_returns_completion_when_tools_offered(
        self, adapter, monkeypatch
    ):
        payload = {"content": [{"type": "text", "text": "the answer is 4"}]}
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        result = adapter.complete(
            system="s", user="u", model="claude-x", max_tokens=100, tools=[SEARCH_TOOL]
        )
        assert isinstance(result, Completion)
        assert result.text == "the answer is 4"

    def test_text_block_alongside_a_tool_use_block_prefers_the_tool_use(self, adapter, monkeypatch):
        """Anthropic can return a text block (e.g. reasoning) plus a tool_use
        block in the same response; the tool call is what matters here."""
        payload = {
            "content": [
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "toolu_1", "name": "search_objects", "input": {}},
            ]
        }
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        result = adapter.complete(
            system="s", user="u", model="claude-x", max_tokens=100, tools=[SEARCH_TOOL]
        )
        assert isinstance(result, ToolCall)
        assert result.id == "toolu_1"

    def test_empty_content_list_is_bad_response(self, adapter, monkeypatch):
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response({"content": []})
        )
        result = adapter.complete(
            system="s", user="u", model="claude-x", max_tokens=100, tools=[SEARCH_TOOL]
        )
        assert isinstance(result, Failure)
        assert result.reason is FailureReason.BAD_RESPONSE
