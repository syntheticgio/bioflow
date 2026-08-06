# Project Q&A chat panel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A project-scoped chat drawer where a natural-language question is
answered by a model that can call `search_objects` and `list_jobs` — never by
generating an answer from training data alone. Implements
[#36](https://github.com/syntheticgio/bioflow/issues/36).

**Architecture:** Tool-calling support added to both AI adapters (new wire
surface, zero existing callers affected since `tools` is opt-in). A bounded
loop (`app/services/ai/qa.py`) drives up to 3 tool calls against
`search_service.search_objects` and a new `jobs` query helper, both scoped to
one project and owner with no model-suppliable override. A new
`answer_project_question` THREAD-mode job (mirrors `summarize_object`) reads
and appends to a per-`(owner, project_id)` `ProjectConversation` document,
with threshold-triggered compaction. Frontend: a footer entry point opening a
minimizable slide-up drawer, polling/SSE-refetching the conversation the same
way every other async-job UI in this app already works.

**Tech Stack:** FastAPI, Beanie/Motor, Python 3.12, pytest + pytest-asyncio
(backend); React, TanStack Query, TypeScript (frontend). No new dependencies.

**Reference:** `docs/superpowers/specs/2026-08-05-project-qa-chat-design.md`
— read it before starting. This plan implements it and does not repeat its
rationale except where a step needs it to make a call correctly.

**Out of scope, deliberately:** streaming responses, any tool beyond the two
named, cross-project questions, a shared drawer component with #35 (left as a
named seam — see Task 12).

---

## Before you start

### Running tests from a worktree

`docker compose exec api python -m pytest` **silently tests main's code** from
a worktree. Use:

```bash
./backend/run-worktree-tests.sh tests/ -q            # whole suite
./backend/run-worktree-tests.sh tests/services -v    # one directory
```

Record the baseline count before touching anything. If the baseline is red,
stop and report rather than starting against it.

### Manual verification needs a real provider

Everything here degrades to `{"skipped": "no_provider"}` with no configured
`TaskSlot.PROJECT_QA` route — tests do not need one, but confirming the tool
loop actually works end to end needs a real local or hosted model that
supports function-calling. Confirm which provider is available in this
environment before Task 11 (manual verification); if none is reachable, say
so explicitly rather than claiming end-to-end verification that did not
happen.

### After merge

`worker` does not hot-reload. `docker compose up -d --build api web worker`
from the main repo root after merging, before manually testing a chat
question — the new handler must be loaded into the running worker process.

---

## Task 1: `ToolCall` result type and `ToolSpec` request type

**Files:**
- Modify: `backend/app/services/ai/adapters.py`
- Test: `backend/tests/services/ai/test_adapters_tool_types.py`

Establishes the shared vocabulary both adapters will target, with no wire
logic yet — a seam to build the rest of the plan against.

- [ ] **Step 1: Write the failing test**

```python
from app.services.ai.adapters import Completion, Failure, ToolCall, ToolSpec


def test_tool_call_is_frozen_and_carries_parsed_arguments():
    call = ToolCall(id="call_1", name="search_objects", arguments={"kinds": ["fastq"]})
    assert call.id == "call_1"
    assert call.arguments == {"kinds": ["fastq"]}
    with pytest.raises(FrozenInstanceError):
        call.name = "other"  # type: ignore[misc]


def test_tool_spec_carries_a_json_schema_dict():
    spec = ToolSpec(
        name="search_objects",
        description="Search files in this project.",
        parameters={"type": "object", "properties": {"kinds": {"type": "array"}}},
    )
    assert spec.parameters["type"] == "object"
```

- [ ] **Step 2: Add the types**

In `adapters.py`, alongside the existing `Completion`/`Failure`:

```python
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON schema, adapter-neutral
```

- [ ] **Step 3: Run tests, verify pass.** No behavior changed yet —
  `Completion`/`Failure`-only call sites are untouched.

- [ ] **Step 4: Commit** — `feat(ai): add ToolCall/ToolSpec types for tool-calling`

---

## Task 2: OpenAI-compat adapter — send `tools`, parse `tool_calls`

**Files:**
- Modify: `backend/app/services/ai/adapters.py`
- Test: `backend/tests/services/ai/test_openai_adapter_tools.py`

- [ ] **Step 1: Write the failing tests**

Use the existing test file's pattern for mocking `urllib.request.urlopen`
(check `backend/tests/services/ai/test_adapters.py` for the exact mock shape
already in use — reuse it rather than inventing a second one).

```python
def test_complete_with_tools_sends_tools_array_in_body(mock_urlopen_capturing_request):
    adapter = OpenAICompatAdapter(base_url="http://x", api_key=None)
    tools = [ToolSpec(name="search_objects", description="...", parameters={"type": "object"})]
    mock_urlopen_capturing_request.respond_with({"choices": [{"message": {"content": "hi"}}]})

    adapter.complete(system="s", user="u", model="m", max_tokens=100, tools=tools)

    sent_body = mock_urlopen_capturing_request.last_body()
    assert sent_body["tools"] == [
        {"type": "function", "function": {"name": "search_objects", "description": "...", "parameters": {"type": "object"}}}
    ]


def test_complete_with_no_tools_omits_the_field_entirely(mock_urlopen_capturing_request):
    """Every existing caller passes no tools; the field must not appear at all,
    not appear as an empty list -- some servers reject an empty tools array."""
    adapter = OpenAICompatAdapter(base_url="http://x", api_key=None)
    mock_urlopen_capturing_request.respond_with({"choices": [{"message": {"content": "hi"}}]})

    adapter.complete(system="s", user="u", model="m", max_tokens=100)

    assert "tools" not in mock_urlopen_capturing_request.last_body()


def test_tool_calls_response_returns_a_toolcall_not_a_completion(mock_urlopen_capturing_request):
    adapter = OpenAICompatAdapter(base_url="http://x", api_key=None)
    mock_urlopen_capturing_request.respond_with({
        "choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "search_objects", "arguments": '{"kinds": ["fastq"]}'}}]
        }}]
    })

    result = adapter.complete(system="s", user="u", model="m", max_tokens=100,
                               tools=[ToolSpec(name="search_objects", description="d", parameters={})])

    assert isinstance(result, ToolCall)
    assert result.id == "call_1"
    assert result.name == "search_objects"
    assert result.arguments == {"kinds": ["fastq"]}


def test_multiple_tool_calls_in_one_response_takes_the_first_and_logs_the_rest(mock_urlopen_capturing_request, caplog):
    adapter = OpenAICompatAdapter(base_url="http://x", api_key=None)
    mock_urlopen_capturing_request.respond_with({
        "choices": [{"message": {"content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "search_objects", "arguments": "{}"}},
            {"id": "call_2", "type": "function", "function": {"name": "list_jobs", "arguments": "{}"}},
        ]}}]
    })

    result = adapter.complete(system="s", user="u", model="m", max_tokens=100,
                               tools=[ToolSpec(name="x", description="d", parameters={})])

    assert result.id == "call_1"
    assert "ai_multi_tool_call_dropped" in caplog.text


def test_malformed_tool_call_arguments_is_a_bad_response(mock_urlopen_capturing_request):
    """Arguments is a JSON string per the wire format; invalid JSON must not raise."""
    adapter = OpenAICompatAdapter(base_url="http://x", api_key=None)
    mock_urlopen_capturing_request.respond_with({
        "choices": [{"message": {"content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "not json"}}
        ]}}]
    })

    result = adapter.complete(system="s", user="u", model="m", max_tokens=100,
                               tools=[ToolSpec(name="x", description="d", parameters={})])

    assert isinstance(result, Failure)
    assert result.reason is FailureReason.BAD_RESPONSE
```

- [ ] **Step 2: Implement**

`OpenAICompatAdapter.complete` gains `tools: list[ToolSpec] | None = None`.
When present and non-empty, add to `body`:

```python
if tools:
    body["tools"] = [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
        for t in tools
    ]
```

Response parsing: before the existing `content` extraction, check
`result["choices"][0]["message"].get("tool_calls")`. If present and
non-empty:
- Take `tool_calls[0]`; if `len(tool_calls) > 1`, `log.info("ai_multi_tool_call_dropped", dropped=len(tool_calls) - 1)`.
- `json.loads(tool_calls[0]["function"]["arguments"])` inside a `try/except (json.JSONDecodeError, KeyError, TypeError)` → `Failure(FailureReason.BAD_RESPONSE)` on failure.
- Return `ToolCall(id=tool_calls[0]["id"], name=tool_calls[0]["function"]["name"], arguments=parsed)`.

Otherwise fall through to the existing `content` extraction unchanged.

- [ ] **Step 3: Run tests, verify pass.** Also re-run the full existing
  `test_adapters.py` file to confirm no regression to the no-`tools` path.

- [ ] **Step 4: Commit** — `feat(ai): tool-calling in the OpenAI-compat adapter`

---

## Task 3: Anthropic adapter — send `tools`, parse `tool_use`

**Files:**
- Modify: `backend/app/services/ai/adapters.py`
- Test: `backend/tests/services/ai/test_anthropic_adapter_tools.py`

- [ ] **Step 1: Write the failing tests**

Same shapes as Task 2, adjusted for Anthropic's wire format:

```python
def test_complete_with_tools_sends_input_schema_not_parameters(mock_urlopen_capturing_request):
    adapter = AnthropicAdapter(base_url="http://x", api_key=None)
    tools = [ToolSpec(name="search_objects", description="d", parameters={"type": "object"})]
    mock_urlopen_capturing_request.respond_with({"content": [{"type": "text", "text": "hi"}]})

    adapter.complete(system="s", user="u", model="m", max_tokens=100, tools=tools)

    sent = mock_urlopen_capturing_request.last_body()
    assert sent["tools"] == [{"name": "search_objects", "description": "d", "input_schema": {"type": "object"}}]


def test_tool_use_content_block_returns_a_toolcall(mock_urlopen_capturing_request):
    adapter = AnthropicAdapter(base_url="http://x", api_key=None)
    mock_urlopen_capturing_request.respond_with({
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "list_jobs", "input": {"state": "running"}}]
    })

    result = adapter.complete(system="s", user="u", model="m", max_tokens=100,
                               tools=[ToolSpec(name="list_jobs", description="d", parameters={})])

    assert isinstance(result, ToolCall)
    assert result.id == "toolu_1"
    assert result.arguments == {"state": "running"}


def test_multiple_tool_use_blocks_takes_the_first_and_logs_the_rest(mock_urlopen_capturing_request, caplog):
    adapter = AnthropicAdapter(base_url="http://x", api_key=None)
    mock_urlopen_capturing_request.respond_with({"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "a", "input": {}},
        {"type": "tool_use", "id": "toolu_2", "name": "b", "input": {}},
    ]})

    result = adapter.complete(system="s", user="u", model="m", max_tokens=100,
                               tools=[ToolSpec(name="x", description="d", parameters={})])

    assert result.id == "toolu_1"
    assert "ai_multi_tool_call_dropped" in caplog.text


def test_text_block_alongside_no_tool_use_still_returns_completion(mock_urlopen_capturing_request):
    """A response with only text content blocks, tools offered but not called."""
    adapter = AnthropicAdapter(base_url="http://x", api_key=None)
    mock_urlopen_capturing_request.respond_with({"content": [{"type": "text", "text": "the answer is 4"}]})

    result = adapter.complete(system="s", user="u", model="m", max_tokens=100,
                               tools=[ToolSpec(name="x", description="d", parameters={})])

    assert isinstance(result, Completion)
    assert result.text == "the answer is 4"
```

- [ ] **Step 2: Implement**

`AnthropicAdapter.complete` gains the same `tools` parameter. Body gains,
when present:

```python
if tools:
    body["tools"] = [
        {"name": t.name, "description": t.description, "input_schema": t.parameters}
        for t in tools
    ]
```

Response parsing: scan `result["content"]` for blocks where
`block["type"] == "tool_use"`. If any found, take the first (log dropped
count for the rest, identical message key to Task 2 so both adapters are
greppable together), return `ToolCall(id=block["id"], name=block["name"],
arguments=block["input"])` — `input` is already a parsed dict per Anthropic's
wire format, no `json.loads` needed (unlike OpenAI's string-encoded
`arguments`, confirmed during design research). If no `tool_use` block is
found, fall through to the existing text-block extraction unchanged.

- [ ] **Step 3: Run tests, verify pass**, plus the existing
  `test_adapters.py` Anthropic tests for regression.

- [ ] **Step 4: Commit** — `feat(ai): tool-calling in the Anthropic adapter`

---

## Task 4: Conversation replay — `history` parameter on both adapters

**Files:**
- Modify: `backend/app/services/ai/adapters.py`
- Test: `backend/tests/services/ai/test_adapter_history_replay.py`

This is what lets the tool loop feed a tool result back to the model as a
follow-up turn, rather than only ever sending one `user` string.

- [ ] **Step 1: Design the neutral turn representation**

Add to `adapters.py`:

```python
@dataclass(frozen=True)
class ConversationTurn:
    role: Literal["user", "assistant", "tool_call", "tool_result"]
    content: str = ""
    # Only set on role == "tool_call"
    tool_call: ToolCall | None = None
    # Only set on role == "tool_result"
    tool_call_id: str | None = None
```

Four roles, not two, because a tool exchange is not representable as plain
user/assistant text in either wire format — `tool_call` records what the
model asked for (needed to echo back the assistant's own `tool_calls`/
`tool_use` block, which both APIs require present before a matching result),
`tool_result` carries the JSON string result keyed to the call it answers.

- [ ] **Step 2: Write the failing tests**

```python
def test_openai_history_renders_tool_call_and_result_as_separate_messages(mock_urlopen_capturing_request):
    adapter = OpenAICompatAdapter(base_url="http://x", api_key=None)
    history = [
        ConversationTurn(role="user", content="how many bams?"),
        ConversationTurn(role="tool_call", tool_call=ToolCall(id="call_1", name="search_objects", arguments={"kinds": ["bam"]})),
        ConversationTurn(role="tool_result", tool_call_id="call_1", content='{"total": 3}'),
    ]
    mock_urlopen_capturing_request.respond_with({"choices": [{"message": {"content": "3 bams"}}]})

    adapter.complete(system="s", user="", model="m", max_tokens=100, history=history)

    messages = mock_urlopen_capturing_request.last_body()["messages"]
    assert messages[0] == {"role": "system", "content": "s"}
    assert messages[1] == {"role": "user", "content": "how many bams?"}
    assert messages[2]["role"] == "assistant"
    assert messages[2]["tool_calls"][0]["id"] == "call_1"
    assert messages[3] == {"role": "tool", "tool_call_id": "call_1", "content": '{"total": 3}'}


def test_anthropic_history_pairs_tool_use_and_tool_result_as_content_blocks(mock_urlopen_capturing_request):
    adapter = AnthropicAdapter(base_url="http://x", api_key=None)
    history = [
        ConversationTurn(role="user", content="how many bams?"),
        ConversationTurn(role="tool_call", tool_call=ToolCall(id="toolu_1", name="search_objects", arguments={"kinds": ["bam"]})),
        ConversationTurn(role="tool_result", tool_call_id="toolu_1", content='{"total": 3}'),
    ]
    mock_urlopen_capturing_request.respond_with({"content": [{"type": "text", "text": "3 bams"}]})

    adapter.complete(system="s", user="", model="m", max_tokens=100, history=history)

    sent = mock_urlopen_capturing_request.last_body()
    assert sent["system"] == "s"
    messages = sent["messages"]
    assert messages[0] == {"role": "user", "content": "how many bams?"}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"][0] == {"type": "tool_use", "id": "toolu_1", "name": "search_objects", "input": {"kinds": ["bam"]}}
    assert messages[2]["role"] == "user"
    assert messages[2]["content"][0] == {"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"total": 3}'}


def test_no_history_behaves_exactly_as_before(mock_urlopen_capturing_request):
    """Every existing call site passes no history -- must be byte-identical to today's body."""
    adapter = OpenAICompatAdapter(base_url="http://x", api_key=None)
    mock_urlopen_capturing_request.respond_with({"choices": [{"message": {"content": "hi"}}]})

    adapter.complete(system="s", user="hello", model="m", max_tokens=100)

    assert mock_urlopen_capturing_request.last_body()["messages"] == [
        {"role": "system", "content": "s"}, {"role": "user", "content": "hello"}
    ]
```

- [ ] **Step 3: Implement**

Both `complete()` methods gain `history: list[ConversationTurn] | None =
None`. When `history` is provided, it replaces the two-message body
construction: build the message/content list by iterating `history`,
rendering each role into that adapter's shape. `user` param becomes unused
when `history` is passed (assert this in a docstring note, not a runtime
check — keeping both parameters lets `_run()`'s call site stay uniform
whether or not history is present, per Task 6).

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit** — `feat(ai): conversation replay via history param on both adapters`

---

## Task 5: Thread `tools`/`history` through `complete()`/`complete_sync()`

**Files:**
- Modify: `backend/app/services/ai/complete.py`, `backend/app/services/ai/__init__.py`
- Test: `backend/tests/services/ai/test_complete_tools.py`

- [ ] **Step 1: Write the failing tests**

```python
async def test_complete_passes_tools_through_to_the_adapter(monkeypatch):
    captured = {}
    def fake_run(provider, **kwargs):
        captured.update(kwargs)
        return Completion("ok", "m")
    monkeypatch.setattr("app.services.ai.complete._run", fake_run)

    provider = make_resolved_provider(model="m")
    tools = [ToolSpec(name="x", description="d", parameters={})]
    await complete(provider, system="s", user="u", tools=tools)

    assert captured["tools"] == tools


async def test_complete_returns_toolcall_without_recording_failure(monkeypatch):
    """A ToolCall is not a Failure -- must not trip provider_service.record_failure."""
    monkeypatch.setattr("app.services.ai.complete._run", lambda p, **k: ToolCall("id", "x", {}))
    record_failure = AsyncMock()
    monkeypatch.setattr("app.services.ai.provider_service.record_failure", record_failure)
    record_success = AsyncMock()
    monkeypatch.setattr("app.services.ai.provider_service.record_success", record_success)

    result = await complete(make_resolved_provider(model="m"), system="s", user="u")

    assert isinstance(result, ToolCall)
    record_failure.assert_not_called()
    record_success.assert_called_once()  # a tool call is a successful round-trip


def test_complete_sync_passes_history_through(monkeypatch):
    captured = {}
    monkeypatch.setattr("app.services.ai.complete._run", lambda p, **k: captured.update(k) or Completion("ok", "m"))
    history = [ConversationTurn(role="user", content="hi")]

    complete_sync(make_resolved_provider(model="m"), system="s", user="", history=history)

    assert captured["history"] == history
```

- [ ] **Step 2: Implement**

Both functions in `complete.py` gain `tools: list[ToolSpec] | None = None`
and `history: list[ConversationTurn] | None = None`, forwarded into the
`_run(provider, **kwargs)` call. Return type annotations widen to
`Completion | ToolCall | Failure`. In `complete()`, the `isinstance(result,
Failure)` branch is unchanged (a `ToolCall` doesn't match it and falls
through) — add `await provider_service.record_success(...)` to fire for a
`ToolCall` result too, since the adapter round-trip succeeded even though no
final text came back yet.

- [ ] **Step 3: Update `app/services/ai/__init__.py`** re-exports to include
  `ToolCall`, `ToolSpec`, `ConversationTurn`.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit** — `feat(ai): thread tools/history through complete() and complete_sync()`

---

## Task 6: `TaskSlot.PROJECT_QA`

**Files:**
- Modify: `backend/app/models/ai.py`
- Test: `backend/tests/models/test_ai_task_slot.py` (extend if it exists,
  create otherwise)

- [ ] **Step 1: Write the failing test**

```python
def test_project_qa_slot_has_a_label():
    assert TaskSlot.PROJECT_QA.label == "Project Q&A chat"


def test_every_task_slot_has_a_label():
    """The registry-audit pattern: an enum member skipped by the label dict
    would render as a blank row on the settings page, not an error."""
    for slot in TaskSlot:
        assert slot.label
```

- [ ] **Step 2: Implement**

```python
class TaskSlot(StrEnum):
    FILE_SUMMARY = "file_summary"
    ORGANISM_BLURB = "organism_blurb"
    PROJECT_QA = "project_qa"
```

`_SLOT_LABELS[TaskSlot.PROJECT_QA] = "Project Q&A chat"`.

- [ ] **Step 3: Run tests, verify pass.** Also check the frontend settings
  page (`TaskRoutingPanel.tsx`) renders a third row with no frontend code
  change needed — confirm this by reading the component rather than assuming
  (it should already enumerate whatever the API returns).

- [ ] **Step 4: Commit** — `feat(ai): add TaskSlot.PROJECT_QA`

---

## Task 7: `ProjectConversation` model

**Files:**
- Create: `backend/app/models/conversation.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/models/test_conversation.py`

- [ ] **Step 1: Write the failing tests**

```python
async def test_conversation_defaults_to_empty():
    convo = ProjectConversation(owner="local", project_id=PydanticObjectId())
    await convo.insert()
    assert convo.turns == []
    assert convo.compacted_summary is None
    assert convo.compacted_through == 0


async def test_owner_and_project_id_are_jointly_unique():
    from pymongo.errors import DuplicateKeyError
    project_id = PydanticObjectId()
    await ProjectConversation(owner="local", project_id=project_id).insert()
    with pytest.raises(DuplicateKeyError):
        await ProjectConversation(owner="local", project_id=project_id).insert()


async def test_two_owners_can_each_have_a_conversation_for_the_same_project():
    """Not a global uniqueness -- per-owner, per the spec's partitioning rule."""
    project_id = PydanticObjectId()
    await ProjectConversation(owner="local", project_id=project_id).insert()
    await ProjectConversation(owner="other", project_id=project_id).insert()  # must not raise


def test_conversation_turn_requires_role_and_content():
    turn = ConversationTurn(role="user", content="hi", created_at=utcnow())
    assert turn.role == "user"
```

- [ ] **Step 2: Implement**

```python
class ConversationTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class ProjectConversation(TimestampedDocument):
    project_id: PydanticObjectId
    turns: list[ConversationTurn] = Field(default_factory=list)
    compacted_summary: str | None = None
    compacted_through: int = 0

    class Settings:
        name = "project_conversations"
        indexes = [
            IndexModel([("owner", ASCENDING), ("project_id", ASCENDING)], unique=True),
        ]
```

Register in `backend/app/models/__init__.py`'s `__all__` and wherever the
Beanie model list for `init_beanie` lives (grep `DataObject` in
`app/db/client.py` to find the registration list and add
`ProjectConversation` alongside it).

- [ ] **Step 3: Run tests, verify pass.**

- [ ] **Step 4: Commit** — `feat(models): add ProjectConversation`

---

## Task 8: `search_objects` and `list_jobs` tool definitions + execution

**Files:**
- Create: `backend/app/services/ai/qa_tools.py`
- Test: `backend/tests/services/ai/test_qa_tools.py`

The scoping-cannot-be-overridden guarantee lives entirely in this file: the
JSON schemas exposed to the model simply have no `project_id`/`owner`
property, and the execution functions take them as explicit keyword
arguments injected by the caller, never read from the parsed tool arguments
dict.

- [ ] **Step 1: Write the failing tests**

```python
SEARCH_OBJECTS_TOOL = qa_tools.SEARCH_OBJECTS_SPEC
LIST_JOBS_TOOL = qa_tools.LIST_JOBS_SPEC


def test_search_objects_schema_has_no_project_id_or_owner_property():
    props = SEARCH_OBJECTS_TOOL.parameters["properties"]
    assert "project_id" not in props
    assert "owner" not in props


def test_list_jobs_schema_has_no_project_id_or_owner_property():
    props = LIST_JOBS_TOOL.parameters["properties"]
    assert "project_id" not in props
    assert "owner" not in props


async def test_execute_search_objects_is_scoped_to_the_given_project_and_owner(seeded_objects):
    """seeded_objects fixture creates objects under two different (owner, project) pairs."""
    result = await qa_tools.execute_search_objects(
        {"kinds": ["fastq"]}, project_id=seeded_objects.project_a, owner=seeded_objects.owner_a
    )
    names = {o["name"] for o in result["objects"]}
    assert names == seeded_objects.expected_names_for_a
    assert seeded_objects.owner_b_only_name not in names


async def test_execute_search_objects_ignores_a_model_supplied_project_id_or_owner():
    """Even if a malicious/confused model includes these keys in arguments,
    they must be dropped before building the SearchQuery."""
    result = await qa_tools.execute_search_objects(
        {"kinds": ["fastq"], "project_id": "someone-elses-id", "owner": "someone-else"},
        project_id=seeded_objects.project_a, owner=seeded_objects.owner_a,
    )
    # Assert scoping held -- same assertion shape as the prior test, applied
    # to the case where arguments actively try to override it.


async def test_execute_search_objects_returns_a_trimmed_projection_not_full_objectout():
    result = await qa_tools.execute_search_objects({}, project_id=..., owner=...)
    obj = result["objects"][0]
    assert set(obj.keys()) <= {"id", "name", "kind", "status", "size", "facts"}


async def test_execute_list_jobs_is_scoped_to_the_given_project_and_owner(seeded_jobs):
    result = await qa_tools.execute_list_jobs(
        {"job_type": "trim_reads"}, project_id=seeded_jobs.project_a, owner=seeded_jobs.owner_a
    )
    types = {j["type"] for j in result["jobs"]}
    assert types == {"trim_reads"}
    # And assert a job under owner_b with the same project_id is absent --
    # both directions, per CLAUDE.md's warning about scoping tests.


async def test_execute_list_jobs_limit_is_capped_regardless_of_requested_value():
    result = await qa_tools.execute_list_jobs({"limit": 10000}, project_id=..., owner=...)
    assert len(result["jobs"]) <= qa_tools.MAX_TOOL_RESULT_ROWS
```

- [ ] **Step 2: Implement**

```python
SEARCH_OBJECTS_SPEC = ToolSpec(
    name="search_objects",
    description="Search files in this project by kind, status, tags, or metadata.",
    parameters={
        "type": "object",
        "properties": {
            "kinds": {"type": "array", "items": {"type": "string"}},
            "statuses": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}},
            "metadata": {"type": "object"},
            "size_min": {"type": "integer"},
            "size_max": {"type": "integer"},
            "limit": {"type": "integer"},
        },
    },
)

LIST_JOBS_SPEC = ToolSpec(
    name="list_jobs",
    description="List queue jobs for this project, optionally filtered by state or type.",
    parameters={
        "type": "object",
        "properties": {
            "state": {"type": "string"},
            "job_type": {"type": "string"},
            "object_id": {"type": "string"},
        },
    },
)

MAX_TOOL_RESULT_ROWS = 50

async def execute_search_objects(arguments: dict, *, project_id: PydanticObjectId, owner: str) -> dict:
    query = SearchQuery(
        project_id=project_id,
        kinds=arguments.get("kinds") or [],
        statuses=arguments.get("statuses") or [],
        tags=arguments.get("tags") or [],
        metadata=arguments.get("metadata") or {},
        size_min=arguments.get("size_min"),
        size_max=arguments.get("size_max"),
        limit=min(arguments.get("limit", MAX_TOOL_RESULT_ROWS), MAX_TOOL_RESULT_ROWS),
    )
    result = await search_service.search_objects(query, owner=owner)
    return {
        "objects": [_trim_object(o) for o in result["objects"]],
        "total": result["total"],
        "has_more": result["has_more"],
    }


async def execute_list_jobs(arguments: dict, *, project_id: PydanticObjectId, owner: str) -> dict:
    filt = {"owner": {"$in": [owner, keys.SYSTEM_OWNER]}, "project_id": project_id}
    if arguments.get("state"):
        filt["state"] = arguments["state"]
    if arguments.get("job_type"):
        filt["type"] = arguments["job_type"]
    if arguments.get("object_id"):
        filt["object_id"] = PydanticObjectId(arguments["object_id"])
    jobs = await Job.find(filt).limit(MAX_TOOL_RESULT_ROWS).to_list()
    return {"jobs": [_trim_job(j) for j in jobs]}
```

`_trim_object`/`_trim_job` are small local helpers producing the reduced
projections named in the spec (name/kind/status/size/facts;
type/state/progress/timing/error).

**Note on argument dict handling**: `arguments.get(...)` reading only named
keys, with `project_id`/`owner` always passed as explicit keyword arguments
from the caller (never read from `arguments`), is what makes the "ignores a
model-supplied override" test pass structurally — there is no code path that
could read those keys even if present, which is the point.

- [ ] **Step 3: Run tests, verify pass — both scoping tests in both
  directions.**

- [ ] **Step 4: Commit** — `feat(ai): search_objects and list_jobs tool execution, project/owner-scoped`

---

## Task 9: The tool-calling loop

**Files:**
- Create: `backend/app/services/ai/qa.py`
- Test: `backend/tests/services/ai/test_qa_loop.py`

- [ ] **Step 1: Write the failing tests**

```python
async def test_loop_returns_immediately_when_the_model_answers_with_no_tool_call(monkeypatch):
    monkeypatch.setattr(qa, "complete_sync", lambda *a, **k: Completion("42", "m"))
    result = qa.answer(provider=..., question="how many files?", project_id=..., owner=...)
    assert isinstance(result, Completion)
    assert result.text == "42"


async def test_loop_executes_a_tool_call_and_feeds_the_result_back(monkeypatch):
    calls = []
    def fake_complete(*a, **k):
        calls.append(k)
        if len(calls) == 1:
            return ToolCall(id="1", name="search_objects", arguments={"kinds": ["bam"]})
        return Completion("there are 3 bams", "m")
    monkeypatch.setattr(qa, "complete_sync", fake_complete)
    monkeypatch.setattr(qa_tools, "execute_search_objects", AsyncMock(return_value={"objects": [], "total": 3}))

    result = qa.answer(provider=..., question="how many bams?", project_id=..., owner=...)

    assert isinstance(result, Completion)
    assert len(calls) == 2
    # Second call's history includes the tool_call and tool_result turns
    second_history = calls[1]["history"]
    assert any(t.role == "tool_result" for t in second_history)


async def test_loop_stops_after_three_tool_calls_and_forces_a_final_answer(monkeypatch):
    call_count = 0
    def fake_complete(*a, tools=None, **k):
        nonlocal call_count
        call_count += 1
        if tools is None:
            return Completion("best guess given what I found", "m")
        return ToolCall(id=str(call_count), name="search_objects", arguments={})
    monkeypatch.setattr(qa, "complete_sync", fake_complete)
    monkeypatch.setattr(qa_tools, "execute_search_objects", AsyncMock(return_value={}))

    result = qa.answer(provider=..., question="...", project_id=..., owner=...)

    assert isinstance(result, Completion)
    assert call_count == 4  # 3 tool-call attempts + 1 forced final call
    # The 4th call must have been made with tools=None
    

async def test_loop_aborts_immediately_on_failure(monkeypatch):
    monkeypatch.setattr(qa, "complete_sync", lambda *a, **k: Failure(FailureReason.UNREACHABLE))
    result = qa.answer(provider=..., question="...", project_id=..., owner=...)
    assert isinstance(result, Failure)


async def test_unknown_tool_name_is_a_bad_response_not_a_crash(monkeypatch):
    """Defensive: a model naming a tool that doesn't exist must not KeyError."""
    monkeypatch.setattr(qa, "complete_sync", lambda *a, **k: ToolCall(id="1", name="delete_everything", arguments={}))
    result = qa.answer(provider=..., question="...", project_id=..., owner=...)
    assert isinstance(result, Failure)
    assert result.reason is FailureReason.BAD_RESPONSE
```

- [ ] **Step 2: Implement**

```python
MAX_TOOL_CALLS = 3
QA_TOOLS = [qa_tools.SEARCH_OBJECTS_SPEC, qa_tools.LIST_JOBS_SPEC]
QA_SYSTEM_PROMPT = (
    "You answer questions about one bioinformatics project using only the "
    "search_objects and list_jobs tools. Never answer from prior knowledge "
    "about specific files or jobs -- if you have not called a tool for this "
    "question, say you don't know rather than guessing."
)

_DISPATCH = {
    "search_objects": qa_tools.execute_search_objects,
    "list_jobs": qa_tools.execute_list_jobs,
}


def answer(*, provider, question: str, project_id, owner, prior_turns=None) -> Completion | Failure:
    history = list(prior_turns or []) + [ConversationTurn(role="user", content=question)]
    for _ in range(MAX_TOOL_CALLS):
        result = complete_sync(provider, system=QA_SYSTEM_PROMPT, user="", history=history, tools=QA_TOOLS)
        if isinstance(result, Failure):
            return result
        if isinstance(result, Completion):
            return result
        # ToolCall
        executor = _DISPATCH.get(result.name)
        if executor is None:
            log.warning("qa_unknown_tool", name=result.name)
            return Failure(FailureReason.BAD_RESPONSE, f"unknown tool: {result.name}")
        tool_result = run_from_thread(executor(result.arguments, project_id=project_id, owner=owner))
        history.append(ConversationTurn(role="tool_call", tool_call=result))
        history.append(ConversationTurn(role="tool_result", tool_call_id=result.id, content=json.dumps(tool_result)))
    # Exhausted -- force a final answer with tools withdrawn.
    return complete_sync(provider, system=QA_SYSTEM_PROMPT, user="", history=history, tools=None)
```

Note `execute_search_objects`/`execute_list_jobs` are async (they call
`search_service.search_objects`, itself async) but `qa.answer` runs
synchronously inside a THREAD handler — `run_from_thread` (already imported
by the caller context, see Task 10) is the bridge, exactly as
`summary_handlers._resolve_sync` uses it for `router.resolve`. This module
should accept `run_from_thread` as an injectable rather than importing it at
module scope directly, matching `summary_handlers.py`'s own late-import
pattern (`from app.db.client import run_from_thread` inside the function) —
copy that pattern here rather than a fresh one.

- [ ] **Step 3: Run tests, verify pass.**

- [ ] **Step 4: Commit** — `feat(ai): the tool-calling loop for project Q&A`

---

## Task 10: Compaction

**Files:**
- Create: `backend/app/services/ai/qa_compaction.py`
- Modify: `backend/app/config.py` (two new constants)
- Test: `backend/tests/services/ai/test_qa_compaction.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_estimate_is_under_threshold_for_a_short_conversation():
    convo = ProjectConversation(owner="local", project_id=..., turns=[
        ConversationTurn(role="user", content="hi", created_at=utcnow()),
        ConversationTurn(role="assistant", content="hello", created_at=utcnow()),
    ])
    assert not qa_compaction.needs_compaction(convo, context_length=8000)


def test_estimate_crosses_threshold_for_a_long_conversation():
    long_turns = [ConversationTurn(role="user", content="x" * 5000, created_at=utcnow()) for _ in range(20)]
    convo = ProjectConversation(owner="local", project_id=..., turns=long_turns)
    assert qa_compaction.needs_compaction(convo, context_length=8000)


def test_compact_folds_turns_before_compacted_through_and_advances_it(monkeypatch):
    monkeypatch.setattr(qa_compaction, "complete_sync", lambda *a, **k: Completion("condensed summary", "m"))
    convo = ProjectConversation(owner="local", project_id=..., turns=[
        ConversationTurn(role="user", content="q1", created_at=utcnow()),
        ConversationTurn(role="assistant", content="a1", created_at=utcnow()),
    ])

    qa_compaction.compact(convo, provider=...)

    assert convo.compacted_summary == "condensed summary"
    assert convo.compacted_through == 2
    assert len(convo.turns) == 2  # turns are retained on disk, never deleted


def test_compact_failure_leaves_the_conversation_unchanged(monkeypatch):
    monkeypatch.setattr(qa_compaction, "complete_sync", lambda *a, **k: Failure(FailureReason.UNREACHABLE))
    convo = ProjectConversation(owner="local", project_id=..., turns=[...])
    before = (convo.compacted_summary, convo.compacted_through)

    qa_compaction.compact(convo, provider=...)

    assert (convo.compacted_summary, convo.compacted_through) == before


def test_context_length_none_falls_back_to_the_configured_default():
    convo = ProjectConversation(owner="local", project_id=..., turns=[
        ConversationTurn(role="user", content="x" * (settings.qa_default_context_tokens * 4), created_at=utcnow())
    ])
    assert qa_compaction.needs_compaction(convo, context_length=None)
```

- [ ] **Step 2: Add config constants**

```python
qa_compaction_threshold: float = 0.75
qa_default_context_tokens: int = 8000
```

- [ ] **Step 3: Implement**

```python
def _estimate_tokens(turns: list[ConversationTurn]) -> int:
    return sum(len(t.content) for t in turns) // 4


def needs_compaction(convo: ProjectConversation, *, context_length: int | None) -> bool:
    limit = context_length or settings.qa_default_context_tokens
    live_turns = convo.turns[convo.compacted_through:]
    return _estimate_tokens(live_turns) >= limit * settings.qa_compaction_threshold


def compact(convo: ProjectConversation, *, provider) -> None:
    """Mutates convo in place. Caller is responsible for saving it."""
    turns_to_fold = convo.turns[convo.compacted_through:]
    if not turns_to_fold:
        return
    transcript = "\n".join(f"{t.role}: {t.content}" for t in turns_to_fold if t.role in ("user", "assistant"))
    prior = f"Existing summary: {convo.compacted_summary}\n\n" if convo.compacted_summary else ""
    result = complete_sync(
        provider,
        system="Condense this conversation into a short paragraph of context, preserving anything the user would expect remembered.",
        user=f"{prior}{transcript}",
    )
    if not isinstance(result, Completion):
        return
    convo.compacted_summary = result.text
    convo.compacted_through = len(convo.turns)
```

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit** — `feat(ai): threshold-triggered conversation compaction`

---

## Task 11: `context_length` capture — `list_models_with_context()`

**Files:**
- Modify: `backend/app/services/ai/adapters.py`, `backend/app/models/ai.py`
- Test: `backend/tests/services/ai/test_list_models_with_context.py`

Per the spec's recommendation: a second method, not a changed return shape
for the existing `list_models()`.

- [ ] **Step 1: Write the failing tests**

```python
def test_list_models_with_context_captures_context_length_when_present(mock_urlopen_capturing_request):
    adapter = OpenAICompatAdapter(base_url="http://x", api_key=None)
    mock_urlopen_capturing_request.respond_with({"data": [
        {"id": "model-a", "context_length": 32000},
        {"id": "model-b"},  # no context_length -- some providers omit it
    ]})

    result = adapter.list_models_with_context()

    assert result == {"model-a": 32000, "model-b": None}


def test_list_models_with_context_propagates_failure(mock_urlopen_capturing_request):
    adapter = OpenAICompatAdapter(base_url="http://x", api_key=None)
    mock_urlopen_capturing_request.respond_with_error(500)

    result = adapter.list_models_with_context()

    assert isinstance(result, Failure)


def test_anthropic_list_models_with_context_returns_none_for_every_model(mock_urlopen_capturing_request):
    """Anthropic's /v1/models has never been observed to carry context_length."""
    adapter = AnthropicAdapter(base_url="http://x", api_key=None)
    mock_urlopen_capturing_request.respond_with({"data": [{"id": "claude-x"}]})

    result = adapter.list_models_with_context()

    assert result == {"claude-x": None}
```

- [ ] **Step 2: Implement** `list_models_with_context(self) -> dict[str, int
  | None] | Failure` on both adapters, reusing each adapter's existing
  `/v1/models` request plumbing rather than duplicating it — refactor the
  shared GET-and-parse-`data` logic into a small private helper both
  `list_models()` and `list_models_with_context()` call, if that can be done
  without changing `list_models()`'s existing tested behavior.

- [ ] **Step 3: Add `context_windows: dict[str, int] = Field(default_factory=dict)`
  to `AiProvider`** (`app/models/ai.py`), populated in `provider_service`'s
  fetch-models flow alongside `models_cache` (find that flow, likely
  `provider_service.fetch_models` or similar — grep for where `models_cache`
  is currently assigned and add the parallel write there).

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit** — `feat(ai): capture context_length via list_models_with_context`

---

## Task 12: `answer_project_question` job handler

**Files:**
- Create: `backend/app/queue/qa_handlers.py`
- Test: `backend/tests/queue/test_qa_handlers.py`

- [ ] **Step 1: Write the failing tests**

Mirror `test_summary_handlers.py`'s structure if it exists (check first).

```python
def test_no_provider_configured_is_a_skip(monkeypatch):
    monkeypatch.setattr(qa_handlers, "_resolve_sync", lambda: None)
    ctx = make_job_context(payload={"project_id": str(pid), "question": "q", "conversation_id": str(cid)})

    result = qa_handlers.answer_project_question(ctx)

    assert result["skipped"] == "no_provider"


async def test_failure_does_not_append_a_half_turn(monkeypatch):
    monkeypatch.setattr(qa_handlers, "_resolve_sync", lambda: fake_provider)
    monkeypatch.setattr(qa_handlers.qa, "answer", lambda **k: Failure(FailureReason.UNREACHABLE))
    convo = await ProjectConversation(owner="local", project_id=pid).insert()
    ctx = make_job_context(payload={"project_id": str(pid), "question": "q", "conversation_id": str(convo.id)})

    result = qa_handlers.answer_project_question(ctx)

    assert result["skipped"]
    saved = await ProjectConversation.get(convo.id)
    assert saved.turns == []


async def test_success_appends_both_turns_and_returns_the_answer(monkeypatch):
    monkeypatch.setattr(qa_handlers, "_resolve_sync", lambda: fake_provider)
    monkeypatch.setattr(qa_handlers.qa, "answer", lambda **k: Completion("42 files", "m"))
    convo = await ProjectConversation(owner="local", project_id=pid).insert()
    ctx = make_job_context(payload={"project_id": str(pid), "question": "how many files?", "conversation_id": str(convo.id)})

    result = qa_handlers.answer_project_question(ctx)

    assert result["answer"] == "42 files"
    saved = await ProjectConversation.get(convo.id)
    assert [t.content for t in saved.turns] == ["how many files?", "42 files"]
    assert [t.role for t in saved.turns] == ["user", "assistant"]


def test_missing_payload_fields_raise_permanent_error():
    ctx = make_job_context(payload={})
    with pytest.raises(PermanentError):
        qa_handlers.answer_project_question(ctx)


async def test_compaction_runs_before_the_question_is_appended_when_over_threshold(monkeypatch):
    """Verify the ordering: compact() sees the conversation as it was before
    this question, not including it."""
    ...
```

- [ ] **Step 2: Implement**

```python
@handler(
    "answer_project_question",
    mode=HandlerMode.THREAD,
    job_class=JobClass.USER_INTERACTIVE,
    resources=JobResources(cpu=0, mem_mb=64, io=IoClass.LIGHT),
    max_attempts=1,
)
def answer_project_question(ctx: JobContext) -> dict:
    project_id = ctx.payload.get("project_id")
    question = ctx.payload.get("question")
    conversation_id = ctx.payload.get("conversation_id")
    if not project_id or not question or not conversation_id:
        raise PermanentError("answer_project_question requires project_id, question, conversation_id")

    ctx.check_cancel()
    provider = _resolve_sync()
    if provider is None:
        return {"conversation_id": conversation_id, "skipped": "no_provider"}

    convo = run_from_thread(ProjectConversation.get(PydanticObjectId(conversation_id)))
    context_length = _context_length_for(provider)  # reads provider.context_windows.get(provider.model)
    if qa_compaction.needs_compaction(convo, context_length=context_length):
        qa_compaction.compact(convo, provider=provider)

    prior_turns = _turns_to_conversation_turns(convo)  # respects compacted_through
    ctx.extend_lease(180)
    result = qa.answer(provider=provider, question=question, project_id=PydanticObjectId(project_id), owner=ctx.owner, prior_turns=prior_turns)

    if not isinstance(result, Completion):
        return {"conversation_id": conversation_id, "project_id": project_id, "skipped": str(getattr(result, "reason", result))}

    now = utcnow()
    convo.turns.append(ConversationTurn(role="user", content=question, created_at=now))
    convo.turns.append(ConversationTurn(role="assistant", content=result.text, created_at=now))
    run_from_thread(convo.save())

    return {"conversation_id": conversation_id, "project_id": project_id, "answer": result.text}


def _resolve_sync():
    from app.db.client import run_from_thread
    from app.models.ai import TaskSlot
    from app.services.ai import router
    return run_from_thread(router.resolve(TaskSlot.PROJECT_QA))
```

Follow `summary_handlers.py`'s exact `importlib`-based import for `complete`/
`complete_sync` if the same package `__init__.py` name-shadowing issue
applies here (check whether `qa.py`/`qa_compaction.py` importing
`complete_sync` hits the same problem `summary_handlers.py:25-34` documents;
if `qa.py` imports `from app.services.ai.complete import complete_sync`
directly rather than `from app.services.ai import complete_sync`, the
shadowing issue may not apply — verify which import style avoids it before
copying the workaround unnecessarily).

- [ ] **Step 3: Register in `queue/registry.py`'s handler-loading import list**
  (`load_handlers()` imports `app.queue.handlers` for side effects — check
  whether `qa_handlers` needs its own import there or whether `handlers.py`
  itself imports every handler submodule).

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit** — `feat(queue): answer_project_question job handler`

---

## Task 13: Result applier + `qa.answered` event

**Files:**
- Modify: `backend/app/queue/results.py`
- Test: `backend/tests/queue/test_qa_applier.py`

- [ ] **Step 1: Write the failing tests**

```python
async def test_applier_is_a_noop_on_the_data_model_but_publishes_an_event(monkeypatch):
    published = []
    monkeypatch.setattr(results, "publish_event", lambda *a, **k: published.append((a, k)))

    await results._apply_answer_project_question(
        {"conversation_id": "abc", "project_id": "def", "answer": "42"}, owner="local"
    )

    assert published
    event_name = published[0][0][0] if published[0][0] else published[0][1].get("name")
    assert "qa.answered" in str(published[0])


async def test_applier_is_a_noop_on_skip(monkeypatch):
    published = []
    monkeypatch.setattr(results, "publish_event", lambda *a, **k: published.append((a, k)))

    await results._apply_answer_project_question({"conversation_id": "abc", "skipped": "no_provider"}, owner="local")

    assert not published  # nothing to tell the frontend if there's no new turn


def test_dispatch_table_includes_the_new_type():
    assert "answer_project_question" in results._APPLIERS
```

Adjust the exact `publish_event` call signature to match whatever
`_apply_summarize_object` or another applier already uses for a comparable
publish (grep `publish_event` call sites in `results.py` for the real
keyword shape before writing this test).

- [ ] **Step 2: Implement**

```python
async def _apply_answer_project_question(result: dict, *, owner: str) -> None:
    if "answer" not in result:
        return  # skipped -- nothing changed, nothing to announce
    await publish_event(
        "qa.answered",
        owner=owner,
        data={"conversation_id": result["conversation_id"]},
    )
```

Add `"answer_project_question": _apply_answer_project_question` to
`_APPLIERS`.

- [ ] **Step 3: Run tests, verify pass.** Also run the existing
  exhaustiveness test over `_APPLIERS` (if one exists per the registry-audit
  pattern) to confirm it still passes with the new entry.

- [ ] **Step 4: Commit** — `feat(queue): result applier and qa.answered event for project Q&A`

---

## Task 14: API routes

**Files:**
- Create: `backend/app/api/v1/project_qa.py`
- Modify: wherever routers are mounted (`backend/app/main.py` or
  `backend/app/api/v1/__init__.py` — check the existing mounting pattern)
- Test: `backend/tests/api/test_project_qa_api.py`

- [ ] **Step 1: Write the failing tests**

```python
async def test_get_conversation_creates_an_empty_one_on_first_access(client, owner_a, project_a):
    response = await client.get(f"/api/v1/projects/{project_a}/qa/conversation", headers=owner_a.headers)
    assert response.status_code == 200
    assert response.json()["turns"] == []


async def test_get_conversation_is_owner_scoped(client, owner_a, owner_b, project_a):
    """A conversation seeded under owner_a must not appear for owner_b on the same project."""
    await seed_conversation(owner=owner_a.id, project_id=project_a, turns=[...])
    response = await client.get(f"/api/v1/projects/{project_a}/qa/conversation", headers=owner_b.headers)
    assert response.json()["turns"] == []  # owner_b gets their own empty one, not owner_a's


async def test_post_ask_enqueues_a_job_and_returns_its_id(client, owner_a, project_a):
    response = await client.post(f"/api/v1/projects/{project_a}/qa/ask", json={"question": "how many files?"}, headers=owner_a.headers)
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    job = await Job.get(PydanticObjectId(job_id))
    assert job.type == "answer_project_question"
    assert job.owner == owner_a.owner_string
    assert job.dedup_key is None  # two identical questions must both run


async def test_post_ask_twice_in_a_row_enqueues_two_jobs(client, owner_a, project_a):
    r1 = await client.post(f"/api/v1/projects/{project_a}/qa/ask", json={"question": "same question"}, headers=owner_a.headers)
    r2 = await client.post(f"/api/v1/projects/{project_a}/qa/ask", json={"question": "same question"}, headers=owner_a.headers)
    assert r1.json()["job_id"] != r2.json()["job_id"]


async def test_delete_conversation_clears_turns_and_compaction_state(client, owner_a, project_a):
    await seed_conversation(owner=owner_a.id, project_id=project_a, turns=[...], compacted_summary="old")
    response = await client.delete(f"/api/v1/projects/{project_a}/qa/conversation", headers=owner_a.headers)
    assert response.status_code == 204
    convo = await ProjectConversation.find_one(ProjectConversation.owner == owner_a.owner_string, ProjectConversation.project_id == project_a)
    assert convo.turns == []
    assert convo.compacted_summary is None
```

- [ ] **Step 2: Implement**

```python
router = APIRouter(prefix="/projects/{project_id}/qa", tags=["project-qa"])


@router.get("/conversation")
async def get_conversation(project_id: PydanticObjectId, owner: OwnerDep) -> ProjectConversationOut:
    convo = await ProjectConversation.find_one(ProjectConversation.owner == owner, ProjectConversation.project_id == project_id)
    if convo is None:
        convo = ProjectConversation(owner=owner, project_id=project_id)
        await convo.insert()
    return ProjectConversationOut.from_document(convo)


@router.post("/ask")
async def ask(project_id: PydanticObjectId, body: AskRequest, owner: OwnerDep) -> AskResponse:
    convo = await ProjectConversation.find_one(ProjectConversation.owner == owner, ProjectConversation.project_id == project_id)
    if convo is None:
        convo = ProjectConversation(owner=owner, project_id=project_id)
        await convo.insert()
    job = await queue.enqueue(
        "answer_project_question",
        owner=owner,
        project_id=project_id,
        payload={"project_id": str(project_id), "question": body.question, "conversation_id": str(convo.id)},
    )
    return AskResponse(job_id=str(job.id))


@router.delete("/conversation", status_code=204)
async def clear_conversation(project_id: PydanticObjectId, owner: OwnerDep) -> None:
    convo = await ProjectConversation.find_one(ProjectConversation.owner == owner, ProjectConversation.project_id == project_id)
    if convo is not None:
        convo.turns = []
        convo.compacted_summary = None
        convo.compacted_through = 0
        await convo.save()
```

Check the real `queue.enqueue` signature before writing this — confirm
keyword names (`owner=`, `project_id=`, `payload=`) against an existing
`pipeline_service.launch_*` call site rather than guessing.

- [ ] **Step 3: Mount the router** alongside the other `v1` routers.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit** — `feat(api): project Q&A conversation and ask routes`

---

## Task 15: Frontend — API client + query hooks

**Files:**
- Modify: `frontend/src/api/client.ts`, `frontend/src/api/types.ts`
- Create: `frontend/src/hooks/useProjectQa.ts`

No test framework exists for the frontend per CLAUDE.md — this task is
implementation only, verified manually in Task 17.

- [ ] **Step 1: Add types** to `types.ts`: `ConversationTurn { role, content,
  created_at }`, `ProjectConversationOut { turns, compacted_summary }`,
  `AskRequest { question }`, `AskResponse { job_id }`.

- [ ] **Step 2: Add client functions** to `client.ts`: `getProjectConversation(projectId)`,
  `askProjectQuestion(projectId, question)`, `clearProjectConversation(projectId)`
  — following the existing functions' pattern for headers/error handling in
  this file exactly (read a neighboring function first).

- [ ] **Step 3: Add `useProjectQa(projectId)` hook** wrapping a `useQuery`
  for the conversation (query key `["project-conversation", projectId]`) and
  a `useMutation` for asking a question, with an optimistic local append of
  the user's own turn on submit (per the spec's UI-responsiveness note).

- [ ] **Step 4: Register `qa.answered` in `useEvents.ts`'s listener list**,
  mapped to invalidating `["project-conversation", projectId]`. Since the
  event carries `conversation_id` not `project_id`, and the query key is
  keyed by `project_id`, check whether invalidating broadly (all
  `project-conversation` keys) is acceptable here rather than needing an
  extra lookup — likely fine given this is a low-frequency event and a wasted
  refetch of a conversation the user isn't looking at costs nothing
  visible.

- [ ] **Step 5: Commit** — `feat(frontend): project Q&A API client and query hook`

---

## Task 16: Frontend — footer entry point and drawer

**Files:**
- Modify: `frontend/src/components/Footer.tsx`
- Create: `frontend/src/components/ProjectQaDrawer.tsx`

- [ ] **Step 1: Add a `qaOpen` boolean and toggle button to `Footer.tsx`**,
  visible only when a project is currently open (check how `Footer.tsx`
  currently knows the active project — likely a prop or a store read already
  used for the file/project counts).

- [ ] **Step 2: Build `ProjectQaDrawer.tsx`** modeled on `QueuePanel.tsx`'s
  structure (backdrop + positioned panel + head bar + scrollable body) but
  anchored bottom-slide-up per the spec, with a separate minimize control
  (collapses to a small pill, keeps `qaOpen` state distinct from a
  `qaMinimized` state so a running job keeps "working" visually rather than
  disappearing).

- [ ] **Step 3: Wire the message list** to `useProjectQa`'s conversation
  data, a text input + submit calling the ask mutation, and a "thinking..."
  indicator shown between submit and the next successful conversation
  refetch (or a `job.failed` event for the enqueued job id, if that's
  reachable — check whether the ask mutation's response `job_id` can be
  compared against `job.failed` SSE payloads, and if not, a plain timeout
  state is an acceptable fallback for a first version).

- [ ] **Step 4: Commit** — `feat(frontend): project Q&A chat drawer`

---

## Task 17: Full suite + manual verification

- [ ] **Step 1: Run the full backend suite** (`./backend/run-worktree-tests.sh
  tests/ -q`), record the count, compare against the Task 0 baseline —
  should be baseline + every new test file's count, zero regressions.

- [ ] **Step 2: `./ops/worktree-up.sh`** to bring up this worktree's code on
  its own ports.

- [ ] **Step 3: Configure `TaskSlot.PROJECT_QA`** against a real
  function-calling-capable provider in the settings UI (report which
  provider was used and confirm it supports tool-calling before trusting the
  end-to-end result — if none is reachable in this environment, say so
  explicitly rather than skipping this step silently).

- [ ] **Step 4: Open a real project with a mix of files and jobs.** Ask a
  question answerable only via `search_objects` (e.g. "how many BAM files do
  I have"), confirm the answer matches what the file explorer actually
  shows — not just that *an* answer came back. Ask a question answerable
  only via `list_jobs` (e.g. "is anything still running"). Ask a
  two-tool-call question if one is easy to construct (e.g. "did the align
  job for X finish, and how big is the resulting BAM").

- [ ] **Step 5: Confirm the drawer survives being minimized and reopened**
  while a question is in flight — the answer should appear on reopen, not be
  lost.

- [ ] **Step 6: Confirm cross-project isolation manually**: ask a question in
  project A that references a filename that only exists in project B ("do I
  have a file called X" where X is B's file) — the answer must not find it.

- [ ] **Step 7: Commit any fixes found during manual verification separately**
  from the feature commits, with a description of what manual testing caught
  (per this repo's convention of recording what a green suite didn't catch).

---

## Task 18: Close out

- [ ] **Step 1: Update `docs/TODO.md`** if project Q&A chat has an entry
  there — check first; the issue may be the only tracking surface.

- [ ] **Step 2: Comment on and label issue #36** per CLAUDE.md's issue-update
  convention, once implementation is verified working end to end.

- [ ] **Step 3: Merge to `main`** once the suite is green and manual
  verification (Task 17) is complete, per CLAUDE.md's "commit and merge once
  tests are green, without asking."

- [ ] **Step 4: Push `main` to `origin`.**
