# Unified AI Agent — Design

**Issues:** [#289](https://github.com/syntheticgio/bioflow/issues/289) (panel broken),
[#290](https://github.com/syntheticgio/bioflow/issues/290) (missing context)
**Date:** 2026-08-12
**Status:** design

## Problem

BioFlow has two separate AI chat features — **ASK** (Project Q&A) and **Agent** (AI
Agent panel) — that overlap in purpose and share the same persistence layer
(`ProjectConversation`), but differ in execution model, tool access, and streaming
capability. Maintaining both duplicates surface area, confuses users, and has
produced two bugs:

- **#289:** The Agent panel shows "not connected to agent" — the SSE connection
  is broken by a React hook reconnection loop.
- **#290:** Neither feature injects project context into the model's prompt, so
  the first question in a session always wastes a tool call just discovering the
  project's shape.

The original distinction — ASK is a stateless queued question, Agent is a
persistent coding agent — was artificial. The Agent can answer quick questions
as easily as deep ones, and the queued-job model for ASK was built before the
Pi agent existed. This design merges them into one unified Agent panel.

## Approach

One footer button, one drawer, one backend path. The Pi agent handles every
interaction — quick questions, pipeline debugging, code generation, data
exploration. The ASK drawer and all its backend infrastructure are removed.

### Key changes

| Component | Before | After |
|-----------|--------|-------|
| Footer | Two buttons: "Ask" and "Agent" | One button: "Agent" |
| Drawer | `ProjectQaDrawer` + `AgentPanel` | `AgentPanel` (enhanced) |
| Backend AI path | `answer_project_question` (queued job) + `agent_service` (Pi process) | `agent_service` only |
| Tools | ASK: 2 tools; Agent: full MCP (18 tools) | Full MCP (all tools) |
| Streaming | ASK: batch; Agent: token-by-token | Token-by-token always |
| Context injection | System prompt only | System prompt + project summary |

## Frontend changes

### Footer.tsx

Replace the two separate buttons with one:

```tsx
// Before
{projectId && (
  <button onClick={() => setQaOpen(true)}><BioIcon name="ask" /> Ask</button>
)}
{projectId && (
  <button onClick={() => setAgentOpen(true)}><BioIcon name="agent" /> Agent</button>
)}

// After
{projectId && (
  <button onClick={() => setAgentOpen(true)}><BioIcon name="agent" /> Agent</button>
)}
```

Remove `qaOpen` state, `ProjectQaDrawer` import, and the `{qaOpen && <ProjectQaDrawer>}`
rendering block.

### AgentPanel.tsx

The existing `AgentPanel` becomes the unified drawer. No structural changes needed
to the component itself — it already has streaming, tool call display, markdown
rendering, and all the states we need. Two improvements:

1. **Fix the SSE reconnection loop** (see below) so "not connected" stops appearing.
2. **Show a loading state during initial agent spawn** — the first `/ask` for a
   project spawns the Pi process, which takes a moment. The current code shows
   "Starting agent..." but this may not be triggering correctly.

### useAgentSSE.ts — fix the reconnection loop

**Root cause:** Every render of `AgentPanel` creates new inline arrow functions
for `onMessageDelta`, `onToolCall`, etc. These are all dependencies of the
`connect` callback (line 140-150). Since they change on every render, `connect`
changes on every render, and the `useEffect` on line 152 re-runs — calling
`disconnect()` then `connect()` — creating a new `EventSource` on every render.

**Fix:** Stabilize the callbacks with `useRef` wrappers inside the hook. The hook
stores the latest callbacks in refs and reads them at event-handler time, so
the `connect` function is stable across renders:

```typescript
// Inside useAgentSSE, at the top of the hook body:
const onMessageDeltaRef = useRef(onMessageDelta);
const onToolCallRef = useRef(onToolCall);
const onToolResultRef = useRef(onToolResult);
const onDoneRef = useRef(onDone);
const onErrorRef = useRef(onError);
const onConnectionChangeRef = useRef(onConnectionChange);
const onAgentStatusRef = useRef(onAgentStatus);

useEffect(() => { onMessageDeltaRef.current = onMessageDelta; });
useEffect(() => { onToolCallRef.current = onToolCall; });
// ... etc for each callback

// Then in event handlers, use refs instead of captured closures:
source.onopen = () => {
  setConnected(true);
  onConnectionChangeRef.current(true);
  reconnectAttempts.current = 0;
};
```

This is a standard React pattern for hooks that register long-lived subscriptions.
The `connect` function's dependency array shrinks to `[profileId, projectId]` only.

### Remove ProjectQaDrawer.tsx

Delete the file entirely. No replacement needed — the unified Agent panel
supersedes it.

### Remove qa.answered SSE event handling

In `useEvents.ts`, remove the `qa.answered` listener and its invalidation of
`["project-conversation", projectId]`. The Agent panel uses its own SSE stream
(`/agent/events`) for real-time updates, not the global event bus.

## Backend changes

### Remove Project Q&A infrastructure

The following files and code paths are no longer needed:

| File | Reason |
|------|--------|
| `backend/app/api/v1/project_qa.py` | Entire router removed |
| `backend/app/services/ai/qa.py` | Tool-calling loop removed |
| `backend/app/services/ai/qa_tools.py` | Tool definitions removed |
| `backend/app/services/ai/qa_compaction.py` | Compaction logic removed |
| `backend/app/queue/qa_handlers.py` | Job handler removed |
| `backend/app/models/conversation.py` | `ProjectConversation` model removed |
| `backend/app/models/ai.py` | `TaskSlot.PROJECT_QA` removed |

The `ProjectConversation` model was used by both ASK and Agent for persistence.
With the merge, Pi handles its own session persistence via `--session-id` and
`--session-dir` (per the multi-turn sessions design,
`docs/superpowers/specs/2026-08-08-agent-multi-turn-sessions-design.md`). The
Agent's SSE stream already accumulates and persists the assistant turn to
`ProjectConversation` — but since Pi's session file is the source of truth for
agent memory, and the visible transcript is reconstructed from the SSE stream's
own accumulation, `ProjectConversation` becomes unnecessary.

**Migration:** Existing `project_conversations` documents in Mongo can be left in
place — they're small and harmless. No migration script needed. Future agent
interactions simply won't write to them.

### Agent API — add project context injection

The Pi agent's system prompt currently says which project it's in, but nothing
about the project's actual contents. For the first question in a session, the
agent has to call MCP tools just to discover what kind of data exists. Inject
a project summary as an initial system message (not part of the system prompt
itself, but as the first user/assistant exchange or a system message that gets
appended):

```python
def _build_project_context(project) -> str:
    """Generate a compact summary of the project for the agent's context.

    Called once per process spawn (lazy, on first /ask). The summary is injected
    as a system message so the agent can answer the first question without
    burning a tool call on discovery.
    """
    # Count objects by kind
    from app.services import search_service
    from app.models import ObjectStatus

    kinds = search_service.count_by_kind(project.id, owner=project.owner)
    # kinds = {"fastq": 12, "bam": 3, "vcf": 2, "reference": 7}

    recent_jobs = project_service.recent_jobs(project.id, limit=5)
    # recent_jobs = [{"type": "trim", "state": "succeeded", ...}, ...]

    return (
        f"Project: {project.name} (id: {project.id})\n"
        f"Objects: {sum(kinds.values())} total — "
        + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
        + f"\nRecent jobs: {len(recent_jobs)} completed\n"
        + f"Last activity: {project.updated_at.isoformat()}"
    )
```

This is appended as a system message to the Pi agent via the existing
`--system-prompt` flag, or sent as an initial `set_system_prompt` RPC command
after spawn. The content is static for the lifetime of the process — if objects
are added or jobs complete while the agent is running, the agent discovers them
through MCP tools.

### Update results.py

Remove the `answer_project_question` entry from `_APPLIERS` dict. The
`_apply_answer_project_question` function was already a no-op structurally
(nothing to merge into `facts`). Remove it and its test.

### Update main.py

Remove the `project_qa` router mount:

```python
# Before
api_router.include_router(project_qa.router)

# After
# project_qa router removed — everything goes through agent router
```

### Update config.py

No changes needed. The Agent settings (`PI_PATH`, `AGENT_RESPONSE_TIMEOUT`,
`AGENT_IDLE_TIMEOUT`, `agent_sessions_dir`) already exist.

## Testing

### Backend

- **Agent tests unchanged** — `test_agent_service.py` tests still pass. Remove
  `test_qa_handlers.py` and `test_qa_tools.py` (or mark as deleted).
- **Remove `test_every_slot_is_routed` update** — `TaskSlot.PROJECT_QA` is
  removed, so the test that asserts every slot has a route needs updating.
- **Remove `test_every_tool_is_documented` unaffected** — no tools were removed.

### Frontend

- Manual test in browser (localhost:5173 or worktree on 5273):
  - Open a project → only one "Agent" button in footer
  - Click it → Agent panel opens, SSE connects, shows "Ask the AI agent..."
  - Ask a question → agent responds with streaming text and tool calls
  - Close and reopen → panel shows "Resuming an earlier conversation..."
  - New session → clears everything, agent forgets
- No component tests exist in this repo (CLAUDE.md), so manual verification is
  the path.

## Risks

- **Session file format is Pi's, and Pi is pinned.** The Dockerfile pins the pi
  version because its protocol shapes are the contract the backend translates.
  Session format is part of that contract. No change to the existing pin.
- **Existing `project_conversations` documents are orphaned.** They're small and
  harmless, but if the user ever opens an old conversation in the future,
  nothing will read them. Acceptable for a single-user local tool.
- **The context injection adds latency to the first prompt.** Generating the
  project summary requires counting objects and querying recent jobs. This is a
  fast operation (sub-50ms) on the Mongo collections, but it runs synchronously
  before the first prompt is sent. Acceptable — the spawn itself takes longer.

## Deferred

- **Replaying prior turns into the drawer's scrollback on reopen.** The agent's
  *memory* survives a reopen (per the multi-turn sessions design); the *visible
  transcript* still does not. That remains a separate concern and is not worse
  than today.
- **A "quick ask" mode that hides tool calls.** The unified panel always shows
  the full agent UI. If users find the tool call display noisy for simple
  questions, we can add a collapse toggle later.
