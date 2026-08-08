# Agent multi-turn sessions

**Issue:** [#99](https://github.com/syntheticgio/bioflow/issues/99)
**Date:** 2026-08-08
**Status:** design

## The issue's premise is wrong, and that changes the work

[#99](https://github.com/syntheticgio/bioflow/issues/99) asks to "add the
ability for the agent to ask clarifying questions, refine answers based on
follow-up prompts, and maintain context across multiple turns." That
capability already exists.

`AgentService` keeps one long-lived `pi --mode rpc` subprocess per
`(profile, project)`, spawned lazily on the first `/ask` and kept until the
idle reaper or a crash takes it. Every subsequent `/ask` writes another
`prompt` line into that same process's stdin. Pi holds the conversation in
its own memory across those turns.

Verified against pi 0.84.1 in the `api` image on 2026-08-08, by driving the
RPC protocol directly. Two prompts written into one process, spaced so the
first run finished before the second arrived, produced:

- two `{"type":"response","command":"prompt","success":true}` acks
- two complete, separate run cycles: `agent_start` → `turn_start` →
  `message_start`/`message_end` → `turn_end` → `agent_end` → `agent_settled`
- an `agent_end` on the second run whose `messages` array carried **both**
  turns, user and assistant, accumulated

So the multi-turn machinery works. What is missing is everything that makes
it *durable and visible*: the context dies silently, the user cannot clear
it, and the UI never says which of those has happened.

**One caveat on the verification, stated because the design leans on it.**
No working model was available in the container during testing — pi has no
provider configured (`~/.pi/agent/settings.json` lists only the MCP adapter
package), and an attempt to point it at the local MLX server via
`OPENAI_BASE_URL` was ignored: pi routed to real OpenAI and returned 401. So
what is proven is the *turn structure* and pi's own message accumulation, not
an end-to-end "the model answered using turn 1's context." That gap is the
first task in the implementation plan (see [Risks](#risks)).

## What actually breaks today

**Context evaporates with no signal.** Three paths destroy the conversation,
all silent:

- `cleanup_idle` kills any non-busy process after `agent_idle_timeout`
  (1800s default)
- a pi crash, caught by `_watch_process`
- an `api` container restart, which takes every process with it

In all three the next `/ask` calls `get_or_create`, finds no live process,
and transparently spawns a fresh one. The user's next message lands in an
agent with amnesia. Nothing in the drawer says so.

**No way to clear a poisoned context.** There is a `DELETE
/projects/{id}/agent` endpoint, but nothing in the UI calls it. The panel's
only control is restart, which today kills the process and therefore also
its memory — the two operations are accidentally the same thing.

**The panel and the agent disagree after a reopen.** `AgentPanel` holds
`messages` in `useState`, so closing the drawer discards the visible
transcript while the subprocess keeps running with full memory. Reopening
shows an empty panel over an agent that remembers everything.

## Design

### Persist through pi's own session layer

`pi --help` documents a session system BioFlow explicitly opts out of:
`--session-id <id>` ("creating it if missing"), `--session-dir <dir>`,
`--continue`, `--resume`, `--fork`. The current spawn passes `--no-session`
(`agent_service.py`).

Drop `--no-session`; give each `(profile, project)` a stable session id and
a durable directory:

```
pi --mode rpc \
   --session-dir <settings.agent_sessions_dir> \
   --session-id bioflow-<profile_id>-<project_id> \
   --mcp-config <tmpfile>
```

Pi then owns persistence. A process reaped for idleness, killed by a crash,
or lost to a container restart comes back on the next `/ask` and reloads its
session file — the agent still remembers the earlier turns.

**Why this rather than storing transcripts in Mongo.** The alternative
(#97's original shape) means a new collection, a schema for turns and tool
calls, retention policy, compaction of long conversations, and code to
replay history into a fresh process. Pi already implements all of that.
Using it means BioFlow stores no transcripts at all, and it is the reason
this issue can absorb the durability half of #97 without inheriting its
design questions.

`agent_sessions_dir` follows the existing `*_dir` property pattern in
`config.py`, deriving from `BIOINFO_HOME` so it follows a relocated home:

```python
@property
def agent_sessions_dir(self) -> Path:
    return self.bioinfo_home / "agent-sessions"
```

Not under `tmp/`: unlike `ncbi_dir`, these files are not a rebuildable
cache.

The session id reuses the `(profile, project)` pair that
`AgentService._key` already keys on, serialized to a string. It must be
stable across respawns and distinct across both axes — sharing an id
between profiles would leak one user's conversation into another's, the same
boundary the per-process split already respects.

### Distinguish "restart" from "new session"

With persistence, the two operations stop being synonyms and both become
useful:

- **Restart** (exists) — kill and respawn the process, *keeping* the
  session file. For an agent that is stuck or wedged.
- **New session** (new) — stop the process, delete the session file, clear
  the panel. For a conversation that has gone wrong.

New session needs a backend endpoint that does the file deletion, since the
existing `DELETE` only stops the process. The panel gets a second button
beside restart.

### Make session state visible

The panel should distinguish "fresh agent, no history" from "resuming a
conversation you cannot see above." Precise wording is an implementation
detail; the requirement is that reopening a drawer onto a live session says
so rather than presenting a blank panel that implies a blank agent.

### Guard against prompt coalescing

Sending a second prompt while a run is in flight does **not** produce a
second run. `streamingBehavior: "steer"` folds it into the current one —
observed directly: two prompts written back-to-back yielded a single
`agent_start` and a single response. This is pi behaving as documented, and
`send_prompt` sends `steer` unconditionally on purpose (see the module
docstring's note on removing the is-it-streaming race).

The drawer's `messages` array assumes one assistant bubble per user bubble,
so a steered prompt would strand a user message with no reply attached to
it, permanently. The guard is to disable input while `isStreaming` is true.

**Correction, found while planning: this is already implemented.**
`AgentPanel.submit` returns early when `isStreaming`, and `AgentPanelInput`
disables both the textarea and the send button on the same flag — three
checkpoints. No work is needed here. It is recorded because the underlying
pi behaviour is real and non-obvious, and because a future change that makes
the input always-enabled would reintroduce the bug silently.

What *is* needed is making `isStreaming` trustworthy: see the display bugs
below, which prevent it from ever being set correctly.

### Fix the two display bugs that hide the conversation entirely

Found while planning, by reading `AgentPanel.tsx`. Both are blocking: with
either present, no multi-turn conversation is visible at all, so none of the
persistence work above can be verified in the UI.

**The user's prompt never renders.** `ask.onSuccess` builds the optimistic
user message as `{ role: "user", content: "" }` — the prompt text is
available as the mutation's variable but is not used. Every user bubble is
blank.

**Streamed assistant text has nowhere to land.** `ask.onSuccess` appends the
user message but no assistant placeholder. Every handler
(`onMessageDelta`, `onToolCall`, `onToolResult`) mutates
`updated[lastIdx]` only `if (lastIdx >= 0 && updated[lastIdx].isStreaming)`
— and the last message is the user's, which has no `isStreaming`. So every
token is silently discarded.

Fix both in the same place: append a user message carrying the prompt text,
plus an assistant message with `isStreaming: true`, so the handlers have a
target.

### Fix the silent failure in event translation

A provider failure arrives in `message.errorMessage` on `message_start`,
`message_end`, and `turn_end`. `_translate` handles none of those three
types — it reads `message_update`, `tool_execution_start`,
`tool_execution_end`, and `extension_error`. A hard failure therefore
reaches the drawer as `agent_start` followed by `done`, with no text and no
error: **a failed turn that renders as a successful empty response.**

Observed in the 401 run. The captured `turn_end` payload:

```json
{"type": "turn_end", "message": {"role": "assistant", "content": [],
 "api": "openai-responses", "provider": "openai",
 "stopReason": "error",
 "errorMessage": "OpenAI API error (401): {\"message\":\"Incorrect API key
 provided: dummy...\"}"}}
```

Fix: read `errorMessage` off `turn_end` and emit an `error` event.

This was going to be filed as its own issue, and is folded in here instead
for a reason that only appears once sessions persist: a failed turn that
looks successful now also gets **written into the session file**, so the
error compounds across restarts rather than dying with the process.

## Scope

**In:**

- pi session persistence per `(profile, project)`; drop `--no-session`
- `agent_sessions_dir` config property
- new-session endpoint (stop process + delete session file)
- "New session" button; session-state indication in the panel
- `errorMessage` translation on `turn_end`
- the two display bugs: render the user's prompt, and give streamed
  assistant text a bubble to land in

**Already implemented, no work needed** (recorded so it is not mistaken for
a gap): input is already disabled while streaming, so prompt coalescing is
already guarded.

**Out:**

- **Replaying prior turns into the drawer's scrollback.** The agent's
  *memory* survives a reopen (this issue); the *visible transcript* still
  does not. That remains #97's job, and the two are now cleanly separable.
- Retention or cleanup of old session files (see
  [Deferred](#deferred-deliberately))
- Branching, search, editing previous turns — out per the issue

## Testing

Backend via `pytest`; panel changes verified in the browser at
localhost:5273 (`./ops/worktree-up.sh`), since this repo has no
component-testing setup and expects none (CLAUDE.md).

In `backend/tests/services/test_agent_service.py`:

- spawn args carry `--session-id` and `--session-dir`, and no longer carry
  `--no-session`
- the session id is stable across respawns for one `(profile, project)`, and
  differs when either the profile or the project differs
- a `turn_end` carrying `errorMessage` produces an `error` event — **fails
  today**
- new-session teardown removes the session file

**Write the error test from the captured payload above, not from a
hand-built fixture.** CLAUDE.md's warning applies exactly here: every
existing agent test feeds `_translate` payloads already shaped the way the
code expects, which is precisely why this gap survived a green suite. A
fixture invented to match the implementation would pass while proving
nothing.

Rehydration cannot be unit-tested — it needs a real model and a real
process lifecycle. It is a manual check, and the first task in the plan:
prompt, force the process to be reaped, prompt again referencing the first
turn, confirm the agent still knows it.

## Risks

**The load-bearing assumption is that pi reloads a session on respawn.**
`--session-id` is accepted and pi confirms create-if-missing semantics —
observed: `Warning: No project session found with id 'bioflow-testproj';
creating a new session with that id.` But no session file was ever written
during testing, because every run failed at the provider before a turn
completed. The reload path was never exercised.

If pi does not reload as advertised, this approach collapses and the
fallback is Mongo-side persistence — #97's original shape, with the schema
and compaction questions that come with it. **Verify this first, before any
other implementation work.** It is cheap to check and it determines whether
the rest of the plan is worth writing.

**Session file format is pi's, and pi is pinned.** The Dockerfile pins the
pi version because its protocol shapes are the contract this module
translates. Session format is now part of that contract; an upgrade could
change it. This is an argument for the existing pin, not against the
approach — but a pi bump should re-check session compatibility.

## Deferred, deliberately

Session files accumulate under `BIOINFO_HOME` with no retention policy.
Left unsolved: single-user local tool, small text files, and a retention
mechanism would be speculative. Recorded here so it reads as a decision
rather than an oversight.
