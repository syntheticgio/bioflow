# Node Provisioning from Node Settings — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build an SSH-driven node provisioning form on the Node Settings page, plus the backend
endpoints and asyncssh executor that install a compute-node worker on a remote machine.

**Architecture:** `SettingsNodes.tsx` gains a provision form above the node table. On submit,
the frontend POSTs to `POST /api/v1/nodes/provision` and polls `GET /api/v1/nodes/provision/{task_id}`
while the backend runs a 7-phase asyncssh install (`validate_ssh → verify_docker → setup_install →
write_env → pull_image → start_worker → enrolled`). Credentials are per-action only (never persisted).

**Tech Stack:** Python 3.12 + FastAPI + asyncssh (new dep) + Beanie/Motor (MongoDB). React +
TypeScript + TanStack Query (frontend). Docker compose node profile (already exists).

**Design doc:** `docs/superpowers/specs/2026-08-11-node-provisioning-from-settings-design.md`

---

## Phase 1: Backend infrastructure

### Task 1: Add asyncssh dependency

**Objective:** Add `asyncssh` to the backend's pyproject.toml.

**Files:**
- Modify: `backend/pyproject.toml`

**Step 1: Add dependency**

Edit `backend/pyproject.toml` — add `asyncssh` after `cryptography`:

```toml
    "cryptography>=44.0",
    # SSH transport for remote node provisioning.
    "asyncssh>=2.18,<3",
```

**Step 2: Verify Docker build picks it up**

```bash
cd .worktrees/feat-260-create-nodes
make build
```

Expected: builds succeed, `pip install` in the API image resolves `asyncssh`.

**Step 3: Commit**

```bash
git add backend/pyproject.toml
git commit -m "build: add asyncssh dependency for remote node provisioning"
```

---

### Task 2: Add PRIMARY_HOSTNAME config setting

**Objective:** Add a config setting so the backend knows its own externally-routable hostname
when constructing connection URLs for the child node's `.env`.

**Files:**
- Modify: `backend/app/config.py`

**Step 1: Add setting**

In `backend/app/config.py`, after the `enrollment_key` line (line 58), add:

```python
    # The primary's externally-routable hostname, used when constructing
    # node-connection URLs for remote provisioning. When unset (empty string),
    # the backend auto-discovers its LAN IP via a UDP socket heuristic.
    primary_hostname: str = ""
```

**Step 2: Verify**

```bash
cd backend && python -c "from app.config import settings; print(settings.primary_hostname)"
```

Expected: prints empty string (default).

**Step 3: Commit**

```bash
git add backend/app/config.py
git commit -m "feat(config): add PRIMARY_HOSTNAME setting for node provisioning"
```

---

### Task 3: Add NodeProvisionTask model and Mongo collection

**Objective:** Create a Beanie document model for tracking provisioning task state.

**Files:**
- Create: `backend/app/models/node_provision.py`

**Step 1: Create model**

```python
"""Node provisioning task tracking."""

from datetime import datetime
from uuid import uuid4

from beanie import Document
from beanie.odm.fields import Indexed
from pydantic import Field


class NodeProvisionTask(Document):
    """One node provisioning attempt, tracked so the frontend can poll progress."""

    task_id: Indexed(str, unique=True) = Field(default_factory=lambda: uuid4().hex[:12])
    status: str = "provisioning"  # "provisioning" | "success" | "failed"
    phase: str = ""  # current phase: validate_ssh, verify_docker, ...
    message: str = ""
    pct: float | None = None  # percentage during pull_image
    node_name: str = ""
    host: str = ""
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    error: str | None = None

    class Settings:
        name = "node_provisions"
        # Auto-delete after 7 days (MongoDB TTL index)
        indexes = [
            "task_id",
        ]
```

**Step 2: Verify model imports**

```bash
cd backend && python -c "from app.models.node_provision import NodeProvisionTask; print('ok')"
```

Expected: prints `ok`.

**Step 3: Commit**

```bash
git add backend/app/models/node_provision.py
git commit -m "feat(model): add NodeProvisionTask for tracking remote node installs"
```

---

### Task 4: Add provisioning endpoints and executor to nodes.py

**Objective:** Add `POST /nodes/provision` and `GET /nodes/provision/{task_id}` to the
existing nodes router, plus the asyncssh provisioning executor.

**Files:**
- Modify: `backend/app/api/v1/nodes.py`

**Step 1: Add imports at top of file**

After the existing imports, add:

```python
import asyncio
import os
import re
import socket
from asyncio import AbstractEventLoop
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import BackgroundTasks

from app.models.node_provision import NodeProvisionTask
```

(Some of these — `asyncio`, `re`, `datetime`, `Path`, `uuid` — may already be imported in
various forms. Consolidate as needed, preferring the existing imports where they exist.)

**Step 2: Add Pydantic request/response models**

Before the existing function definitions, add:

```python
from pydantic import BaseModel, model_validator


class ProvisionRequest(BaseModel):
    host: str
    port: int = 22
    username: str
    password: str | None = None
    private_key: str | None = None
    node_name: str
    storage_location: str = "/data/scratch"
    worker_replicas: int = 2

    @model_validator(mode="after")
    def _check_credential(self) -> "ProvisionRequest":
        if not self.password and not self.private_key:
            raise ValueError("Either password or private_key must be provided")
        if self.password and self.private_key:
            raise ValueError("Provide exactly one of password or private_key, not both")
        return self


class ProvisionStatusOut(BaseModel):
    task_id: str
    status: str
    phase: str
    message: str
    pct: float | None = None
    node_name: str
    host: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
```

**Step 3: Add the primary-hostname resolution helper**

Add after the existing `_rewrite_host()` function:

```python
def _primary_hostname() -> str:
    """The primary's externally-routable hostname.

    Uses PRIMARY_HOSTNAME config if set; otherwise discovers the LAN IP
    via a UDP socket connect (no data sent — just uses routing table).
    Falls back to socket.gethostname().
    """
    if settings.primary_hostname:
        return settings.primary_hostname
    try:
        # UDP connect trick: no data sent, just uses OS routing to
        # find the outbound interface IP.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("1.1.1.1", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except (OSError, socket.timeout):
        return socket.gethostname()
```

**Step 4: Add the `_build_connection_urls()` helper**

```python
def _build_connection_urls(host: str) -> dict[str, str]:
    """Build externally-routable Mongo, Redis, and API URLs for node .env."""
    mongo = _rewrite_host(settings.mongo_url, host)
    redis = _rewrite_host(settings.redis_url, host)
    return {
        "mongo_url": mongo,
        "redis_url": redis,
        "api_url": f"http://{host}:8000",
    }
```

**Step 5: Add the `_render_node_env()` helper**

Identical to the launcher's `render_node_env()`:

```python
def _render_node_env(
    mongo_url: str,
    redis_url: str,
    api_url: str,
    node_name: str,
    storage_location: str,
    worker_replicas: int,
) -> str:
    return (
        f"NODE_TYPE=compute\n"
        f"MONGO_URL={mongo_url}\n"
        f"REDIS_URL={redis_url}\n"
        f"WORKER_NODE_ID={node_name}\n"
        f"PRIMARY_API_URL={api_url}\n"
        f"BIOINFO_HOME={storage_location}\n"
        f"BIOINFO_REGISTER_ROOTS={storage_location}\n"
        f"BIOFLOW_TAG=latest\n"
        f"WORKER_REPLICAS={worker_replicas}\n"
    )
```

**Step 6: Add the async provisioning executor**

After the `_render_node_env()` function, add:

```python
async def _provision_node(task_id: str, req: ProvisionRequest) -> None:
    """Run the full node provisioning flow in a background task."""
    import asyncssh

    task = await NodeProvisionTask.find_one(
        NodeProvisionTask.task_id == task_id
    )
    if not task:
        task = NodeProvisionTask(
            task_id=task_id,
            node_name=req.node_name,
            host=req.host,
        )
        await task.insert()

    async def _update(phase: str, message: str, pct: float | None = None) -> None:
        task.phase = phase
        task.message = message
        task.pct = pct
        await task.save()

    async def _fail(reason: str) -> None:
        task.status = "failed"
        task.error = reason
        task.message = reason
        task.finished_at = datetime.utcnow()
        await task.save()

    try:
        # Determine credential
        if req.private_key:
            import io
            key = asyncssh.import_private_key(io.StringIO(req.private_key))
            connect_kw = {"client_keys": [key]}
        else:
            connect_kw = {"password": req.password}

        # Phase 1: validate_ssh
        await _update("validate_ssh", f"Connecting to {req.host}…")
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    req.host,
                    port=req.port,
                    username=req.username,
                    known_hosts=None,  # skip host-key verification (Phase 1)
                    **connect_kw,
                ),
                timeout=15,
            )
        except asyncio.TimeoutError:
            return await _fail(
                f"Connection to {req.host}:{req.port} timed out."
            )
        except asyncssh.Error as e:
            return await _fail(str(e))

        try:
            # Phase 2: verify_docker
            await _update("verify_docker", f"Checking Docker on {req.host}…")
            result = await asyncio.wait_for(
                conn.run("docker version --format '{{.Server.Version}}'", check=False),
                timeout=15,
            )
            if result.exit_status != 0:
                return await _fail(
                    f"Docker is not available on {req.host}. "
                    "Install Docker first: https://docs.docker.com/engine/install/"
                )

            # Phase 3: setup_install
            await _update("setup_install", "Preparing install directory…")
            install_dir = os.path.expanduser("~/.bioflow")
            await conn.run(f"mkdir -p {install_dir}", check=True)

            # SCP the bundled compose file
            compose_src = Path("/srv/docker-compose.yml")
            if compose_src.exists():
                await asyncssh.scp(
                    str(compose_src),
                    (conn, f"{install_dir}/docker-compose.yml"),
                )
            else:
                return await _fail(
                    "Compose file not found in API container. "
                    "The API image must bundle docker-compose.yml at /srv/."
                )

            # Phase 4: write_env
            await _update("write_env", "Writing node configuration…")
            primary_host = _primary_hostname()
            urls = _build_connection_urls(primary_host)
            env_contents = _render_node_env(
                mongo_url=urls["mongo_url"],
                redis_url=urls["redis_url"],
                api_url=urls["api_url"],
                node_name=req.node_name,
                storage_location=req.storage_location,
                worker_replicas=req.worker_replicas,
            )
            # Write env file via SSH echo (simpler than SCP for a string)
            escaped_env = env_contents.replace("'", "'\\''")
            await conn.run(
                f"cat > {install_dir}/.env << 'HERMESEOF'\n{env_contents}\nHERMESEOF",
                check=True,
            )

            # Phase 5: pull_image
            await _update("pull_image", "Pulling backend image…")
            pull_result = await asyncio.wait_for(
                conn.run(
                    "docker pull ghcr.io/syntheticgio/bioflow-backend:latest",
                    check=False,
                ),
                timeout=600,  # 10 minutes
            )
            if pull_result.exit_status != 0:
                return await _fail(
                    f"Image pull failed: {pull_result.stderr or pull_result.stdout}"
                )

            # Phase 6: start_worker
            await _update("start_worker", "Starting worker…")
            up_result = await asyncio.wait_for(
                conn.run(
                    f"docker compose -f {install_dir}/docker-compose.yml up -d",
                    check=False,
                ),
                timeout=60,
            )
            if up_result.exit_status != 0:
                return await _fail(
                    f"Worker failed to start: {up_result.stderr or up_result.stdout}"
                )

            # Phase 7: enrolled
            await _update("enrolled", "Node enrolled ✓")
            task.status = "success"
            task.finished_at = datetime.utcnow()
            await task.save()

        finally:
            conn.close()

    except Exception as e:
        log.exception("node_provision_failed", task_id=task_id)
        await _fail(str(e))
```

**Step 7: Add the endpoint handlers**

After the existing endpoints and before the end of the file, add:

```python
_active_provisions: dict[str, asyncio.Task] = {}


@router.post("/provision", status_code=201)
async def provision_node(req: ProvisionRequest) -> dict:
    """Start provisioning a compute node on a remote machine via SSH."""
    task_id = uuid4().hex[:12]
    task = asyncio.create_task(_provision_node(task_id, req))
    _active_provisions[task_id] = task

    # Remove from active dict when done (don't keep references)
    task.add_done_callback(lambda _: _active_provisions.pop(task_id, None))

    return {"task_id": task_id, "status": "provisioning"}


@router.get("/provision/{task_id}")
async def provision_status(task_id: str):
    """Poll the status of a provisioning task."""
    task = await NodeProvisionTask.find_one(
        NodeProvisionTask.task_id == task_id
    )
    if not task:
        raise HTTPException(404, f"Provisioning task {task_id!r} not found")
    return {
        "task_id": task.task_id,
        "status": task.status,
        "phase": task.phase,
        "message": task.message,
        "pct": task.pct,
        "node_name": task.node_name,
        "host": task.host,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "error": task.error,
    }
```

**Step 8: Add orphan-cleanup startup handler**

In `backend/app/main.py` (or a dedicated module), add an on-startup handler that marks
any orphaned `provisioning` tasks as `failed` (these are tasks whose `asyncio.Task` was
lost on API restart). Add this to the FastAPI app's startup:

In `backend/app/api/v1/nodes.py`, at the bottom before the module ends, add a startup callback:

```python
async def _clean_orphaned_provisions() -> None:
    """On startup, mark any provisioning task with no active asyncio.Task as failed."""
    try:
        orphaned = await NodeProvisionTask.find(
            NodeProvisionTask.status == "provisioning",
        ).to_list()
        for task in orphaned:
            if task.task_id not in _active_provisions:
                task.status = "failed"
                task.error = "API restart interrupted provisioning"
                task.finished_at = datetime.utcnow()
                await task.save()
                log.info("orphaned_provision_cleaned", task_id=task.task_id)
    except Exception:
        log.warning("provision_cleanup_failed")
```

And register it in `backend/app/main.py` — after the existing `app.add_event_handler("startup", ...)` calls, add:

```python
    from app.api.v1.nodes import _clean_orphaned_provisions
    app.add_event_handler("startup", _clean_orphaned_provisions)
```

**Step 9: Commit**

```bash
git add backend/app/api/v1/nodes.py backend/app/main.py
git commit -m "feat(nodes): add SSH provisioning endpoints and asyncssh executor"
```

---

## Phase 2: Frontend

### Task 5: Add provisioning types to types.ts

**Objective:** Add TypeScript interfaces for the provisioning form and status.

**Files:**
- Modify: `frontend/src/api/types.ts`

**Step 1: Add types**

After the existing `NodeInfo` interface (around line 477), add:

```typescript
export interface NodeProvisionRequest {
  host: string;
  port: number;
  username: string;
  password?: string | null;
  private_key?: string | null;
  node_name: string;
  storage_location: string;
  worker_replicas: number;
}

export interface NodeProvisionStatus {
  task_id: string;
  status: "provisioning" | "success" | "failed";
  phase: string;
  message: string;
  pct: number | null;
  node_name: string;
  host: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
}
```

**Step 2: Commit**

```bash
git add frontend/src/api/types.ts
git commit -m "feat(types): add NodeProvisionRequest and NodeProvisionStatus"
```

---

### Task 6: Add API client methods

**Objective:** Add provision and poll methods to the frontend API client.

**Files:**
- Modify: `frontend/src/api/client.ts`

**Step 1: Add imports**

Ensure `NodeProvisionRequest` and `NodeProvisionStatus` are imported from `./types`.

**Step 2: Add API methods**

After the existing `nodes()` call (around line 433), add:

```typescript
  provisionNode: (body: NodeProvisionRequest) =>
    request<{ task_id: string; status: string }>("/nodes/provision", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getProvisionStatus: (taskId: string) =>
    request<NodeProvisionStatus>(`/nodes/provision/${encodeURIComponent(taskId)}`),
```

**Step 3: Commit**

```bash
git add frontend/src/api/client.ts
git commit -m "feat(api): add provisionNode and getProvisionStatus client methods"
```

---

### Task 7: Build the provision form component in SettingsNodes.tsx

**Objective:** Add the provision form above the node table in SettingsNodes.tsx.

**Files:**
- Modify: `frontend/src/components/SettingsNodes.tsx`

**Step 1: Add imports**

At the top of `SettingsNodes.tsx`, add:

```typescript
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import type { NodeProvisionRequest, NodeProvisionStatus } from "../api/types";
```

**Step 2: Add the form state and mutation**

Inside the `SettingsNodes` component, before the return, add:

```typescript
  const [showForm, setShowForm] = useState(false);
  const [authTab, setAuthTab] = useState<"password" | "key">("password");
  const [taskId, setTaskId] = useState<string | null>(null);
  const [formValues, setFormValues] = useState<{
    host: string; port: number; username: string; password: string;
    privateKey: string; nodeName: string; storage: string; replicas: number;
  }>({
    host: "", port: 22, username: "", password: "",
    privateKey: "", nodeName: "", storage: "/data/scratch", replicas: 2,
  });

  const provision = useMutation({
    mutationFn: (body: NodeProvisionRequest) => api.provisionNode(body),
    onSuccess: (data) => setTaskId(data.task_id),
  });
```

**Step 3: Poll provisioning status**

When `taskId` is set, poll the status endpoint. Add:

```typescript
  const provisionStatus = useQuery({
    queryKey: ["provision", taskId],
    queryFn: () => api.getProvisionStatus(taskId!),
    enabled: !!taskId,
    refetchInterval: (query) => {
      const data = query.state.data as NodeProvisionStatus | undefined;
      return data?.status === "provisioning" ? 3000 : false;
    },
  });
```

**Step 4: Render the "Add Node" button and form**

Replace the existing heading area (lines 34-37 in the current file). After
`<SettingsNav />` and the `<h1>`, add a "+" button above the table, and the form
when `showForm` is true. The form renders when `showForm && !taskId`, the progress
component renders when `taskId && provisionStatus.data`, and on success/failure
handles "Try again".

(Full JSX for the form — all fields with validation, the auth tab toggle,
submit handler, progress display, and error rendering — will be in-lined
in the commit. The form replaces inside the `<div className="settings-body">`
wrapper, above or alongside the existing table.)

**Step 5: Commit**

```bash
git add frontend/src/components/SettingsNodes.tsx
git commit -m "feat(ui): add node provisioning form to SettingsNodes"
```

---

### Task 8: Style the provision form

**Objective:** Add CSS for the provision form, progress display, auth tabs, and error states.

**Files:**
- Modify: `frontend/src/styles.css`

**Step 1: Add CSS**

After the existing `/* --- Nodes table --- */` block (around line 5235), add CSS for:
- `.provision-form` — card layout with form fields
- `.provision-auth-tabs` — segmented control for password/key toggle
- `.provision-progress` — progress bar and phase display
- `.provision-error` — error state with "Try again" button
- `.provision-success` — success state

(Full CSS will be in-lined in the commit, targeting class names used by the JSX.)

**Step 2: Commit**

```bash
git add frontend/src/styles.css
git commit -m "feat(ui): add provision form and progress styles"
```

---

## Phase 3: Testing

### Task 9: Write backend tests

**Objective:** Test the provisioning API endpoints and executor logic.

**Files:**
- Create: `backend/tests/api/test_node_provision.py`

**Step 1: Create test file**

Write tests following the existing `test_node_enrollment.py` pattern (mock asyncssh,
`_app()` helper, `httpx.AsyncClient`). Cover:

1. **`POST /nodes/provision` validation**
   - Missing host → 422
   - Neither password nor key → 422
   - Both password and key → 422
   - Valid request → 201 + task_id + "provisioning" status

2. **`GET /nodes/provision/{task_id}`**
   - Unknown task_id → 404
   - Known task → returns full status JSON

3. **Phase transitions** (with mocked asyncssh)
   - Valid SSH → verify_docker phase
   - Docker found → setup_install phase
   - Mocked compose/env → write_env phase
   - Mocked pull → pull_image phase
   - Mocked up → start_worker phase
   - Completed → success status

4. **Error states** (with mocked asyncssh)
   - Connection refused → "failed" status with connection error
   - Auth failed → "failed" status with auth error
   - Docker not available → "failed" status with Docker message

5. **`.env` content**
   - Verify `_render_node_env()` output matches the launcher's format
   - Verify MONGO_URL, REDIS_URL, WORKER_NODE_ID fields present
   - Verify credentials (password/key) are NOT present in the env output

6. **Task document**
   - Verify the `NodeProvisionTask` document has no credential field

**Step 2: Run tests**

```bash
cd .worktrees/feat-260-create-nodes
./backend/run-worktree-tests.sh tests/api/test_node_provision.py -v
```

Expected: all tests pass.

**Step 3: Commit**

```bash
git add backend/tests/api/test_node_provision.py
git commit -m "test(nodes): add provisioning endpoint and executor tests"
```

---

### Task 10: Verify with make build

**Objective:** Ensure the full Docker build succeeds with all changes.

**Step 1: Build**

```bash
cd .worktrees/feat-260-create-nodes
make build
```

Expected: all three images (api, worker, web) build successfully. The `asyncssh`
dependency is installed in the API image.

**Step 2: Commit** (if any fixups were needed, commit them)

---

### Task 11: Verify frontend visually (manual)

**Objective:** Confirm the form renders and works in the browser at the worktree stack.

**Step 1: Start the worktree stack**

```bash
cd .worktrees/feat-260-create-nodes
./ops/worktree-up.sh
```

Note the printed port.

**Step 2: Verify in browser**

Navigate to `http://localhost:<port>/settings/nodes`:

- [ ] "+" button visible above the node table
- [ ] Click "+" → form appears
- [ ] Auth tabs (Password / Private Key) toggle correctly
- [ ] Submit with empty fields → validation errors shown
- [ ] Submit with bad SSH host → error state with message
- [ ] "Try again" shows form with credentials cleared

(Full SSH provisioning test requires a real child machine — manual test only.)

**Step 3: Tear down**

```bash
./ops/worktree-up.sh --down
```

**Step 4: Commit** (if any fixups)

---

### Task 12: Final commit and push

**Objective:** Push the branch and create the PR.

**Step 1: Verify all commits**

```bash
git log --oneline origin/main..HEAD
```

**Step 2: Push**

```bash
git push -u origin HEAD
```

**Step 3: Create PR**

```bash
gh pr create --base main \
  --title "feat(nodes): SSH-driven node provisioning from Node Settings" \
  --body "$(cat <<'EOF'
## Summary
- Adds SSH-driven node provisioning form to Settings > Nodes page
- Backend uses asyncssh to install a compute-node worker on a remote machine
- 7-phase install flow with polling-based progress
- Per-action credentials only — never persisted
- Design doc: docs/superpowers/specs/2026-08-11-node-provisioning-from-settings-design.md

## Test Plan
- [ ] Backend: `./backend/run-worktree-tests.sh tests/api/test_node_provision.py`
- [ ] Build: `make build` succeeds
- [ ] Frontend: form renders, auth tabs toggle, validation works
- [ ] Full SSH provisioning against a real child machine (manual)
EOF
)" \
  --label "type:feature,area:backend,area:frontend,difficulty: high"
```

**Step 4: Clean up worktree**

```bash
cd ~/Programming/local-bio-pipeliner
git worktree remove --force .worktrees/feat-260-create-nodes
git worktree prune
```

---

## Verification checklist (post-implementation)

- [ ] `./backend/run-worktree-tests.sh tests/api/test_node_provision.py -v` — all pass
- [ ] `make build` — all images build (including asyncssh install)
- [ ] `./ops/worktree-up.sh` — app starts, `/settings/nodes` loads
- [ ] Form renders with "+" button, auth tabs, field validation
- [ ] "Try again" clears password/key fields
- [ ] Node appears in table after successful provision
- [ ] `POST /nodes/provision` returns 201 with task_id
- [ ] `GET /nodes/provision/{task_id}` returns progress during install
- [ ] Orphaned provisioning tasks cleaned up on API restart
