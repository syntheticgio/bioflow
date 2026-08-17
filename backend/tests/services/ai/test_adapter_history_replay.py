"""`history`: replaying a multi-turn conversation, including a tool call and
its result, into each adapter's own wire shape. This is what lets the
project Q&A tool loop feed a tool result back to the model as a follow-up
turn rather than only ever sending one `user` string.
"""

import json

import pytest

from app.services.ai import adapters
from app.services.ai.adapters import (
    AnthropicAdapter,
    ConversationTurn,
    OpenAICompatAdapter,
    ToolCall,
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


@pytest.fixture
def openai_adapter():
    return OpenAICompatAdapter(base_url="http://model:1234", api_key=None)


@pytest.fixture
def anthropic_adapter():
    return AnthropicAdapter(base_url="https://api.anthropic.com", api_key="sk-ant-x")


@pytest.fixture
def capture(monkeypatch):
    seen = {}

    def _capture(request, *a, **k):
        seen["body"] = json.loads(request.data) if request.data else None
        return _Response(
            {
                "choices": [{"message": {"content": "hi"}}],
                "content": [{"type": "text", "text": "hi"}],
            }
        )

    monkeypatch.setattr(adapters.urllib.request, "urlopen", _capture)
    return seen


HISTORY = [
    ConversationTurn(role="user", content="how many bams?"),
    ConversationTurn(
        role="tool_call",
        tool_call=ToolCall(id="call_1", name="search_objects", arguments={"kinds": ["bam"]}),
    ),
    ConversationTurn(role="tool_result", tool_call_id="call_1", content='{"total": 3}'),
]


class TestOpenAiHistoryReplay:
    def test_renders_tool_call_and_result_as_separate_messages(self, openai_adapter, capture):
        openai_adapter.complete(system="s", user="", model="m", max_tokens=100, history=HISTORY)

        messages = capture["body"]["messages"]
        assert messages[0] == {"role": "system", "content": "s"}
        assert messages[1] == {"role": "user", "content": "how many bams?"}
        assert messages[2]["role"] == "assistant"
        assert messages[2]["tool_calls"] == [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "search_objects",
                    "arguments": json.dumps({"kinds": ["bam"]}),
                },
            }
        ]
        assert messages[3] == {"role": "tool", "tool_call_id": "call_1", "content": '{"total": 3}'}

    def test_a_plain_user_assistant_history_with_no_tool_turns(self, openai_adapter, capture):
        history = [
            ConversationTurn(role="user", content="q1"),
            ConversationTurn(role="assistant", content="a1"),
            ConversationTurn(role="user", content="q2"),
        ]
        openai_adapter.complete(system="s", user="", model="m", max_tokens=100, history=history)
        messages = capture["body"]["messages"]
        assert messages == [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]


class TestAnthropicHistoryReplay:
    def test_pairs_tool_use_and_tool_result_as_content_blocks(self, anthropic_adapter, capture):
        anthropic_adapter.complete(
            system="s", user="", model="claude-x", max_tokens=100, history=HISTORY
        )

        sent = capture["body"]
        assert sent["system"] == "s"
        messages = sent["messages"]
        assert messages[0] == {"role": "user", "content": "how many bams?"}
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == [
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "search_objects",
                "input": {"kinds": ["bam"]},
            }
        ]
        assert messages[2]["role"] == "user"
        assert messages[2]["content"] == [
            {"type": "tool_result", "tool_use_id": "call_1", "content": '{"total": 3}'}
        ]

    def test_a_plain_user_assistant_history_with_no_tool_turns(self, anthropic_adapter, capture):
        history = [
            ConversationTurn(role="user", content="q1"),
            ConversationTurn(role="assistant", content="a1"),
        ]
        anthropic_adapter.complete(
            system="s", user="", model="claude-x", max_tokens=100, history=history
        )
        assert capture["body"]["messages"] == [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]


class TestNoHistoryIsUnaffected:
    def test_openai_no_history_behaves_exactly_as_before(self, openai_adapter, capture):
        openai_adapter.complete(system="s", user="hello", model="m", max_tokens=100)
        assert capture["body"]["messages"] == [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "hello"},
        ]

    def test_anthropic_no_history_behaves_exactly_as_before(self, anthropic_adapter, capture):
        anthropic_adapter.complete(system="s", user="hello", model="claude-x", max_tokens=100)
        assert capture["body"]["system"] == "s"
        assert capture["body"]["messages"] == [{"role": "user", "content": "hello"}]

    def test_openai_empty_history_list_behaves_like_no_history(self, openai_adapter, capture):
        openai_adapter.complete(system="s", user="hello", model="m", max_tokens=100, history=[])
        assert capture["body"]["messages"] == [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "hello"},
        ]
