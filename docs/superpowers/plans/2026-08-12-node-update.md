# Node Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show which compute nodes are running a stale backend image, and update them from Settings → Nodes over SSH.

**Architecture:** Workers report their image digest in the heartbeat, which the primary persists on the `Node` document. Provisioning installs a dedicated Ed25519 keypair on the node and stores its private half encrypted. `POST /nodes/{node_id}/update` opens SSH with that key and runs `docker pull` + `docker compose up -d` — the same two phases provisioning already performs — reporting success only once a worker re-enrolls with the pulled digest.

**Tech Stack:** FastAPI, Beanie/MongoDB, Redis, asyncssh, cryptography (Fernet), React + TanStack Query.

**Spec:** [`docs/superpowers/specs/2026-08-12-node-update-design.md`](../specs/2026-08-12-node-update-design.md) — requirement IDs (NU-N) below refer to it.

---

## File Structure

**Create:**
- `backend/app/models/node_update.py` — the `NodeUpdateTask` document.
- `backend/app/services/node_ssh.py` — SSH primitives: keypair generation, `authorized_keys` install, verification. No FastAPI imports; pure async functions over an `asyncssh` connection.
- `backend/app/services/node_update_service.py` — the update executor (connect → pull → drain → restart → verify).
- `frontend/src/lib/nodeStaleness.ts` — pure staleness/affordance logic, unit-testable without jsdom (mirrors `launcher/src/update-logic.ts`).
- `frontend/src/lib/nodeStaleness.test.ts`
- `backend/tests/services/test_node_ssh.py`
- `backend/tests/services/test_node_update_service.py`
- `backend/tests/api/test_node_update.py`

**Modify:**
- `backend/app/models/__init__.py` — register `NodeProvisionTask` and `NodeUpdateTask` in `ALL_MODELS`.
- `backend/app/models/node.py` — SSH connection fields and reported-version fields.
- `backend/app/queue/worker.py:507` — heartbeat carries `image_digest` and `version`; `_enroll` sends them too.
- `backend/app/api/v1/nodes.py` — `install_key` phase, update endpoints, digest persistence in `enroll_node`, digest surfaced from `enumerate_nodes`.
- `backend/app/main.py:140` — orphan sweep covers update tasks.
- `frontend/src/api/types.ts`, `frontend/src/api/client.ts` — types and calls.
- `frontend/src/components/SettingsNodes.tsx` — version column, Update control, drain dialog, progress.

**Why a service layer:** `nodes.py` is already ~600 lines. The SSH executor and key installation go in `app/services/` (following `provider_service.py`, `share_service.py`, etc.) so `nodes.py` keeps only routing and the existing provisioning flow.

---

## Task 1: Register node task documents with Beanie

`NodeProvisionTask` is missing from `ALL_MODELS`, so Beanie never initializes its collection. Verified against the running stack: `await NodeProvisionTask.find_all().count()` raises `CollectionWasNotInitialized`. **Provisioning is broken on `main` today**, and `NodeUpdateTask` would inherit the same bug. This task fixes it first and adds a regression test for the whole class of error.

**Files:**
- Modify: `backend/app/models/__init__.py:69`
- Test: `backend/tests/models/test_model_registration.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/models/test_model_registration.py`:

```python
"""Every Document subclass must be registered with init_beanie.

A model missing from ALL_MODELS raises CollectionWasNotInitialized on its
first query -- at runtime, in the one code path that uses it, with nothing
failing at import or startup. NodeProvisionTask shipped that way.
"""

import importlib
import pkgutil

from beanie import Document

import app.models
from app.models import ALL_MODELS


def _all_document_subclasses() -> set[type]:
    """Every Document subclass defined under app.models."""
    for mod in pkgutil.iter_modules(app.models.__path__):
        importlib.import_module(f"app.models.{mod.name}")

    found: set[type] = set()
    stack = [Document]
    while stack:
        for sub in stack.pop().__subclasses__():
            if sub in found:
                continue
            # Only models defined in this package -- not beanie internals.
            if sub.__module__.startswith("app.models"):
                found.add(sub)
            stack.append(sub)
    return found


def test_every_document_model_is_registered():
    registered = set(ALL_MODELS)
    missing = sorted(m.__name__ for m in _all_document_subclasses() - registered)
    assert not missing, (
        f"Document models missing from ALL_MODELS: {missing}. "
        "Unregistered models raise CollectionWasNotInitialized on first query."
    )
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/models/test_model_registration.py -q
```

Expected: FAIL — `Document models missing from ALL_MODELS: ['NodeProvisionTask']`

- [ ] **Step 3: Register the model**

In `backend/app/models/__init__.py`, add the import beside the other node import (near line 27):

```python
from app.models.node import Node
from app.models.node_provision import NodeProvisionTask
```

Add to the `ALL_MODELS` list (line 69), next to `Node`:

```python
    Node,
    NodeProvisionTask,
```

And to `__all__`:

```python
    "NodeProvisionTask",
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/models/test_model_registration.py -q
```

Expected: PASS

- [ ] **Step 5: Verify against the real database**

```bash
docker compose restart api && sleep 5 && docker compose exec -T api python -c "
import asyncio
from app.models.node_provision import NodeProvisionTask
async def probe():
    print('count =', await NodeProvisionTask.find_all().count())
asyncio.run(probe())"
```

Expected: `count = 0` — not `CollectionWasNotInitialized`. A green unit test alone does not prove this, because the test fixture initializes Beanie differently than the app does.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/__init__.py backend/tests/models/test_model_registration.py
git commit -m "fix(api): register NodeProvisionTask so provisioning can write its task document"
```

---

## Task 2: `NodeUpdateTask` document

**Files:**
- Create: `backend/app/models/node_update.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/models/test_model_registration.py` (already covers it)

- [ ] **Step 1: Create the model**

Create `backend/app/models/node_update.py`:

```python
"""Node update task tracking.

Mirrors NodeProvisionTask rather than sharing its collection: the phase
vocabularies differ, and a shared collection would make "when was this node
last updated" filter on a discriminator forever.
"""

from datetime import datetime
from uuid import uuid4

from beanie import Document
from beanie.odm.fields import Indexed
from pydantic import Field


class NodeUpdateTask(Document):
    """One attempt to update a compute node's backend image."""

    task_id: Indexed(str, unique=True) = Field(default_factory=lambda: uuid4().hex[:12])
    status: str = "updating"  # "updating" | "success" | "failed"
    phase: str = ""  # connect, pull_image, drain, restart, verify, done
    message: str = ""
    pct: float | None = None
    node_id: str = ""
    host: str = ""
    from_digest: str | None = None
    to_digest: str | None = None
    drain: bool = True
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    error: str | None = None

    class Settings:
        name = "node_updates"
        indexes = ["task_id", "node_id"]
```

- [ ] **Step 2: Register it**

In `backend/app/models/__init__.py`, add the import:

```python
from app.models.node_update import NodeUpdateTask
```

Add `NodeUpdateTask,` to `ALL_MODELS` and `"NodeUpdateTask",` to `__all__`.

- [ ] **Step 3: Run the registration test**

```bash
docker compose exec api python -m pytest tests/models/test_model_registration.py -q
```

Expected: PASS (the test from Task 1 now also covers `NodeUpdateTask`)

- [ ] **Step 4: Commit**

```bash
git add backend/app/models/node_update.py backend/app/models/__init__.py
git commit -m "feat(models): add NodeUpdateTask for tracking node image updates"
```

---

## Task 3: `Node` gains SSH and version fields

Implements the data model for NU-5, NU-13.

**Files:**
- Modify: `backend/app/models/node.py`
- Test: `backend/tests/api/test_nodes.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/api/test_nodes.py`:

```python
async def test_node_stores_ssh_and_version_fields():
    from app.models.node import Node

    node = Node(
        node_id="n1",
        ssh_host="10.0.0.5",
        ssh_port=2222,
        ssh_username="ops",
        ssh_key_enc=b"ciphertext",
        image_digest="sha256:abc",
        version="0.4.0",
    )
    await node.insert()

    loaded = await Node.find_one(Node.node_id == "n1")
    assert loaded.ssh_host == "10.0.0.5"
    assert loaded.ssh_port == 2222
    assert loaded.ssh_username == "ops"
    assert loaded.ssh_key_enc == b"ciphertext"
    assert loaded.image_digest == "sha256:abc"
    assert loaded.version == "0.4.0"
    await loaded.delete()


async def test_node_ssh_fields_default_to_none():
    """A hand-provisioned node has no stored key -- that is what makes it
    non-updatable, so the null must survive a round trip."""
    from app.models.node import Node

    node = Node(node_id="n2")
    await node.insert()

    loaded = await Node.find_one(Node.node_id == "n2")
    assert loaded.ssh_key_enc is None
    assert loaded.ssh_host is None
    assert loaded.ssh_port == 22
    assert loaded.image_digest is None
    await loaded.delete()
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/api/test_nodes.py -q -k "ssh_and_version or ssh_fields_default"
```

Expected: FAIL — `Node` has no field `ssh_host`

- [ ] **Step 3: Add the fields**

In `backend/app/models/node.py`, inside `class Node`, after `status`:

```python
    # How to reach this node over SSH for updates. Null on nodes that
    # enrolled themselves rather than being provisioned from the UI --
    # those report a version but cannot be updated from here.
    ssh_host: str | None = None
    ssh_port: int = 22
    ssh_username: str | None = None
    ssh_key_enc: bytes | None = None  # Fernet; the managed key's private half
    ssh_key_installed_at: datetime | None = None

    # What this node is running, from the worker heartbeat. Persisted here
    # rather than only in Redis because Redis entries expire with the worker,
    # and an offline node's last-known version is exactly what someone
    # deciding whether to bring it up wants to see.
    image_digest: str | None = None
    version: str | None = None
```

- [ ] **Step 4: Run to verify it passes**

```bash
docker compose exec api python -m pytest tests/api/test_nodes.py -q -k "ssh_and_version or ssh_fields_default"
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/node.py backend/tests/api/test_nodes.py
git commit -m "feat(models): record SSH connection details and reported version on Node"
```

---

## Task 4: Worker reports its image digest

Implements NU-1, NU-2, NU-3.

**Files:**
- Modify: `backend/app/queue/worker.py:507`
- Test: `backend/tests/queue/test_worker_version_report.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/queue/test_worker_version_report.py`:

**Correction made during execution (round 1):** the plan originally called `docker inspect --format {{.Image}}` against the worker's own container and used the result directly as the digest. That value is the container's *image ID* (a local config hash), not a registry digest — comparing it to the primary's own digest would silently never match.

**Correction made during execution (round 2):** the round-1 fix over-corrected the other way — it inspected a hardcoded `ghcr.io/syntheticgio/bioflow-backend:latest` reference directly. That silently reports the wrong digest, or none, on any node whose deployment pins `BIOFLOW_TAG` to a real version: `BIOFLOW_TAG` is a `docker-compose.yml`-time substitution into the `image:` line (`ghcr.io/syntheticgio/bioflow-backend:${BIOFLOW_TAG:-latest}`), with no equivalent passed through as an environment variable inside the running container, so there is no way for the process to know its own tag. The final version below resolves the digest in two steps instead: read the running container's own image id (round 1's approach, but used correctly this time — as an id to look up, not as the digest itself), then resolve *that* id's `RepoDigests`. This is immune to `BIOFLOW_TAG` by construction: whatever image the container actually is, is exactly the image whose digest gets reported, regardless of which tag pulled it.

```python
"""The worker reports what image it is running, so the primary can tell a
current node from a stale one.

Two docker calls: this container's own image id, then that id's
RepoDigests. Not a single call against a hardcoded tag reference -- a node
whose deployment pins BIOFLOW_TAG has no way to expose that tag to the
process (compose substitutes it into `image:` at parse time, nothing passes
it through as an environment variable), so inspecting a fixed "...:latest"
reference would silently report the wrong image on any pinned node.
Resolving through the container's own image id sidesteps that: whatever
image the container is, is exactly what gets inspected.
"""

from unittest.mock import patch

from app.queue.worker import _own_image_digest, _own_image_id


def test_own_image_id_reads_docker_inspect():
    completed = type("R", (), {"returncode": 0, "stdout": "sha256:localid\n"})()
    with patch("app.queue.worker.subprocess.run", return_value=completed):
        assert _own_image_id() == "sha256:localid"


def test_own_image_id_returns_none_without_docker_client():
    with patch("app.queue.worker.shutil.which", return_value=None):
        assert _own_image_id() is None


def test_own_image_digest_resolves_through_own_image_id():
    completed = type("R", (), {
        "returncode": 0,
        "stdout": "ghcr.io/syntheticgio/bioflow-backend@sha256:deadbeef\n",
    })()
    with patch("app.queue.worker._own_image_id", return_value="sha256:localid"), \
         patch("app.queue.worker.subprocess.run", return_value=completed) as run:
        assert _own_image_digest() == "sha256:deadbeef"
    # The id from the first call is what gets inspected in the second.
    assert "sha256:localid" in run.call_args.args[0]


def test_own_image_digest_returns_none_without_docker_client():
    with patch("app.queue.worker.shutil.which", return_value=None):
        assert _own_image_digest() is None


def test_own_image_digest_returns_none_when_own_id_unavailable():
    """A node whose socket is not mounted reports no digest rather than
    failing to heartbeat (NU-3)."""
    with patch("app.queue.worker._own_image_id", return_value=None):
        assert _own_image_digest() is None


def test_own_image_digest_returns_none_when_inspect_fails():
    with patch("app.queue.worker._own_image_id", return_value="sha256:localid"), \
         patch("app.queue.worker.subprocess.run",
               return_value=type("R", (), {"returncode": 1, "stdout": ""})()):
        assert _own_image_digest() is None


def test_own_image_digest_returns_none_on_exception():
    with patch("app.queue.worker._own_image_id", return_value="sha256:localid"), \
         patch("app.queue.worker.subprocess.run", side_effect=OSError("boom")):
        assert _own_image_digest() is None


def test_own_image_digest_returns_none_on_malformed_output():
    """RepoDigests can be empty (locally built image, never pushed/pulled) --
    docker then prints the template literal '<no value>' or an empty line,
    neither of which contains '@'."""
    completed = type("R", (), {"returncode": 0, "stdout": "<no value>\n"})()
    with patch("app.queue.worker._own_image_id", return_value="sha256:localid"), \
         patch("app.queue.worker.subprocess.run", return_value=completed):
        assert _own_image_digest() is None
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/queue/test_worker_version_report.py -q
```

Expected: FAIL — `cannot import name '_own_image_digest'`

- [ ] **Step 3: Implement the digest probe**

In `backend/app/queue/worker.py`, add to the imports at the top:

```python
import shutil
import subprocess
```

Add this module-level function after the imports and before `class Worker`:

```python
def _own_image_id() -> str | None:
    """This container's own image id (a local content hash, not a registry
    digest), by asking Docker about the container Docker itself thinks this
    process is running in. The hostname is the container id in Docker's
    default network mode.
    """
    client = shutil.which("docker")
    if client is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [client, "inspect", socket.gethostname(), "--format", "{{.Image}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _own_image_digest() -> str | None:
    """The registry digest of the image this process is running, or None.

    Two docker calls, not one, and deliberately not keyed by an image tag.
    An earlier version of this function inspected a hardcoded
    "...:latest" reference directly -- which silently reports the wrong
    digest, or none, on any node whose deployment pins BIOFLOW_TAG to a real
    version, since BIOFLOW_TAG is a docker-compose-time image reference
    substitution with no equivalent inside the running container (nothing
    passes it through as an environment variable). Resolving through this
    container's own image id sidesteps the whole problem: whatever image the
    container actually is, is exactly the image whose RepoDigests this reads,
    regardless of what tag was used to pull it or whether BIOFLOW_TAG is set
    at all.

    RepoDigests is the same field the launcher's own update check
    (update_check.rs's DockerImageInspector) compares against GHCR for the
    primary, so a node's reported digest and the primary's are genuinely
    comparable. None on any failure -- a node whose socket is not mounted, or
    whose image was built locally and never pushed (so RepoDigests is empty),
    must still heartbeat; it simply reports no version (NU-3).
    """
    image_id = _own_image_id()
    if image_id is None:
        return None
    client = shutil.which("docker")
    if client is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [
                client, "image", "inspect", image_id,
                "--format", "{{index .RepoDigests 0}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # RepoDigests entries look like "ghcr.io/org/name@sha256:...";
    # only the part after '@' is the digest. No '@' (e.g. docker's
    # "<no value>" template literal for an empty RepoDigests -- a locally
    # built image, never pushed or pulled) means no digest to report.
    text = result.stdout.strip()
    if "@" not in text:
        return None
    return text.rsplit("@", maxsplit=1)[-1] or None
```

In `Worker.__init__`, after `self.node_id`:

```python
        # Read once: it cannot change while this process lives.
        self.image_digest: str | None = _own_image_digest()
        self.version: str = __version__
```

Add the version import at the top of the file:

```python
from app.version import __version__
```

In `_register_worker` (line 507), add the two fields to the JSON payload:

```python
                    "node_id": self.node_id,
                    "image_digest": self.image_digest,
                    "version": self.version,
```

- [ ] **Step 4: Run to verify it passes**

```bash
docker compose exec api python -m pytest tests/queue/test_worker_version_report.py -q
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/worker.py backend/tests/queue/test_worker_version_report.py
git commit -m "feat(queue): worker reports its image digest and version in the heartbeat"
```

---

## Task 5: Persist and surface the reported version

Implements NU-4, NU-5. The worker sends its digest at enrollment (which already POSTs to the primary); `enumerate_nodes` surfaces it.

**Files:**
- Modify: `backend/app/queue/worker.py` (`_enroll`)
- Modify: `backend/app/api/v1/nodes.py` (`enroll_node`, `enumerate_nodes`)
- Test: `backend/tests/api/test_node_enrollment.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/api/test_node_enrollment.py`:

```python
async def test_enroll_persists_reported_version(client):
    from app.models.node import Node

    res = await client.post("/nodes/enroll", json={
        "node_id": "vnode",
        "hostname": "box",
        "image_digest": "sha256:aaa",
        "version": "0.4.0",
    })
    assert res.status_code == 200

    node = await Node.find_one(Node.node_id == "vnode")
    assert node.image_digest == "sha256:aaa"
    assert node.version == "0.4.0"
    await node.delete()


async def test_enroll_without_version_leaves_existing_value(client):
    """A node that cannot read its digest must not erase what it last
    reported (NU-3, NU-5)."""
    from app.models.node import Node

    await Node(node_id="vnode2", image_digest="sha256:old", version="0.3.0").insert()

    res = await client.post("/nodes/enroll", json={"node_id": "vnode2"})
    assert res.status_code == 200

    node = await Node.find_one(Node.node_id == "vnode2")
    assert node.image_digest == "sha256:old"
    assert node.version == "0.3.0"
    await node.delete()
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/api/test_node_enrollment.py -q -k "reported_version or without_version"
```

Expected: FAIL — `node.image_digest` is `None`

- [ ] **Step 3: Persist on enrollment**

In `backend/app/api/v1/nodes.py`, inside `enroll_node`, after reading `key`:

```python
    image_digest = str(payload.get("image_digest") or "").strip() or None
    version = str(payload.get("version") or "").strip() or None
```

In the `if existing:` branch, after `existing.last_seen = now`:

```python
        # Only overwrite when reported: a node that cannot read its digest
        # must not erase the version it last reported.
        if image_digest:
            existing.image_digest = image_digest
        if version:
            existing.version = version
```

In the `else:` branch, pass them to the constructor:

```python
        node = Node(
            node_id=node_id,
            hostname=hostname,
            last_seen=now,
            status="active",
            image_digest=image_digest,
            version=version,
        )
```

- [ ] **Step 4: Send them from the worker**

In `backend/app/queue/worker.py`, in `_enroll`, extend the payload:

```python
        payload = {
            "node_id": self.node_id,
            "hostname": socket.gethostname(),
            "enrollment_key": settings.enrollment_key,
            "image_digest": self.image_digest,
            "version": self.version,
        }
```

- [ ] **Step 5: Surface it from `enumerate_nodes`**

In `backend/app/api/v1/nodes.py`, in `enumerate_nodes`, extend the Mongo read:

```python
            mongo_nodes[doc.node_id] = {
                "hostname": doc.hostname,
                "registered_at": doc.registered_at.isoformat() if doc.registered_at else None,
                "enrollment": doc.status,
                "last_seen": doc.last_seen.isoformat() if doc.last_seen else None,
                "image_digest": doc.image_digest,
                "version": doc.version,
                "updatable": doc.ssh_key_enc is not None,
            }
```

In the merge loop near the end of the function, after `entry["last_seen_mongo"] = ...`:

```python
        entry["image_digest"] = mongo_info.get("image_digest")
        entry["version"] = mongo_info.get("version")
        entry["updatable"] = mongo_info.get("updatable", False)
```

In `list_nodes`, the orphaned-queue placeholder dict needs the same three keys so every row has the same shape:

```python
            "image_digest": None,
            "version": None,
            "updatable": False,
```

- [ ] **Step 6: Run to verify it passes**

```bash
docker compose exec api python -m pytest tests/api/test_node_enrollment.py tests/api/test_nodes.py -q
```

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/v1/nodes.py backend/app/queue/worker.py backend/tests/api/test_node_enrollment.py
git commit -m "feat(api): persist and expose the backend version each node reports"
```

---

## Task 6: Primary exposes its own digest

The frontend compares each node's digest against the primary's. Implements the reference side of NU-7.

**Files:**
- Modify: `backend/app/api/v1/nodes.py`
- Test: `backend/tests/api/test_nodes.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/api/test_nodes.py`:

```python
async def test_current_version_reports_primary_digest(client):
    with patch("app.api.v1.nodes._own_image_digest", return_value="sha256:cur"):
        res = await client.get("/nodes/current-version")
    assert res.status_code == 200
    body = res.json()
    assert body["image_digest"] == "sha256:cur"
    assert body["version"]


async def test_current_version_tolerates_unknown_digest(client):
    with patch("app.api.v1.nodes._own_image_digest", return_value=None):
        res = await client.get("/nodes/current-version")
    assert res.status_code == 200
    assert res.json()["image_digest"] is None
```

Add `from unittest.mock import patch` to the imports if not present.

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/api/test_nodes.py -q -k current_version
```

Expected: FAIL — 404

- [ ] **Step 3: Add the endpoint**

In `backend/app/api/v1/nodes.py`, add the import:

```python
from app.queue.worker import _own_image_digest
from app.version import __version__
```

Add the route (place it above `@router.get("/{node_id}/status")` so the literal path is not captured by the parameterized route):

```python
@router.get("/current-version")
async def current_version() -> dict:
    """The image the primary is running, as the reference for staleness.

    Nodes are compared against this rather than against a registry tag: the
    digest is what actually differs when an image is republished under the
    same tag.
    """
    return {"image_digest": _own_image_digest(), "version": __version__}
```

- [ ] **Step 4: Run to verify it passes**

```bash
docker compose exec api python -m pytest tests/api/test_nodes.py -q -k current_version
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/nodes.py backend/tests/api/test_nodes.py
git commit -m "feat(api): expose the primary's image digest as the staleness reference"
```

---

## Task 7: SSH key generation and installation

Implements NU-9, NU-10, NU-11. Pure service module — no FastAPI, no database.

**Files:**
- Create: `backend/app/services/node_ssh.py`
- Test: `backend/tests/services/test_node_ssh.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_node_ssh.py`:

```python
"""Managed SSH key install.

Every test mocks asyncssh, so these verify the logic and the shell commands
we construct -- not that a real sshd accepts the key. That gap is closed by
the manual verification in Task 12.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import node_ssh


def _conn(exit_status: int = 0):
    conn = MagicMock()
    result = MagicMock()
    result.exit_status = exit_status
    result.stdout = ""
    result.stderr = ""
    conn.run = AsyncMock(return_value=result)
    return conn


def test_generate_keypair_returns_private_and_public():
    private_pem, public_line = node_ssh.generate_keypair("mynode")
    assert "PRIVATE KEY" in private_pem
    assert public_line.startswith("ssh-ed25519 ")
    assert "bioflow-node-mynode" in public_line


async def test_install_appends_and_never_truncates():
    """Overwriting authorized_keys would destroy the user's own access."""
    conn = _conn()
    await node_ssh.install_public_key(conn, "ssh-ed25519 AAAA bioflow-node-x")

    commands = " ; ".join(c.args[0] for c in conn.run.call_args_list)
    assert ">>" in commands
    assert ">" in commands  # sanity: the append operator is present
    # A single '>' redirect into authorized_keys truncates it.
    assert "> ~/.ssh/authorized_keys" not in commands.replace(">>", "")
    assert "chmod 700" in commands
    assert "chmod 600" in commands


async def test_install_raises_when_a_command_fails():
    conn = _conn(exit_status=1)
    with pytest.raises(node_ssh.KeyInstallError):
        await node_ssh.install_public_key(conn, "ssh-ed25519 AAAA x")


async def test_verify_opens_a_new_connection_with_the_new_key():
    """Verification must authenticate with the key, not reuse the open
    session -- an appended key can still be ignored by sshd."""
    fake_conn = MagicMock()
    fake_conn.run = AsyncMock(return_value=MagicMock(exit_status=0))
    fake_conn.close = MagicMock()

    with patch("asyncssh.connect", AsyncMock(return_value=fake_conn)) as conn_mock, \
         patch("asyncssh.import_private_key", MagicMock(return_value="KEY")):
        await node_ssh.verify_key("10.0.0.5", 22, "ops", "PEM")

    assert conn_mock.await_count == 1
    assert conn_mock.await_args.kwargs["client_keys"] == ["KEY"]


async def test_verify_raises_when_authentication_fails():
    import asyncssh

    with patch("asyncssh.connect", AsyncMock(side_effect=asyncssh.Error(1, "denied"))), \
         patch("asyncssh.import_private_key", MagicMock(return_value="KEY")):
        with pytest.raises(node_ssh.KeyInstallError):
            await node_ssh.verify_key("10.0.0.5", 22, "ops", "PEM")
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/services/test_node_ssh.py -q
```

Expected: FAIL — `cannot import name 'node_ssh'`

- [ ] **Step 3: Implement the module**

Create `backend/app/services/node_ssh.py`:

```python
"""Managed SSH keys for compute nodes.

BioFlow installs a keypair of its own on each node it provisions rather than
retaining the password or key the user typed. The user's credential is used
once, to install this key, and never stored: a generated key is revocable per
node, survives the user changing their password, and is scoped to this purpose.

The install is verified by opening a second connection that authenticates with
the new key. Appending to authorized_keys can succeed and still leave a key
that sshd ignores -- wrong directory permissions, an AuthorizedKeysFile
pointing elsewhere, PubkeyAuthentication disabled. Only a round trip proves it.
"""

import asyncio
import io

import asyncssh

from app.logging import get_logger

log = get_logger(__name__)

_VERIFY_TIMEOUT_SECONDS = 15


class KeyInstallError(Exception):
    """The managed key could not be installed or did not authenticate."""


def generate_keypair(node_name: str) -> tuple[str, str]:
    """A new Ed25519 keypair as (private PEM, public authorized_keys line)."""
    key = asyncssh.generate_private_key(
        "ssh-ed25519", comment=f"bioflow-node-{node_name}"
    )
    private_pem = key.export_private_key().decode()
    public_line = key.export_public_key().decode().strip()
    return private_pem, public_line


async def install_public_key(conn, public_line: str) -> None:
    """Append `public_line` to the remote user's authorized_keys.

    Append, never overwrite: this file usually holds the key the user reaches
    their own machine with, and truncating it would lock them out.
    """
    commands = [
        "mkdir -p ~/.ssh",
        "chmod 700 ~/.ssh",
        "touch ~/.ssh/authorized_keys",
        f"printf '%s\\n' {_quote(public_line)} >> ~/.ssh/authorized_keys",
        "chmod 600 ~/.ssh/authorized_keys",
    ]
    for command in commands:
        result = await asyncio.wait_for(conn.run(command, check=False), timeout=15)
        if result.exit_status != 0:
            raise KeyInstallError(
                f"Could not install the BioFlow key on this node: {command!r} "
                f"failed ({result.stderr or result.stdout or 'no output'})."
            )


async def verify_key(host: str, port: int, username: str, private_pem: str) -> None:
    """Prove the installed key authenticates, by using it."""
    try:
        key = asyncssh.import_private_key(io.StringIO(private_pem))
        conn = await asyncio.wait_for(
            asyncssh.connect(
                host,
                port=port,
                username=username,
                known_hosts=None,
                client_keys=[key],
            ),
            timeout=_VERIFY_TIMEOUT_SECONDS,
        )
    except (TimeoutError, asyncssh.Error, ValueError) as e:
        raise KeyInstallError(
            "The BioFlow key was written to this node but does not "
            f"authenticate: {e}. Check that sshd allows public-key login."
        ) from e

    try:
        result = await asyncio.wait_for(conn.run("true", check=False), timeout=15)
        if result.exit_status != 0:
            raise KeyInstallError("The BioFlow key authenticated but no command ran.")
    finally:
        conn.close()


def _quote(value: str) -> str:
    """Single-quote a value for a POSIX shell."""
    return "'" + value.replace("'", "'\\''") + "'"
```

- [ ] **Step 4: Run to verify it passes**

```bash
docker compose exec api python -m pytest tests/services/test_node_ssh.py -q
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/node_ssh.py backend/tests/services/test_node_ssh.py
git commit -m "feat(services): generate and install a managed SSH key on compute nodes"
```

---

## Task 8: Provisioning installs the managed key

Implements NU-12, NU-14, NU-15. New phase between `write_env` and `pull_image`.

**Files:**
- Modify: `backend/app/api/v1/nodes.py` (`_provision_node`)
- Test: `backend/tests/api/test_node_provision.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/api/test_node_provision.py`:

```python
async def test_provision_stores_encrypted_key_not_the_password():
    """The user's password is used once and never stored (NU-15)."""
    from app.api.v1.nodes import ProvisionRequest, _provision_node
    from app.models.node import Node

    req = ProvisionRequest(
        host="10.0.0.9", username="ops", password="hunter2", node_name="keynode",
    )

    with patch("app.api.v1.nodes.asyncssh") as ssh, \
         patch("app.services.node_ssh.verify_key", AsyncMock()), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("app.api.v1.nodes.asyncssh.scp", AsyncMock()):
        conn = ssh.connect.return_value
        conn.run = AsyncMock(return_value=type("R", (), {
            "exit_status": 0, "stdout": "", "stderr": "",
        })())
        await _provision_node("t-key", req)

    node = await Node.find_one(Node.node_id == "keynode")
    assert node is not None
    assert node.ssh_key_enc is not None
    assert b"hunter2" not in node.ssh_key_enc
    assert node.ssh_host == "10.0.0.9"
    assert node.ssh_username == "ops"
    assert node.ssh_key_installed_at is not None

    from app.services.ai import crypto
    assert "PRIVATE KEY" in crypto.decrypt(node.ssh_key_enc)
    await node.delete()


async def test_provision_fails_loudly_when_key_cannot_be_installed():
    """No fallback to storing the user's credential (NU-14): a node that
    cannot take a managed key is not provisioned at all."""
    from app.api.v1.nodes import ProvisionRequest, _provision_node
    from app.models.node import Node
    from app.services.node_ssh import KeyInstallError

    req = ProvisionRequest(
        host="10.0.0.10", username="ops", password="pw", node_name="badkey",
    )

    with patch("app.api.v1.nodes.asyncssh") as ssh, \
         patch("app.services.node_ssh.install_public_key",
               AsyncMock(side_effect=KeyInstallError("read-only home"))), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("app.api.v1.nodes.asyncssh.scp", AsyncMock()):
        conn = ssh.connect.return_value
        conn.run = AsyncMock(return_value=type("R", (), {
            "exit_status": 0, "stdout": "", "stderr": "",
        })())
        await _provision_node("t-badkey", req)

    task = await NodeProvisionTask.find_one(NodeProvisionTask.task_id == "t-badkey")
    assert task.status == "failed"
    assert "read-only home" in task.error
    assert task.phase == "install_key"

    # The image must not have been pulled: provisioning stopped first.
    commands = " ".join(str(c) for c in conn.run.call_args_list)
    assert "docker pull" not in commands

    assert await Node.find_one(Node.node_id == "badkey") is None
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/api/test_node_provision.py -q -k "encrypted_key or fails_loudly"
```

Expected: FAIL — no `install_key` phase exists

- [ ] **Step 3: Add the phase**

In `backend/app/api/v1/nodes.py`, add imports:

```python
from app.services import node_ssh
from app.services.ai import crypto
```

In `_provision_node`, between the `write_env` block and `# Phase 5: pull_image`:

```python
            # Phase 5: install_key
            #
            # Before the image is pulled, so a node that cannot take the key
            # costs nothing. Failing here leaves the node unprovisioned rather
            # than provisioned-but-not-updatable: a fallback to storing the
            # user's own credential would make the security property depend on
            # a condition nobody observed.
            await _update("install_key", "Installing the BioFlow update key…")
            private_pem, public_line = node_ssh.generate_keypair(req.node_name)
            await node_ssh.install_public_key(conn, public_line)
            await node_ssh.verify_key(req.host, req.port, req.username, private_pem)

            node_doc = await Node.find_one(Node.node_id == req.node_name)
            if node_doc is None:
                node_doc = Node(node_id=req.node_name, hostname=req.host)
            node_doc.ssh_host = req.host
            node_doc.ssh_port = req.port
            node_doc.ssh_username = req.username
            node_doc.ssh_key_enc = crypto.encrypt(private_pem)
            node_doc.ssh_key_installed_at = datetime.now(UTC)
            await node_doc.save()
```

Renumber the following comments to `# Phase 6: pull_image`, `# Phase 7: start_worker`, `# Phase 8: enrolled`.

Add `KeyInstallError` handling to the executor's `except` clause, above the generic `except Exception`:

```python
    except node_ssh.KeyInstallError as e:
        await _fail(str(e))
```

Note: the existing `except Exception` block is `async def _provision_node`'s outermost handler, so place this immediately before it.

- [ ] **Step 4: Run to verify it passes**

```bash
docker compose exec api python -m pytest tests/api/test_node_provision.py -q
```

Expected: PASS (all provisioning tests, including the pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/nodes.py backend/tests/api/test_node_provision.py
git commit -m "feat(api): install a managed SSH key during node provisioning"
```

---

## Task 9: The update executor

Implements NU-22 through NU-26.

**Files:**
- Create: `backend/app/services/node_update_service.py`
- Test: `backend/tests/services/test_node_update_service.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_node_update_service.py`:

```python
"""The update executor: pull, drain, restart, verify."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.node import Node
from app.models.node_update import NodeUpdateTask
from app.services import node_update_service as svc


@pytest.fixture(autouse=True)
async def _clean():
    """conftest.py drops collections at session start, not between tests --
    these tests each insert a Node and a task under fixed ids."""
    yield
    await NodeUpdateTask.find_all().delete()
    await Node.find_all().delete()


def _conn(pull_status: int = 0, up_status: int = 0):
    conn = MagicMock()

    async def run(command, **kwargs):
        result = MagicMock()
        result.stdout = ""
        result.stderr = "boom" if pull_status else ""
        if " pull " in command or command.endswith(" pull"):
            result.exit_status = pull_status
        elif "up -d" in command:
            result.exit_status = up_status
        else:
            result.exit_status = 0
        return result

    conn.run = AsyncMock(side_effect=run)
    conn.close = MagicMock()
    return conn


async def _node(node_id="un1"):
    node = Node(
        node_id=node_id, ssh_host="10.0.0.7", ssh_username="ops",
        ssh_key_enc=b"enc", image_digest="sha256:old",
    )
    await node.insert()
    return node


async def test_failed_pull_leaves_the_node_running():
    """Pull before stop: a failed download must cost nothing (NU-23, NU-24)."""
    node = await _node("un-pull")
    task = NodeUpdateTask(task_id="u1", node_id="un-pull")
    await task.insert()
    conn = _conn(pull_status=1)

    with patch("app.services.ai.crypto.decrypt", return_value="PEM"), \
         patch("asyncssh.import_private_key", MagicMock()), \
         patch("asyncssh.connect", AsyncMock(return_value=conn)):
        await svc.run_update("u1", node, drain=False)

    done = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == "u1")
    assert done.status == "failed"
    assert done.phase == "pull_image"

    commands = " ".join(str(c) for c in conn.run.call_args_list)
    assert "up -d" not in commands  # nothing was restarted
    await node.delete()
    await done.delete()


async def test_success_requires_the_new_digest_to_be_reported():
    """NU-25: exit 0 from compose is also what a crash-looping container returns."""
    node = await _node("un-ok")
    task = NodeUpdateTask(task_id="u2", node_id="un-ok")
    await task.insert()
    conn = _conn()

    with patch("app.services.ai.crypto.decrypt", return_value="PEM"), \
         patch("asyncssh.import_private_key", MagicMock()), \
         patch("asyncssh.connect", AsyncMock(return_value=conn)), \
         patch.object(svc, "_await_digest", AsyncMock(return_value="sha256:new")):
        await svc.run_update("u2", node, drain=False)

    done = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == "u2")
    assert done.status == "success"
    assert done.to_digest == "sha256:new"
    await node.delete()
    await done.delete()


async def test_worker_that_never_reports_fails_the_update():
    node = await _node("un-crash")
    task = NodeUpdateTask(task_id="u3", node_id="un-crash")
    await task.insert()
    conn = _conn()

    with patch("app.services.ai.crypto.decrypt", return_value="PEM"), \
         patch("asyncssh.import_private_key", MagicMock()), \
         patch("asyncssh.connect", AsyncMock(return_value=conn)), \
         patch.object(svc, "_await_digest", AsyncMock(return_value=None)):
        await svc.run_update("u3", node, drain=False)

    done = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == "u3")
    assert done.status == "failed"
    assert done.phase == "verify"
    assert "120" in done.error or "did not" in done.error.lower()
    await node.delete()
    await done.delete()


async def test_drain_stops_the_worker_before_restarting():
    node = await _node("un-drain")
    task = NodeUpdateTask(task_id="u4", node_id="un-drain")
    await task.insert()
    conn = _conn()

    with patch("app.services.ai.crypto.decrypt", return_value="PEM"), \
         patch("asyncssh.import_private_key", MagicMock()), \
         patch("asyncssh.connect", AsyncMock(return_value=conn)), \
         patch.object(svc, "_await_drained", AsyncMock(return_value=True)), \
         patch.object(svc, "_await_digest", AsyncMock(return_value="sha256:new")):
        await svc.run_update("u4", node, drain=True)

    commands = [str(c) for c in conn.run.call_args_list]
    joined = " ".join(commands)
    assert "stop" in joined
    # The pull happens before the stop, so the download overlaps with jobs
    # finishing rather than running after them.
    assert joined.index(" pull ") < joined.index("stop")


async def test_unreachable_machine_reports_connect_failure():
    """NU-20: a node whose worker is down is still attempted; only a failed
    SSH connection reports the machine unreachable."""
    import asyncssh

    node = await _node("un-down")
    task = NodeUpdateTask(task_id="u5", node_id="un-down")
    await task.insert()

    with patch("app.services.ai.crypto.decrypt", return_value="PEM"), \
         patch("asyncssh.import_private_key", MagicMock()), \
         patch("asyncssh.connect", AsyncMock(side_effect=asyncssh.Error(1, "refused"))):
        await svc.run_update("u5", node, drain=False)

    done = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == "u5")
    assert done.status == "failed"
    assert done.phase == "connect"
    await node.delete()
    await done.delete()
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/services/test_node_update_service.py -q
```

Expected: FAIL — `cannot import name 'node_update_service'`

- [ ] **Step 3: Implement the executor**

Create `backend/app/services/node_update_service.py`:

```python
"""Updating a compute node's backend image over SSH.

The update is phases 6-7 of provisioning (`docker pull`, `docker compose up
-d`) run again later, against a node whose managed key the primary already
holds.

Two orderings matter. The pull happens before anything is stopped, so a failed
download leaves the node running its current image. And success means a worker
re-enrolled reporting the new digest -- `docker compose up -d` exits 0 for a
container that immediately crash-loops, which is the failure this feature
exists to fix.

Pulls via `docker compose ... pull`, not `docker pull <hardcoded image:tag>`.
A node's deployment can pin BIOFLOW_TAG to a real version -- provisioning's
own `_render_node_env` writes BIOFLOW_TAG into the node's `.env` next to its
docker-compose.yml -- and a tag hardcoded here would silently update a pinned
node to the wrong image, the same bug already found and fixed for the
worker's own digest probe (see worker.py's _own_image_digest). `docker
compose pull` reads the compose file's `image: ...${BIOFLOW_TAG:-latest}`
directive and resolves BIOFLOW_TAG from the node's own .env, exactly as
`docker compose up -d` already does for the restart phase two lines below --
so this needs no image reference of its own, and the primary never needs to
know what tag any given node runs.
"""

import asyncio
from datetime import UTC, datetime

import asyncssh

from app.logging import get_logger
from app.models.node import Node
from app.models.node_update import NodeUpdateTask
from app.services.ai import crypto

log = get_logger(__name__)

INSTALL_DIR = "~/.bioflow"

_VERIFY_TIMEOUT_SECONDS = 120
_DRAIN_TIMEOUT_SECONDS = 900
_POLL_INTERVAL_SECONDS = 5


async def run_update(task_id: str, node: Node, drain: bool) -> None:
    """Execute one node update, recording progress on the task document."""
    task = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == task_id)
    if task is None:
        log.warning("update_task_missing", task_id=task_id)
        return

    async def _phase(phase: str, message: str, pct: float | None = None) -> None:
        task.phase = phase
        task.message = message
        task.pct = pct
        await task.save()

    async def _fail(phase: str, reason: str) -> None:
        task.status = "failed"
        task.phase = phase
        task.error = reason
        task.message = reason
        task.finished_at = datetime.now(UTC)
        await task.save()
        log.warning("node_update_failed", task_id=task_id, phase=phase, reason=reason)

    conn = None
    try:
        # ---- connect ----
        await _phase("connect", f"Connecting to {node.ssh_host}…", 5)
        private_pem = crypto.decrypt(node.ssh_key_enc) if node.ssh_key_enc else None
        if not private_pem:
            return await _fail(
                "connect",
                "The stored update key could not be decrypted. Re-provision this node.",
            )
        try:
            key = asyncssh.import_private_key(private_pem)
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    node.ssh_host,
                    port=node.ssh_port,
                    username=node.ssh_username,
                    known_hosts=None,
                    client_keys=[key],
                ),
                timeout=20,
            )
        except (TimeoutError, asyncssh.Error, ValueError) as e:
            return await _fail(
                "connect",
                f"Could not reach {node.ssh_host}: {e}. "
                "The machine may be off, or the update key may have been removed.",
            )

        # ---- pull (before stopping anything) ----
        await _phase("pull_image", "Pulling the new image…", 20)
        result = await asyncio.wait_for(
            conn.run(
                f"docker compose -f {INSTALL_DIR}/docker-compose.yml pull worker",
                check=False,
            ),
            timeout=1800,
        )
        if result.exit_status != 0:
            return await _fail(
                "pull_image",
                f"Image pull failed: {result.stderr or result.stdout or 'no output'}",
            )

        # ---- drain ----
        if drain:
            await _phase("drain", "Waiting for running jobs to finish…", 40)
            await asyncio.wait_for(
                conn.run(
                    f"docker compose -f {INSTALL_DIR}/docker-compose.yml stop -t "
                    f"{_DRAIN_TIMEOUT_SECONDS} worker",
                    check=False,
                ),
                timeout=_DRAIN_TIMEOUT_SECONDS + 60,
            )
            await _await_drained(node.node_id)

        # ---- restart ----
        await _phase("restart", "Starting the updated worker…", 70)
        result = await asyncio.wait_for(
            conn.run(
                f"docker compose -f {INSTALL_DIR}/docker-compose.yml up -d",
                check=False,
            ),
            timeout=120,
        )
        if result.exit_status != 0:
            return await _fail(
                "restart",
                f"Worker failed to start: {result.stderr or result.stdout}",
            )

        # ---- verify ----
        await _phase("verify", "Waiting for the updated worker to report in…", 85)
        new_digest = await _await_digest(node.node_id, node.image_digest)
        if new_digest is None:
            return await _fail(
                "verify",
                f"The updated worker did not report in within "
                f"{_VERIFY_TIMEOUT_SECONDS}s. It may be failing to start -- "
                f"check `docker compose logs worker` on {node.ssh_host}.",
            )

        task.status = "success"
        task.phase = "done"
        task.to_digest = new_digest
        task.pct = 100
        task.message = "Node updated ✓"
        task.finished_at = datetime.now(UTC)
        await task.save()
        log.info("node_updated", node_id=node.node_id, digest=new_digest)

    except Exception as e:  # noqa: BLE001 - a failed update must not kill the API
        log.exception("node_update_error", task_id=task_id)
        await _fail(task.phase or "unknown", str(e))
    finally:
        if conn is not None:
            conn.close()


async def _await_drained(node_id: str) -> bool:
    """Wait for the node's workers to stop reporting running jobs."""
    from app.api.v1.nodes import enumerate_nodes

    deadline = asyncio.get_running_loop().time() + _DRAIN_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        nodes = await enumerate_nodes()
        entry = nodes.get(node_id)
        if entry is None or entry.get("running_jobs", 0) == 0:
            return True
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    return False


async def _await_digest(node_id: str, previous: str | None) -> str | None:
    """Wait for a worker on `node_id` to report a digest other than `previous`.

    Returns the new digest, or None if none arrived in time.
    """
    deadline = asyncio.get_running_loop().time() + _VERIFY_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        node = await Node.find_one(Node.node_id == node_id)
        if node and node.image_digest and node.image_digest != previous:
            return node.image_digest
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    return None
```

- [ ] **Step 4: Run to verify it passes**

```bash
docker compose exec api python -m pytest tests/services/test_node_update_service.py -q
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/node_update_service.py backend/tests/services/test_node_update_service.py
git commit -m "feat(services): update a node's image over SSH, verified by re-enrollment"
```

---

## Task 10: Update endpoints

Implements NU-17 through NU-21, NU-27, NU-28.

**Files:**
- Modify: `backend/app/api/v1/nodes.py`
- Modify: `backend/app/main.py:140`
- Test: `backend/tests/api/test_node_update.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_node_update.py`:

```python
"""Update endpoints: what can be updated, and what cannot."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.models.node import Node
from app.models.node_update import NodeUpdateTask


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(pytest.importorskip("app.api.v1.nodes").router)
    return app


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        yield ac


@pytest.fixture(autouse=True)
async def _clean():
    yield
    await NodeUpdateTask.find_all().delete()
    await Node.find_all().delete()


async def test_update_rejects_node_without_stored_key(client):
    """A hand-provisioned node has no key: the button is not offered, and the
    endpoint refuses it (NU-18)."""
    await Node(node_id="manual").insert()
    res = await client.post("/nodes/manual/update", json={"drain": True})
    assert res.status_code == 409
    assert "provision" in res.json()["detail"].lower()


async def test_update_rejects_unknown_node(client):
    res = await client.post("/nodes/ghost/update", json={"drain": True})
    assert res.status_code == 404


async def test_update_rejects_concurrent_update(client):
    """NU-19: two updates racing on one node would fight over the container."""
    await Node(node_id="busy", ssh_host="h", ssh_username="u", ssh_key_enc=b"k").insert()
    await NodeUpdateTask(task_id="running", node_id="busy", status="updating").insert()

    res = await client.post("/nodes/busy/update", json={"drain": True})
    assert res.status_code == 409
    assert "already" in res.json()["detail"].lower()


async def test_update_starts_and_returns_task_id(client):
    await Node(node_id="ok", ssh_host="h", ssh_username="u", ssh_key_enc=b"k").insert()

    with patch("app.api.v1.nodes.node_update_service.run_update", AsyncMock()):
        res = await client.post("/nodes/ok/update", json={"drain": True})

    assert res.status_code == 201
    assert res.json()["task_id"]


async def test_update_passes_the_drain_choice(client):
    await Node(node_id="d", ssh_host="h", ssh_username="u", ssh_key_enc=b"k").insert()
    runner = AsyncMock()

    with patch("app.api.v1.nodes.node_update_service.run_update", runner):
        await client.post("/nodes/d/update", json={"drain": False})
        # The endpoint schedules a background task; give it a tick to start.
        import asyncio
        await asyncio.sleep(0.05)

    assert runner.await_args.kwargs["drain"] is False


async def test_update_status_returns_progress(client):
    await NodeUpdateTask(
        task_id="t9", node_id="n", status="updating", phase="pull_image",
        message="Pulling…",
    ).insert()

    res = await client.get("/nodes/update/t9")
    assert res.status_code == 200
    assert res.json()["phase"] == "pull_image"


async def test_update_status_404_for_unknown_task(client):
    res = await client.get("/nodes/update/nope")
    assert res.status_code == 404


async def test_orphaned_updates_are_failed_on_startup():
    """An API restart mid-update leaves a task nothing will ever finish."""
    from app.api.v1.nodes import _clean_orphaned_provisions

    await NodeUpdateTask(task_id="orphan", node_id="n", status="updating").insert()
    await _clean_orphaned_provisions()

    task = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == "orphan")
    assert task.status == "failed"
    assert "restart" in task.error.lower()
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/api/test_node_update.py -q
```

Expected: FAIL — 404 on `/nodes/{id}/update`

- [ ] **Step 3: Add the endpoints**

In `backend/app/api/v1/nodes.py`, add the imports:

```python
from app.models.node_update import NodeUpdateTask
from app.services import node_update_service
```

Add the request model beside `ProvisionRequest`:

```python
class UpdateRequest(BaseModel):
    """Whether to let running jobs finish before swapping the image."""

    drain: bool = True
```

Add the endpoints (place them above `@router.get("/{node_id}/status")`):

```python
_active_updates: dict[str, asyncio.Task] = {}


@router.post("/{node_id}/update", status_code=201)
async def update_node(node_id: str, req: UpdateRequest) -> dict:
    """Pull the current backend image on a node and restart its worker."""
    node = await Node.find_one(Node.node_id == node_id)
    if node is None:
        raise HTTPException(404, f"Node {node_id!r} not found")
    if node.ssh_key_enc is None:
        raise HTTPException(
            409,
            f"Node {node_id!r} was not provisioned from BioFlow, so there is no "
            "stored key to reach it with. Re-provision it to enable updates.",
        )

    running = await NodeUpdateTask.find_one(
        NodeUpdateTask.node_id == node_id,
        NodeUpdateTask.status == "updating",
    )
    if running is not None:
        raise HTTPException(409, f"Node {node_id!r} is already being updated.")

    task_doc = NodeUpdateTask(
        node_id=node_id,
        host=node.ssh_host or "",
        from_digest=node.image_digest,
        drain=req.drain,
        message="Queued…",
    )
    await task_doc.insert()

    bg = asyncio.create_task(
        node_update_service.run_update(task_doc.task_id, node, drain=req.drain)
    )
    _active_updates[task_doc.task_id] = bg
    bg.add_done_callback(lambda _: _active_updates.pop(task_doc.task_id, None))

    return {"task_id": task_doc.task_id, "status": "updating"}


@router.get("/update/{task_id}")
async def update_status(task_id: str) -> dict:
    """Poll the status of an update task."""
    task = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == task_id)
    if task is None:
        raise HTTPException(404, f"Update task {task_id!r} not found")
    return {
        "task_id": task.task_id,
        "status": task.status,
        "phase": task.phase,
        "message": task.message,
        "pct": task.pct,
        "node_id": task.node_id,
        "host": task.host,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "error": task.error,
    }
```

Extend `_clean_orphaned_provisions` to cover update tasks — append inside its `try` block:

```python
        orphaned_updates = await NodeUpdateTask.find(
            NodeUpdateTask.status == "updating",
        ).to_list()
        for t in orphaned_updates:
            if t.task_id not in _active_updates:
                t.status = "failed"
                t.error = "API restart interrupted the update"
                t.finished_at = datetime.now(UTC)
                await t.save()
                log.info("orphaned_update_cleaned", task_id=t.task_id)
```

- [ ] **Step 4: Run to verify it passes**

```bash
docker compose exec api python -m pytest tests/api/test_node_update.py -q
```

Expected: PASS

- [ ] **Step 5: Run the whole backend suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass. Read the count, not just the exit code.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/nodes.py backend/tests/api/test_node_update.py
git commit -m "feat(api): endpoints to update a compute node and poll its progress"
```

---

## Task 11: Frontend — staleness logic, version column, Update control

Implements NU-6, NU-7, NU-8, NU-29 through NU-32.

**Files:**
- Create: `frontend/src/lib/nodeStaleness.ts`, `frontend/src/lib/nodeStaleness.test.ts`
- Modify: `frontend/src/api/types.ts`, `frontend/src/api/client.ts`, `frontend/src/components/SettingsNodes.tsx`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/lib/nodeStaleness.test.ts`:

```typescript
import { describe, expect, it } from "vitest";

import { updateAffordance } from "./nodeStaleness";

const base = { imageDigest: "sha256:a", updatable: true, primaryDigest: "sha256:a" };

describe("updateAffordance", () => {
  it("hides the control when the node matches the primary", () => {
    expect(updateAffordance(base).kind).toBe("current");
  });

  it("offers an update when the digests differ", () => {
    expect(
      updateAffordance({ ...base, imageDigest: "sha256:old" }).kind,
    ).toBe("available");
  });

  it("offers an update when the node reports no version but has a key", () => {
    // A node whose worker is down reports nothing; it is exactly the node
    // most in need of the button.
    expect(
      updateAffordance({ ...base, imageDigest: null }).kind,
    ).toBe("available");
  });

  // NU-30: disabled and self-explaining, never a button that cannot work.
  it("disables the control, with a reason, on a node with no stored key", () => {
    const result = updateAffordance({
      ...base,
      imageDigest: "sha256:old",
      updatable: false,
    });
    expect(result.kind).toBe("unavailable");
    if (result.kind === "unavailable") {
      expect(result.reason).toMatch(/provision/i);
    }
  });

  it("does not claim staleness when the primary's digest is unknown", () => {
    expect(
      updateAffordance({ ...base, imageDigest: "sha256:old", primaryDigest: null }).kind,
    ).toBe("current");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd frontend && npx vitest run src/lib/nodeStaleness.test.ts
```

Expected: FAIL — cannot resolve `./nodeStaleness`

- [ ] **Step 3: Implement the pure module**

Create `frontend/src/lib/nodeStaleness.ts`:

```typescript
// Pure logic for the Update control in Settings → Nodes, split out the way
// launcher/src/update-logic.ts is: this repo has no jsdom or testing-library
// setup and no .test.tsx files, so a pure module is the only testable seam.

export type UpdateAffordance =
  /** Node matches the primary, or there is nothing to compare against. */
  | { kind: "current" }
  /** Stale, or reporting nothing while updatable. Offer the button. */
  | { kind: "available" }
  /** Visible, disabled, self-explaining. */
  | { kind: "unavailable"; reason: string };

export interface StalenessInputs {
  /** The digest this node last reported; null if it never reported one. */
  imageDigest: string | null;
  /** Whether the primary holds an SSH key for this node. */
  updatable: boolean;
  /** The digest the primary is running; null if it cannot read its own. */
  primaryDigest: string | null;
}

export function updateAffordance({
  imageDigest,
  updatable,
  primaryDigest,
}: StalenessInputs): UpdateAffordance {
  // A node reporting no version is either offline or has no Docker socket.
  // Either way it is a candidate for an update, not a node known to be
  // current -- and a down worker is the case the button matters most for.
  const stale = imageDigest === null || (primaryDigest !== null && imageDigest !== primaryDigest);

  if (!stale) return { kind: "current" };

  if (!updatable) {
    return {
      kind: "unavailable",
      reason: "Not provisioned from BioFlow — no stored key to reach this node.",
    };
  }
  return { kind: "available" };
}

/** The version string to show in the table. */
export function versionLabel(version: string | null): string {
  return version ?? "Unknown";
}
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd frontend && npx vitest run src/lib/nodeStaleness.test.ts
```

Expected: PASS (5 tests)

- [ ] **Step 5: Add API types and calls**

In `frontend/src/api/types.ts`, extend `NodeInfo` with:

```typescript
  image_digest: string | null;
  version: string | null;
  updatable: boolean;
```

And add:

```typescript
export interface NodeUpdateStatus {
  task_id: string;
  status: "updating" | "success" | "failed";
  phase: string;
  message: string;
  pct: number | null;
  node_id: string;
  host: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
}

export interface CurrentVersion {
  image_digest: string | null;
  version: string;
}
```

In `frontend/src/api/client.ts`, add beside the existing node calls (match the surrounding style for the fetch helper in use):

```typescript
  currentVersion: () => get<CurrentVersion>("/nodes/current-version"),
  updateNode: (nodeId: string, drain: boolean) =>
    post<{ task_id: string }>(`/nodes/${nodeId}/update`, { drain }),
  getUpdateStatus: (taskId: string) =>
    get<NodeUpdateStatus>(`/nodes/update/${taskId}`),
```

- [ ] **Step 6: Wire up `SettingsNodes.tsx`**

Add a `Version` column header after `Status` in the `<thead>`, and in each row render:

```tsx
<td className={affordance.kind === "available" ? "version-stale" : undefined}>
  {versionLabel(node.version)}
</td>
```

Add an `Actions` column whose cell renders the control:

```tsx
<td>
  {affordance.kind === "available" && (
    <button
      type="button"
      className="btn btn-warn"
      onClick={() => setPendingUpdate(node)}
    >
      Update
    </button>
  )}
  {affordance.kind === "unavailable" && (
    <button type="button" className="btn" disabled title={affordance.reason}>
      Update
    </button>
  )}
</td>
```

Add the primary-version query and the drain dialog state:

```tsx
const current = useQuery({
  queryKey: ["current-version"],
  queryFn: api.currentVersion,
});

const [pendingUpdate, setPendingUpdate] = useState<NodeInfo | null>(null);
const [updateTaskId, setUpdateTaskId] = useState<string | null>(null);

const updateStatus = useQuery({
  queryKey: ["node-update", updateTaskId],
  queryFn: () => api.getUpdateStatus(updateTaskId!),
  enabled: !!updateTaskId,
  refetchInterval: (query) => {
    const data = query.state.data as NodeUpdateStatus | undefined;
    return data?.status === "updating" ? 3000 : false;
  },
});

const startUpdate = useMutation({
  mutationFn: ({ nodeId, drain }: { nodeId: string; drain: boolean }) =>
    api.updateNode(nodeId, drain),
  onSuccess: (data) => {
    setUpdateTaskId(data.task_id);
    setPendingUpdate(null);
  },
});
```

Compute the affordance per row:

```tsx
const affordance = updateAffordance({
  imageDigest: node.image_digest,
  updatable: node.updatable,
  primaryDigest: current.data?.image_digest ?? null,
});
```

Render the drain dialog when `pendingUpdate` is set. When the node has running jobs, it asks; when it has none, it starts a drain-mode update directly (NU-31):

```tsx
{pendingUpdate && (
  <div className="update-confirm">
    {pendingUpdate.running_jobs > 0 ? (
      <>
        <p>
          {pendingUpdate.node_id} is running {pendingUpdate.running_jobs} job
          {pendingUpdate.running_jobs === 1 ? "" : "s"}.
        </p>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() =>
            startUpdate.mutate({ nodeId: pendingUpdate.node_id, drain: true })
          }
        >
          Finish jobs first
        </button>
        <button
          type="button"
          className="btn"
          onClick={() =>
            startUpdate.mutate({ nodeId: pendingUpdate.node_id, drain: false })
          }
        >
          Update now (jobs requeue)
        </button>
      </>
    ) : (
      <>
        <p>Update {pendingUpdate.node_id} to the current backend image?</p>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() =>
            startUpdate.mutate({ nodeId: pendingUpdate.node_id, drain: true })
          }
        >
          Update
        </button>
      </>
    )}
    <button type="button" className="btn" onClick={() => setPendingUpdate(null)}>
      Cancel
    </button>
  </div>
)}
```

Render progress while an update runs, reusing the shape `ProvisionProgress` already uses:

```tsx
{updateTaskId && updateStatus.data && (
  <div className="update-progress">
    <p>{updateStatus.data.message}</p>
    {updateStatus.data.error && (
      <p className="error">{updateStatus.data.error}</p>
    )}
    {updateStatus.data.status !== "updating" && (
      <button type="button" className="btn" onClick={() => setUpdateTaskId(null)}>
        Close
      </button>
    )}
  </div>
)}
```

Add the imports:

```tsx
import { updateAffordance, versionLabel } from "../lib/nodeStaleness";
```

- [ ] **Step 7: Typecheck**

```bash
cd frontend && npm run lint
```

`lint` is `tsc --noEmit` in this repo — one command covers both. Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/lib/nodeStaleness.ts frontend/src/lib/nodeStaleness.test.ts \
        frontend/src/api/types.ts frontend/src/api/client.ts \
        frontend/src/components/SettingsNodes.tsx
git commit -m "feat(ui): show each node's version and offer an update when it is behind"
```

---

## Task 12: Manual verification and provisioning-form disclosure

Implements NU-16, and closes the gap no test can: every backend test mocks `asyncssh`, so the real `authorized_keys` round trip is unexercised.

**Files:**
- Modify: `frontend/src/components/SettingsNodes.tsx` (`ProvisionForm`)

- [ ] **Step 1: Add the disclosure to the provisioning form**

In `ProvisionForm`, above the submit button:

```tsx
<p className="muted provision-key-notice">
  BioFlow will install its own SSH key on this machine and keep it, so it can
  update the node later. Your password is used once and is not stored. The
  key is encrypted on this machine; anyone with shell access here can read it.
</p>
```

- [ ] **Step 2: Rebuild the stack**

```bash
docker compose up -d --build api web worker
```

- [ ] **Step 3: Verify the version column against real data**

Open http://localhost:5173 → Settings → Nodes. The local node should show a version rather than "Unknown". Confirm the primary agrees:

```bash
curl -s localhost:8000/api/v1/nodes/current-version
```

The digest here should match what the node row reports, and the row should show no Update button.

- [ ] **Step 4: Provision a real node and confirm the key round trip**

This is the step tests cannot replace. Against a real machine with Docker and sshd:

1. Settings → Nodes → **+ Add Node**, fill in host/username/password.
2. Watch for the `install_key` phase in the progress output.
3. On that machine, confirm the key landed **and nothing was destroyed**:

```bash
grep -c bioflow-node ~/.ssh/authorized_keys   # expect 1
wc -l ~/.ssh/authorized_keys                  # expect prior count + 1
```

4. Confirm the primary stored it encrypted, not in plaintext:

```bash
docker compose exec -T api python -c "
import asyncio
from app.models.node import Node
async def go():
    n = await Node.find_one(Node.node_id == 'YOUR_NODE_NAME')
    print('has key:', n.ssh_key_enc is not None)
    print('plaintext leaked:', b'PRIVATE KEY' in (n.ssh_key_enc or b''))
asyncio.run(go())"
```

Expected: `has key: True`, `plaintext leaked: False`.

- [ ] **Step 5: Verify the negative case**

On a node whose `~/.ssh` cannot be written (e.g. `chmod 500 ~`), provisioning must fail at `install_key`, name the reason, and leave no `Node` document — not fall back to storing the password.

- [ ] **Step 6: Verify an actual update**

With a node enrolled, click **Update**. Confirm: the drain prompt appears only when jobs are running; the progress panel advances through pull → restart → verify; and the row's version updates once the worker re-enrolls.

Then verify the failure path is honest — stop Docker on the node mid-update and confirm the task fails at `verify` with the crash-loop message rather than reporting success.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/SettingsNodes.tsx
git commit -m "feat(ui): say what SSH key BioFlow installs and retains when provisioning"
```

---

## Task 13: Close out and open the PR

- [ ] **Step 1: Run the full backend suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Read the pass count. Every prior node test must still pass — Task 5 changed `enumerate_nodes`' output shape, which `test_nodes.py` asserts against.

- [ ] **Step 2: Run frontend checks**

```bash
cd frontend && npm test && npm run lint
```

- [ ] **Step 3: Run ruff as CI does**

```bash
./run_ruff.sh
```

CI runs rules the local suite does not — `I001` import-order in particular has broken a PR here before. Fix what it reports.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --fill
```

Title: `feat(api): update compute nodes to a new backend image from Settings`

The description must carry the *why*: nodes had no way to report their version and no route back in after provisioning; and it must note that Task 1 fixes a pre-existing bug where `NodeProvisionTask` was never registered with Beanie, so provisioning failed at runtime on `main`. Include `Closes #240`.

- [ ] **Step 5: Label the PR**

```bash
gh pr edit <N> --add-label "type:feature" --add-label "area:backend" --add-label "area:frontend" --add-label "area:infrastructure"
```

`.github/release.yml` categorizes release notes by label, not by the title's prefix — an unlabelled PR lands under "Other changes".

- [ ] **Step 6: Watch CI**

```bash
gh pr checks <N>
```

Poll until every check reports pass or fail, not just until the command returns. Fix what CI finds, push, re-poll. Only report the PR URL once checks are green and `gh pr view <N> --json mergeable` is clean.

---

## Notes for the implementer

**Restart the worker after backend changes.** `worker` does not hot-reload — it runs `python -m app.worker_main` with no reload mechanism. After any change to `worker.py`:

```bash
docker compose restart worker
```

Otherwise the old code keeps running and the change reads as "didn't work."

**Run tests from the main repo root.** `docker compose exec api python -m pytest` inside a worktree silently tests *main's* code, because the `api` container bind-mounts the main checkout. From a worktree use `./backend/run-worktree-tests.sh tests/ -q` instead.

**The digest probe needs the Docker socket.** `docker-compose.child-node.yml` mounts it already. If the local `worker` service does not, `_own_image_digest()` returns `None` and the local node shows "Unknown" — correct behavior (NU-3), not a bug, but worth knowing before chasing it.
