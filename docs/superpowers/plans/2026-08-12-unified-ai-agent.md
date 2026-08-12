# Unified AI Agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) for tracking.

**Goal:** Merge the ASK (Project Q&A) and AI Agent features into a single unified
Agent panel, fixing the broken SSE connection (#289) and adding project context
injection (#290). Remove all ASK-specific backend infrastructure.

**Reference:** `docs/superpowers/specs/2026-08-12-unified-ai-agent-design.md`
— read it before starting. This plan implements it and does not repeat its
rationale except where a step needs it to make a call correctly.

**Tech Stack:** FastAPI, asyncio, Python 3.12, pytest + pytest-asyncio
(backend); React, TanStack Query, TypeScript, SSE (frontend). Pi v0.84+ as the
agent runtime.

---

## Before you start

### Run the test baseline

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner
./backend/run-worktree-tests.sh tests/ -q
```

Record the baseline count before touching anything. If the baseline is red,
stop and report rather than starting against it.

### Work in the worktree

All file changes below are relative to the worktree root:
`/Users/syntheticgio/Programming/worktrees/brainstorm-ai-agent`

---

## Task 0: Fix the SSE reconnection loop in useAgentSSE.ts (fixes #289)

**Files:**
- `frontend/src/hooks/useAgentSSE.ts`

**What:** The hook creates a new EventSource on every render because inline
arrow function callbacks from `AgentPanel` cause the `connect` function's
dependency array to change every render. Fix by storing callbacks in refs.

**Changes:**
1. Add `useRef` wrappers for all 7 callback props at the top of the hook body
2. Add `useEffect` hooks to sync each ref with its current prop value
3. Update all event handlers to use `Ref.current(...)` instead of captured closures
4. Shrink `connect`'s dependency array to `[profileId, projectId]` only
5. The `useEffect` that calls `connect` now depends on `[profileId, connect]` —
   since `connect` is stable, this effect runs only on mount/unmount and
   profileId changes

**Verification:**
- Open a project, open Agent panel → SSE connects once (check browser Network tab)
- Navigate between projects → SSE reconnects with new projectId
- Close panel → SSE disconnects
- Reopen → SSE reconnects once

---

## Task 1: Remove ProjectQaDrawer.tsx

**Files:**
- `frontend/src/components/ProjectQaDrawer.tsx` — delete
- `frontend/src/components/Footer.tsx` — update

**Changes to Footer.tsx:**
1. Remove `import { ProjectQaDrawer } from "./ProjectQaDrawer"`
2. Remove `qaOpen` state variable
3. Remove `{qaOpen && <ProjectQaDrawer ...>}` rendering block
4. Remove the "Ask" button (the one with `BioIcon name="ask"`)
5. Keep the "Agent" button unchanged

**Verification:**
- Footer shows only one AI-related button: "Agent"
- Clicking it opens the Agent panel
- No "Ask" button exists

---

## Task 2: Remove Project Q&A backend

**Files to delete:**
- `backend/app/api/v1/project_qa.py` — entire file
- `backend/app/services/ai/qa.py` — entire file
- `backend/app/services/ai/qa_tools.py` — entire file
- `backend/app/services/ai/qa_compaction.py` — entire file
- `backend/app/queue/qa_handlers.py` — entire file

**Files to modify:**
- `backend/app/api/v1/__init__.py` — remove `project_qa` import and router mount
- `backend/app/main.py` — remove `project_qa` router mount (if mounted here instead of in __init__.py)
- `backend/app/models/ai.py` — remove `PROJECT_QA = "project_qa"` from `TaskSlot` enum and its label from `_SLOT_LABELS`
- `backend/app/services/ai/__init__.py` — remove `qa` and `qa_compaction` imports
- `backend/app/queue/__init__.py` — remove `qa_handlers` import
- `backend/app/services/ai/complete.py` — remove `qa` import if present

**Verification:**
- `make test` passes
- `GET /api/v1/projects/{id}/qa/ask` returns 404
- `TaskSlot.PROJECT_QA` no longer exists in the enum

---

## Task 3: Remove ProjectConversation model

**Files:**
- `backend/app/models/conversation.py` — delete entire file (or keep as stub if other code imports it)

**Check for imports:** Search for imports of `ProjectConversation` and
`ConversationTurn` across the codebase. The Agent's SSE stream in `agent.py`
currently writes to `ProjectConversation` — this needs to be removed.

**Changes to agent.py:**
Remove the `_append_turn` calls in the SSE stream generator. The Pi agent's own
session file is now the source of truth for conversation persistence. The
visible transcript is reconstructed from the SSE stream's in-memory accumulation
during the current drawer session — on reopen, the panel shows "Resuming an
earlier conversation" because the agent remembers but the scrollback is lost
(per existing design, this is a known limitation).

**Changes to the API:**
- Remove `GET /projects/{project_id}/agent/conversation` endpoint
- Remove `POST /projects/{project_id}/agent/conversation/turns` endpoint
- Remove `DELETE /projects/{project_id}/agent/conversation` endpoint
- Remove `GET /conversation` and related models from `agent.py`

**Verification:**
- Agent still works: ask a question, get a streamed response
- Close and reopen the panel — shows "Resuming an earlier conversation"
- No writes to `project_conversations` collection in Mongo

---

## Task 4: Remove results.py applier for answer_project_question

**Files:**
- `backend/app/services/results.py` — remove `answer_project_question` from `_APPLIERS`

**Changes:**
1. Find the `_apply_answer_project_question` function and remove it
2. Remove the `"answer_project_question"` key from the `_APPLIERS` dict
3. Remove the `qa.answered` event publishing from the applier

**Verification:**
- `make test` passes
- The `_APPLIERS` exhaustiveness test still passes (it checks all handler names have appliers)

---

## Task 5: Add project context injection (fixes #290)

**Files:**
- `backend/app/api/v1/agent.py` — modify `_system_prompt` or add a new helper

**What:** Before the agent processes its first prompt, inject a summary of the
project's contents into the system prompt. This lets the agent answer the first
question without burning a tool call on discovery.

**Changes:**
1. Add a helper function `_build_project_context(project) -> str` in `agent.py`
   that counts objects by kind and fetches recent jobs
2. Append this to the system prompt in `_system_prompt()` or inject it as a
   separate system message after spawn
3. The context is generated once per process spawn (lazy, on first `/ask`)

```python
def _build_project_context(project) -> str:
    from app.services import search_service, project_service
    from app.models import ObjectStatus

    kinds = search_service.count_by_kind(project.id, owner=project.owner)
    recent = project_service.recent_jobs(project.id, limit=5)
    
    lines = [
        f"Project: {project.name} (id: {project.id})",
        f"Objects: {sum(kinds.values())} total — "
        + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items())),
    ]
    if recent:
        lines.append(f"Recent jobs: {len(recent)} completed")
    return "\n".join(lines)
```

**Verification:**
- First question in a session: agent knows the project shape without calling
  a discovery tool
- Second question: agent still uses MCP tools for specific queries
- Context is static for the process lifetime (doesn't update mid-session)

---

## Task 6: Clean up tests

**Files:**
- `backend/tests/queue/test_qa_handlers.py` — delete
- `backend/tests/services/test_qa_tools.py` — delete (or rename to .bak)
- `backend/tests/services/test_qa_compaction.py` — delete
- `backend/tests/api/test_project_qa.py` — delete
- `backend/tests/test_compose_agent_servers.py` — check if this needs updates

**Modify existing tests:**
- `backend/tests/api/test_agent.py` — remove any tests that reference
  `ProjectConversation` or conversation endpoints
- `backend/tests/services/test_agent_service.py` — remove any tests that
  reference conversation persistence
- `backend/tests/test_every_slot_is_routed.py` — remove `PROJECT_QA` from the
  expected slots list

**Verification:**
- `make test` passes with the same count as baseline (minus deleted tests)
- No test imports from deleted modules

---

## Task 7: Update TODO-done.md

**Files:**
- `docs/TODO-done.md`

Add entries for #289 and #290, noting that they were resolved by the unified
agent merge. Follow the established format: append `— FIXED` to the heading,
write a short note, and move the entry to `docs/TODO-done.md`.

---

## Manual verification checklist

1. Open a project → footer shows one "Agent" button (no "Ask")
2. Click Agent → panel opens, SSE connects, shows "Ask the AI agent..."
3. Type a question → agent responds with streaming text and tool calls
4. Close and reopen → panel shows "Resuming an earlier conversation..."
5. Click "New session" → clears everything, agent forgets
6. Click "Restart" → agent restarts, keeps memory
7. Check browser Network tab → SSE connects once per project open
