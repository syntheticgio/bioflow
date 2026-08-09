# Custom system prompts for the in-app agent

Issue: [#98](https://github.com/syntheticgio/bioflow/issues/98)
Parent: [#30](https://github.com/syntheticgio/bioflow/issues/30)
Date: 2026-08-08

## Problem

`_system_prompt()` in `backend/app/api/v1/agent.py` builds a fixed string from
the project name and id. A user who wants the agent to behave differently in
one project -- answer tersely, always cite the tool it used, assume a
particular organism -- has nowhere to say so.

## Decisions

Four design questions were settled during brainstorming. Each had a cheaper or
more flexible alternative that was rejected for a specific reason, recorded
here so the next person does not re-litigate it.

### The custom prompt augments the default; it does not replace it

The default block is *infrastructure grounding*, not personality: it tells the
agent it has MCP tools, which project it is in, and to prefer running a tool
over describing one. That text changes as the codebase changes.

If a user could replace it, their copy would freeze at whatever the grounding
said on the day they edited it -- and a later change to the tool surface would
leave them with a prompt that describes an agent that no longer exists. Worse,
a user writing "always answer in bullet points" would silently discard tool
awareness entirely.

So the spawn-time prompt is always:

```
<default grounding block>

Additional instructions from the user:
<custom text>
```

with the second half omitted entirely when the custom text is empty. A
"replace vs. append" toggle was considered and dropped as YAGNI.

### Storage: one typed field on `Project`

`agent_system_prompt: str = ""` on `app/models/project.py`.

Rejected: stuffing it into the existing `metadata: dict`. That needs no model
change, which is its only advantage. It is an untyped bag -- no validation, no
single place to enforce the length limit, and nothing to grep for but a string
literal. That is the same silent-key shape CLAUDE.md's "hand-maintained
registries" section warns about.

Rejected: a separate collection. Only warranted for versioning or per-profile
prompts, both explicitly out of scope in the issue.

Beanie fills a missing field with its default on read, so existing project
documents need no migration.

**Per-project only.** A global cross-project default layer was considered and
deliberately left out; if it is ever wanted, the concatenation order is
`default grounding -> global -> project`.

### A saved edit takes effect on the next `/ask`, by respawning

This is the part the issue does not mention and the part most likely to be got
wrong.

`system_prompt` is read exactly once, at spawn: `AgentProcess.start()` puts it
in the pi argv. `AgentService.get_or_create()` returns any live process
untouched, so today a changed prompt would sit inert until the idle reaper
happened to kill the process -- minutes later, invisibly, with the agent
answering under its old instructions the whole time.

Fix: `AgentProcess` records the prompt it was spawned with.
`get_or_create()` compares the incoming prompt against the live process's and,
when they differ, stops and respawns before returning.

The user therefore edits, hits send, and gets the new behavior. The cost is
that the in-flight conversation is lost on the first prompt after an edit --
correct (a system prompt cannot be changed mid-conversation) but it must be
stated in the UI rather than left as a surprise.

Rejected: restarting immediately on save (saving text in one panel silently
kills a conversation in another). Rejected: doing nothing and showing a
"restart to apply" hint (cheapest, most annoying).

#### Adjacent bug, fixed in the same change

`AgentService.restart_agent()` calls `get_or_create(profile_id, project_id)`
with no `system_prompt` argument, so it respawns with `None`. The restart
button already visible in the agent panel header therefore drops the project
grounding entirely -- a restarted agent no longer knows which project it is
in. This predates the feature but sits squarely in the code path being
changed, so it is fixed here: `restart_agent` takes and forwards the prompt.

### The editor lives in the agent panel, behind a gear in its header

`AgentPanel.tsx`'s header already carries a restart button and a close button;
this adds a third toggle that swaps the message list for a prompt editor with
Save and "Reset to default" (which clears the field to `""`).

Rejected: a section in the global `SettingsView`. That view is profile-scoped
while this setting is project-scoped, so it would need a project picker inside
it -- inventing a per-project settings surface that does not exist, for one
field. The agent panel is already per-project, which makes the scoping
self-evident at the point of editing.

### 4000-character limit

Enforced in the Pydantic update schema, with a live counter in the textarea.

The reason is concrete rather than precautionary: the prompt is passed as an
**argv element** (`cmd += ["--system-prompt", self._system_prompt]`).
Unbounded text there risks `ARG_MAX`, and the failure surfaces as an opaque
`OSError` -> `AgentUnavailableError` ("agent unavailable") with nothing
pointing at the prompt as the cause. A cap makes that failure unreachable.

## Changes

**Backend**

- `app/models/project.py` -- add `agent_system_prompt: str = ""`.
- `app/api/v1/schemas.py` -- add `agent_system_prompt: str | None = None`
  (`max_length=4000`) to `ProjectUpdate`; add the field to `ProjectOut`.
- `app/services/project_service.py` -- add `agent_system_prompt` to the
  `for field in (...)` pass-through loop in `update_project`. Note the loop
  skips `None` but not `""`, which is what lets a reset clear the field.
- `app/api/v1/agent.py` -- `_system_prompt(project)` appends the custom block
  when `project.agent_system_prompt` is non-empty after stripping.
- `app/services/agent_service.py` -- `AgentProcess` stores its spawn prompt;
  `get_or_create` respawns on mismatch; `restart_agent` accepts and forwards
  a prompt.

**Frontend**

- `components/AgentPanel.tsx` -- gear toggle in the header; editor view with
  textarea, character counter, Save, Reset to default, and a line saying a
  saved change restarts the agent on the next message.
- `api` client -- reuse the existing project update call.

## Testing

Backend, via `./backend/run-worktree-tests.sh tests/ -q` from this worktree:

- `_system_prompt` returns the default alone when the field is empty or
  whitespace, and default-plus-custom when set.
- `ProjectUpdate` rejects 4001 characters and accepts 4000.
- `update_project` sets a prompt, and clears it when given `""` (the case the
  `None`-skipping loop makes worth asserting explicitly).
- `get_or_create` returns the same process for an unchanged prompt and a
  *different* process when the prompt changed -- the second direction is the
  one that fails if the comparison is dropped.
- `restart_agent` forwards the prompt it was given (the regression guard for
  the adjacent bug above).

Frontend: manual, at `localhost:5273` via `./ops/worktree-up.sh` -- there is
no headless component-testing setup in this repo. Set a distinctive prompt
("end every reply with the word BANANA"), send a message, confirm the
behavior; confirm Reset to default restores the plain agent.

## Out of scope

Per-profile prompts, prompt templates, prompt versioning (all per the issue),
and the global cross-project layer noted above.
