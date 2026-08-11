# Node provisioning from Node Settings

Date: 2026-08-11
Status: Design
Issue: [#260](https://github.com/syntheticgio/bioflow/issues/260)

## Problem

Nodes can be added from the launcher (installing a child worker via the
launcher's node-onboarding flow), but the Node Settings page in the web UI
only lists existing nodes. There is no way to provision a new compute node
from the web UI itself.

## Goal

Node Settings offers a form that provisions a compute node on a remote
machine over SSH — the same install steps the launcher performs locally
(`validate → copy compose → write .env → docker pull → docker compose up worker`),
driven from the primary's web UI instead of from the child machine.

## Architecture

```
┌───────────────────────────┐       SSH (asyncssh)      ┌──────────────────────┐
│  Primary (web UI + API)   │ ──────────────────────────▶│  Child machine       │
│                           │                            │                     │
│  Node Settings form       │  1. validate SSH+Docker    │  Docker daemon      │
│        │                  │  2. SCP compose.yml        │        │            │
│        ▼                  │  3. write .env             │        ▼            │
│  POST /nodes/provision    │  4. docker pull (backend)  │  ┌─────────────┐    │
│        │                  │  5. docker compose up node │  │ worker      │───▶│ enrolls
│        ▼                  │                            │  └─────────────┘    │  with
│  Background task ─────────┤  progress via              │                     │  primary
│        │                  │  GET /nodes/provision/{id} │                     │
│        ▼                  │                            │                     │
│  Node appears in table ◀──┼── POST /nodes/enroll       │                     │
└───────────────────────────┘                            └──────────────────────┘
```

The backend replicates `launcher/src-tauri/src/setup/node.rs`'s
`install_node()` step for step, substituting SSH transport for local
filesystem and Docker commands.

## Scope

**In scope:**
- Provision a new compute node on a remote machine via SSH
- Form in Node Settings: host, SSH port, username, credential (password or
  private key), node name, storage location, worker replicas
- Install phases: validate SSH, verify Docker, create install dir, copy
  compose file, write `.env`, pull backend image, start worker
- Progress reporting during install (polling-based)
- Per-action credentials only — never persisted

**Out of scope (matches launcher contract):**
- Installing Docker on the child (link to docs, same as launcher)
- Data transfer between nodes (Phase 2)
- Managing SSH keys, authorized_keys, or firewalls
- Reinstalling or updating an existing node (future: PUT endpoint)
- Storing SSH credentials (per-action only)
- SSH agent forwarding, jump hosts, or bastion configurations

## Backend: SSH transport + provisioning executor

### New dependency

`asyncssh>=2.18` added to `backend/pyproject.toml`. Pure Python, async-native,
no system libraries. The repo already carries `aiofiles`, `psutil`, and
`cryptography` — asyncssh fits the same dependency profile.

### API endpoints

**`POST /api/v1/nodes/provision`**

Request body:

```json
{
  "host": "192.168.1.50",
  "port": 22,
  "username": "jane",
  "password": "ssh-password",
  "private_key": null,
  "node_name": "child-laptop",
  "storage_location": "/data/scratch",
  "worker_replicas": 2
}
```

Exactly one of `password` or `private_key` must be set. `port` defaults to 22.
`worker_replicas` defaults to 2.

Response (201):

```json
{
  "task_id": "prov_abc123",
  "status": "provisioning"
}
```

Returns immediately. The provisioning work runs as a background `asyncio.Task`.
Credentials are read from the request body and never persisted.

**`GET /api/v1/nodes/provision/{task_id}`**

Response:

```json
{
  "task_id": "prov_abc123",
  "status": "provisioning",
  "phase": "pull_image",
  "message": "Pulling backend image (45%)…",
  "node_name": "child-laptop",
  "host": "192.168.1.50",
  "started_at": "2026-08-11T14:30:00Z"
}
```

`status` is one of `provisioning | success | failed`. On failure, `message`
carries the error detail and the phase where it failed. On success,
`message` is `"Node enrolled"` and the new node appears in `GET /nodes`.

### Provisioning phases

Each phase reports to the status endpoint:

| Phase | Steps | Progress message |
|---|---|---|
| `validate_ssh` | Open asyncssh connection, handshake | "Connecting to <host>…" |
| `verify_docker` | Run `docker version` on the child | "Checking Docker on <host>…" |
| `setup_install` | `mkdir -p ~/.bioflow`, SCP the bundled compose file | "Preparing install directory…" |
| `write_env` | SCP `.env` (see `.env` contents below) | "Writing node configuration…" |
| `pull_image` | `docker pull ghcr.io/syntheticgio/bioflow-backend:latest` | "Pulling backend image…" |
| `start_worker` | `docker compose -f ~/.bioflow/docker-compose.yml up -d` (node profile) | "Starting worker…" |
| `enrolled` | Worker calls `POST /nodes/enroll`, appears in node list | "Node enrolled ✓" |

`pull_image` is the only phase lasting more than a few seconds (~minutes for a
cold pull of the ~8 GB backend image). The `docker pull` output is streamed;
the backend parses the progress lines that Docker writes to stderr and reports
`pct` when parseable.

### Node `.env` contents

Identical to `launcher/src-tauri/src/setup/node.rs:render_node_env()`:

```env
NODE_TYPE=compute
MONGO_URL=mongodb://<primary-ip>:27017/biopipe?replicaSet=rs0&directConnection=true
REDIS_URL=redis://<primary-ip>:6379/0
WORKER_NODE_ID=<node_name>
PRIMARY_API_URL=http://<primary-ip>:8000
BIOINFO_HOME=<storage_location>
BIOINFO_REGISTER_ROOTS=<storage_location>
BIOFLOW_TAG=latest
WORKER_REPLICAS=<worker_replicas>
```

The Mongo and Redis URLs are derived by applying the same `_rewrite_host()`
logic that `GET /nodes/connection-details` uses: internal Docker hostnames
(`mongo`, `redis`) are replaced with the primary's externally-routable
hostname. The primary's hostname is determined from the incoming request or
from a new `PRIMARY_HOSTNAME` config setting if the request comes via
localhost.

### Compose file

The production compose file (image-based, no `build:` directives) is bundled
into the API container at `/srv/docker-compose.yml` and SCP'd to the child
at `~/.bioflow/docker-compose.yml`. This is the same file the launcher ships
as a build resource.

### Connection details resolution

The child worker needs externally-routable Mongo/Redis URLs to reach the
primary. The backend already has `_rewrite_host()` in
`backend/app/api/v1/nodes.py` that replaces Docker service hostnames with the
request's client IP. For SSH provisioning, `request.client.host` is the
browser's IP (often localhost), which is not useful to the child machine.

Resolution: use `socket.gethostname()` or the primary's LAN IP. A new config
setting `PRIMARY_HOSTNAME` (optional, defaults to `socket.gethostname()`) lets
the user override when auto-detection picks the wrong interface. If unset, the
backend discovers its own routable IP via a UDP socket connect to a public
address (does not send data — just uses the OS routing table to find the
outbound interface).

## Frontend: Node Settings form

### Form fields

The form lives in `SettingsNodes.tsx`, above the existing node table. Fields:

| Field | Default | Validation |
|---|---|---|
| Hostname / IP | (required) | Non-empty, valid hostname or IPv4/v6 |
| SSH Port | 22 | 1–65535 |
| Username | (required) | Non-empty |
| Authentication | password (default tab) | One of password or private key required |
| Password | (masked) | Required if auth tab = password |
| Private Key | (textarea) | Required if auth tab = private key |
| Node Name | suggestion from `GET /nodes/connection-details` | Non-empty, valid hostname chars |
| Storage Location | `/data/scratch` | Non-empty |
| Worker Replicas | 2 | 1–8 |

A note below the authentication tab: "Credentials are used only for this
install and are not stored."

The authentication field is a segmented control (tab-style) toggling between
password and private key inputs. Only the visible input is sent in the request.

### Submit and progress

On submit: POST to `/nodes/provision`. The form is replaced with an inline
progress component showing:

- Current phase name (e.g. "Pulling backend image")
- Progress message
- Percentage bar (when `pct` is available; indeterminate bar during phases
  without a percentage)
- A cancel button (sends SSH interrupt; only meaningful during `pull_image`)

**On success:** the progress component shows a success state briefly, then
collapses. The node table below (which polls `GET /nodes` every 10s) picks up
the new node automatically.

**On failure:** the error message + failing phase are shown. A "Try again"
button re-shows the form with all values preserved **except credentials**
(password/key fields are cleared on retry). The user must re-enter the
credential to retry.

### Error states

| Failure | User sees |
|---|---|
| SSH unreachable | "Could not connect to 192.168.1.50:22. Check the hostname and that the SSH server is running." |
| SSH auth failed | "Authentication failed for user 'jane'. Check the password or key." |
| Docker not installed | "Docker is not installed on 192.168.1.50." + link to Docker install docs |
| Storage path missing | "Path /data/scratch does not exist on 192.168.1.50." + offer to pre-create (checkbox: "Create this directory") |
| Disk full during pull | "Image pull failed: no space left on device" |
| Worker fails to start | Docker compose log output shown in the error message |
| Image pull timeout | "Image pull timed out after 10 minutes. Check network connectivity to ghcr.io." |

### Pre-create storage directory

When `verify_docker` passes but `storage_location` doesn't exist, the progress
component offers "Create this directory on the node?" If the user confirms,
the backend runs `mkdir -p <path>` on the child via SSH before writing `.env`.

## Progress model

### Storage

A `node_installs` MongoDB collection (one document per task, TTL-indexed to
auto-delete after 7 days):

```python
class NodeProvisionTask(BaseModel):
    task_id: str          # opaque id returned to frontend
    status: str           # provisioning | success | failed
    phase: str            # current phase name
    message: str          # human-readable status
    pct: float | None     # percentage during pull_image, None otherwise
    node_name: str        # WORKER_NODE_ID
    host: str             # SSH target
    started_at: datetime
    finished_at: datetime | None
    error: str | None     # failure reason, if status == failed
```

### Transport

The frontend polls `GET /nodes/provision/{task_id}` at 3-second intervals
while `status == "provisioning"`. On `"success"` or `"failed"`, polling stops
and the final state is shown.

No SSE or Redis pub/sub for this single-task case — polling is simpler and
token-efficient for one consumer.

## Error handling (backend)

The asyncssh session is established once and reused across all phases.
If the session drops mid-provision:

- A `ConnectionError` is raised and caught by the provisioning task
- The task status transitions to `failed` with the error message
- The child machine is left in whatever state it reached (idempotent: a
  re-run overwrites the compose file and `.env`)
- `docker compose up` is idempotent (no-op if already running)

If the backend process restarts during provisioning:

- The `asyncio.Task` is lost
- The `node_installs` document remains with status `provisioning`
- A cleanup path in the API module (startup hook) transitions orphaned
  provisioning tasks to `failed` with `"API restart interrupted provisioning"`
- The compose file and `.env` on the child are intact — the user retries and
  the install continues from `setup_install` (overwrites files) or skips
  immediately to `start_worker` if the worker is detected as already running

## Testing

Backend: `./backend/run-worktree-tests.sh` from the worktree. New test file
`tests/api/test_node_provision.py`:

- Mock asyncssh to avoid real SSH connections
- Test form validation: missing host, invalid port, neither password nor key
- Test phase transitions against a mock SSH session
- Test error states: connection refused, auth failed, Docker missing
- Test `.env` content matches the launcher's `render_node_env()` output
- Test that credentials are never persisted (no credential field in the task
  document)

Frontend: no component tests (matches repo convention). Manual verification
on the worktree stack (`./ops/worktree-up.sh`):

- Form renders and validates fields
- Password/key tabs toggle correctly
- Progress component transitions through phases
- Error state renders and "Try again" clears credentials
- Success: node appears in the table

## Follow-ups

- Per-node enrollment keys (`POST /nodes/provision` accepts an optional
  `enrollment_key` that the `.env` carries as `WORKER_ENROLLMENT_KEY`)
- Reinstall/update an existing node via `PUT /nodes/provision/{node_id}`
- SSH key fingerprint verification on first connect (TOFU trust-on-first-use)
- Docker image tag selection (currently hardcoded to `BIOFLOW_TAG=latest`)
