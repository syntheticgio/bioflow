# AI Agent Harness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A project-scoped agent drawer where the user can converse with a Pi
coding agent that has access to BioFlow's MCP tools — browse data, suggest next
steps, run pipelines. Implements
[#30](https://github.com/syntheticgio/bioflow/issues/30).

**Architecture:** Backend spawns Pi as a subprocess (`pi --mode rpc`) per
project. Pi connects to BioFlow's in-process MCP server via the
pi-mcp-adapter extension
and dynamically-generated config file. The backend proxies JSONL messages
between the frontend (via SSE) and Pi (via stdin/stdout). No new MCP tools
needed — the existing 18 tools are sufficient. Extensions, skills, and
additional MCP servers can be pre-installed in the container and loaded at
startup.

**Tech Stack:** FastAPI, asyncio, Python 3.12, pytest + pytest-asyncio
(backend); React, TanStack Query, TypeScript, SSE (frontend). Pi v0.84+ as the
agent runtime.

**Reference:** `docs/superpowers/specs/2026-08-09-ai-agent-harness-design.md`
— read it before starting. This plan implements it and does not repeat its
rationale except where a step needs it to make a call correctly.

**Out of scope, deliberately:** conversation persistence, file uploads, abort
mid-stream, custom system prompt editing, multi-project agent. All tracked as
follow-ups.

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

### Manual verification needs Pi and pi-mcp-adapter installed

Pi and the pi-mcp-adapter extension must be installed in the `api` container
or accessible on the host. The `api` container's Dockerfile will be updated
to install Node 22 (Pi requires Node >= 22.19; `python:3.12-slim` has no Node
and bookworm's apt nodejs is 18.x), Pi globally via npm, and the adapter via
`pi install npm:pi-mcp-adapter` — which must run inside the container where
Pi is installed.

### After merge

`worker` does not hot-reload, but the agent service lives in the `api` process
(which hot-reloads via `uvicorn --reload`), so no worker restart is needed.
However, the `api` container must be rebuilt to pick up the Pi installation:

```bash
docker compose up -d --build api
```

---

## Task 1: Backend config, Pi installation, and extension/skill setup

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/Dockerfile` (or `docker-compose.override.yml`)
- Create: `pi-skills/` directory with placeholder skills

Adds config settings, installs Node 22, Pi + pi-mcp-adapter in the api
container, and sets up skills/extensions directories.

- [ ] **Step 1: Add config settings**

In `backend/app/config.py`, add:

```python
PI_PATH: str = "/usr/local/bin/pi"  # Path to pi CLI binary
PI_DISABLED: bool = False  # Set True to hide agent UI entirely
AGENT_RESPONSE_TIMEOUT: int = 300  # Seconds before killing unresponsive agent
AGENT_IDLE_TIMEOUT: int = 1800  # Kill agent after 30 min of inactivity
```

- [ ] **Step 2: Install Node 22, Pi, and pi-mcp-adapter in the api container**

Pi requires Node >= 22.19. The `python:3.12-slim` base image has no Node at
all, and Debian bookworm's apt `nodejs` is 18.x — too old. Install Node 22
first (NodeSource), then Pi globally via npm, then the pi-mcp-adapter
extension via Pi's own package manager (`pi install` — this is what registers
the `--mcp-config` flag; it must run inside the container where Pi is
installed):

```dockerfile
# In backend/Dockerfile

# Node 22 for the Pi coding agent (python:3.12-slim has no Node; bookworm's
# apt nodejs is 18.x < pi's >=22.19 requirement)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Pi coding agent globally
RUN npm install -g @earendil-works/pi-coding-agent

# Install pi-mcp-adapter (required for MCP server connectivity).
# pi install registers the extension in ~/.pi/agent/settings.json —
# no interactive prompts, safe to run at build time.
RUN pi install npm:pi-mcp-adapter \
    && pi --version

# Copy skills (teaches Pi about bioinformatics workflows)
COPY ./pi-skills/ /root/.pi/agent/skills/
```

Or in `docker-compose.override.yml` for development:

```yaml
services:
  api:
    environment:
      PI_PATH: /usr/local/bin/pi
    volumes:
      - /usr/local/bin/pi:/usr/local/bin/pi:ro
      - ./pi-skills:/root/.pi/agent/skills:ro
```

- [ ] **Step 3: Create placeholder skills directory**

Create `pi-skills/` with a README and placeholder skills:

```markdown
# pi-skills/README.md
Skills that teach the BioFlow agent about bioinformatics workflows.
See https://agentskills.io/specification for the skill format.
```

Add a starter skill like `pi-skills/run-qc/SKILL.md`:
```markdown
---
name: run-qc
---

# Run QC Pipeline

How to run a quality control pipeline on sequencing data in BioFlow.

## When to Use

When the user has raw sequencing data (FASTQ files) in their project and
wants to assess quality before running further analysis.

## Procedure

1. List the FASTQ files in the project using the `bioflow_list_project_objects`
   tool with type filter "fastq".
2. Select paired-end files (typically *_R1_*.fastq.gz and *_R2_*.fastq.gz).
3. Run the QC pipeline using `bioflow_run_pipeline` with pipeline_id="qc"
   and the selected files as inputs.
4. Check the job status with `bioflow_get_job_status`.
5. Once complete, view the MultiQC report via `bioflow_get_object` on the
   output HTML file.
```

- [ ] **Step 4: Ensure Pi is on the PATH**

Verify the api container has Node.js available and that `pi` resolves. The
base image already includes Node.js; the npm install adds `pi` to
`/usr/local/bin/`.

---

## Task 2: AgentService — process lifecycle and message proxying

**Files:**
- Create: `backend/app/services/agent_service.py`
- Test: `backend/tests/services/test_agent_service.py`

Implements `AgentService` that manages Pi process lifecycle, sends prompts,
reads events, and translates them into a format the API can stream.

- [ ] **Step 1: Define `AgentEvent` types**

```python
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

class AgentStatus(StrEnum):
    IDLE = "idle"
    SPAWNING = "spawning"
    CONNECTED = "connected"
    PROCESSING = "processing"
    ERROR = "error"
    DISCONNECTED = "disconnected"

@dataclass
class AgentEvent:
    type: str  # "message_delta" | "tool_call" | "tool_result" | "done" | "error" | "heartbeat"
    data: dict = field(default_factory=dict)
```

- [ ] **Step 2: Implement `AgentProcess` class**

A single-project agent process wrapper:

```python
class AgentProcess:
    """Manages one Pi subprocess for one project."""

    def __init__(self, project_id: str, owner: str, mcp_url: str, pi_path: str):
        self.project_id = project_id
        self.owner = owner
        self.mcp_url = mcp_url
        self.pi_path = pi_path
        self.process: asyncio.subprocess.Process | None = None
        self.status: AgentStatus = AgentStatus.IDLE
        self._event_queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._last_activity: float = 0
        self._lock = asyncio.Lock()

    async def start(self): ...
    async def send_prompt(self, message: str): ...
    async def stop(self): ...
    async def restart(self): ...
    async def events(self) -> AsyncIterator[AgentEvent]: ...
    async def _read_stdout(self): ...
    async def _read_stderr(self): ...
```

Key behaviors:
- `start()` writes MCP config to a temp file, then spawns
  `pi --mode rpc --no-session --mcp-config <tempfile>`
- `send_prompt()` writes a JSONL `prompt` command to stdin
- `_read_stdout()` reads JSONL lines, parses them, and puts `AgentEvent` objects
  on the queue
- Pi events map to SSE events:
  - `message_update` with `text_delta` → `message_delta` SSE event
  - `tool_execution_start` → `tool_call` SSE event
  - `tool_execution_end` → `tool_result` SSE event
  - `agent_end` → `done` SSE event
- `stop()` terminates the process and cleans up

- [ ] **Step 3: Implement `AgentService` class**

Manages multiple `AgentProcess` instances, one per project:

```python
class AgentService:
    """Manages agent processes across projects."""

    def __init__(self, pi_path: str, mcp_base_url: str):
        self.pi_path = pi_path
        self.mcp_base_url = mcp_base_url
        self._processes: dict[str, AgentProcess] = {}  # project_id -> process

    async def get_or_create(self, project_id: str, owner: str) -> AgentProcess: ...
    async def send_message(self, project_id: str, message: str) -> None: ...
    async def stop_agent(self, project_id: str) -> None: ...
    async def restart_agent(self, project_id: str) -> AgentProcess: ...
    async def cleanup_idle(self) -> None: ...
```

- [ ] **Step 4: Write tests**

Test process spawning (mock subprocess), message sending, event parsing, error
handling, and idle cleanup.

---

## Task 3: API router — agent endpoints and MCP config generation

**Files:**
- Create: `backend/app/api/v1/agent.py`
- Modify: `backend/app/main.py` — mount the router
- Test: `backend/tests/api/test_agent.py`

MCP config generation is part of this task because the API layer has access
to the profile ID from the request context.

- [ ] **Step 1: Implement MCP config builder**

```python
def build_mcp_config(profile_id: str) -> dict:
    """Build MCP servers config for the Pi agent."""
    config = {
        "mcpServers": {
            "bioflow": {
                "url": f"http://localhost:8000/api/v1/mcp?profile={profile_id}"
            }
        }
    }
    # Load additional MCP servers from settings or env
    extra_servers = settings.get("AGENT_EXTRA_MCP_SERVERS", {})
    config["mcpServers"].update(extra_servers)
    return config
```

- [ ] **Step 2: Create the router**

```python
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/projects/{project_id}/agent")

@router.post("/ask")
async def ask_agent(
    project_id: str,
    body: AgentAskRequest,
    owner: str = Depends(get_current_owner),
    profile_id: str = Depends(get_profile_id),
):
    agent = await agent_service.get_or_create(
        project_id, owner, profile_id
    )
    await agent.send_prompt(body.message)
    return {"status": "accepted"}

@router.get("/events")
async def agent_events(
    project_id: str,
    owner: str = Depends(get_current_owner),
):
    agent = agent_service.get(project_id)
    if not agent:
        return StreamingResponse(_no_agent_stream(), media_type="text/event-stream")

    async def event_stream():
        async for event in agent.events():
            yield f"event: {event.type}\ndata: {json.dumps(event.data)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

@router.delete("")
async def stop_agent(
    project_id: str,
    owner: str = Depends(get_current_owner),
):
    await agent_service.stop_agent(project_id)
    return Response(status_code=204)

@router.post("/restart")
async def restart_agent(
    project_id: str,
    owner: str = Depends(get_current_owner),
):
    await agent_service.restart_agent(project_id)
    return {"status": "restarting"}
```

Note: `get_profile_id` is a dependency that extracts the profile from the
X-BioFlow-Profile header or query parameter, matching the pattern used by
the MCP server.

- [ ] **Step 3: Mount the router in main.py**

```python
from app.api.v1.agent import router as agent_router
app.include_router(agent_router, prefix="/api/v1")
```

- [ ] **Step 4: Write API tests**

Test endpoint responses, auth scoping, SSE format, error responses.

---

## Task 4: Frontend — SSE hook

**Files:**
- Create: `frontend/src/hooks/useAgentSSE.ts`

A React hook that manages the SSE connection to the agent events endpoint.

- [ ] **Step 1: Implement `useAgentSSE`**

```typescript
interface AgentSSEOptions {
  projectId: string;
  onMessageDelta: (text: string) => void;
  onToolCall: (tool: string, args: unknown) => void;
  onToolResult: (tool: string, result: unknown) => void;
  onDone: () => void;
  onError: (message: string) => void;
  onConnectionChange: (connected: boolean) => void;
}

function useAgentSSE({ projectId, ...callbacks }: AgentSSEOptions) {
  // Opens EventSource to GET /projects/{projectId}/agent/events
  // Parses SSE events and calls the appropriate callback
  // Auto-reconnects on connection loss with exponential backoff
  // Returns { connected, connect, disconnect }
}
```

Key behaviors:
- Opens `EventSource` to the events endpoint
- Parses `message_delta`, `tool_call`, `tool_result`, `done`, `error` events
- Auto-reconnects on connection loss (exponential backoff: 1s, 2s, 4s, ... 30s max)
- Returns `connected` state and `connect`/`disconnect` controls
- Cleans up on unmount

- [ ] **Step 2: Add API methods to client.ts**

```typescript
askAgent: (projectId: string, message: string) =>
  request<{ status: string }>(`/projects/${projectId}/agent/ask`, {
    method: "POST",
    body: JSON.stringify({ message }),
  }),

restartAgent: (projectId: string) =>
  request<{ status: string }>(`/projects/${projectId}/agent/restart`, {
    method: "POST",
  }),

stopAgent: (projectId: string) =>
  request<void>(`/projects/${projectId}/agent`, { method: "DELETE" }),
```

---

## Task 5: Frontend — AgentPanel components

**Files:**
- Create: `frontend/src/components/AgentPanel.tsx`
- Create: `frontend/src/components/AgentMessageBubble.tsx`
- Create: `frontend/src/components/AgentPanelInput.tsx`
- Create: `frontend/src/styles/agent.css`

- [ ] **Step 1: Implement `AgentPanel`**

The main drawer component, modeled on `ProjectQaDrawer`:

```typescript
interface AgentPanelProps {
  projectId: string;
  onClose: () => void;
}

export function AgentPanel({ projectId, onClose }: AgentPanelProps) {
  // State: messages[], isStreaming, connected, error
  // Uses useAgentSSE hook for streaming
  // Uses api.askAgent / api.restartAgent / api.stopAgent
  // Renders header, message list, input
}
```

States:
- **Loading**: "Starting agent..." with spinner
- **Ready**: Input enabled, empty conversation
- **Streaming**: Agent is responding, input disabled, streaming text visible
- **Error**: Error banner with retry button
- **Disconnected**: "Agent disconnected" with restart button

- [ ] **Step 2: Implement `AgentMessageBubble`**

```typescript
interface MessageBubbleProps {
  role: "user" | "assistant";
  content: string;
  isStreaming?: boolean;
  toolCalls?: ToolCallInfo[];
}
```

Renders:
- User messages: plain text in a user-styled bubble (right-aligned)
- Agent messages: markdown-rendered text in an assistant-styled bubble (left-aligned)
- Tool calls: compact inline indicators like "🔍 Searching objects..."
- Streaming state: cursor animation while text is still arriving

- [ ] **Step 3: Implement `AgentPanelInput`**

```typescript
interface AgentPanelInputProps {
  onSend: (message: string) => void;
  disabled: boolean;
  connected: boolean;
}
```

A textarea input with send button. Disabled while streaming. Shows connection
status indicator. Supports Enter to send, Shift+Enter for newline.

- [ ] **Step 4: Add styles**

Create `agent.css` with styles for:
- Drawer container (slide-up panel, backdrop)
- Message bubbles (user right-aligned, assistant left-aligned)
- Input area (sticky bottom)
- Streaming indicator (pulsing cursor)
- Tool call inline indicators
- Connection status dot (green/yellow/red)
- Error banner

---

## Task 6: Integrate into the app

**Files:**
- Modify: `frontend/src/App.tsx` — add agent drawer to project view
- Modify: `frontend/src/styles.css` — import agent.css
- Modify: `frontend/src/api/client.ts` — add agent methods

- [ ] **Step 1: Add agent drawer to project detail view**

In the project detail component, add an "Agent" button (e.g., in the footer or
toolbar) that opens the AgentPanel.

```tsx
const [agentOpen, setAgentOpen] = useState(false);

// In the project view JSX:
{agentOpen && (
  <AgentPanel
    projectId={project.id}
    onClose={() => setAgentOpen(false)}
  />
)}
```

- [ ] **Step 2: Add footer entry point**

Alongside the Q&A chat entry, add an agent entry:

```tsx
<button onClick={() => setAgentOpen(true)} title="Open AI agent">
  🤖 Agent
</button>
```

- [ ] **Step 3: Import agent.css**

```css
/* In styles.css */
@import "./styles/agent.css";
```

---

## Task 7: Manual verification

- [ ] **Step 1: Build and start the stack**

```bash
docker compose up -d --build api web
```

- [ ] **Step 2: Open a project in the browser**

Navigate to localhost:5173, select a profile, open a project.

- [ ] **Step 3: Open the agent drawer**

Click the Agent button in the project view. Observe the "Starting agent..."
state, then the input becoming enabled.

- [ ] **Step 4: Send a message**

Type "What files are in this project?" and press Enter. Observe:
- The message appears in a user bubble
- The agent starts responding with streaming text
- Tool call indicators appear ("🔍 Searching objects...")
- The full response renders as markdown

- [ ] **Step 5: Test error handling**

Kill the agent process from the host (`pkill -f "pi --mode rpc"`), then send
another message. Observe the error state and restart button.

- [ ] **Step 6: Test restart**

Click "Restart agent" and verify the agent comes back and can respond to new
messages.

- [ ] **Step 7: Test close/reopen**

Close the drawer, reopen it. Verify the conversation is reset (empty state).

---

## Task 8: Update docs/TODO.md

When all tasks above are complete and verified:

- [ ] **Step 1: Mark the issue as done**

```bash
gh issue close 30 --comment "First slice shipped: project-scoped agent drawer with Pi subprocess, MCP integration, SSE streaming, and error handling."
```

- [ ] **Step 2: Add a FIXED entry to docs/TODO.md**

Following the pattern from CLAUDE.md ("Closing out a TODO entry"), append a
`— FIXED` entry describing what shipped, what was done differently from the
plan, and where the code lives.
