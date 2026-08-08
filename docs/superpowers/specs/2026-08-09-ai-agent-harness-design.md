# AI Agent Harness — Design

Design for [#30](https://github.com/syntheticgio/bioflow/issues/30).

An in-app Pi coding agent embedded in the BioFlow web UI, accessible from any
project. The user opens a project-scoped drawer and converses with an AI agent
that can browse their data, suggest next steps, and launch pipelines — all
through BioFlow's existing MCP server.

This is the next step after the MCP server ([#31](https://github.com/syntheticgio/bioflow/issues/31),
`docs/superpowers/specs/2026-08-06-mcp-server-design.md`). That spec established
*that an agent can connect to BioFlow*; this one establishes *that BioFlow can
host an agent*.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Browser (port 5173)                            │
│  ┌─────────────────────────────────────────┐    │
│  │ AgentPanel (new React component)         │    │
│  │  - Message list + streaming text display │    │
│  │  - Input box + send button               │    │
│  │  - Project-scoped, like ProjectQaDrawer  │    │
│  └──────────────┬──────────────────────────┘    │
└─────────────────┼────────────────────────────────┘
                  │ POST /projects/{id}/agent/ask
                  │ GET  /projects/{id}/agent/events (SSE)
                  ▼
┌─────────────────────────────────────────────────┐
│  API (FastAPI, port 8000)                       │
│  ┌─────────────────────────────────────────┐    │
│  │ AgentService                             │    │
│  │  - Manages Pi process lifecycle         │    │
│  │  - Spawns Pi --mode rpc per session     │    │
│  │  - Proxies JSONL messages to/from Pi    │    │
│  │  - Streams assistant text via SSE       │    │
│  └──────────────┬──────────────────────────┘    │
└─────────────────┼────────────────────────────────┘
                  │ MCP (internal HTTP)
                  ▼
┌─────────────────────────────────────────────────┐
│  BioFlow MCP Server (/api/v1/mcp)               │
│  - 18 tools: list projects, objects, search,    │
│    run pipelines, suggest_next, get guides, etc.│
│  - In-process, no HTTP hop                      │
└─────────────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────┐
│  Pi Agent Process (--mode rpc)                  │
│  - Connects to BioFlow MCP server               │
│  - Receives user prompts via JSONL stdin        │
│  - Calls MCP tools to answer                    │
│  - Streams responses via JSONL stdout           │
│  - Has a system prompt about BioFlow            │
└─────────────────────────────────────────────────┘
```

### Key insight: Pi is a subprocess, not a library import

Pi's RPC mode (`pi --mode rpc`) communicates over JSONL on stdin/stdout. The
backend spawns it, sends `prompt` commands, and reads events from its stdout.
This is the designed integration surface — see
[Pi's RPC documentation](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md).

Pi connects to BioFlow's MCP server via an MCP extension that reads an MCP
config file at startup. The extension registers the `--mcp-config` CLI flag
and handles connecting to the servers listed in the config file. This means
Pi can also connect to external MCP servers alongside BioFlow's — just add
them to the config file. No new MCP tools are needed; the existing 18 tools
are sufficient.

### Why a subprocess and not the SDK

Pi's programmatic SDK (`createAgentSession`) exists and could be used in-process,
but:

- **Isolation**: A misbehaving agent (infinite loop, runaway tool calls) crashes
  the agent process, not the API process.
- **Resource control**: The subprocess has its own memory budget; the API can
  kill and restart it.
- **Dependency isolation**: Pi brings its own Node.js runtime and dependencies.
  Running it in-process in the Python backend would require embedding Node or
  calling into it via FFI — the subprocess is the simpler boundary.
- **Upgrade independence**: Pi can be updated independently of BioFlow.

### Extensions, skills, and MCP servers

Pi loads extensions from `~/.pi/agent/extensions/` (or via `--extension`),
skills from `~/.pi/agent/skills/` (or via `--skill`), and MCP servers via an
MCP extension that reads a JSON config file (`--mcp-config`). These are all
installed or mounted into the Docker container so they are available to every
Pi process.

**Pre-installed extensions:** The MCP extension (`mcp-extension`) is required
for Pi to connect to any MCP server. Additional extensions can be installed
for custom tools, event interception, or UI components.

**Pre-installed skills:** Skills teach Pi about specific workflows (e.g.,
"how to run a QC pipeline", "how to interpret FASTQ quality reports"). These
are loaded at startup and available on demand.

**MCP servers:** The config file lists MCP servers Pi should connect to. The
backend generates this file dynamically with the BioFlow MCP server URL and
optionally includes external servers. Each server entry can include the
profile ID in the URL query parameter.

### System prompt

The Pi agent gets a system prompt that orients it to BioFlow:

> You are a bioinformatics assistant integrated into BioFlow, a local-first
> bioinformatics pipeline platform. You have access to BioFlow's MCP server,
> which lets you browse projects, list objects, search for data, suggest next
> pipeline steps, run pipelines, check job status, and access workflow guides.
>
> The user is looking at a specific project. Help them understand their data,
> decide what to run next, interpret results, and debug failures. Always use
> the MCP tools to get real data — never guess or fabricate information about
> the user's projects.
>
> Available tools: [dynamically populated from MCP server]

This is set via Pi's `--system-prompt` flag or passed as the initial RPC
`set_system_prompt` command.

## Frontend: AgentPanel

### Placement

A project-scoped drawer, structurally modeled on `ProjectQaDrawer`:
click-away backdrop + positioned panel sliding up from the bottom. Opened from
a footer entry point or a button in the project toolbar.

Unlike the Q&A drawer (which is a queue job with polling), this drawer
maintains a persistent SSE connection to stream agent responses in real time.

### States

| State | What the user sees |
|-------|-------------------|
| **Idle** | Empty message area + input placeholder "Ask the BioFlow agent..." |
| **Connecting** | "Starting agent..." spinner, then "Agent ready" |
| **Thinking** | Agent is processing (tool calls in progress). Show a streaming indicator |
| **Responding** | Agent text streaming in via SSE |
| **Awaiting input** | Agent has finished responding, input is enabled |
| **Error** | Connection lost, agent crashed, or unrecoverable error — show retry button |
| **Disconnected** | Agent process died — show "Restart agent" button |

### Message display

Each turn shows:
- **User message** — plain text in a user-styled bubble
- **Agent message** — markdown-rendered text in an assistant-styled bubble
  - Tool calls are shown as compact inline tags ("🔍 Searching objects...")
  - Code blocks are syntax-highlighted
  - Streaming text appears character by character

### Component tree

```
AgentPanel (drawer wrapper)
├── AgentPanelHeader (title, minimize, close, restart button)
├── AgentPanelBody (scrollable message list)
│   ├── MessageBubble (user)
│   └── MessageBubble (agent)
│       └── MarkdownRenderer (for agent responses)
├── StreamingIndicator (shown while agent is responding)
└── AgentPanelInput (textarea + send button)
    └── ConnectionStatus (small indicator: connected/reconnecting/disconnected)
```

### Integration points

- **Footer entry**: A new button in the footer alongside the Q&A chat entry
- **Project toolbar**: A button in the project detail toolbar
- **Keyboard shortcut**: `Cmd/Ctrl+Shift+A` to toggle the drawer

## Backend: AgentService

### New API endpoints

```
POST /projects/{project_id}/agent/ask
  Body: { "message": "string" }
  Response: { "status": "accepted" }
  Effect: Sends the message to the Pi agent's RPC stdin

GET /projects/{project_id}/agent/events
  Response: SSE stream
  Events:
    - event: message_delta  data: { "text": "partial response text" }
    - event: tool_call      data: { "tool": "bioflow_search_objects", "args": {...} }
    - event: tool_result    data: { "tool": "...", "result": {...} }
    - event: done           data: { }
    - event: error          data: { "message": "error description" }
    - event: heartbeat      data: { }  (keepalive every 30s)

DELETE /projects/{project_id}/agent
  Response: 204
  Effect: Kills the Pi agent process for this project

POST /projects/{project_id}/agent/restart
  Response: { "status": "restarting" }
  Effect: Kills and respawns the Pi agent process
```

### Pi process lifecycle

```
┌─────────┐     ┌──────────────┐     ┌──────────────┐
│  Idle   │────>│  Spawning    │────>│  Connected   │
│ (no     │     │ (spawns pi   │     │ (pi is ready │
│  agent) │     │  --mode rpc) │     │  to receive  │
└─────────┘     └──────────────┘     │  prompts)    │
       ^               │             └──────┬───────┘
       │               │ (spawn failed)      │
       │               v                     │
       │        ┌──────────────┐            │
       │        │  Error       │            │ (user sends message)
       │        │ (report,     │            │
       │        │  retry later)│            v
       │        └──────────────┘     ┌──────────────┐
       │                             │  Processing  │
       │                             │ (pi is       │
       │                             │  responding) │
       │                             └──────┬───────┘
       │                                    │
       │                    (response done / error / timeout)
       │                                    │
       └────────────────────────────────────┘
```

**Lazy spawning**: The agent process is spawned on first `POST /ask` for a
project, not on drawer open. This avoids running a resource-heavy Node.js
process for every project the user browses.

**Per-project isolation**: Each project gets its own Pi process. This keeps the
agent's context focused on one project and prevents cross-project contamination.

**Timeout**: If the agent doesn't respond within 5 minutes, the process is
killed and restarted. The SSE stream delivers an `error` event.

### MCP connection from Pi to BioFlow

Pi connects to BioFlow's MCP server via an MCP extension that reads a config
file. The backend generates this config file dynamically per project and
passes it to the Pi process via `--mcp-config`:

```
pi --mode rpc --no-session --mcp-config /tmp/bioflow-mcp-config.json
```

The config file contents:

```json
{
  "mcpServers": {
    "bioflow": {
      "url": "http://localhost:8000/api/v1/mcp?profile=<profile_id>"
    }
  }
}
```

Additional MCP servers can be added to the same config file. Each server gets
its own key and URL. The profile id is the one the user selected in the UI —
the backend resolves it from the request context (X-BioFlow-Profile header)
and embeds it in the URL.

**Why a config file instead of a flag?** Pi uses the MCP extension pattern,
which reads a config file rather than accepting a single URL on the command
line. This is more flexible: it supports multiple servers, each with their own
options (timeouts, headers, auth). The trade-off is that the backend must
write a temp file and clean it up.

### RPC protocol flow

```
[Backend]                     [Pi process]
    |                              |
    |--- stdin: {"type":"prompt",  |
    |           "message":"..."}   |
    |                              |
    |    stdout: {"type":"response",  |
    |             "command":"prompt", |
    |             "success":true}     |
    |                              |
    |    stdout: {"type":"message_update",  |
    |             "assistantMessageEvent":  |
    |               {"type":"text_delta",   |
    |                "delta":"partial..."}} |
    |    stdout: {"type":"message_update",  |
    |             "assistantMessageEvent":  |
    |               {"type":"text_delta",   |
    |                "delta":"more text"}}  |
    |                              |
    |    stdout: {"type":"tool_execution_start",  |
    |             "toolName":"bioflow_search_..."}|
    |                              |
    |    stdout: {"type":"tool_execution_end",    |
    |             "result":{...}}  |
    |                              |
    |    stdout: {"type":"message_update", ...}   |
    |    ... more text deltas ...  |
    |                              |
    |    stdout: {"type":"agent_end"}  |
    |                              |
```

The backend translates these Pi events into SSE events for the frontend.

## Pi process management

### Spawning

```python
# Pseudocode for AgentService
import asyncio
import json
import tempfile

class AgentProcess:
    def __init__(self, project_id: str, owner: str, mcp_config: dict, pi_path: str):
        self.project_id = project_id
        self.owner = owner
        self.mcp_config = mcp_config  # dict with mcpServers key
        self.pi_path = pi_path
        self.process: asyncio.subprocess.Process | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._mcp_config_file: str | None = None

    async def start(self):
        # Write MCP config to a temp file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(self.mcp_config, f)
            self._mcp_config_file = f.name

        self.process = await asyncio.create_subprocess_exec(
            self.pi_path,
            "--mode", "rpc",
            "--no-session",  # No persistence for first slice
            "--mcp-config", self._mcp_config_file,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Start reading stdout in background
        asyncio.create_task(self._read_stdout())
        asyncio.create_task(self._read_stderr())

    async def send_prompt(self, message: str):
        cmd = json.dumps({
            "id": str(uuid4()),
            "type": "prompt",
            "message": message,
        })
        self.process.stdin.write((cmd + "\n").encode())
        await self.process.stdin.drain()

    async def _read_stdout(self):
        buffer = ""
        while True:
            chunk = await self.process.stdout.read(4096)
            if not chunk:
                break
            buffer += chunk.decode()
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if line.strip():
                    await self._queue.put(json.loads(line))

    async def _read_stderr(self):
        # Log stderr for debugging
        while True:
            line = await self.process.stderr.readline()
            if not line:
                break
            log.warning("pi_stderr", line=line.decode().strip())

    async def stop(self):
        if self.process:
            self.process.terminate()
            await self.process.wait()
        # Clean up temp file
        if self._mcp_config_file:
            try:
                os.unlink(self._mcp_config_file)
            except OSError:
                pass
```

### System prompt injection

The system prompt can be set via Pi's `set_system_prompt` RPC command right
after spawning, or by passing a file path via `--system-prompt-file`.

```json
{"type": "set_system_prompt", "prompt": "You are a bioinformatics assistant..."}
```

### Health monitoring

The backend sends a periodic heartbeat to check if the process is alive:

```json
{"type": "get_state"}
```

If no response within 10 seconds, the process is considered dead and restarted.

## Files to create

### Backend

| File | Purpose |
|------|---------|
| `backend/app/services/agent_service.py` | `AgentService` class: process lifecycle, MCP config generation, message proxying, SSE event translation |
| `backend/app/api/v1/agent.py` | API router: `POST /ask`, `GET /events` (SSE), `DELETE /`, `POST /restart` |
| `backend/app/models/agent.py` | Agent session state model (if any persistence needed later) |

### Frontend

| File | Purpose |
|------|---------|
| `frontend/src/components/AgentPanel.tsx` | Main drawer component |
| `frontend/src/components/AgentMessageBubble.tsx` | Individual message rendering (user + agent) |
| `frontend/src/components/AgentPanelInput.tsx` | Input box + send button |
| `frontend/src/hooks/useAgentSSE.ts` | Hook managing SSE connection lifecycle |
| `frontend/src/styles/agent.css` | Styles for the agent panel |

### Configuration & Assets

| File | Purpose |
|------|---------|
| `backend/app/config.py` | Add `PI_PATH`, `PI_EXTENSIONS_DIR`, `PI_SKILLS_DIR` config settings |
| `pi-skills/` (new directory) | Pre-installed skills for bioinformatics workflows |
| `pi-extensions/` (new directory, optional) | Additional custom extensions for BioFlow |

### MCP config generation

The backend generates an MCP config file per project session at spawn time:

```python
def build_mcp_config(project_id: str, profile_id: str) -> dict:
    """Build the MCP servers config for this project session."""
    config = {
        "mcpServers": {
            "bioflow": {
                "url": f"http://localhost:8000/api/v1/mcp?profile={profile_id}"
            }
        }
    }
    # Additional MCP servers can be injected here from settings
    return config
```

This file is written to a temp file, passed to Pi via `--mcp-config`, and
cleaned up when the process stops.

## Files to modify

### Backend

| File | Change |
|------|--------|
| `backend/app/main.py` | Mount the agent router |
| `backend/app/config.py` | Add `PI_PATH`, `PI_DISABLED`, `PI_EXTENSIONS_DIR`, `PI_SKILLS_DIR`, `AGENT_RESPONSE_TIMEOUT`, `AGENT_IDLE_TIMEOUT` settings |

### Frontend

| File | Change |
|------|--------|
| `frontend/src/App.tsx` | Add agent drawer to project detail view |
| `frontend/src/api/client.ts` | Add `askAgent`, `getAgentEvents`, `restartAgent`, `stopAgent` methods |
| `frontend/src/api/types.ts` | Add `AgentEvent` type |
| `frontend/src/styles.css` | Import `agent.css` |

## Configuration

```python
# backend/app/config.py additions
PI_PATH: str = "/usr/local/bin/pi"  # Path to pi CLI binary
PI_DISABLED: bool = False  # Set True to hide agent UI entirely
PI_EXTENSIONS_DIR: str = "~/.pi/agent/extensions"  # Extensions directory
PI_SKILLS_DIR: str = "~/.pi/agent/skills"  # Skills directory
AGENT_RESPONSE_TIMEOUT: int = 300  # Seconds before killing unresponsive agent
AGENT_IDLE_TIMEOUT: int = 1800  # Kill agent after 30 min of inactivity
```

On Docker, Pi would need to be installed in the `api` container or accessible
via a bind mount. The `api` container installs Pi via npm and also installs
the MCP extension and any desired skills/extensions:

```dockerfile
# In backend/Dockerfile, or docker-compose.override.yml
RUN npm install -g @earendil-works/pi-coding-agent

# Install MCP extension (required for MCP server connectivity)
RUN mkdir -p /root/.pi/agent/extensions \
  && npm install -g @earendil-works/pi-mcp-extension \
  && ln -s /usr/local/lib/node_modules/@earendil-works/pi-mcp-extension /root/.pi/agent/extensions/mcp

# Install skills (optional, teaches Pi about bioinformatics workflows)
COPY ./pi-skills/ /root/.pi/agent/skills/
```

Alternatively, for development, mount these from the host:

```yaml
# docker-compose.override.yml
services:
  api:
    volumes:
      - /usr/local/bin/pi:/usr/local/bin/pi:ro
      - /usr/local/lib/node_modules/@earendil-works/pi-mcp-extension:/root/.pi/agent/extensions/mcp:ro
      - ./pi-skills:/root/.pi/agent/skills:ro
```

## First slice scope

What ships in the initial implementation:

- [x] Project-scoped agent drawer UI
- [x] Backend spawns Pi as a subprocess per project
- [x] Pi connects to BioFlow's MCP server via MCP extension and config file
- [x] User sends message, agent responds with streaming text
- [x] Tool calls are visible as compact inline indicators
- [x] Single conversation per project (no persistence across sessions)
- [x] Agent restart and stop controls
- [x] Error handling: process crash, timeout, connection loss
- [x] SSE-based streaming response delivery
- [x] Pi installed in the Docker api container
- [x] MCP extension installed in the Docker api container
- [x] Placeholder skills directory for bioinformatics workflows
- [x] Dynamic MCP config generation per project session

Deliberately out of scope for the first slice:

- **Conversation persistence** — closing the drawer or reloading the page loses
  the conversation. Persistence (saving to a `ProjectAgentConversation` model)
  is a follow-up.
- **Multiple conversations** — one ongoing conversation per project.
- **File upload / image attachment** — Pi supports images in prompts, but the
  first slice is text-only.
- **Abort mid-response** — the user can stop the agent process, but there's no
  graceful mid-stream abort.
- **Custom system prompt editing** — the system prompt is hardcoded.
- **Multi-project agent** — the agent is always scoped to one project.

## Testing

### Backend tests

`backend/tests/services/test_agent_service.py`:
- Process spawning and lifecycle
- Message sending and response parsing
- Error handling (process crash, timeout)
- SSE event translation

`backend/tests/api/test_agent.py`:
- Endpoint availability and auth
- SSE stream delivery
- Error responses

### Frontend testing

Manual testing at localhost:5173 (no headless component tests in this repo):
- Open agent drawer from project view
- Send a message, observe streaming response
- Observe tool call indicators
- Restart agent
- Close and reopen drawer (conversation resets)
- Error scenarios: kill agent process, observe reconnection

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Pi is not installed or outdated | `PI_DISABLED` flag hides the UI; clear error message guides installation |
| Pi process consumes too much memory | Per-project processes capped; idle timeout kills after 30 min of inactivity |
| MCP connection fails | Retry logic on spawn; clear error in SSE stream |
| MCP extension not installed | Check for extension file before spawning; clear error message |
| Pi's Node.js version incompatible | Document required Node version; pin Pi version in Dockerfile |
| Multiple projects each run a Pi process | Each process is ~100-200 MB; for a single-user tool this is acceptable |
| SSE connection drops mid-response | Frontend reconnects and sends a follow-up to get the current state |
| Extension/tool name conflicts | Pi handles conflicts via priority ordering; document known conflicts |
| Skills directory missing | Graceful fallback: Pi continues without skills; log a warning |
| Pi's response is slow (>5 min) | Timeout kills the process and reports error; user can restart |

## Future slices (post-first-slice)

- Conversation persistence (`ProjectAgentConversation` model, like
  `ProjectConversation` for Q&A)
- Multi-turn context preservation across sessions (compaction, like Q&A)
- Image/file attachment support
- Abort mid-stream
- Editable system prompt
- Agent status indicator in the footer (connected/disconnected/thinking)
- Agent process pooling (share one process across projects, resetting context)
- Multiple conversation threads per project
- Pi version check and auto-update notification
