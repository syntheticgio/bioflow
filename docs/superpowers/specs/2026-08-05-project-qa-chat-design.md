# Project Q&A chat panel — design

Issue: [#36](https://github.com/syntheticgio/bioflow/issues/36). The issue
body linked a spec and plan at these same two paths; neither existed anywhere
in git history when this was picked up (`git log --all` on both files: zero
commits). This document and its companion plan are written fresh against the
issue body's own description, which reads as settled design decisions from an
earlier brainstorm that never got committed.

## Problem

A user looking at a project full of FASTQs, BAMs, VCFs, and jobs has no way to
ask "which samples still need trimming" or "did the align job for SRR123
finish" without manually clicking through the file explorer and Activity tab.
An LLM could answer this well if it could actually see the project's data —
but this app has no embeddings, no vector index, and building one is
explicitly out of scope (a single-user local tool does not need semantic
search over a few hundred rows a structured query already reaches in
milliseconds).

## Approach

A chat drawer scoped to one project. Every user turn goes through a bounded
tool-calling loop: the model can call `search_objects` or `list_jobs` — thin
wrappers around the exact `search_service.search_objects` and `jobs.py`
listing logic the UI itself uses — up to 3 times, then must answer in prose
grounded in what those calls returned. The model never gets free-text
retrieval and never answers from training data alone; the system prompt says
so and the tool results are the only project-specific content in context.

This requires tool-calling support that does not exist in either AI adapter
today (confirmed: `adapters.py` builds a bare `{system, messages}` body with
no `tools` field, and parses only `.content`/`.text` — zero references to
`tool_calls` or `tool_use` anywhere in the codebase). That is real new surface,
not a wrapper around something already there.

## Why tool-calling instead of RAG

This app's `SearchQuery` (`search_service.py:38-51`) already expresses
everything a user would ask in the first place — kind, status, tags, metadata
filters, size range — over a corpus of a few hundred objects. An embedding
index would duplicate that reach at real cost (a new dependency, an index to
keep in sync with every mutation) to answer questions structured search
already answers exactly. The two tools this exposes are not a retrieval layer
bolted onto a chat feature; they are the search/jobs API the UI already calls,
handed to the model instead of a click.

## Scope

**In scope:**
- Tool-calling support in both `OpenAICompatAdapter` and `AnthropicAdapter`.
- `search_objects` and `list_jobs` tool definitions and their execution,
  scoped to one project and one owner.
- A new `TaskSlot.PROJECT_QA` and its routing.
- `ProjectConversation` — one document per project holding message history,
  with compaction when a routed provider's context window is threatened.
- `answer_project_question`, a new THREAD-mode job, mirroring
  `summarize_object`'s shape (queued, not synchronous — an answer must survive
  the drawer being closed or minimized).
- A footer-bar entry point and a slide-up, minimizable chat drawer.

**Out of scope:**
- Semantic search / embeddings / a vector index of any kind.
- Cross-project questions. Every conversation and every tool call is scoped to
  exactly one project; nothing here reaches into a second project's objects or
  jobs even if the model tries to ask a question implying it should.
- Any tool beyond the two named. No `delete_object`, no `launch_pipeline` — a
  broader "agent that acts" is a different feature with a different safety
  posture and is explicitly not what this builds.
- Streaming token-by-token responses. The answer arrives once, the way a
  `summarize_object` result does — via a terminal job event triggering a
  refetch, not a live token stream. Providers here are heterogeneous
  local/hosted servers behind two different wire formats; streaming both
  correctly is its own scope, deferred until non-streaming ships and proves
  the rest of the shape out.

## Tool-calling: adapter changes

### Wire shapes

**OpenAI-compatible** (`POST /v1/chat/completions`): request gains a `tools`
array of `{"type": "function", "function": {"name", "description",
"parameters"}}` and, when a tool result is being fed back, an `assistant`
message carrying `tool_calls` followed by one `tool` message per call
(`{"role": "tool", "tool_call_id", "content"}`). Response parsing gains a
branch: `choices[0].message.tool_calls` (a list of `{"id", "function":
{"name", "arguments"}}`, `arguments` a JSON-encoded string) alongside the
existing `.content` branch — a response can carry one or the other, never
usefully both in this loop, so a `tool_calls` non-empty response is
recognized before falling through to text extraction.

**Anthropic** (`POST /v1/messages`): request gains a `tools` array of
`{"name", "description", "input_schema"}` (its own key name, not
`parameters`), and turns already fed back use `content` blocks tagged
`tool_use` (assistant) and `tool_result` (user, `{"type": "tool_result",
"tool_use_id", "content"}") rather than OpenAI's separate message-per-call
shape. Response parsing gains a branch reading `content` blocks of type
`tool_use` (`{"id", "name", "input"}`, `input` already a parsed object, not a
string — the one place the two formats genuinely diverge in a way calling
code must handle, not just wire format).

### New result type

```python
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict
```

`complete()` and `complete_sync()` gain a `tools: list[ToolSpec] | None =
None` parameter and their return type widens to `Completion | ToolCall |
Failure`. A response with zero tool calls and non-empty text still returns
`Completion` exactly as today — every existing caller (`summarize_object`,
organism blurbs) passes no `tools` and is unaffected. `ToolSpec` is the
adapter-neutral tool definition (`name`, `description`, `parameters` as a
JSON-schema dict); each adapter's `complete()` translates it into its own
wire shape (`function.parameters` vs. `input_schema`) internally, so callers
never touch adapter-specific keys.

Only one tool call is modeled per turn (`ToolCall`, not `list[ToolCall]`).
Confirmed against both wire formats that a response can carry more than one
`tool_calls` entry / `tool_use` block in a single turn — but the router's own
`_qa_loop` (below) only ever needs one tool result before deciding its next
move, and modeling multiple would mean either running them concurrently
(no precedent anywhere in this codebase for concurrent AI-adapter calls) or
silently picking the first and discarding the rest. If a model does return
multiple tool_calls in one response, the adapter takes the first and logs the
rest as dropped (`ai_multi_tool_call_dropped`) rather than guessing which
matters — revisit only if a real provider is observed doing this so often that
dropping wastes turns.

### Conversation replay

A multi-turn tool loop needs the adapter to accept prior turns, not just one
`system` + one `user` string. `complete()`/`complete_sync()` gain an optional
`history: list[ConversationTurn] | None = None` parameter — when present, it
replaces the single `user` message with the full turn sequence (each adapter
renders it in its own shape: OpenAI's flat list, Anthropic's `tool_use`/
`tool_result` content-block pairing). `system` stays a separate parameter in
both cases, matching each API's existing convention. This is additive: every
existing call site passes no `history` and behaves exactly as today.

## The tool-calling loop

Lives in `app/services/ai/qa.py`, called from the new job handler (below).

```
turns = [ {"role": "user", "content": question} ]
for _ in range(MAX_TOOL_CALLS):  # 3
    result = complete_sync(provider, system=QA_SYSTEM_PROMPT, history=turns, tools=QA_TOOLS)
    if isinstance(result, Failure):
        return result
    if isinstance(result, Completion):
        return result  # model chose to answer
    # ToolCall
    tool_result = _execute(result, project_id=project_id, owner=owner)
    turns.append(assistant_tool_call_turn(result))
    turns.append(tool_result_turn(result.id, tool_result))
# Loop exhausted without an answer: ask once more with tools withdrawn,
# forcing a prose response from whatever the model has already learned.
return complete_sync(provider, system=QA_SYSTEM_PROMPT, history=turns, tools=None)
```

`MAX_TOOL_CALLS = 3` is a hard cap, not a suggestion the model can talk its way
past — enforced by the loop itself, since nothing in either wire format lets a
caller tell the model "you get 3 calls." The forced-final-answer call after
exhaustion means a pathological question ("what about all my other files"
repeated) degrades to a possibly-incomplete answer rather than a stuck job or
an infinite-looking wait.

`_execute` dispatches on `result.name`:
- `search_objects`: builds a `SearchQuery` from `result.arguments` (a subset
  of the real fields — `kinds`, `statuses`, `tags`, `metadata`, `size_min`,
  `size_max`, capped `limit` — with `project_id` and `owner` always injected
  server-side, never taken from model output) and calls
  `search_service.search_objects(query, owner=owner)` directly. Returns a
  trimmed JSON projection (name, kind, status, size, key facts) — not the full
  `ObjectOut` shape, since the model needs enough to reason and answer, not
  every field the UI renders.
- `list_jobs`: builds the same filter dict `jobs.py:list_jobs` does (`state`,
  `job_type`, `object_id`) with `project_id` and `owner` injected the same
  way, queries `Job` directly (not via HTTP — this runs in a worker thread
  with a live Mongo connection already, an HTTP round-trip to itself would be
  pure overhead), and returns a trimmed projection (type, state, progress,
  timing, error message if failed).

**Every tool call is owner- and project-scoped by construction, never by the
model's argument.** `search_objects`'s and `list_jobs`'s JSON schemas exposed
to the model do not include `project_id` or `owner` as parameters at all — the
model cannot ask for a different project's data because there is no field to
put one in, mirroring `search_service.SearchQuery`'s own
"owner is a keyword-only argument, never a request field" convention
(`search_service.py:54-71`).

## New `TaskSlot`

```python
class TaskSlot(StrEnum):
    FILE_SUMMARY = "file_summary"
    ORGANISM_BLURB = "organism_blurb"
    PROJECT_QA = "project_qa"
```

Plus `_SLOT_LABELS[TaskSlot.PROJECT_QA] = "Project Q&A chat"`. The settings
page needs no other change — it already renders one row per `TaskSlot` member.

Tool-calling support is not universal among configured providers (a small
local model may advertise a `/v1/chat/completions`-compatible endpoint with no
function-calling support at all). This is not detected or gated up front —
resolving `PROJECT_QA` to a provider that cannot follow the `tools` field
simply produces a plain-text non-answer or a malformed response, which the
loop treats as `Failure(BAD_RESPONSE)` the same way any other unparseable
response is handled today. A provider that repeatedly fails this slot shows
the existing settings-page failure badge; there is no separate
"doesn't support tools" diagnosis, matching the existing coarse `FailureReason`
vocabulary's own stated philosophy ("a 401 from Anthropic and a 401 from
DeepSeek mean the same thing to the person who has to fix it").

## `ProjectConversation`

```python
class ConversationTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime

class ProjectConversation(TimestampedDocument):
    project_id: PydanticObjectId
    turns: list[ConversationTurn]
    compacted_summary: str | None = None
    # Index of the first turn in `turns` not yet folded into
    # compacted_summary. Turns before this index are retained on disk (an
    # honest transcript, never destroyed) but excluded from what gets sent
    # to the model.
    compacted_through: int = 0

    class Settings:
        name = "project_conversations"
        indexes = [IndexModel([("owner", ASCENDING), ("project_id", ASCENDING)], unique=True)]
```

One conversation per `(owner, project_id)` pair — a project's Q&A history is
per-profile, not shared across profiles that both happen to see the project
(consistent with every other collection's owner-partitioning). Tool-call
turns (the intermediate `ToolCall`/tool-result exchanges inside one answer's
loop) are **not** persisted as their own `ConversationTurn` rows — only the
final user question and final assistant answer are. The tool loop is
per-question scratch state reconstructed fresh each time from `turns` plus the
current question; persisting every intermediate tool call would let history
grow by up to 3x per question for no benefit the user-visible transcript
needs, and would complicate compaction (which turns to fold: only outer
answers, or also inner tool exchanges nobody reads back).

### Compaction

Triggered before building `history` for a new question, not on a schedule.
Given the routed provider's `context_length` (see below) and a token estimate
of `turns[compacted_through:]`, if the estimate crosses **75% of
context_length** (configurable via `settings.qa_compaction_threshold`,
default `0.75`), the turns before the newest exchange are summarized by one
extra LLM call (same provider, a dedicated compaction prompt: "condense this
conversation into a short paragraph of context, preserving anything the user
would expect remembered") and folded into `compacted_summary`;
`compacted_through` advances to the current end of `turns`. The next
question's `history` becomes `[system prompt mentioning compacted_summary if
present] + turns[compacted_through:]` — never a truncation, since dropping the
tail of what a user asked ten minutes ago silently changes what "it" refers to
in their next message.

Token estimate is a coarse `len(text) // 4` heuristic across `content` fields,
not a real tokenizer — this codebase has no tokenizer dependency today and
model-specific tokenizers vary per provider/model in ways not worth chasing
for a threshold whose only job is "don't wait until the request 400s." Worth
revisiting if the 75% default proves too conservative or too late in practice.

### `context_length` capture

`list_models()` in both adapters discards every field but `id` (and LM
Studio's `loaded`) from `/v1/models`. This needs the `context_length` field
kept: `AiProvider` gains `context_windows: dict[str, int]` (model id →
context length, populated alongside `models_cache` whenever models are
fetched), and `list_models()`'s return type widens from `list[str] | Failure`
to a small dataclass carrying both the id list and available context lengths
— or, more conservatively, a second method `list_models_with_context()` is
added rather than changing the widely-used existing return shape.
**Recommendation: the second option.** `list_models()` has existing callers in
the settings-fetch-models flow that only want the id list; changing its
return shape to satisfy a new caller adds an unpacking step everywhere else
for a feature only Q&A compaction needs. A field not every provider actually
returns (confirmed present on the local server checked during the brainstorm,
not universal — OpenAI's `/v1/models` does not return it) degrades to "assume
a conservative default" (`settings.qa_default_context_tokens`, e.g. 8000)
rather than failing compaction outright.

## `answer_project_question` job

```python
@handler(
    "answer_project_question",
    mode=HandlerMode.THREAD,
    job_class=JobClass.USER_INTERACTIVE,
    resources=JobResources(cpu=0, mem_mb=64, io=IoClass.LIGHT),
    max_attempts=1,
)
def answer_project_question(ctx: JobContext) -> dict:
    ...
```

`max_attempts=1`, not `summarize_object`'s 2 — a retried Q&A answer after a
partial tool-loop failure risks asking the model to re-derive context it may
answer differently the second time, and unlike a file summary (idempotent,
same facts every attempt) a conversation's `turns` list is mutated by the
first attempt already partially completing, so a naive retry would replay
against changed state. `job_class=USER_INTERACTIVE` rather than
`USER_BACKGROUND` — a chat message is something the user is actively waiting
on inside an open drawer, closer in urgency to an interactive click than to a
background summarization pass, and should not queue behind a large batch job.

Body mirrors `summarize_object`'s shape:
1. Read `project_id`, `question`, and `conversation_id` from `ctx.payload`.
2. `_resolve_sync()` → `TaskSlot.PROJECT_QA`. `None` → `{"skipped":
   "no_provider"}`.
3. Load or create the `ProjectConversation` for `(ctx.owner, project_id)`.
4. Run compaction if needed (may issue its own `complete_sync` call).
5. Append the user's question as a `ConversationTurn`, run the tool-calling
   loop (`ctx.extend_lease(...)` before it, matching the pattern for any
   phase involving a model call whose duration is not bounded by this
   process).
6. On `Completion`: append the answer as a `ConversationTurn`, save the
   conversation, return `{"conversation_id", "project_id", "answer"}`.
7. On `Failure`: return `{"conversation_id", "project_id", "skipped":
   str(reason)}` — the question is **not** appended to `turns` on failure, so
   a failed attempt does not permanently occupy a slot in the visible
   transcript with no matching answer.

## Result surfacing

Registered in `results.py`'s dispatch dict as `"answer_project_question":
_apply_answer_project_question`. Unlike every existing applier, this one
writes nothing onto a `DataObject` — there is no object this job is "about."
It is a no-op applier in the structural sense (nothing to merge into `facts`),
existing only so the dispatch table's exhaustiveness holds and so a place
exists to publish the new SSE event described below. This is a legitimate
instance of the "genuinely nothing to derive" case named in CLAUDE.md's
registry-audit guidance, not a corner being cut.

A new SSE event, `qa.answered`, is published (owner-scoped, matching every
other event) on job success — carrying no payload data beyond
`conversation_id` (SSE events are advisory-only everywhere else in this app;
see `useEvents.ts`, which never reads event body content). The frontend's
`useEvents` hook gains this event name in its listener list, mapped to
invalidating a new `["project-conversation", projectId]` query key, causing
the chat drawer (if open) to refetch the conversation document and render the
new turn — the same "event says something changed → refetch the owning
document" pattern `summarize_object` already established.

## API surface

- `GET /projects/{project_id}/qa/conversation` — returns the
  `ProjectConversation` for `(owner, project_id)`, creating an empty one on
  first access rather than 404ing (a fresh project's chat history is
  legitimately empty, not missing).
- `POST /projects/{project_id}/qa/ask` — body `{"question": str}`. Enqueues
  `answer_project_question` with `dedup_key` **omitted** (unlike most
  launches, two identical questions asked deliberately in a row should both
  run — deduping here would silently drop a user's "actually, ask that
  again"). Returns the enqueued `job_id` so the frontend can show a
  "thinking" state without waiting on the SSE round-trip.
- `DELETE /projects/{project_id}/qa/conversation` — clears `turns` and resets
  compaction state. A user-facing "clear chat" action; not exposed for any
  other conversation-holding feature because none exists yet.

All three take `owner: OwnerDep`, matching every other route in this app.

## Frontend

**Entry point**: a new icon/button in `Footer.tsx`, alongside the existing
queue-panel toggle and file/project counts — visible only when a project is
open (there is no meaningful project-scoped chat with no project selected).
Toggles a `qaOpen` boolean the same way `queueOpen` does.

**Drawer**: `ProjectQaDrawer.tsx`, structurally modeled on `QueuePanel.tsx` —
a full-screen click-away backdrop plus a positioned panel, but anchored to
slide up from the bottom rather than `QueuePanel`'s corner-popover placement,
and with a persistent minimize control distinct from close (`close` clears
`qaOpen`; `minimize` collapses to a small pill without ending the chat
session, since a running `answer_project_question` job should keep working
while the user goes back to the file explorer). Message list renders
`ProjectConversation.turns` after client-side markdown-lite rendering (plain
text is likely sufficient for a first version — no code blocks or tables are
expected in these answers, so no markdown library needs to be introduced
solely for this).

**Data flow**: `useQuery(["project-conversation", projectId], ...)` for the
transcript (invalidated by `qa.answered`, as above, and by an optimistic
local append of the user's own question immediately on submit, before the job
even starts, so the UI never looks unresponsive waiting on a queue slot).
Submitting calls `POST .../qa/ask`, which enqueues the job; a "thinking..."
indicator shows from submit until the corresponding `qa.answered` event (or a
`job.failed` event for the same job id) arrives.

**Shared component candidate with #35**: the issue names the queue-panel
drawer fix in #35 as a candidate for sharing a component with this drawer.
Deferred to whichever ships second — building a shared base component before
either concrete instance exists risks guessing the wrong abstraction (per
CLAUDE.md's stance against premature abstraction). If #35 lands first, its
drawer shape should be checked for reuse before this one is built; if this
lands first, no forward-looking generalization is added on its account.

## Testing

- **Adapter tool-calling**: both adapters, each wire shape (request built
  correctly with `tools`; response parsed correctly for a `tool_calls`/
  `tool_use` response vs. a plain-text response vs. a multi-tool-call response
  with the drop-and-log behavior); `history` replay producing the correct
  per-adapter turn shape.
- **The loop**: reaches an answer in 0, 1, 2, 3 tool calls; hits the cap and
  forces a final tools-withdrawn call; a `Failure` mid-loop aborts immediately
  without a partial answer.
- **Tool execution**: `search_objects`/`list_jobs` scoping — a model-supplied
  argument dict can never smuggle a different `project_id` or reach another
  owner's rows (mirrors the existing owner-scoping test pattern: assert both
  directions, per CLAUDE.md's warning about tests that pass against a
  hardcoded value).
- **Compaction**: crossing the threshold folds old turns and preserves
  `compacted_through`; a conversation under threshold is untouched; a
  provider with no reported `context_length` falls back to the configured
  default rather than skipping compaction silently forever.
- **The job handler**: no-provider skip; failure skip does not append a
  half-turn; success appends both turns and returns the answer.
- **Route scoping**: the same three-request pattern used elsewhere (A sees
  its own conversation, B does not, and a fresh project returns an empty
  conversation rather than 404).
- Per CLAUDE.md's own recorded traps: any test asserting scoping must check
  both directions, and rules dispatched from a dict keyed by an enum
  (`_APPLIERS`, `TOOL_META`-shaped registries) should be checked against
  real objects before trusting a green suite — this feature doesn't add a
  new hand-maintained registry of that shape, but `_APPLIERS` itself gains one
  more entry and its own exhaustiveness test must still pass.

## What is deliberately not decided here

- The exact system prompt wording for the QA loop and for compaction. Left to
  implementation — the plan's tasks should draft and iterate on it against a
  real running provider rather than freezing prose in a design doc no one
  will re-read once the code exists.
- Whether `qa_compaction_threshold` and `qa_default_context_tokens` need to be
  user-facing settings or can stay `backend/app/config.py` constants for a
  first version. Recommendation: constants first: nothing here suggests a
  single-user local tool needs this tunable from day one, and it can move to
  settings later without a migration (it is a plain int, not stored data).
