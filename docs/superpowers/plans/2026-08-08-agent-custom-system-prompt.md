# Custom Agent System Prompt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user write per-project extra instructions for the in-app Pi agent, appended to the default project-awareness prompt, edited from the agent panel.

**Architecture:** One new typed field on `Project` (`agent_system_prompt`), exposed through the existing project PATCH endpoint. `_system_prompt()` in the agent router concatenates it onto the default grounding block. Because pi reads its system prompt only at spawn (it is an argv element), `AgentService.get_or_create` records the prompt each process was spawned with and respawns when the incoming prompt differs -- so a saved edit takes effect on the next message.

**Tech Stack:** FastAPI, Beanie/MongoDB, Pydantic v2, pytest/pytest-asyncio; React + TypeScript, TanStack Query.

**Spec:** [`docs/superpowers/specs/2026-08-08-agent-custom-system-prompt-design.md`](../specs/2026-08-08-agent-custom-system-prompt-design.md)

---

## Important context before you start

**Run the tests from this worktree with the worktree runner, never `docker compose exec api`:**

```bash
./backend/run-worktree-tests.sh tests/path/test_file.py -q
```

`docker compose exec api python -m pytest` bind-mounts the *main* checkout, so
from a worktree it silently tests main's code and reports results for the wrong
tree. The worktree runner also gives the run its own Mongo replica set, which
matters because `conftest.py` drops every collection in `biopipe_test` at
session start -- sharing Mongo with the running stack makes a rotating handful
of unrelated DB tests fail.

**Existing test helpers you will reuse** (do not rewrite them):

- `backend/tests/services/test_agent_service.py` -- `make_service(**kwargs)`
  builds an `AgentService` with a fake pi path and long timeouts; the `spawn`
  fixture patches `app.services.agent_service.create_subprocess_exec` and is
  called as `calls, _ = spawn()` to get the list of argv lists.
- `backend/tests/api/test_agent.py` -- its own `spawn` fixture returns
  `(cmds, spawned)`, and `_project(owner)` creates a project.

**Always stop processes you create in a test** (`await proc.stop()`), as the
existing tests do -- a live `AgentProcess` leaks reader tasks into the next
test.

## File Structure

**Backend**

| File | Responsibility | Change |
| --- | --- | --- |
| `backend/app/models/project.py` | Project document | Add `agent_system_prompt: str = ""` |
| `backend/app/api/v1/schemas.py` | Request/response shapes | Add the field to `ProjectUpdate` (max 4000) and to `ProjectOut` + `ProjectOut.of` |
| `backend/app/services/project_service.py` | Project persistence | Add the field to the pass-through loop in `update_project` |
| `backend/app/api/v1/agent.py` | Agent HTTP surface | `_system_prompt()` appends the custom block; `/restart` passes the prompt |
| `backend/app/services/agent_service.py` | pi subprocess lifecycle | `AgentProcess` remembers its spawn prompt; `get_or_create` respawns on mismatch; `restart_agent` forwards a prompt |

**Frontend**

| File | Responsibility | Change |
| --- | --- | --- |
| `frontend/src/api/types.ts` | Shared TS types | Add `agent_system_prompt: string` to `Project` |
| `frontend/src/components/AgentPanel.tsx` | Agent drawer | Gear toggle + editor view |
| `frontend/src/styles/agent.css` | Drawer styling | Editor layout classes |

No frontend API-client change: `api.updateProject(id, body)`
(`frontend/src/api/client.ts:237`) already accepts an open
`Record<string, unknown>` body, and `api.getProject(id)` (`:232`) already
returns `ProjectDetail`.

---

### Task 1: Store the prompt on the project

**Files:**
- Modify: `backend/app/models/project.py:22-34`
- Modify: `backend/app/api/v1/schemas.py:27-33` (`ProjectUpdate`), `:34-63` (`ProjectOut`)
- Modify: `backend/app/services/project_service.py:109-131`
- Test: `backend/tests/services/test_project_service_owner.py` (modify),
  `backend/tests/api/test_project_schemas.py` (create)

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/services/test_project_service_owner.py` — that is
where `update_project` is exercised today; there is no
`tests/services/test_project_service.py`.

Two conventions from that file the tests below follow: there is **no `owner`
fixture** (owner ids are inline literals), and each test uses an owner id
unique to itself, because the database is module-scoped and shared — a generic
`"owner-a"` picks up projects other tests created. `create_project` is called
with just `name=` and `owner=`.

```python
class TestAgentSystemPrompt:
    async def test_defaults_to_empty_string(self):
        project = await project_service.create_project(
            name="prompt-default", owner="prompt-default-owner"
        )
        assert project.agent_system_prompt == ""

    async def test_update_sets_the_prompt(self):
        owner = "prompt-set-owner"
        project = await project_service.create_project(name="prompt-set", owner=owner)

        updated = await project_service.update_project(
            project.id, {"agent_system_prompt": "Always cite the tool."}, owner=owner
        )

        assert updated.agent_system_prompt == "Always cite the tool."

    async def test_empty_string_clears_the_prompt(self):
        """Reset-to-default sends "", which the None-skipping loop must honour."""
        owner = "prompt-clear-owner"
        project = await project_service.create_project(name="prompt-clear", owner=owner)
        await project_service.update_project(
            project.id, {"agent_system_prompt": "temporary"}, owner=owner
        )

        cleared = await project_service.update_project(
            project.id, {"agent_system_prompt": ""}, owner=owner
        )

        assert cleared.agent_system_prompt == ""

    async def test_none_leaves_the_prompt_alone(self):
        """A PATCH that omits the field must not wipe it."""
        owner = "prompt-untouched-owner"
        project = await project_service.create_project(
            name="prompt-untouched", owner=owner
        )
        await project_service.update_project(
            project.id, {"agent_system_prompt": "keep me"}, owner=owner
        )

        same = await project_service.update_project(
            project.id, {"name": "prompt-renamed"}, owner=owner
        )

        assert same.agent_system_prompt == "keep me"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/services/test_project_service_owner.py -k AgentSystemPrompt -q
```

Expected: FAIL — `AttributeError: 'Project' object has no attribute 'agent_system_prompt'`.

- [ ] **Step 3: Add the model field**

In `backend/app/models/project.py`, inside `class Project`, after the
`description: str = ""` line:

```python
    # Extra per-project instructions appended to the agent's default
    # grounding prompt (see api/v1/agent.py:_system_prompt). Empty means
    # "default only". Length-capped in ProjectUpdate because the value is
    # passed to pi as an argv element.
    agent_system_prompt: str = ""
```

- [ ] **Step 4: Add the field to the update loop**

In `backend/app/services/project_service.py`, in `update_project`, change the
pass-through tuple:

```python
    for field in ("description", "tags", "archived", "agent_system_prompt"):
        if updates.get(field) is not None:
            setattr(project, field, updates[field])
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/services/test_project_service_owner.py -k AgentSystemPrompt -q
```

Expected: PASS (4 passed).

- [ ] **Step 6: Add the API schema fields**

In `backend/app/api/v1/schemas.py`, add to `ProjectUpdate`:

```python
    agent_system_prompt: str | None = Field(default=None, max_length=4000)
```

Add to `ProjectOut` (after `description: str`):

```python
    agent_system_prompt: str
```

and to `ProjectOut.of`, after the `description=p.description,` line:

```python
            agent_system_prompt=p.agent_system_prompt,
```

`Field` is already imported in this module.

- [ ] **Step 7: Write the schema limit test**

There is no `tests/api/test_projects.py`. These are pure Pydantic checks with
no database, so create `backend/tests/api/test_project_schemas.py`:

```python
"""ProjectUpdate's validation rules.

The 4000-character cap on agent_system_prompt is not cosmetic: the value
becomes an argv element when pi is spawned (agent_service.start), so an
unbounded string risks ARG_MAX and a spawn failure that surfaces only as
"agent unavailable", with nothing pointing at the prompt as the cause.
"""

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.api.v1.schemas import ProjectUpdate


class TestAgentSystemPromptLimit:
    def test_accepts_4000_characters(self):
        body = ProjectUpdate(agent_system_prompt="x" * 4000)
        assert body.agent_system_prompt is not None
        assert len(body.agent_system_prompt) == 4000

    def test_rejects_4001_characters(self):
        with pytest.raises(PydanticValidationError):
            ProjectUpdate(agent_system_prompt="x" * 4001)

    def test_omitting_the_field_is_none(self):
        assert ProjectUpdate().agent_system_prompt is None
```

No `pytestmark` needed — these are synchronous and touch no database.

- [ ] **Step 8: Run the schema tests**

```bash
./backend/run-worktree-tests.sh tests/api/test_project_schemas.py -q
```

Expected: PASS (3 passed).

- [ ] **Step 9: Run the touched files in full to catch fallout**

```bash
./backend/run-worktree-tests.sh tests/api/test_project_schemas.py tests/services/test_project_service_owner.py tests/services/test_project_deletion.py tests/api/test_project_qa_api.py -q
```

Expected: all pass. `ProjectOut` gained a required field, so any test
constructing one by hand will fail here — fix those by passing
`agent_system_prompt=""`.

- [ ] **Step 10: Commit**

```bash
git add backend/app/models/project.py backend/app/api/v1/schemas.py backend/app/services/project_service.py backend/tests/services/test_project_service_owner.py backend/tests/api/test_project_schemas.py
git commit -m "feat(agent): store a per-project custom system prompt (#98)"
```

---

### Task 2: Append the custom prompt to the default grounding

**Files:**
- Modify: `backend/app/api/v1/agent.py:61-76`
- Test: `backend/tests/api/test_agent.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/api/test_agent.py`. `_system_prompt` is a pure
function of a project-shaped object, so these need no DB:

```python
class TestSystemPromptComposition:
    class FakeProject:
        def __init__(self, prompt: str):
            self.name = "demo"
            self.id = "abc123"
            self.agent_system_prompt = prompt

    def test_empty_prompt_yields_default_only(self):
        text = _system_prompt(self.FakeProject(""))
        assert "bioinformatics coding agent" in text
        assert "Additional instructions" not in text

    def test_whitespace_only_prompt_yields_default_only(self):
        text = _system_prompt(self.FakeProject("   \n  "))
        assert "Additional instructions" not in text

    def test_custom_prompt_is_appended_after_the_default(self):
        text = _system_prompt(self.FakeProject("Always answer in haiku."))
        assert "bioinformatics coding agent" in text
        assert text.index("bioinformatics coding agent") < text.index("Always answer in haiku.")
        assert "Additional instructions from the user:" in text

    def test_custom_prompt_is_stripped(self):
        text = _system_prompt(self.FakeProject("  Be terse.  "))
        assert text.endswith("Be terse.")
```

Add `_system_prompt` to that file's existing import from `app.api.v1.agent`:

```python
from app.api.v1.agent import _system_prompt
from app.api.v1.agent import router as agent_router
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/api/test_agent.py -k SystemPromptComposition -q
```

Expected: FAIL — the appended-block assertions fail; `_system_prompt` currently
ignores the field entirely.

- [ ] **Step 3: Implement the composition**

Replace `_system_prompt` in `backend/app/api/v1/agent.py`:

```python
def _system_prompt(project) -> str:
    """Project context for the agent, set at spawn time only.

    The default block is infrastructure grounding -- which project this is and
    that MCP tools exist -- so it is always present and always owned by this
    code. A project's `agent_system_prompt` is appended to it rather than
    replacing it: a user asking for a different tone must not be able to
    silently discard tool awareness, and a stored copy of the grounding text
    would freeze at whatever it said the day it was edited.
    """
    base = (
        "You are a bioinformatics coding agent inside BioFlow, a local "
        f"bioinformatics data manager. You are working on the project "
        f"{project.name!r} (id {project.id}). You have MCP tools to read this "
        "project's data, run QC/trim/align/assemble pipelines, and inspect "
        "jobs. Prefer running a tool over describing what you would do, and "
        "keep answers concrete and short."
    )
    custom = (project.agent_system_prompt or "").strip()
    if not custom:
        return base
    return f"{base}\n\nAdditional instructions from the user:\n{custom}"
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/api/test_agent.py -k SystemPromptComposition -q
```

Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/agent.py backend/tests/api/test_agent.py
git commit -m "feat(agent): append the project's custom prompt to the default grounding (#98)"
```

---

### Task 3: Respawn when the prompt changes; stop dropping it on restart

**Files:**
- Modify: `backend/app/services/agent_service.py:60-80` (`AgentProcess.__init__`), `:384-410` (`get_or_create`), `:424-427` (`restart_agent`)
- Test: `backend/tests/services/test_agent_service.py`

This is the task the whole feature turns on. Two distinct behaviours:

1. A live process spawned with prompt A must be replaced when prompt B arrives.
2. `restart_agent` currently respawns with **no** prompt at all, so today's
   restart button drops the project grounding. It must forward one.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/services/test_agent_service.py`, inside the same class
that holds `test_restart_spawns_a_fresh_process` (it already has the `spawn`
fixture in scope):

```python
    async def test_same_prompt_reuses_the_process(self, spawn):
        service = make_service()
        first = await service.get_or_create("p", "j", system_prompt="A")
        second = await service.get_or_create("p", "j", system_prompt="A")
        assert second is first
        calls, _ = spawn()
        assert len(calls) == 1
        await second.stop()

    async def test_changed_prompt_respawns(self, spawn):
        """The direction that fails if the comparison is dropped."""
        service = make_service()
        first = await service.get_or_create("p", "j", system_prompt="A")
        second = await service.get_or_create("p", "j", system_prompt="B")
        assert second is not first
        calls, _ = spawn()
        assert len(calls) == 2
        assert "B" in calls[1]
        assert first.process is None
        await second.stop()

    async def test_prompt_going_empty_respawns(self, spawn):
        """Reset-to-default must reach the running agent too."""
        service = make_service()
        first = await service.get_or_create("p", "j", system_prompt="A")
        second = await service.get_or_create("p", "j", system_prompt=None)
        assert second is not first
        calls, _ = spawn()
        assert len(calls) == 2
        assert "--system-prompt" not in calls[1]
        await second.stop()

    async def test_restart_forwards_the_system_prompt(self, spawn):
        """Regression guard: restart used to respawn with no prompt at all."""
        service = make_service()
        first = await service.get_or_create("p", "j", system_prompt="grounding")
        await service.restart_agent("p", "j", system_prompt="grounding")
        calls, _ = spawn()
        assert len(calls) == 2
        assert "--system-prompt" in calls[1]
        assert "grounding" in calls[1]
        second = service.get("p", "j")
        assert second is not None and second is not first
        await second.stop()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/services/test_agent_service.py -k "prompt" -q
```

Expected: FAIL — `test_changed_prompt_respawns` sees one spawn where it wants
two; `test_restart_forwards_the_system_prompt` fails on the unexpected keyword
argument `system_prompt`.

- [ ] **Step 3: Record the spawn prompt on the process**

In `AgentProcess.__init__` in `backend/app/services/agent_service.py`, the
existing line

```python
        self._system_prompt = system_prompt
```

already stores it. Make it a documented part of the class's surface by adding,
directly beneath it:

```python
        # Read once, at spawn: it becomes an argv element in start(). The
        # service compares against this to decide whether a live process is
        # still running the caller's prompt.
        self.spawned_with_prompt = system_prompt
```

- [ ] **Step 4: Compare and respawn in get_or_create**

Replace the reuse branch of `get_or_create`:

```python
        key = self._key(profile_id, project_id)
        proc = self._processes.get(key)
        if proc is not None and proc.process is not None:
            if proc.spawned_with_prompt == system_prompt:
                return proc
            # pi takes its system prompt as an argv element, so a changed
            # prompt cannot reach a running process. Replace it rather than
            # answering the next message under the old instructions.
            log.info("agent_prompt_changed", profile=profile_id, project=str(project_id))
            await self.stop_agent(profile_id, project_id)
            proc = None
        if proc is not None:
            # Dead process from a crashed pi; reap it and start over.
            self._processes.pop(key, None)
```

- [ ] **Step 5: Forward the prompt through restart_agent**

Replace `restart_agent`:

```python
    async def restart_agent(
        self, profile_id: str, project_id: str, *, system_prompt: str | None = None
    ) -> AgentProcess:
        """Stop and respawn.

        `system_prompt` is forwarded because the respawned process gets its
        prompt only from here -- omitting it (as this method used to) drops
        the project grounding, leaving a restarted agent with no idea which
        project it is in.
        """
        await self.stop_agent(profile_id, project_id)
        return await self.get_or_create(profile_id, project_id, system_prompt=system_prompt)
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/services/test_agent_service.py -k "prompt" -q
```

Expected: PASS (8 passed — the four new ones plus four pre-existing tests whose
names also match `-k prompt`: `test_system_prompt_is_passed_as_a_flag`,
`test_prompt_line_has_steer_behavior`,
`test_rejected_prompt_surfaces_as_an_error_event`, and
`test_send_prompt_on_dead_process_raises`).

- [ ] **Step 7: Run the whole agent service file**

```bash
./backend/run-worktree-tests.sh tests/services/test_agent_service.py -q
```

Expected: all pass. `test_restart_spawns_a_fresh_process` still calls
`restart_agent` with no prompt — that keeps working, since the parameter is
keyword-only with a `None` default.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/agent_service.py backend/tests/services/test_agent_service.py
git commit -m "fix(agent): respawn on prompt change and stop dropping it on restart (#98)"
```

---

### Task 4: Wire the router's restart to the project's prompt

**Files:**
- Modify: `backend/app/api/v1/agent.py` (the `/restart` handler)
- Test: `backend/tests/api/test_agent.py`

Task 3 gave `restart_agent` the parameter; the HTTP handler must actually pass
it, or the restart button still drops the grounding.

- [ ] **Step 1: Note what the handler looks like now**

`backend/app/api/v1/agent.py:158-161` currently reads:

```python
@router.post("/restart", response_model=AgentAskResponse)
async def restart_agent(project_id: PydanticObjectId, profile_id: ProfileIdDep) -> AgentAskResponse:
    await agent_service.restart_agent(profile_id, str(project_id))
    return AgentAskResponse(status="restarting")
```

It takes no `owner`, so it cannot look the project up. Step 4 adds
`owner: OwnerDep` — already imported in this module for `ask_agent`.

- [ ] **Step 2: Write the failing test**

Append to `class TestLifecycle` in `backend/tests/api/test_agent.py`, directly
after `test_restart_spawns_a_fresh_process`, whose fixture usage this mirrors:

```python
    async def test_restart_respawns_with_the_project_prompt(
        self, client, two_profiles, spawn
    ):
        """Regression guard: restart used to respawn with no prompt at all."""
        owner = two_profiles["a"].owner_id()
        project = await _project(owner)
        await project_service.update_project(
            project.id, {"agent_system_prompt": "Be terse."}, owner=owner
        )
        headers = two_profiles["a_headers"]
        url = f"/api/v1/projects/{project.id}/agent"
        await client.post(url + "/ask", json={"message": "hi"}, headers=headers)

        response = await client.post(url + "/restart", headers=headers)
        assert response.status_code == 200

        cmds, _ = spawn
        assert len(cmds) == 2
        prompt_arg = cmds[1][cmds[1].index("--system-prompt") + 1]
        assert "bioinformatics coding agent" in prompt_arg
        assert "Be terse." in prompt_arg
```

Note the details this file's conventions impose, all of which differ from the
service-level tests: the `spawn` fixture here returns `(cmds, spawned)` as a
tuple to unpack (not to call), URLs carry the `/api/v1` prefix, every request
needs `headers`, and the owner comes from `two_profiles["a"].owner_id()`.
No explicit teardown — the surrounding lifecycle tests do none.

- [ ] **Step 3: Run it to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/api/test_agent.py -k restart_respawns -q
```

Expected: FAIL — the second argv has no `--system-prompt`.

- [ ] **Step 4: Pass the prompt from the handler**

Replace the handler in full:

```python
@router.post("/restart", response_model=AgentAskResponse)
async def restart_agent(
    project_id: PydanticObjectId, owner: OwnerDep, profile_id: ProfileIdDep
) -> AgentAskResponse:
    """Stop and respawn, keeping the project's composed prompt.

    The ownership-scoped lookup is here for the prompt, not just the 404: a
    respawn that forwarded nothing would drop the project grounding, which is
    what this endpoint used to do.
    """
    project = await project_service.get_project(project_id, owner=owner)
    await agent_service.restart_agent(
        profile_id, str(project_id), system_prompt=_system_prompt(project)
    )
    return AgentAskResponse(status="restarting")
```

- [ ] **Step 5: Run it to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/api/test_agent.py -k restart_respawns -q
```

Expected: PASS.

- [ ] **Step 6: Run the whole agent API file**

```bash
./backend/run-worktree-tests.sh tests/api/test_agent.py -q
```

Expected: all pass. If an existing restart test calls the endpoint without an
owner-scoped project, it may now 404 — fix it by creating the project through
`_project(owner)`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/v1/agent.py backend/tests/api/test_agent.py
git commit -m "feat(agent): restart respawns with the project's composed prompt (#98)"
```

---

### Task 5: The editor in the agent panel

**Files:**
- Modify: `frontend/src/components/AgentPanel.tsx`
- Modify: `frontend/src/styles/agent.css`

No automated tests: this repo has no headless component-testing setup (no
jsdom, no `.test.tsx`), and none is expected. Verification is Task 6.

- [ ] **Step 1: Fetch the project so the editor has a starting value**

At the top of `AgentPanel`, alongside the existing hooks:

```tsx
  const [showSettings, setShowSettings] = useState(false);
  const [draftPrompt, setDraftPrompt] = useState("");

  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId),
  });

  // Load the saved value into the draft whenever the editor is opened.
  useEffect(() => {
    if (showSettings) {
      setDraftPrompt(project.data?.agent_system_prompt ?? "");
    }
  }, [showSettings, project.data?.agent_system_prompt]);
```

Add `useQuery` and `useQueryClient` to the existing `@tanstack/react-query`
import. `api.getProject(id)` is real (`frontend/src/api/client.ts:232`) and
returns `ProjectDetail`.

Before this compiles, add the field to the TypeScript type — in
`frontend/src/api/types.ts`, in `interface Project`, after `description: string;`:

```ts
  agent_system_prompt: string;
```

`ProjectDetail extends Project`, so it inherits the field.

- [ ] **Step 2: Add the save mutation**

```tsx
  const queryClient = useQueryClient();

  const savePrompt = useMutation({
    mutationFn: (value: string) =>
      api.updateProject(projectId, { agent_system_prompt: value }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["project", projectId] });
      setShowSettings(false);
    },
  });
```

- [ ] **Step 3: Add the gear button to the header**

In the header, immediately before the restart button, and move the
`marginLeft: "auto"` off the restart button onto this one so the group stays
right-aligned:

```tsx
          <button
            type="button"
            className="icon-btn"
            onClick={() => setShowSettings((v) => !v)}
            title="Agent instructions"
            style={{ marginLeft: "auto" }}
          >
            ⚙️
          </button>
```

- [ ] **Step 4: Render the editor in place of the message list**

Wrap the existing `<div className="agent-drawer-body">` block in a conditional.
When `showSettings` is true, render this instead:

```tsx
        {showSettings ? (
          <div className="agent-drawer-body agent-prompt-editor">
            <label className="agent-prompt-label" htmlFor="agent-prompt">
              Extra instructions for this project
            </label>
            <p className="agent-prompt-help">
              Added on top of the agent's built-in project knowledge — it always
              knows which project it is in and which tools it has. Saving
              restarts the agent on your next message, which clears the current
              conversation.
            </p>
            <textarea
              id="agent-prompt"
              className="agent-prompt-textarea"
              value={draftPrompt}
              maxLength={4000}
              onChange={(e) => setDraftPrompt(e.target.value)}
              placeholder="e.g. Always say which tool you used. Assume paired-end Illumina reads."
            />
            <div className="agent-prompt-actions">
              <span className="agent-prompt-count">{draftPrompt.length} / 4000</span>
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setDraftPrompt("")}
                disabled={draftPrompt.length === 0}
              >
                Reset to default
              </button>
              <button
                type="button"
                className="btn-primary"
                onClick={() => savePrompt.mutate(draftPrompt)}
                disabled={savePrompt.isPending}
              >
                {savePrompt.isPending ? "Saving…" : "Save"}
              </button>
            </div>
            {savePrompt.isError && (
              <div className="agent-prompt-error">Could not save. Try again.</div>
            )}
          </div>
        ) : (
          /* the existing message-list <div className="agent-drawer-body"> block, unchanged */
        )}
```

"Reset to default" only clears the textarea; the user still presses Save. That
keeps one write path and makes the reset undoable before it commits.

Hide the composer while the editor is open — wrap `<AgentPanelInput .../>` in
`{!showSettings && ( ... )}`.

- [ ] **Step 5: Add the styles**

Append to `frontend/src/styles/agent.css`, matching the variable names already
used in that file (open it and reuse its existing custom properties for colors
and borders rather than hardcoding hex values):

```css
.agent-prompt-editor {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding: 0.75rem;
}

.agent-prompt-label {
  font-weight: 600;
}

.agent-prompt-help {
  margin: 0;
  font-size: 0.85em;
  opacity: 0.75;
}

.agent-prompt-textarea {
  flex: 1;
  min-height: 12rem;
  resize: vertical;
  font-family: inherit;
  padding: 0.5rem;
}

.agent-prompt-actions {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.agent-prompt-count {
  margin-right: auto;
  font-size: 0.8em;
  opacity: 0.7;
}

.agent-prompt-error {
  font-size: 0.85em;
}
```

- [ ] **Step 6: Confirm it compiles**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors. Fix any name mismatch against the real API client here.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/AgentPanel.tsx frontend/src/styles/agent.css
git commit -m "feat(agent): edit per-project agent instructions from the drawer (#98)"
```

---

### Task 6: Verify end to end against a running stack

**Files:** none — this is the manual verification the repo relies on for UI work.

- [ ] **Step 1: Bring up the worktree stack**

```bash
./ops/worktree-up.sh
```

UI on `localhost:5273`, API on `8100`. Do not use plain `docker compose` from
this worktree — it repoints the main 5173 stack at this branch.

- [ ] **Step 2: Run the full backend suite against the worktree**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: read the count, not just the exit status. Everything passes.

- [ ] **Step 3: Exercise the feature in the browser**

At `localhost:5273`, open a project, open the agent drawer, and:

1. Ask "what project am I in?" — it names the project (grounding intact).
2. Open the gear, enter `End every reply with the word BANANA.`, Save.
3. Send another message — the reply ends with BANANA (the respawn worked).
4. Press the restart button, ask again — still BANANA (Task 4's fix; before it,
   restart dropped the prompt entirely).
5. Gear → Reset to default → Save → send — no BANANA, and it still names the
   project.

- [ ] **Step 4: Tear the stack down**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 5: Close out the issue tracking**

Update [#98](https://github.com/syntheticgio/bioflow/issues/98) with what
shipped and what differed from this plan. Note the `restart_agent`
prompt-drop bug fixed alongside it — it was not in the issue's scope as
written.

---

## Notes for the implementer

**`docs/TODO.md` needs no entry here.** This is issue-tracked work, not a
backlog entry; check `docs/TODO.md` for an entry mentioning the agent system
prompt before assuming that, and if one exists, close it out per CLAUDE.md
(append ` — FIXED`, note what differed, move it to `docs/TODO-done.md`).

**The worker does not hot-reload**, but nothing here touches a queue handler,
so `docker compose restart worker` is not needed. The `api` service runs
`uvicorn --reload` and picks up backend edits on the next request.

**Merging:** once the suite is green and `main` is clean, merge and push to
`origin` without asking — that is this repo's standing instruction.
