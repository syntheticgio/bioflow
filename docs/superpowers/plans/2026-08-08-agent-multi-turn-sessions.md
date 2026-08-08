# Agent Multi-Turn Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the agent's conversation survive process death, give the user a way to clear it, and fix the display and error-reporting bugs that currently make multi-turn conversation invisible.

**Architecture:** Drop `--no-session` and let pi persist each `(profile, project)` conversation in its own session store, so a reaped or crashed subprocess reloads its memory on respawn. Add a "New session" endpoint and button that deletes the session file. Fix three separate silent failures found during design: a provider error that renders as a successful empty response, user messages that render blank, and streamed assistant text that has nowhere to land.

**Tech Stack:** Python 3.12 / FastAPI / Beanie / pytest (backend), React + TypeScript + TanStack Query (frontend), pi 0.84.1 RPC protocol over JSONL.

**Spec:** `docs/superpowers/specs/2026-08-08-agent-multi-turn-sessions-design.md`

---

## Critical context for the engineer

**Read the spec first.** This plan implements it, and the spec explains why
#99's stated premise ("add multi-turn") was wrong — multi-turn already
works, the conversation just dies silently and cannot be seen.

**Task 0 is a gate, not a formality.** The whole persistence approach rests
on pi reloading a session on respawn, which was never verified during design
(no model was configured in the container). If Task 0 fails, stop and
escalate — Tasks 1-4 are wasted work and the fallback is Mongo-side
persistence.

**Run backend tests from this worktree with:**

```bash
./backend/run-worktree-tests.sh tests/services/test_agent_service.py -q
```

Do **not** use `docker compose exec api python -m pytest` — from a worktree
that silently tests `main`'s code, not yours (CLAUDE.md).

**Frontend verification is manual, in a browser.** This repo has no
jsdom/testing-library setup and expects none. Bring the worktree stack up
with `./ops/worktree-up.sh` (UI on 5273) and look at it.

**The test file already has a fake-process harness.** `FakeProcess`,
`FakeReader`, `FakeWriter`, the `spawn` fixture, `collect()`, and
`make_service()` are all in `backend/tests/services/test_agent_service.py`.
Reuse them; do not build new ones.

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `backend/app/config.py` | `agent_sessions_dir` property | Modify |
| `backend/app/services/agent_service.py` | Session flags at spawn; session id; `errorMessage` translation; session-file deletion | Modify |
| `backend/app/api/v1/agent.py` | `POST /new-session` endpoint | Modify |
| `backend/tests/services/test_agent_service.py` | Unit tests for all backend behaviour | Modify |
| `backend/tests/api/test_agent.py` | Endpoint test for new-session | Modify |
| `frontend/src/api/client.ts` | `newAgentSession()` client call | Modify |
| `frontend/src/components/AgentPanel.tsx` | Message display fixes; New session button | Modify |

---

## Task 0: Verify pi actually reloads a session (GATE)

**This task writes no code.** It answers the one question that determines
whether the rest of the plan is valid.

**Files:** none.

- [ ] **Step 1: Configure a working model for pi in the api container**

pi has no provider configured. Check what is available:

```bash
docker compose -p biopipe exec -T api sh -c 'pi auth print-api-key --provider anthropic 2>&1 | head -3'
```

If no credential is available, ask the user which provider to use and how to
authenticate it. **Do not proceed without a model that can complete a
turn** — a run that fails at the provider never writes a session file, which
is exactly what blocked verification during design.

- [ ] **Step 2: Run one prompt with a session id and confirm a file appears**

```bash
docker compose -p biopipe exec -T api sh -c 'rm -rf /tmp/sess && mkdir -p /tmp/sess && { printf "%s\n" "{\"type\":\"prompt\",\"message\":\"My favorite enzyme is helicase. Reply with just: OK\",\"streamingBehavior\":\"steer\"}"; sleep 45; } | timeout 90 pi --mode rpc --session-dir /tmp/sess --session-id bioflow-test --no-tools >/dev/null 2>&1; find /tmp/sess -type f'
```

Expected: at least one file listed. If the directory is empty, the turn did
not complete — go back to Step 1.

- [ ] **Step 3: Start a SECOND process with the same id and confirm it remembers**

```bash
docker compose -p biopipe exec -T api sh -c '{ printf "%s\n" "{\"type\":\"prompt\",\"message\":\"What is my favorite enzyme? Answer in one word.\",\"streamingBehavior\":\"steer\"}"; sleep 45; } | timeout 90 pi --mode rpc --session-dir /tmp/sess --session-id bioflow-test --no-tools 2>/dev/null | grep -o "helicase" | head -1'
```

Expected: prints `helicase`. This is a *different process* answering from
the first one's context — the whole design in one command.

- [ ] **Step 4: Decide**

If Step 3 printed `helicase`, continue to Task 1.

If it did not: **stop and report to the user.** Record what pi did instead.
The fallback is Mongo-side transcript persistence (#97's original shape),
which is a different plan.

---

## Task 1: Add the `agent_sessions_dir` config property

**Files:**
- Modify: `backend/app/config.py` (the directory-layout properties, near `logs_dir` ~line 305)
- Test: `backend/tests/services/test_agent_service.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/services/test_agent_service.py`:

```python
class TestSessionsDir:
    def test_sessions_dir_derives_from_bioinfo_home(self):
        from app.config import settings

        assert settings.agent_sessions_dir == settings.bioinfo_home / "agent-sessions"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_agent_service.py::TestSessionsDir -q`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'agent_sessions_dir'`

- [ ] **Step 3: Add the property**

In `backend/app/config.py`, after the `logs_dir` property:

```python
    @property
    def agent_sessions_dir(self) -> Path:
        """Where pi keeps the agent's per-(profile, project) session files.

        Derived from BIOINFO_HOME so it follows a relocated home, and
        deliberately not under tmp/: unlike ncbi_dir, these files are the
        conversation itself, not a rebuildable cache.
        """
        return self.bioinfo_home / "agent-sessions"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_agent_service.py::TestSessionsDir -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/tests/services/test_agent_service.py
git commit -m "feat(agent): add agent_sessions_dir config property"
```

---

## Task 2: Spawn pi with a stable session id instead of --no-session

**Files:**
- Modify: `backend/app/services/agent_service.py:108` (the `cmd` list in `start()`)
- Modify: `backend/app/services/agent_service.py` (`AgentProcess.__init__`, `AgentService.get_or_create`)
- Test: `backend/tests/services/test_agent_service.py`

**Note:** the existing test `TestSpawn::test_spawns_with_rpc_flags_and_mcp_config`
asserts `calls[0][:5] == [..., "--no-session", "--mcp-config"]`. It will fail
once `--no-session` is gone. Updating it is part of Step 3.

- [ ] **Step 1: Write the failing tests**

Add a new class to `backend/tests/services/test_agent_service.py`:

```python
class TestSessionFlags:
    async def test_spawn_uses_session_id_and_dir_not_no_session(self, spawn, tmp_path):
        service = make_service(sessions_dir=tmp_path)
        await service.get_or_create("prof-1", "proj-1")
        calls, _ = spawn()
        cmd = calls[0]
        assert "--no-session" not in cmd
        assert "--session-dir" in cmd
        assert cmd[cmd.index("--session-dir") + 1] == str(tmp_path)
        assert "--session-id" in cmd
        assert cmd[cmd.index("--session-id") + 1] == "bioflow-prof-1-proj-1"
        await service.stop_agent("prof-1", "proj-1")

    async def test_session_id_is_stable_across_respawns(self, spawn, tmp_path):
        service = make_service(sessions_dir=tmp_path)
        await service.get_or_create("prof-1", "proj-1")
        await service.stop_agent("prof-1", "proj-1")
        await service.get_or_create("prof-1", "proj-1")
        calls, _ = spawn()
        first = calls[0][calls[0].index("--session-id") + 1]
        second = calls[1][calls[1].index("--session-id") + 1]
        assert first == second
        await service.stop_agent("prof-1", "proj-1")

    async def test_session_id_differs_by_profile_and_by_project(self, spawn, tmp_path):
        service = make_service(sessions_dir=tmp_path)
        await service.get_or_create("prof-A", "proj-1")
        await service.get_or_create("prof-B", "proj-1")
        await service.get_or_create("prof-A", "proj-2")
        calls, _ = spawn()
        ids = [c[c.index("--session-id") + 1] for c in calls]
        assert len(set(ids)) == 3, f"session ids must be distinct, got {ids}"
        for prof, proj in (("prof-A", "proj-1"), ("prof-B", "proj-1"), ("prof-A", "proj-2")):
            await service.stop_agent(prof, proj)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/services/test_agent_service.py::TestSessionFlags -q`
Expected: FAIL — `make_service()` does not accept `sessions_dir`.

- [ ] **Step 3: Implement**

In `backend/tests/services/test_agent_service.py`, update the helper so the
new kwarg has a default:

```python
def make_service(**kwargs) -> AgentService:
    kwargs.setdefault("pi_path", "/usr/local/bin/pi")
    kwargs.setdefault("response_timeout", 60)
    kwargs.setdefault("idle_timeout", 3600)
    return AgentService(**kwargs)
```

(no change needed if `sessions_dir` is passed through `**kwargs`; the
`AgentService.__init__` change below is what accepts it.)

In the same file, fix the now-stale assertion in
`TestSpawn::test_spawns_with_rpc_flags_and_mcp_config`. Replace:

```python
        expected_head = [
            "/usr/local/bin/pi",
            "--mode",
            "rpc",
            "--no-session",
            "--mcp-config",
        ]
        assert calls[0][:5] == expected_head
        # The temp config file exists and carries the profile URL.
        config = read_config(calls[0][5])
```

with a position-independent form (the flag order is no longer fixed):

```python
        assert calls[0][:3] == ["/usr/local/bin/pi", "--mode", "rpc"]
        assert "--mcp-config" in calls[0]
        # The temp config file exists and carries the profile URL.
        config = read_config(calls[0][calls[0].index("--mcp-config") + 1])
```

Note the last line of that test also references the config path
positionally:

```python
        assert not config_file_exists(calls[0][5])
```

Replace it with:

```python
        assert not config_file_exists(calls[0][calls[0].index("--mcp-config") + 1])
```

In `backend/app/services/agent_service.py`, add the session id helper at
module level, next to `_tool_call_payload`:

```python
def session_id_for(profile_id: str, project_id: str) -> str:
    """The pi session id for one (profile, project) pair.

    Stable across respawns -- that is what lets a reaped or crashed process
    reload the conversation -- and distinct across both axes, since sharing
    an id between profiles would leak one user's conversation into another's.
    """
    return f"bioflow-{profile_id}-{project_id}"
```

In `AgentProcess.__init__`, add a `sessions_dir` parameter. Change the
signature to include it after `pi_path`:

```python
    def __init__(
        self,
        *,
        profile_id: str,
        project_id: str,
        mcp_config: dict,
        pi_path: str,
        response_timeout: float,
        sessions_dir: Path,
        system_prompt: str | None = None,
    ) -> None:
```

and store it in the body alongside the other assignments:

```python
        self._sessions_dir = sessions_dir
```

Add the import at the top of the file:

```python
from pathlib import Path
```

In `AgentProcess.start()`, replace the `cmd` construction at line 108:

```python
        cmd = [self._pi_path, "--mode", "rpc", "--no-session", "--mcp-config", path]
```

with:

```python
        # Sessions replace --no-session: pi persists the conversation itself,
        # keyed by (profile, project), so a process lost to the idle reaper,
        # a crash, or an api restart reloads its memory on respawn.
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            self._pi_path,
            "--mode",
            "rpc",
            "--session-dir",
            str(self._sessions_dir),
            "--session-id",
            session_id_for(self.profile_id, self.project_id),
            "--mcp-config",
            path,
        ]
```

In `AgentService.__init__`, accept and store the directory:

```python
    def __init__(
        self,
        *,
        pi_path: str | None = None,
        mcp_base_url: str = _MCP_BASE_URL,
        extra_mcp_servers: dict | None = None,
        response_timeout: float | None = None,
        idle_timeout: float | None = None,
        sessions_dir: Path | None = None,
    ) -> None:
```

with, in the body:

```python
        self._sessions_dir = sessions_dir or settings.agent_sessions_dir
```

In `AgentService.get_or_create`, pass it into the constructor:

```python
        proc = AgentProcess(
            profile_id=profile_id,
            project_id=str(project_id),
            mcp_config=self._build_mcp_config(profile_id),
            pi_path=self._pi_path,
            response_timeout=self._response_timeout,
            sessions_dir=self._sessions_dir,
            system_prompt=system_prompt,
        )
```

- [ ] **Step 4: Run the full agent test file**

Run: `./backend/run-worktree-tests.sh tests/services/test_agent_service.py -q`
Expected: PASS, including the updated `TestSpawn` test.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_service.py backend/tests/services/test_agent_service.py
git commit -m "feat(agent): persist conversations via pi sessions per (profile, project)"
```

---

## Task 3: Surface provider errors that currently look like success

A provider failure arrives in `message.errorMessage` on `turn_end`.
`_translate` does not handle `turn_end` at all, so the drawer sees
`agent_start` → `done` with no text: a hard failure rendering as a
successful empty response.

**The test payload below is a real capture** from a pi 0.84.1 run that
401'd — not a hand-built fixture. This matters: every existing test in this
file feeds `_translate` payloads already shaped the way the code expects,
which is exactly why this gap survived a green suite.

**Files:**
- Modify: `backend/app/services/agent_service.py` (`_translate`, ~line 283)
- Test: `backend/tests/services/test_agent_service.py`

- [ ] **Step 1: Write the failing test**

```python
class TestTurnEndErrors:
    async def test_turn_end_error_message_becomes_an_error_event(self, spawn):
        service = make_service()
        proc = await service.get_or_create("p", "j")
        _, fake = spawn()
        # Real capture from a pi 0.84.1 run against a bad API key.
        fake.stdout.feed(
            {
                "type": "turn_end",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "api": "openai-responses",
                    "provider": "openai",
                    "model": "gemma-4-E2B-it-Q6_K",
                    "stopReason": "error",
                    "errorMessage": 'OpenAI API error (401): {"message":"Incorrect API key provided: dummy."}',
                },
            }
        )
        events = await collect(proc, 1)
        assert events[0].type == "error"
        assert "401" in events[0].data["message"]
        await service.stop_agent("p", "j")

    async def test_turn_end_without_an_error_emits_nothing(self, spawn):
        """A normal turn must stay silent -- agent_settled already ends it."""
        service = make_service()
        proc = await service.get_or_create("p", "j")
        _, fake = spawn()
        fake.stdout.feed(
            {"type": "turn_end", "message": {"role": "assistant", "content": [], "stopReason": "end_turn"}}
        )
        fake.stdout.feed({"type": "agent_settled"})
        events = await collect(proc, 1)
        assert events[0].type == "done", f"expected only done, got {events[0].type}"
        await service.stop_agent("p", "j")
```

- [ ] **Step 2: Run tests to verify the first fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_agent_service.py::TestTurnEndErrors -q`
Expected: `test_turn_end_error_message_becomes_an_error_event` FAILS (it
hangs on `collect` or times out — no event is ever emitted, which is the bug).
`test_turn_end_without_an_error_emits_nothing` passes already.

- [ ] **Step 3: Implement**

In `_translate`, add a branch before the `extension_error` branch:

```python
        elif etype == "turn_end":
            # A provider failure lands here, not in an error event of its own:
            # without this the drawer sees agent_start -> done with no text and
            # reads a hard failure as a successful empty response.
            message = payload.get("message") or {}
            error_message = message.get("errorMessage")
            if error_message:
                await self._put_event("error", {"message": error_message})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_agent_service.py::TestTurnEndErrors -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_service.py backend/tests/services/test_agent_service.py
git commit -m "fix(agent): surface provider errors from turn_end instead of reporting success"
```

---

## Task 4: Add the new-session endpoint

Stops the process *and* deletes its session file. This is what makes
"New session" different from "Restart", which now keeps the memory.

**Files:**
- Modify: `backend/app/services/agent_service.py` (`AgentService`)
- Modify: `backend/app/api/v1/agent.py`
- Test: `backend/tests/services/test_agent_service.py`, `backend/tests/api/test_agent.py`

- [ ] **Step 1: Write the failing service test**

```python
class TestNewSession:
    async def test_new_session_stops_process_and_deletes_session_files(self, spawn, tmp_path):
        service = make_service(sessions_dir=tmp_path)
        await service.get_or_create("prof-1", "proj-1")
        # Simulate the session files pi would have written for this pair.
        sid = "bioflow-prof-1-proj-1"
        (tmp_path / f"{sid}.jsonl").write_text("{}\n")
        (tmp_path / f"{sid}.json").write_text("{}\n")
        (tmp_path / "bioflow-other-proj-9.jsonl").write_text("{}\n")

        await service.new_session("prof-1", "proj-1")

        assert service.get("prof-1", "proj-1") is None
        assert not (tmp_path / f"{sid}.jsonl").exists()
        assert not (tmp_path / f"{sid}.json").exists()
        # Another pair's session must survive.
        assert (tmp_path / "bioflow-other-proj-9.jsonl").exists()

    async def test_new_session_is_safe_when_nothing_exists(self, tmp_path):
        service = make_service(sessions_dir=tmp_path)
        await service.new_session("nobody", "nothing")  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/services/test_agent_service.py::TestNewSession -q`
Expected: FAIL with `AttributeError: 'AgentService' object has no attribute 'new_session'`

- [ ] **Step 3: Implement the service method**

In `AgentService`, after `restart_agent`:

```python
    async def new_session(self, profile_id: str, project_id: str) -> None:
        """Stop the process and forget the conversation.

        The counterpart to restart_agent, which keeps the session file: this
        is the only way to clear a context that has gone wrong. Matches every
        file pi may have written for this id (it keeps the transcript and its
        metadata under the same stem).
        """
        await self.stop_agent(profile_id, str(project_id))
        sid = session_id_for(profile_id, str(project_id))
        if not self._sessions_dir.exists():
            return
        for path in self._sessions_dir.glob(f"{sid}*"):
            try:
                path.unlink()
            except OSError as e:
                log.warning("agent_session_unlink_failed", path=str(path), error=str(e))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_agent_service.py::TestNewSession -q`
Expected: 2 passed

- [ ] **Step 5: Write the failing endpoint test**

In `backend/tests/api/test_agent.py`, inside `class TestLifecycle`:

```python
    async def test_new_session_stops_the_agent(self, client, two_profiles, spawn):
        project = await _project(two_profiles["a"].owner_id())
        headers = two_profiles["a_headers"]
        url = f"/api/v1/projects/{project.id}/agent"
        await client.post(url + "/ask", json={"message": "hi"}, headers=headers)
        assert agent_service.get(str(two_profiles["a"].id), str(project.id)) is not None

        response = await client.post(url + "/new-session", headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == "cleared"
        assert agent_service.get(str(two_profiles["a"].id), str(project.id)) is None
```

This mirrors `TestLifecycle::test_delete_stops_the_agent` exactly — same
`_project(owner_id)` helper, same `two_profiles` dict keys (`"a"` and
`"a_headers"`), same url-building. Note `two_profiles` is a dict, not a
list.

- [ ] **Step 6: Run the endpoint test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/api/test_agent.py::TestLifecycle -q`
Expected: FAIL with 404 (route does not exist)

- [ ] **Step 7: Add the endpoint**

In `backend/app/api/v1/agent.py`, after `restart_agent`:

```python
@router.post("/new-session", response_model=AgentAskResponse)
async def new_agent_session(
    project_id: PydanticObjectId, profile_id: ProfileIdDep
) -> AgentAskResponse:
    """Clear the conversation: stop the process and delete its session file.

    Distinct from /restart, which respawns against the same session and so
    keeps the agent's memory.
    """
    await agent_service.new_session(profile_id, str(project_id))
    return AgentAskResponse(status="cleared")
```

- [ ] **Step 8: Run both test files**

Run: `./backend/run-worktree-tests.sh tests/api/test_agent.py tests/services/test_agent_service.py -q`
Expected: all pass

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/agent_service.py backend/app/api/v1/agent.py backend/tests/services/test_agent_service.py backend/tests/api/test_agent.py
git commit -m "feat(agent): add new-session endpoint that clears the conversation"
```

---

## Task 5: Fix the message display bugs

Two bugs found while reading `AgentPanel.tsx`. Both are blocking for this
issue: with either one present, a multi-turn conversation cannot be seen at
all, so none of Tasks 1-4 are verifiable in the UI.

1. `ask.onSuccess` builds the user message with `content: ""` — **the
   user's own text never renders.**
2. It never appends an assistant placeholder with `isStreaming: true`. Every
   delta handler bails on `updated[lastIdx].isStreaming`, so **streamed
   assistant text has nowhere to land.**

**Files:**
- Modify: `frontend/src/components/AgentPanel.tsx` (the `ask` mutation, ~line 111)

- [ ] **Step 1: Fix both in the ask mutation**

Replace the `ask` mutation:

```tsx
  const ask = useMutation({
    mutationFn: (q: string) => api.askAgent(projectId, q),
    onSuccess: () => {
      // Optimistic: add the user message immediately
      const userMsg: Message = { id: crypto.randomUUID(), role: "user", content: "" };
      setMessages((prev) => [...prev, userMsg]);
      setIsStreaming(true);
      streamingContentRef.current = "";
      currentToolCallsRef.current = [];
    },
  });
```

with a version that keeps the prompt text and opens an assistant bubble for
the deltas to accumulate into:

```tsx
  const ask = useMutation({
    mutationFn: (q: string) => api.askAgent(projectId, q),
    onSuccess: (_data, q) => {
      // Optimistic: the user's message, plus an empty assistant bubble for
      // the stream to fill. The delta/tool handlers all target the last
      // message when it is isStreaming, so without this placeholder every
      // token is silently dropped.
      const userMsg: Message = { id: crypto.randomUUID(), role: "user", content: q };
      const assistantMsg: Message = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: "",
        isStreaming: true,
      };
      setMessages((prev) => [...prev, userMsg, assistantMsg]);
      setIsStreaming(true);
      streamingContentRef.current = "";
      currentToolCallsRef.current = [];
    },
  });
```

`useMutation`'s `onSuccess` receives `(data, variables)`, so `q` is the
prompt string passed to `ask.mutate(q)` in `submit`.

- [ ] **Step 2: Verify in the browser**

```bash
./ops/worktree-up.sh
```

Open http://localhost:5273, open a project, open the agent drawer, send a
message.

Expected: your message appears with its text (not blank), and the assistant's
reply streams into a bubble beneath it. Before this fix, both were empty.

If the agent has no model configured, you will instead see an error badge —
which is Task 3 working. That still verifies the user message renders.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/AgentPanel.tsx
git commit -m "fix(agent): render the user's prompt and stream replies into a bubble"
```

---

## Task 6: Add the New session button

**Files:**
- Modify: `frontend/src/api/client.ts` (~line 1129, beside `stopAgent`)
- Modify: `frontend/src/components/AgentPanel.tsx`

- [ ] **Step 1: Add the client call**

In `frontend/src/api/client.ts`, after `stopAgent`:

```ts
  newAgentSession: (projectId: string) =>
    request<{ status: string }>(`/projects/${projectId}/agent/new-session`, {
      method: "POST",
    }),
```

Match the surrounding style — if neighbouring POSTs pass a body or headers,
copy that shape.

- [ ] **Step 2: Add the mutation**

In `AgentPanel.tsx`, after the `restart` mutation:

```tsx
  const newSession = useMutation({
    mutationFn: () => api.newAgentSession(projectId),
    onSuccess: () => {
      setMessages([]);
      setError(null);
      setIsStreaming(false);
      streamingContentRef.current = "";
      currentToolCallsRef.current = [];
    },
  });
```

- [ ] **Step 3: Add the button**

In the `queue-panel-head` block, before the restart button. Note the restart
button currently carries `style={{ marginLeft: "auto" }}` — move that to this
new button so it stays the start of the right-hand group:

```tsx
          <button
            type="button"
            className="icon-btn"
            onClick={() => newSession.mutate()}
            title="New session (clears the agent's memory)"
            style={{ marginLeft: "auto" }}
            disabled={isStreaming}
          >
            🗑
          </button>
```

and remove `style={{ marginLeft: "auto" }}` from the restart button, leaving:

```tsx
          <button
            type="button"
            className="icon-btn"
            onClick={() => restart.mutate()}
            title="Restart agent (keeps the conversation)"
          >
            🔄
          </button>
```

Note the restart button's `title` also changes — restart and new-session are
no longer the same operation, and the tooltips are the only place the
difference is visible.

- [ ] **Step 4: Verify in the browser**

At http://localhost:5273 with the drawer open:

1. Send a message, wait for a reply.
2. Click 🔄 (restart). Ask a follow-up referring to the earlier message
   ("what did I just ask you?"). Expected: **it remembers** — the session
   file survived.
3. Click 🗑 (new session). Ask the same follow-up. Expected: **it does not
   remember** — the session file is gone.

This is the end-to-end proof of the whole feature. If step 2 does not
remember, the session flags from Task 2 are not reaching pi.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/client.ts frontend/src/components/AgentPanel.tsx
git commit -m "feat(agent): add a New session button that clears the conversation"
```

---

## Task 7: Tell the user when the agent is resuming

Without this, reopening the drawer shows an empty panel over an agent that
remembers everything — the UI and the agent disagree, which is the confusion
the spec set out to remove.

`GET /events` already emits `agent_status` with `{"running": bool}` as its
first event, and `useAgentSSE` already listens for it — but it currently
folds that into `connected`, discarding the distinction between "connected
to a live agent" and "connected, nothing running yet".

**Files:**
- Modify: `frontend/src/components/AgentPanel.tsx`

- [ ] **Step 1: Track whether we attached to a running agent**

In `AgentPanel.tsx`, add state beside the others:

```tsx
  const [resumed, setResumed] = useState(false);
```

In the `useAgentSSE` options, extend `onConnectionChange`:

```tsx
    onConnectionChange: (isConnected) => {
      if (!isConnected) {
        setError("Disconnected from agent. Click restart to reconnect.");
      } else {
        setError(null);
        // Connected to an agent that was already running: it has a
        // conversation we are not showing (scrollback is not restored -- see
        // issue #97), so say so rather than implying a blank agent.
        if (messages.length === 0) setResumed(true);
      }
    },
```

Clear it wherever the conversation is cleared — add `setResumed(false)` to
the `newSession` mutation's `onSuccess`, and to `ask`'s `onSuccess`.

- [ ] **Step 2: Show it in the empty state**

Replace the empty-state block:

```tsx
          {messages.length === 0 && !isStreaming ? (
            <div className="queue-empty">
              Ask the AI agent about your project data. It can run QC, trim, align, and
              assemble pipelines, inspect jobs, and answer questions about your files.
            </div>
          ) : (
```

with:

```tsx
          {messages.length === 0 && !isStreaming ? (
            <div className="queue-empty">
              {resumed ? (
                <>
                  Resuming an earlier conversation — the agent still remembers it,
                  but the messages above are not shown. Ask a follow-up, or start
                  over with New session.
                </>
              ) : (
                <>
                  Ask the AI agent about your project data. It can run QC, trim, align,
                  and assemble pipelines, inspect jobs, and answer questions about your
                  files.
                </>
              )}
            </div>
          ) : (
```

- [ ] **Step 3: Verify in the browser**

At http://localhost:5273: send a message, close the drawer, reopen it.

Expected: the "Resuming an earlier conversation" text, not the generic
first-run blurb. Click 🗑, and the generic blurb returns.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/AgentPanel.tsx
git commit -m "feat(agent): say when the drawer is resuming an existing conversation"
```

---

## Task 8: Full suite, docs, and merge

- [ ] **Step 1: Run the entire backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`

Expected: all pass. **Read the count**, not just the exit code (CLAUDE.md).
Compare against the pre-change baseline — roughly 1872 passing. If a handful
of unrelated DB-touching tests fail, re-run: that pattern is two test runs
sharing Mongo, not your change.

- [ ] **Step 2: Update the spec's open risk**

The spec names pi session reload as an unverified, load-bearing assumption.
Task 0 settled it. In
`docs/superpowers/specs/2026-08-08-agent-multi-turn-sessions-design.md`,
under `## Risks`, replace the first paragraph's "the reload path was never
exercised" with what Task 0 actually observed, including the date.

- [ ] **Step 3: Check for a TODO.md entry**

```bash
grep -n -i "agent\|multi-turn\|session" docs/TODO.md
```

If an entry covers this work, append ` — FIXED` to its heading, note what
shipped and what differed from the plan, and move the whole entry to
`docs/TODO-done.md` (CLAUDE.md). If there is no entry, skip this step.

- [ ] **Step 4: Commit any doc changes**

```bash
git add docs/
git commit -m "docs(agent): record the verified session-reload behaviour"
```

- [ ] **Step 5: Merge and push**

Per CLAUDE.md: once the suite is green and `main` is clean, merge and push
without asking.

```bash
git checkout main && git pull && git merge claude/issue-99-brainstorm-20b9aa
```

If `main` moved, re-run the suite after merging before pushing.

```bash
./backend/run-worktree-tests.sh tests/ -q && git push origin main
```

- [ ] **Step 6: Update the issue**

Comment on [#99](https://github.com/syntheticgio/bioflow/issues/99) with what
shipped and what Task 0 found, and set the label to reflect completion.
Note explicitly that scrollback restoration remains open in
[#97](https://github.com/syntheticgio/bioflow/issues/97).

---

## Deferred, deliberately

- **Scrollback restoration on drawer reopen** — the agent's memory survives,
  the visible transcript does not. That is #97's job, and Task 7 makes the
  seam explicit to the user rather than hiding it.
- **Session file retention** — files accumulate under `BIOINFO_HOME` with no
  cleanup. Single-user local tool, small text files; a retention mechanism
  would be speculative.
- **Prompt coalescing guard** — the spec listed this as work, but reading
  the code during planning showed it is already implemented:
  `AgentPanel.submit` returns early when `isStreaming`, and
  `AgentPanelInput` disables both the textarea and the send button on the
  same flag. No change needed. Task 5's assistant-placeholder fix is what
  makes `isStreaming` reliable enough for that guard to be trusted.
