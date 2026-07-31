# Profiles (Backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Partition the metadata layer (`projects`, `objects`, `runs`, `jobs`, `schedules`) by an `owner` string tied to a new `Profile` collection, while keeping `blobs` global — so several profiles share one library on disk but see only their own projects, files, and runs.

**Architecture:** A new `Profile` document plus a FastAPI dependency (`get_current_owner`) that resolves an `X-BioFlow-Profile` header to an owner string. Every service function that queries `Project`, `DataObject`, `PipelineRun`, or `Job` gains an explicit `owner: str` parameter threaded into its query — no context vars, so a forgotten filter is a missing argument, not silent leakage. `queue.enqueue` gains an `owner` parameter; every existing `dedup_key` that is not already scoped by something profile-specific gets `owner` prefixed onto it. `queue/results.py`'s handlers gain `owner`, threaded from `_apply_result`'s existing `job: Job` (which already has `job.owner` — no new lookup needed). First boot creates a profile literally named `"local"` so the existing library needs no data migration.

**Tech Stack:** FastAPI, Beanie/Motor (MongoDB), Python 3.12, pytest + pytest-asyncio, hashlib/secrets from stdlib for the password speed bump (no new dependency — this is explicitly not a security boundary, see the design spec).

**Reference:** `docs/superpowers/specs/2026-07-31-profiles-design.md` — read it before starting; this plan implements it exactly and does not repeat its rationale except where a step needs it to make a call correctly.

---

## Before you start

Run the existing suite once so you have a clean baseline to compare against after each task:

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all tests pass (no failures). If they don't, stop and report — do not start this plan against a red baseline.

All backend commands in this plan run inside the `api` container, per `CLAUDE.md`: `docker compose exec api python -m pytest tests/ -q`, never a bare host `.venv`. After any change to `backend/app/queue/pipeline_handlers.py` or files it imports, `docker compose restart worker` is required before that change is live — but this plan does not touch that file, so it applies only if you deviate.

Run `docker compose up -d --build api web worker` **from the main repo root**, never from this worktree — see `CLAUDE.md`'s warning about bind-mount project-name collisions.

---

## Task 1: The `Profile` model

**Files:**
- Create: `backend/app/models/profile.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/models/test_profile.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/models/__init__.py` if it does not already exist (empty file, makes the directory a package):

```bash
docker compose exec api test -f tests/models/__init__.py && echo exists || echo missing
```

If missing, create it as an empty file.

Write `backend/tests/models/test_profile.py`:

```python
import pytest

from app.models import Profile


pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


class TestProfile:
    async def test_username_is_required_and_unique(self):
        await Profile(username="ada", display={"emoji": "🧬", "colour": "#4a9eff"}).insert()

        with pytest.raises(Exception):
            await Profile(username="ada", display={"emoji": "🔬", "colour": "#000"}).insert()

    async def test_password_hash_defaults_to_none(self):
        profile = await Profile(
            username="grace", display={"emoji": "⚓", "colour": "#4a9eff"}
        ).insert()

        assert profile.password_hash is None

    async def test_owner_id_is_its_own_stringified_object_id(self):
        profile = await Profile(
            username="hopper", display={"emoji": "⚓", "colour": "#4a9eff"}
        ).insert()

        assert str(profile.id) == profile.owner_id()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/models/test_profile.py -v
```

Expected: FAIL with `ImportError: cannot import name 'Profile'` (the model doesn't exist yet).

- [ ] **Step 3: Write the model**

Create `backend/app/models/profile.py`:

```python
"""Profiles: the organizational boundary between people sharing one library.

Not a security mechanism. A profile's password, when set, exists only to stop
someone entering the *wrong* profile by accident -- see
docs/superpowers/specs/2026-07-31-profiles-design.md, "Passwords are a speed
bump". The rest of the API stays unauthenticated.

A Profile is deliberately outside the owner partition it defines: every other
collection is scoped by `owner`, and a Profile's own `owner` field (inherited
from TimestampedDocument, always "local") is meaningless -- what matters is its
own `id`, stringified, which becomes the `owner` value on every document it
creates.
"""

from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument


class ProfileDisplay(BaseModel):
    emoji: str = "🧬"
    colour: str = "#4a9eff"


class Profile(TimestampedDocument):
    username: str
    password_hash: str | None = None  # None means no password
    email: str | None = None
    display: ProfileDisplay = Field(default_factory=ProfileDisplay)
    # Free-form: name, institution, research areas. Never validated -- it is
    # display-only and has no effect on partitioning or behavior.
    details: dict = Field(default_factory=dict)
    last_used_at: None = None  # set on successful profile selection

    def owner_id(self) -> str:
        """The value this profile's documents carry in their `owner` field.

        A method rather than reading `.id` directly at call sites: the "local"
        special case (Task 6) needs the same accessor to return the literal
        string "local" for the adopted profile, and every caller should go
        through one place rather than each reimplementing str(profile.id).
        """
        return str(self.id)

    class Settings:
        name = "profiles"
        indexes = [
            IndexModel([("username", ASCENDING)], name="uniq_username", unique=True),
        ]
```

Note: `last_used_at: None = None` above is a placeholder type that step 4 corrects — Pydantic needs the real type. Use this instead:

```python
from datetime import datetime
```

and change the field to:

```python
    last_used_at: datetime | None = None
```

Add both the import and the corrected field in the actual file (the intermediate `None = None` shown above is only to flag the mistake explicitly — write the corrected version directly).

Register it in `backend/app/models/__init__.py`. Add the import near the other single-model imports (alphabetical placement, after `OrganismBlurb` and before `PipelineRun` per the existing `__all__` ordering):

```python
from app.models.profile import Profile, ProfileDisplay
```

Add `Profile` to `ALL_MODELS` (find the list in the same file — it is what `init_beanie` registers) and add `"Profile"` and `"ProfileDisplay"` to `__all__` in the same alphabetical position as the import.

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec api python -m pytest tests/models/test_profile.py -v
```

Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/profile.py backend/app/models/__init__.py backend/tests/models/
git commit -m "feat: add the Profile model"
```

---

## Task 2: `get_current_owner` — the request-scoping dependency

**Files:**
- Create: `backend/app/api/deps.py`
- Test: `backend/tests/api/test_deps.py`

This is the **first use of FastAPI's `Depends()`/`Header()` in this codebase** (confirmed by search — every existing route reads params inline). `ruff.toml`'s `ignore = ["B008"]` comment ("FastAPI Depends() in defaults is idiomatic") already anticipates this, so it needs no lint-config change.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_deps.py`:

```python
import pytest
from fastapi import Header, HTTPException

from app.api.deps import get_current_owner
from app.models import Profile


pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


class TestGetCurrentOwner:
    async def test_resolves_a_known_profile_header_to_its_owner_id(self):
        profile = await Profile(username="deps-known", display={}).insert()

        owner = await get_current_owner(x_bioflow_profile=str(profile.id))

        assert owner == str(profile.id)

    async def test_missing_header_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            await get_current_owner(x_bioflow_profile=None)

        assert exc_info.value.status_code == 400

    async def test_unknown_profile_id_raises_404(self):
        with pytest.raises(HTTPException) as exc_info:
            await get_current_owner(x_bioflow_profile="000000000000000000000000")

        assert exc_info.value.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/api/test_deps.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.api.deps'`.

- [ ] **Step 3: Write the dependency**

Create `backend/app/api/deps.py`:

```python
"""FastAPI dependencies shared across routers.

`get_current_owner` is the seam every partitioned query goes through: it turns
the client-supplied X-BioFlow-Profile header into the `owner` string that
service functions take as an explicit parameter. See
docs/superpowers/specs/2026-07-31-profiles-design.md, "Request scoping" -- the
explicit-parameter choice there is why this dependency returns a plain `str`
rather than stashing anything in request state.
"""

from beanie import PydanticObjectId
from fastapi import Header, HTTPException

from app.models import Profile


async def get_current_owner(
    x_bioflow_profile: str | None = Header(default=None),
) -> str:
    if not x_bioflow_profile:
        raise HTTPException(status_code=400, detail="X-BioFlow-Profile header is required")

    # "local" is the one owner id that is not a real ObjectId (Task 6's
    # first-boot adoption). Every other value must resolve to a live profile.
    if x_bioflow_profile == "local":
        profile = await Profile.find_one(Profile.username == "local")
        if profile is None:
            raise HTTPException(status_code=404, detail="No profile found for 'local'")
        return "local"

    try:
        profile_id = PydanticObjectId(x_bioflow_profile)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Malformed profile id") from e

    profile = await Profile.get(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Unknown profile: {x_bioflow_profile}")

    return str(profile.id)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec api python -m pytest tests/api/test_deps.py -v
```

Expected: PASS (3 tests). Note `test_resolves_a_known_profile_header_to_its_owner_id` will pass because the profile id is a real ObjectId, not literally `"local"` — the `"local"` branch is exercised separately in Task 6's tests once the adoption flow exists.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/deps.py backend/tests/api/test_deps.py
git commit -m "feat: add the X-BioFlow-Profile resolution dependency"
```

---

## Task 3: Thread `owner` through `project_service.py`

**Files:**
- Modify: `backend/app/services/project_service.py`
- Modify: `backend/tests/services/test_project_deletion.py` (existing calls need an `owner` arg — see Step 5)
- Test: `backend/tests/services/test_project_service_owner.py`

This is the first of three services (this one, `object_service.py` in Task 4, `run_service.py` in Task 5) that get the same treatment: every `Project`/`DataObject`/`PipelineRun` query gains an `owner` filter, and every function that constructs a new document sets `owner` explicitly instead of relying on the `"local"` default.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_project_service_owner.py`:

```python
import pytest

from app.models import Project
from app.services import project_service


pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


class TestProjectServiceOwnerScoping:
    """Negative tests: two profiles, one query, assert isolation.

    Per docs/superpowers/specs/2026-07-31-profiles-design.md, "Testing" --
    asserting a profile *can* see its own data passes whether or not the
    filter was ever applied. These assert what the OTHER profile cannot see.
    """

    async def test_create_project_stamps_the_given_owner(self):
        project = await project_service.create_project(name="owner-stamp", owner="owner-a")

        assert project.owner == "owner-a"

    async def test_list_projects_excludes_other_owners(self):
        await project_service.create_project(name="alpha", owner="owner-a")
        await project_service.create_project(name="beta", owner="owner-b")

        owner_a_projects = await project_service.list_projects(owner="owner-a")

        assert [p.name for p in owner_a_projects] == ["alpha"]

    async def test_get_project_raises_not_found_for_wrong_owner(self):
        """A wrong-owner lookup is indistinguishable from a missing one --
        deliberately. get_project already raises NotFoundError rather than
        returning None (project_service.py:55), and preserving that contract
        keeps every existing caller working unchanged."""
        from app.errors import NotFoundError

        project = await project_service.create_project(name="cross-get", owner="owner-a")

        with pytest.raises(NotFoundError):
            await project_service.get_project(project.id, owner="owner-b")

    async def test_same_name_allowed_across_owners(self):
        """The uniq_sibling_name index already leads with owner (project.py:41)
        -- this proves create_project actually uses that scoping rather than
        the index silently never being exercised because owner was always
        'local' before this feature."""
        a = await project_service.create_project(name="shared-name", owner="owner-a")
        b = await project_service.create_project(name="shared-name", owner="owner-b")

        assert a.id != b.id
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/services/test_project_service_owner.py -v
```

Expected: FAIL — `create_project() got an unexpected keyword argument 'owner'`.

- [ ] **Step 3: Thread `owner` through the service**

Read the current file first:

```bash
docker compose exec api python -c "import app.services.project_service as m; print(m.__file__)"
```

Edit `backend/app/services/project_service.py`. Change `create_project`:

```python
async def create_project(
    *,
    name: str,
    owner: str,
    description: str = "",
    parent_id: PydanticObjectId | None = None,
    metadata: dict | None = None,
    tags: list[str] | None = None,
) -> Project:
    name = name.strip()
    if not name:
        raise ValidationError("Project name cannot be empty")

    path: list[PydanticObjectId] = []
    if parent_id is not None:
        parent = await Project.get(parent_id)
        if parent is None:
            raise NotFoundError(f"Parent project not found: {parent_id}")
        path = [*parent.path, parent.id]

    project = Project(
        name=name,
        owner=owner,
        slug=slugify(name),
        description=description,
        parent_id=parent_id,
        path=path,
        metadata=metadata or {},
        tags=tags or [],
    )
    try:
        await project.insert()
    except DuplicateKeyError as e:
        raise ConflictError(
            f"A project named {name!r} already exists here",
            details={"name": name, "parent_id": str(parent_id) if parent_id else None},
        ) from e
    return project
```

Change `get_project` to filter by owner. **Keep the `NotFoundError` raise** — the current function raises rather than returning `None` (`project_service.py:55-59`), and every caller depends on that. A wrong-owner lookup raises the same error as a missing one, which is both the smallest change and the right behavior: another profile's project should be indistinguishable from one that does not exist.

```python
async def get_project(project_id: PydanticObjectId, *, owner: str) -> Project:
    project = await Project.get(project_id)
    if project is None or project.owner != owner:
        raise NotFoundError(f"Project not found: {project_id}")
    return project
```

Change `list_projects`:

```python
async def list_projects(
    *,
    owner: str,
    parent_id: PydanticObjectId | None = None,
    include_archived: bool = False,
    limit: int = 200,
) -> list[Project]:
    query: dict = {"owner": owner, "parent_id": parent_id}
    if not include_archived:
        query["archived"] = False
    return await Project.find(query).sort("-updated_at").limit(limit).to_list()
```

Change `breadcrumbs` to accept `owner` and filter the ancestor lookup (find the function; it currently does `Project.find({"_id": {"$in": project.path}})`):

```python
async def breadcrumbs(project: Project, *, owner: str) -> list[dict]:
    if not project.path:
        return [{"id": str(project.id), "name": project.name}]

    ancestors = await Project.find({"_id": {"$in": project.path}, "owner": owner}).to_list()
    # ... (keep the rest of the function body unchanged; it only orders and
    # shapes `ancestors`, and does not itself construct any new query)
```

Change `collect_subtree` (currently `Project.find({"parent_id": {"$in": frontier}})`) to add `"owner": owner` to that query dict, and add `*, owner: str` to its signature.

Change `deletion_preview` and `delete_project_tree` the same way: add `owner: str` to each signature (keyword-only, after any existing required positional/keyword args), and add `"owner": owner` (or `.owner == owner` for the typed-field query style already used) to every `DataObject.find`, `Job.find`, `PipelineRun.find`, and `UploadSession.find` call inside them. Every query dict/expression in both functions gets the same treatment — there is no query in either function that should stay unscoped, since both operate on a project the caller has already resolved to one owner.

`update_project` (`project_service.py:91`) and `delete_project` (`:115`) — both take a `project_id` and act on it. Add `*, owner: str` to each and have them resolve the project through the now-scoped `get_project(project_id, owner=owner)` rather than `Project.get(project_id)` directly, so the owner check and the `NotFoundError` behavior live in one place:

```python
async def update_project(project_id: PydanticObjectId, updates: dict, *, owner: str) -> Project:
    project = await get_project(project_id, owner=owner)
    # ... rest of the existing body unchanged, operating on `project`
```

Apply the same shape to `delete_project(project_id, *, cascade: bool = False, owner: str)`. Read both bodies before editing — if either currently calls `Project.get(project_id)` and handles `None` itself, replacing that with the `get_project(..., owner=owner)` call removes the now-redundant `None` branch, since `get_project` raises.

`bump_counters` (`project_service.py:258`) takes a `project_id` and increments counters via `$inc` on that document directly by id. It needs **no** `owner` parameter: it never queries by anything other than the specific `_id` it was given, and it is called from inside `ingest_local_file` and friends which have already resolved ownership. Adding one would be threading a parameter for no isolation benefit.

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec api python -m pytest tests/services/test_project_service_owner.py -v
```

Expected: PASS (4 tests).

- [ ] **Step 5: Fix the now-broken existing test file**

`backend/tests/services/test_project_deletion.py` calls `project_service.create_project(name=..., parent_id=...)` without `owner` — it will now fail with a missing required argument. Also `backend/tests/services/helpers.py`'s `make_project` (used by other test files) calls `create_project` too.

Run the full suite to see every break:

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: multiple failures, all `TypeError: create_project() missing 1 required keyword-only argument: 'owner'` or similar for `get_project`/`list_projects`/`breadcrumbs`/`collect_subtree`/`deletion_preview`/`delete_project_tree`.

Fix `backend/tests/services/helpers.py`'s `make_project` first (it is the shared factory other test files call):

```python
async def make_project(name: str, parent: Project | None = None, *, owner: str = "test-owner") -> Project:
    return await project_service.create_project(
        name=name, owner=owner, parent_id=parent.id if parent else None
    )
```

Using a default (`owner: str = "test-owner"`) rather than making it required keeps every existing call site in `test_project_deletion.py` compiling unchanged, since none of those tests care about cross-owner behavior — they are testing deletion cascade correctness, which Task 3's new file is what specifically exercises the owner boundary. Fix any other direct `project_service.create_project(...)` / `get_project(...)` / `list_projects(...)` / `breadcrumbs(...)` / `collect_subtree(...)` / `deletion_preview(...)` / `delete_project_tree(...)` calls found by the failing run the same way — add `owner="test-owner"` (or reuse the object's own `.owner` where one is already in scope).

- [ ] **Step 6: Run the full suite to confirm nothing else broke**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: PASS, all tests green.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/project_service.py backend/tests/services/
git commit -m "feat: scope project_service queries by owner"
```

---

## Task 4: Thread `owner` through `object_service.py`

**Files:**
- Modify: `backend/app/services/object_service.py`
- Test: `backend/tests/services/test_object_service_owner.py`

Same treatment as Task 3, applied to `DataObject`. `DataObject` has no `owner`-leading index today (per the design spec, `by_status` leads with `owner` but nothing else does) — this task does not add an index, since none of the new queries are a new access pattern; they add a filter to an existing one.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_object_service_owner.py`:

```python
import pytest

from app.services import object_service, project_service


pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


async def _make_project(owner: str, name: str = "obj-owner-test"):
    return await project_service.create_project(name=name, owner=owner)


class TestObjectServiceOwnerScoping:
    async def test_ingest_local_file_stamps_the_given_owner(self, tmp_path):
        project = await _make_project("owner-a")
        path = tmp_path / "reads.fastq"
        path.write_text("@r1\nACGT\n+\nIIII\n")

        obj = await object_service.ingest_local_file(
            project_id=project.id, path=path, name="reads.fastq", owner="owner-a"
        )

        assert obj.owner == "owner-a"

    async def test_list_objects_excludes_other_owners_object(self, tmp_path):
        project_a = await _make_project("owner-a", "list-a")
        project_b = await _make_project("owner-b", "list-b")
        path_a = tmp_path / "a.fastq"
        path_a.write_text("@r1\nACGT\n+\nIIII\n")
        path_b = tmp_path / "b.fastq"
        path_b.write_text("@r1\nTTTT\n+\nIIII\n")

        obj_a = await object_service.ingest_local_file(
            project_id=project_a.id, path=path_a, name="a.fastq", owner="owner-a"
        )
        await object_service.ingest_local_file(
            project_id=project_b.id, path=path_b, name="b.fastq", owner="owner-b"
        )

        results = await object_service.list_objects(project_a.id, owner="owner-a")

        assert [o.id for o in results] == [obj_a.id]

    async def test_get_object_raises_not_found_for_wrong_owner(self, tmp_path):
        """get_object already raises NotFoundError rather than returning None
        (object_service.py:35-39); preserve that contract so existing callers
        keep working, and so another profile's file is indistinguishable from
        a file that does not exist."""
        from app.errors import NotFoundError

        project = await _make_project("owner-a", "get-wrong")
        path = tmp_path / "reads.fastq"
        path.write_text("@r1\nACGT\n+\nIIII\n")
        obj = await object_service.ingest_local_file(
            project_id=project.id, path=path, name="reads.fastq", owner="owner-a"
        )

        with pytest.raises(NotFoundError):
            await object_service.get_object(obj.id, owner="owner-b")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/services/test_object_service_owner.py -v
```

Expected: FAIL — `ingest_local_file() got an unexpected keyword argument 'owner'`.

- [ ] **Step 3: Thread `owner` through the service**

Edit `backend/app/services/object_service.py`.

`get_object` — keep the existing `NotFoundError` raise, add the owner check to it:

```python
async def get_object(object_id: PydanticObjectId, *, owner: str) -> DataObject:
    obj = await DataObject.get(object_id)
    if obj is None or obj.owner != owner:
        raise NotFoundError(f"Object not found: {object_id}")
    return obj
```

`list_objects`:

```python
async def list_objects(
    project_id: PydanticObjectId,
    *,
    owner: str,
    limit: int = 200,
    status: ObjectStatus | None = None,
    include_sidecars: bool = False,
) -> list[DataObject]:
    query: dict = {"project_id": project_id, "owner": owner}
    if status is not None:
        query["status"] = status.value
    if not include_sidecars:
        query["sidecar_of"] = None
    return await DataObject.find(query).sort("-created_at").limit(limit).to_list()
```

`list_sidecars` (currently `DataObject.find(DataObject.sidecar_of == parent_id)`) — add `*, owner: str` and change the query to `DataObject.find(DataObject.sidecar_of == parent_id, DataObject.owner == owner)`.

`ingest_local_file` — add `owner: str` (keyword-only, required, placed right after `project_id` for readability) and pass it into the `DataObject(...)` constructor:

```python
async def ingest_local_file(
    *,
    project_id: PydanticObjectId,
    owner: str,
    path: Path,
    name: str,
    role: ObjectRole | None = None,
    derived_from: list[PydanticObjectId] | None = None,
    produced_by_job: PydanticObjectId | None = None,
    facts: dict | None = None,
    metadata: dict | None = None,
    sidecar_of: PydanticObjectId | None = None,
    sidecar_role: SidecarRole | None = None,
) -> DataObject:
    require_home()

    project = await Project.get(project_id)
    if project is None:
        raise NotFoundError(f"Project not found: {project_id}")

    safe_name = Path(name).name.strip()
    if not safe_name or safe_name in (".", ".."):
        raise ValidationError(f"Unsafe filename: {name!r}")

    if not await asyncio.to_thread(path.exists):
        raise NotFoundError(f"Produced file is missing: {path}")

    obj = DataObject(
        project_id=project_id,
        owner=owner,
        name=safe_name,
        status=ObjectStatus.HASHING,
        role=role,
        derived_from=derived_from or [],
        produced_by_job=produced_by_job,
        facts=facts or {},
        metadata=metadata or {},
        sidecar_of=sidecar_of,
        sidecar_role=sidecar_role,
        source=SourceInfo(mode=SourceMode.UPLOAD, original_name=safe_name),
    )
    await obj.insert()
    # ... (rest of the function body is unchanged -- hashing, blob attach,
    # counters, ingest job enqueue, run linking, error handling all stay as
    # they are; only the DataObject(...) construction above changes)
```

At the end of `ingest_local_file`, it calls `return await get_object(obj.id)` — this now needs `owner=owner` added: `return await get_object(obj.id, owner=owner)`.

Apply the identical pattern (add `owner: str`, add `owner=owner` to the `DataObject(...)` call, add `"owner": owner` or `.owner == owner` to any query) to `ingest_stream` and `register_in_place` — both already do `Project.get(project_id)` and both already construct a `DataObject(...)`.

`set_pair` (`object_service.py:485`) and `clear_pair` (`:558`) — add `*, owner: str` to each signature and add `DataObject.owner == owner` to their `find_one` calls (they currently query by `.id` alone, e.g. `DataObject.find_one(DataObject.id == mate.id, ...)` — add the owner clause as an additional positional filter argument to the same `find_one` call, following Beanie's typed-comparison-query style already in use there).

`update_object` (`:592`), `delete_object` (`:630`), and `object_with_blob` (`:702`) — all three resolve one object by id. Add `*, owner: str` to each and have them go through the now-scoped `get_object(object_id, owner=owner)` rather than `DataObject.get(object_id)` directly, so the owner check and the `NotFoundError` live in one place:

```python
async def update_object(object_id: PydanticObjectId, updates: dict, *, owner: str) -> DataObject:
    obj = await get_object(object_id, owner=owner)
    # ... rest of the existing body unchanged, operating on `obj`
```

`delete_object` matters most of the three: it cascades to sidecars and decrements blob refcounts (its docstring explains why the cascade is required rather than tidy), so an unscoped version would let one profile delete another's file *and* its bytes. Read its body and confirm the sidecar lookup inside it (`list_sidecars`, or a direct `DataObject.find(DataObject.sidecar_of == ...)`) also receives `owner=owner` — a sidecar always shares its parent's owner, so scoping it changes nothing functionally but keeps the query honest.

`remove_report_dirs` (`:667`) is a pure filesystem helper keyed by object id with no database query; it needs no `owner`.

`enqueue_ingest` (`:292`) enqueues the header-parse job. Task 8 covers its `enqueue(...)` call; it needs an `owner` parameter threaded from its callers (`ingest_local_file`, `ingest_stream`, `register_in_place`) so it can pass `owner=owner` to `enqueue`. Add `*, owner: str` to it here in Task 4, and pass the caller's `owner` at each of its call sites — Task 8's Step 5 then has a real value to use rather than a placeholder.

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec api python -m pytest tests/services/test_object_service_owner.py -v
```

Expected: PASS (3 tests).

- [ ] **Step 5: Fix every existing call site**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: failures anywhere `object_service.ingest_local_file`, `ingest_stream`, `register_in_place`, `get_object`, `list_objects`, `list_sidecars`, `set_pair`, or `clear_pair` are called without `owner`. This includes:

- `backend/tests/services/helpers.py`'s `make_object` — add `owner: str = "test-owner"` to its signature and pass it through to whatever it calls to build the `DataObject` (per the research, `make_object` constructs the `DataObject` and `Blob` directly rather than calling `ingest_local_file`, so check whether it needs an `owner=owner` argument on its own `DataObject(...)` construction instead of a service call — read the actual file to confirm before editing).
- Any test in `backend/tests/services/` or `backend/tests/api/` calling these functions directly.

Fix each with `owner="test-owner"` (or the project's own `.owner` where already in scope), matching the pattern from Task 3 Step 5.

- [ ] **Step 6: Run the full suite to confirm nothing else broke**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: PASS, all tests green.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/object_service.py backend/tests/services/
git commit -m "feat: scope object_service queries by owner"
```

---

## Task 5: Thread `owner` through `run_service.py`

**Files:**
- Modify: `backend/app/services/run_service.py`
- Test: `backend/tests/services/test_run_service_owner.py`

Both `PipelineRun` and `RunJob` extend `TimestampedDocument` (`models/run.py:69` and `:121`), so both already carry `owner`. Verified facts this task depends on:

- `create_run` (`run_service.py:72-92`) takes `kind`, `project_id`, `label`, `inputs: list[RunInput]`, `params: dict` (**required**, not optional), `tool: str | None = None`.
- There is **no `get_run` function in `run_service.py`**. Single-run lookup happens in the *route*, `api/v1/runs.py:97`, which calls `PipelineRun.get(run_id)` directly and bypasses the service layer. Three routes do this (`runs.py:89`, `:99`, `:117`) plus `list_runs` (`runs.py:74`) which builds its own `PipelineRun.find(query)`.

Because those routes query `PipelineRun` directly, scoping them is route-layer work in the same category as the other ~12 unwired route files — deferred with the rest (see "What this plan does not include"). This task scopes the **service** functions only.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_run_service_owner.py`:

```python
import pytest

from app.models import RunKind
from app.services import project_service, run_service


pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


class TestRunServiceOwnerScoping:
    async def test_create_run_stamps_the_given_owner(self):
        project = await project_service.create_project(name="run-owner-a", owner="owner-a")

        run = await run_service.create_run(
            kind=RunKind.TRIM,
            project_id=project.id,
            label="trim",
            inputs=[],
            params={},
            owner="owner-a",
        )

        assert run.owner == "owner-a"

    async def test_two_owners_runs_do_not_leak_into_each_others_status(self):
        """status_for_many is the multi-run query the activity view uses.
        Asserting owner-a's call returns only owner-a's run is the negative
        direction: it fails if the owner filter was never applied."""
        project_a = await project_service.create_project(name="run-iso-a", owner="owner-a")
        project_b = await project_service.create_project(name="run-iso-b", owner="owner-b")
        run_a = await run_service.create_run(
            kind=RunKind.TRIM,
            project_id=project_a.id,
            label="a",
            inputs=[],
            params={},
            owner="owner-a",
        )
        run_b = await run_service.create_run(
            kind=RunKind.TRIM,
            project_id=project_b.id,
            label="b",
            inputs=[],
            params={},
            owner="owner-b",
        )

        statuses = await run_service.status_for_many([run_a.id, run_b.id], owner="owner-a")

        assert run_b.id not in statuses
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/services/test_run_service_owner.py -v
```

Expected: FAIL — `create_run() got an unexpected keyword argument 'owner'`.

- [ ] **Step 3: Thread `owner` through the service**

Edit `backend/app/services/run_service.py`. `create_run` — add `owner: str` and pass it to the `PipelineRun(...)` constructor, keeping every existing parameter and the log line unchanged:

```python
async def create_run(
    *,
    kind: RunKind,
    project_id: PydanticObjectId,
    label: str,
    inputs: list[RunInput],
    params: dict,
    owner: str,
    tool: str | None = None,
) -> PipelineRun:
    """Record what a user asked for, before any of it is enqueued."""
    run = PipelineRun(
        kind=kind,
        project_id=project_id,
        owner=owner,
        label=label,
        inputs=inputs,
        params=params,
        tool=tool,
    )
    await run.insert()
    log.info("run_created", run_id=str(run.id), kind=kind.value, label=label)
    return run
```

`discard_run` (`run_service.py:95`) — add `*, owner: str` and scope both queries:

```python
async def discard_run(run_id: PydanticObjectId, *, owner: str) -> None:
    """Delete a run whose work was deduplicated away before it started.

    Only for the launch path: a run that turns out to describe nothing must not
    linger in the activity view implying work is happening. Its membership rows
    go too, but the *jobs* they referenced are untouched -- a shared index build
    is real work owned by whichever run queued it first.
    """
    run = await PipelineRun.get(run_id)
    if run is None or run.owner != owner:
        return
    await RunJob.find(RunJob.run_id == run_id).delete()
    await run.delete()
    log.info("run_discarded", run_id=str(run_id))
```

Note the reordering: the original deleted `RunJob` rows *before* fetching the run. Fetching first is required now, because the owner check must happen before anything is deleted — otherwise a wrong-owner call would still destroy another profile's membership rows before discovering it should not have.

`record_outputs` (`run_service.py:212`) — add `*, owner: str`, and after its `run = await PipelineRun.get(run_id)` add the same guard:

```python
    run = await PipelineRun.get(run_id)
    if run is None or run.owner != owner:
        return
```

`status_for` (`run_service.py:145`) and `status_for_many` (`run_service.py:182`) — add `*, owner: str` to each. Both resolve `RunJob` rows and then look up `Job` documents by id. Scope the `Job.find({"_id": {"$in": [...]}})` query in each by adding `"owner": owner` to that query dict. For `status_for_many`, also filter the `RunJob.find({"run_id": {"$in": run_ids}})` result set down to runs this owner owns — fetch the owned run ids first and intersect:

```python
async def status_for_many(
    run_ids: list[PydanticObjectId], *, owner: str
) -> dict[PydanticObjectId, tuple[RunStatus, list[dict]]]:
    owned = await PipelineRun.find(
        {"_id": {"$in": run_ids}, "owner": owner}
    ).to_list()
    owned_ids = [r.id for r in owned]
    if not owned_ids:
        return {}
    # ... existing body unchanged, but querying with `owned_ids` in place of
    # `run_ids` for the RunJob lookup, and with `"owner": owner` added to the
    # Job.find query dict
```

(Read the real bodies of `status_for` and `status_for_many` before editing — the shape above shows where the owner filter goes; keep every other line of their existing logic, including the `derive_status` call and the per-job dict construction, exactly as it is.)

`run_for_job` (`run_service.py:129`), `members` (`:141`), `link_job` (`:110`), and `link_job_to_run_of` (`:228`) query `RunJob` only, by `job_id`/`run_id` foreign keys they were handed. Leave these unscoped: they are internal plumbing called from within a flow that has already resolved ownership, and adding an owner parameter to them would mean threading it through the applier chain for no isolation benefit — `RunJob` rows are reachable only via a `run_id` or `job_id` the caller already holds.

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec api python -m pytest tests/services/test_run_service_owner.py -v
```

Expected: PASS (2 tests).

- [ ] **Step 5: Fix every existing call site, then confirm the full suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: failures at every `create_run(`, `discard_run(`, `status_for(`, `status_for_many(`, `record_outputs(` call missing `owner`. Find them all:

```bash
docker compose exec api grep -rn "create_run(\|discard_run(\|status_for(\|status_for_many(\|record_outputs(" app/ tests/
```

Most are in `services/pipeline_service.py`, `services/sra_service.py`, `services/assembly_service.py` (all launch paths), and `api/v1/runs.py`. Per this plan's scope boundary, pass `owner="local"` at each of those call sites with a `# TODO(profiles): thread owner from the route once its API layer resolves get_current_owner` comment — the same interim treatment Task 8 uses for `enqueue`. Test call sites get `owner="test-owner"`.

- [ ] **Step 6: Run the full suite to confirm nothing else broke**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: PASS, all tests green.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/run_service.py backend/tests/services/
git commit -m "feat: scope run_service queries by owner"
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/run_service.py backend/tests/services/
git commit -m "feat: scope run_service queries by owner"
```

---

## Task 6: First boot adopts `"local"`

**Files:**
- Create: `backend/app/services/profile_service.py`
- Test: `backend/tests/services/test_profile_service.py`

This is the migration-avoidance mechanism the spec depends on: creating the first profile with the literal id `"local"` means the entire existing library (every document already at `owner: "local"`) becomes that profile's data with zero rewrites.

The tricky part: the *profile document itself* still needs a normal, unique `ObjectId` as its `_id` — the `Profile` collection is not itself partitioned (per the design spec, "deliberately outside the partition — it is the thing that defines partitions"). So `"local"` cannot be the profile's `_id`; it is the value that profile's *documents* (projects, objects, runs, jobs it creates) carry in their `owner` field instead. That distinction needs a stored flag, `adopted_legacy_owner`, because after creation there is nothing else that tells `owner_id()` which value to return — see Task 7, which is the first place that distinction actually matters (checking what a profile owns before allowing its deletion).

- [ ] **Step 1: Add `adopted_legacy_owner` to the `Profile` model**

Edit `backend/app/models/profile.py`. Add a field and a method:

```python
class Profile(TimestampedDocument):
    username: str
    password_hash: str | None = None
    email: str | None = None
    display: ProfileDisplay = Field(default_factory=ProfileDisplay)
    details: dict = Field(default_factory=dict)
    last_used_at: datetime | None = None
    # True only for the profile created by first-boot adoption. Needed because
    # its documents carry owner="local", not str(id) -- every owner lookup
    # after creation time needs to know which value to use, and there is
    # nothing else that distinguishes "the profile that adopted the
    # pre-feature library" once more than one profile exists.
    adopted_legacy_owner: bool = False

    def owner_id(self) -> str:
        """The value this profile's documents carry in their `owner` field.

        "local" for the profile that adopted the pre-feature library
        (adopted_legacy_owner=True); its own stringified _id otherwise.
        """
        return "local" if self.adopted_legacy_owner else str(self.id)
```

- [ ] **Step 2: Write the failing test**

Create `backend/tests/services/test_profile_service.py`:

```python
import pytest

from app.errors import ConflictError, ValidationError
from app.models import Profile, Project
from app.services import profile_service, project_service


pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


class TestFirstBootAdoption:
    async def test_first_profile_on_empty_db_adopts_local(self):
        profile = await profile_service.create_profile(username="ada", is_first_boot=True)

        assert str(profile.id) != "local"  # the profile's own _id is a real ObjectId
        assert profile.username == "ada"
        assert profile.owner_id() == "local"  # but its documents carry "local"

    async def test_pre_existing_local_documents_are_untouched_and_visible(self):
        # Simulate a pre-feature library: a project created before Profile existed.
        pre_existing = await project_service.create_project(name="legacy", owner="local")

        profile = await profile_service.create_profile(username="legacy-owner", is_first_boot=True)

        visible = await project_service.list_projects(owner=profile.owner_id())

        assert pre_existing.id in [p.id for p in visible]

    async def test_second_profile_is_not_first_boot(self):
        await profile_service.create_profile(username="first", is_first_boot=True)

        with pytest.raises(ValidationError):
            await profile_service.create_profile(username="second", is_first_boot=True)

    async def test_second_profile_does_not_adopt_local(self):
        await profile_service.create_profile(username="first-of-two", is_first_boot=True)
        second = await profile_service.create_profile(username="second-of-two")

        assert second.owner_id() == str(second.id)

    async def test_username_must_be_unique(self):
        await profile_service.create_profile(username="dupe", is_first_boot=True)

        with pytest.raises(ConflictError):
            await profile_service.create_profile(username="dupe", is_first_boot=False)
```

- [ ] **Step 3: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/services/test_profile_service.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.profile_service'`.

- [ ] **Step 4: Write the service**

Create `backend/app/services/profile_service.py`:

```python
"""Profile lifecycle: creation, first-boot adoption, deletion refusal.

The "local" special case exists so the first profile costs zero data
migration -- see docs/superpowers/specs/2026-07-31-profiles-design.md,
"First boot adopts local". Every document already in the database at
owner="local" (the pre-feature default) becomes that profile's data the
instant it is created, with no document rewritten. `Profile.owner_id()` is
what every caller should use to get the right owner value for a given
profile -- never `str(profile.id)` directly, which is wrong for the adopted
profile.
"""

from pymongo.errors import DuplicateKeyError

from app.errors import ConflictError, ValidationError
from app.models import Profile


async def create_profile(
    *,
    username: str,
    password: str | None = None,
    email: str | None = None,
    is_first_boot: bool = False,
) -> Profile:
    username = username.strip()
    if not username:
        raise ValidationError("Username cannot be empty")

    if is_first_boot:
        existing_count = await Profile.find({}).count()
        if existing_count > 0:
            raise ValidationError(
                "Cannot create a first-boot profile: profiles already exist"
            )

    profile = Profile(
        username=username,
        password_hash=_hash_password(password) if password else None,
        email=email,
        adopted_legacy_owner=is_first_boot,
    )
    try:
        await profile.insert()
    except DuplicateKeyError as e:
        raise ConflictError(f"A profile named {username!r} already exists") from e
    return profile


def _hash_password(password: str) -> str:
    """A speed bump, not a security boundary -- see the design spec's
    "Passwords are a speed bump". stdlib hashlib with a random salt is
    sufficient for "stop an accidental wrong-profile login"; it deliberately
    avoids adding bcrypt/argon2 as a dependency for a threat model that does
    not include a determined attacker.
    """
    import hashlib
    import secrets

    salt = secrets.token_hex(16)
    digest = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${digest}"


def verify_password(password: str, password_hash: str) -> bool:
    import hashlib

    salt, _, digest = password_hash.partition("$")
    return hashlib.sha256((salt + password).encode()).hexdigest() == digest
```

`count_owned_documents` and `delete_profile` are added to this same file in Task 7, once deletion refusal actually needs them — no reason to define them before anything calls them.

- [ ] **Step 5: Run test to verify it passes**

```bash
docker compose exec api python -m pytest tests/services/test_profile_service.py -v
```

Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/profile.py backend/app/services/profile_service.py backend/tests/services/test_profile_service.py
git commit -m "feat: first-boot profile creation adopts the existing local library"
```

---

## Task 7: Profile deletion refusal

**Files:**
- Modify: `backend/app/services/profile_service.py`
- Test: `backend/tests/services/test_profile_service.py`

Per the spec: refuse deletion while a profile owns any projects or objects, and refuse deleting the last remaining profile outright. Uses `Profile.owner_id()` and `adopted_legacy_owner` from Task 6 — this is exactly why they exist: `delete_profile` needs to check what a profile *currently* owns, and for the adopted profile that is `"local"`, not its own `_id`.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/services/test_profile_service.py`:

```python
class TestProfileDeletion:
    async def test_refuses_when_profile_owns_projects(self):
        profile = await profile_service.create_profile(username="owner-with-data", is_first_boot=True)
        await project_service.create_project(name="keeps-profile-alive", owner=profile.owner_id())

        with pytest.raises(ConflictError) as exc_info:
            await profile_service.delete_profile(profile.id)

        assert exc_info.value.details.get("projects") == 1

    async def test_deletes_an_empty_non_last_profile(self):
        await profile_service.create_profile(username="first-of-two", is_first_boot=True)
        empty = await profile_service.create_profile(username="empty-one")

        await profile_service.delete_profile(empty.id)

        assert await Profile.get(empty.id) is None

    async def test_refuses_to_delete_the_last_profile_even_if_empty(self):
        only = await profile_service.create_profile(username="only-one", is_first_boot=True)

        with pytest.raises(ConflictError):
            await profile_service.delete_profile(only.id)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/services/test_profile_service.py::TestProfileDeletion -v
```

Expected: FAIL — `AttributeError: module 'app.services.profile_service' has no attribute 'delete_profile'`.

- [ ] **Step 3: Write `delete_profile` and `count_owned_documents`**

Add to `backend/app/services/profile_service.py`:

```python
async def count_owned_documents(owner: str) -> dict[str, int]:
    """How many projects and objects a profile owns, so a deletion refusal
    can report real counts rather than a generic "not empty"."""
    projects = await Project.find(Project.owner == owner).count()
    objects = await DataObject.find(DataObject.owner == owner).count()
    return {"projects": projects, "objects": objects}


async def delete_profile(profile_id) -> None:
    profile = await Profile.get(profile_id)
    if profile is None:
        raise ValidationError(f"Profile not found: {profile_id}")

    total = await Profile.find({}).count()
    if total <= 1:
        raise ConflictError(
            "Cannot delete the last profile",
            details={"profile_id": str(profile_id)},
        )

    counts = await count_owned_documents(profile.owner_id())
    if counts["projects"] > 0 or counts["objects"] > 0:
        raise ConflictError(
            f"Profile {profile.username!r} still owns data: "
            f"{counts['projects']} project(s), {counts['objects']} object(s). "
            "Delete its projects first.",
            details=counts,
        )

    await profile.delete()
```

Update the import line at the top of `backend/app/services/profile_service.py` to add the two models this task needs: `from app.models import DataObject, Profile, Project`.

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec api python -m pytest tests/services/test_profile_service.py::TestProfileDeletion -v
```

Expected: PASS (3 tests).

- [ ] **Step 5: Run the full suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: PASS, all tests green.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/profile_service.py backend/tests/services/test_profile_service.py
git commit -m "feat: refuse to delete a profile that still owns data, or the last profile"
```

---

## Task 8: `enqueue` gains `owner`, and dedup keys get prefixed

**Files:**
- Modify: `backend/app/queue/queue.py`
- Modify: `backend/app/queue/results.py` (the two `enqueue("index_bam", ...)` call sites — see Task 9 for the rest of this file)
- Modify: `backend/app/services/pipeline_service.py` (dedup key construction only, in this task; owner-parameter threading through pipeline_service's own launch functions is out of scope for this plan — see "What this plan does not include")
- Modify: `backend/app/services/assembly_service.py`, `backend/app/services/sra_service.py`
- Test: `backend/tests/queue/test_queue_owner.py`

This is the trap the spec names explicitly: `build_index` (`pipeline_service.py:904`, key `f"build_index:{digest or path}:{aligner.value}"`) and `index_bam` (`results.py:935` and `pipeline_service.py:1296`, key `f"index_bam:{bam.blob_sha256}"`) key purely on content digest, with no owner component — two profiles both aligning against the same shared reference would have the second's `build_index` silently deduplicated away.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/queue/test_queue_owner.py`:

```python
import pytest

from app.queue import queue


pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


class TestEnqueueOwner:
    async def test_enqueue_stamps_the_given_owner(self):
        job = await queue.enqueue("noop", owner="owner-a")

        assert job.owner == "owner-a"

    async def test_identical_dedup_key_different_owner_does_not_collide(self):
        """The trap: a dedup_key that is only content-scoped (e.g. a digest)
        would let owner-b's job silently vanish if enqueue doesn't fold owner
        into the key itself. This asserts enqueue's own behavior in isolation
        -- callers are also expected to prefix owner into dedup_key (Step 3
        covers the call sites), but enqueue must not make that the caller's
        only defense.
        """
        job_a = await queue.enqueue("noop", owner="owner-a", dedup_key="shared-digest-key")
        job_b = await queue.enqueue("noop", owner="owner-b", dedup_key="shared-digest-key")

        assert job_a is not None
        assert job_b is not None
        assert job_a.id != job_b.id

    async def test_same_owner_same_dedup_key_still_deduplicates(self):
        """Confirms the fix doesn't accidentally defeat dedup for the same
        profile -- that protection must survive."""
        job_a = await queue.enqueue("noop", owner="owner-a", dedup_key="repeat-key")
        job_b = await queue.enqueue("noop", owner="owner-a", dedup_key="repeat-key")

        assert job_a is not None
        assert job_b is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/queue/test_queue_owner.py -v
```

Expected: FAIL — `enqueue() got an unexpected keyword argument 'owner'`.

- [ ] **Step 3: Add `owner` to `enqueue`, folding it into the dedup key itself**

Edit `backend/app/queue/queue.py`. Change the signature and body:

```python
async def enqueue(
    job_type: str,
    *,
    owner: str,
    payload: dict | None = None,
    job_class: JobClass = JobClass.USER_BACKGROUND,
    dedup_key: str | None = None,
    project_id: PydanticObjectId | None = None,
    object_id: PydanticObjectId | None = None,
    resources: JobResources | None = None,
    max_attempts: int | None = None,
    delay_seconds: float = 0,
    depends_on: list[PydanticObjectId] | None = None,
    parent_job_id: PydanticObjectId | None = None,
) -> Job | None:
    """Create and dispatch a job. Returns None if deduplicated away.

    The Mongo insert is the deduplication guard: a unique partial index over
    non-terminal states means a concurrent duplicate raises DuplicateKeyError
    rather than producing two jobs.

    `owner` is folded into the stored dedup_key itself (not left to callers)
    so a caller who builds a dedup_key from content alone -- a digest, an
    accession -- cannot silently collide across profiles. See
    docs/superpowers/specs/2026-07-31-profiles-design.md, "Trap: dedup keys
    are global".

    `depends_on` holds the job back until every listed job has *succeeded*.
    Such a job is never pushed to Redis; `_release_dependents` puts it there
    when its last dependency finishes. If any dependency fails, the dependent
    fails too, with that dependency named as the reason -- an alignment whose
    index build died must not sit queued forever waiting for a file that is
    never coming.
    """
    now = datetime.now(UTC)
    resources = resources or JobResources()
    available_at = now + timedelta(seconds=delay_seconds) if delay_seconds > 0 else None
    depends_on = list(depends_on or [])

    stored_dedup_key = f"{owner}:{dedup_key}" if dedup_key is not None else None

    job = Job(
        type=job_type,
        owner=owner,
        job_class=job_class,
        state=JobState.PENDING,
        payload=payload or {},
        dedup_key=stored_dedup_key,
        project_id=project_id,
        object_id=object_id,
        resources=resources,
        max_attempts=max_attempts or settings.job_max_attempts,
        available_at=available_at,
        depends_on=depends_on,
        parent_job_id=parent_job_id,
        timing=JobTiming(enqueued_at=now),
    )

    try:
        await job.insert()
    except DuplicateKeyError:
        log.debug("job_deduplicated", type=job_type, dedup_key=stored_dedup_key)
        return None

    # ... (rest of the function body -- the depends_on handling below the
    # insert -- is unchanged; it operates on `job` and `depends_on`, neither
    # of which this step alters)
```

Folding `owner` into `enqueue` itself (rather than only fixing call sites) is the safer of the two approaches the spec names: it means a call site someone adds later, that builds a purely-content-scoped `dedup_key` and forgets the spec's warning, is still safe by construction. Call sites should still pass a *reasonably* scoped `dedup_key` (Step 4 below still fixes the two named traps) because relying solely on the `owner` prefix at the `enqueue` layer means every profile still gets its own copy of otherwise-identical work — correct, but worth being deliberate about, not an excuse to stop thinking about what the key should contain.

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec api python -m pytest tests/queue/test_queue_owner.py -v
```

Expected: PASS (3 tests).

- [ ] **Step 5: Fix every existing `enqueue(` call site to pass `owner`**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: many failures — `enqueue() missing 1 required keyword-only argument: 'owner'` — at every call site listed in the research (19 total, across `results.py`, `scheduler.py`, `api/v1/jobs.py`, `pipeline_service.py` x9, `assembly_service.py`, `sra_service.py`, `upload_service.py`, `object_service.py` x2).

This plan's scope (see "What this plan does not include" below) is the **model/service owner-scoping layer**, not rewiring every pipeline launch function's signature. For this task, the minimum correct fix at each of the 19 sites is: pass `owner="local"` as a literal placeholder **only** at call sites this plan's earlier tasks did not already give an `owner` value to thread through. Concretely:

- `services/object_service.py:323` (`ingest_headers`) and `:435` (`register_hash`) — these run inside `ingest_local_file`/`ingest_stream`/`register_in_place`, which Task 4 already gave an `owner` parameter. Use that `owner` value, not a literal.
- `queue/results.py:935` (`_apply_align_reads`'s follow-on `enqueue("index_bam", ...)`) — Task 9 gives every `results.py` handler an `owner` parameter; use it here too once Task 9 lands. For now (this task, before Task 9), pass `owner="local"` as an explicit interim placeholder and leave a comment `# TODO(profiles): use the handler's owner once Task 9 threads it` so Task 9 has an exact grep target.
- `queue/scheduler.py:117` and `:151` — these are global system schedules (GC, verification), not user actions. Pass `owner="local"` literally and permanently; there is nothing else it could mean, since these jobs are never scoped to a person.
- `api/v1/jobs.py:147` — this is a raw dev/testing endpoint per the research. This plan does not add owner-resolution to every API route (Task 10 adds it only to the new profiles routes and the minimum needed to prove the pattern end-to-end — see that task's scope note). Pass `owner="local"` literally here for now.
- `services/pipeline_service.py` (9 sites) and `services/assembly_service.py`, `services/sra_service.py` — these are launch functions called from API routes that do not yet resolve `get_current_owner` (that wiring is explicitly deferred; see "What this plan does not include"). Pass `owner="local"` literally at each, and leave the same `# TODO(profiles): thread owner from the route once its API layer resolves get_current_owner` comment. This keeps the suite green and the dedup-key fix (the actual trap named in the spec) real and tested, without pulling the entire pipeline-launch API surface into this plan.
- `services/upload_service.py:301` — same treatment: literal `owner="local"` plus the same TODO comment.

Also fix the two named dedup-key traps directly, since fixing them is this task's actual point (folding `owner` into `enqueue`'s stored key, Step 3, already fixes them mechanically — but confirm by reading the call sites: `pipeline_service.py:904`'s `build_index` call and `results.py:935` / `pipeline_service.py:1296`'s `index_bam` calls need no change to their `dedup_key=` argument itself, since `enqueue` now prefixes owner onto whatever they pass).

- [ ] **Step 6: Run the full suite to confirm nothing else broke**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: PASS, all tests green. (All jobs in the current test suite and current running system are attributed to `"local"` at this point in the plan — that is correct and matches the fact that no API route resolves a real profile yet.)

- [ ] **Step 7: Commit**

```bash
git add backend/app/queue/queue.py backend/app/queue/results.py backend/app/services/pipeline_service.py backend/app/services/assembly_service.py backend/app/services/sra_service.py backend/app/services/upload_service.py backend/app/services/object_service.py backend/app/api/v1/jobs.py backend/app/queue/scheduler.py backend/tests/queue/test_queue_owner.py
git commit -m "feat: enqueue takes an owner and folds it into the dedup key

Fixes the trap named in the design spec: build_index and index_bam key purely
on content digest, so two profiles aligning against the same shared reference
would have had the second job silently deduplicated away. Folding owner into
the stored dedup_key at the enqueue layer fixes this for every call site,
present and future, rather than relying on each caller remembering to prefix
it themselves.

Call sites not yet reached by a real profile-resolving API route pass a
literal owner=\"local\" with a TODO marking where Task 9/10 (or later work)
should thread the real value through instead."
```

---

## Task 9: `results.py` propagates `owner` from the job

**Files:**
- Modify: `backend/app/queue/executor.py`
- Modify: `backend/app/queue/results.py`
- Modify: `backend/app/services/object_service.py` (remove Task 8's placeholder TODOs where this task now supplies the real value)
- Test: `backend/tests/queue/test_results_owner.py`

The research found the exact fix: `_apply_result(self, job: Job, result: dict | None)` in `executor.py` **already has the full `Job` document**, including `job.owner`. It calls `results.apply(job.type, result)` without ever passing that owner through. This task threads it.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/queue/test_results_owner.py`:

```python
import pytest

from app.queue import results


pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


class TestResultsApplyOwner:
    async def test_apply_requires_owner(self):
        # apply() must accept owner as a required keyword -- calling it
        # without one is a signature error, not a silent default.
        import inspect

        sig = inspect.signature(results.apply)
        assert "owner" in sig.parameters
        assert sig.parameters["owner"].default is inspect.Parameter.empty
```

This is a signature-shape test rather than a full end-to-end ingest test, deliberately: a true end-to-end test of e.g. `_apply_trim_reads` would require a real trim job result dict and a real file on disk, which is expensive to construct for a pure plumbing check. The real behavioral proof is Task 4's `test_ingest_local_file_stamps_the_given_owner` (already covers `ingest_local_file` itself) plus this task's job: prove the value reaching `ingest_local_file` from a `results.py` handler is the *job's* owner, not a hardcoded one. Step 2 below adds that proof directly against one handler.

Add a second test against the simplest handler that calls `ingest_local_file` — check which one is simplest by reading `results.py`'s eight handlers found in the research; `_apply_trim_reads` is the first one listed. Rather than construct a full trim result dict, test the propagation at the boundary that matters: that `apply()` passes its `owner` argument down to the handler function it dispatches to. Add:

```python
    async def test_apply_passes_owner_to_the_dispatched_handler(self, monkeypatch):
        captured = {}

        async def fake_handler(result: dict, *, owner: str):
            captured["owner"] = owner

        monkeypatch.setitem(results._APPLIERS, "fake_job_type", fake_handler)

        await results.apply("fake_job_type", {"job_id": "x"}, owner="owner-from-job")

        assert captured["owner"] == "owner-from-job"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/queue/test_results_owner.py -v
```

Expected: FAIL — `apply() got an unexpected keyword argument 'owner'`.

- [ ] **Step 3: Thread `owner` through `apply` and every handler**

Edit `backend/app/queue/results.py`. Change `apply`:

```python
async def apply(job_type: str, result: dict, *, owner: str) -> None:
    handler = _APPLIERS.get(job_type)
    if handler is not None:
        await handler(result, owner=owner)
```

For each of the eight handlers found in the research (`_apply_trim_reads`, `_apply_sra_download`, `_apply_assembly_download`, `_apply_build_index`, `_apply_align_reads`, `_apply_index_bam`, `_apply_call_variants`, `_apply_annotate_variants`): add `*, owner: str` to the signature (they currently take only `result: dict`), and pass `owner=owner` to every `ingest_local_file(...)` call inside them. For `_apply_align_reads`, also pass `owner=owner` to its own `queue.enqueue("index_bam", ..., owner=owner, ...)` call — this replaces Task 8's `owner="local"` placeholder for that one site with the real value, so remove that TODO comment there.

Example for `_apply_trim_reads` (adapt to the function's actual current body — this shows the shape of the two edits every handler needs):

```python
async def _apply_trim_reads(result: dict, *, owner: str) -> None:
    # ... existing body up to the ingest_local_file call is unchanged ...
    obj = await object_service.ingest_local_file(
        project_id=project_id,
        owner=owner,
        path=path,
        name=name,
        # ... existing remaining kwargs unchanged ...
    )
    # ... rest of function unchanged ...
```

Apply the identical two-part edit (signature gains `*, owner: str`; every `ingest_local_file(...)` call gains `owner=owner`) to the other seven handlers.

- [ ] **Step 4: Thread `owner` from `executor.py`'s existing `job` into the call**

Edit `backend/app/queue/executor.py`. Change `_apply_result`:

```python
    async def _apply_result(self, job: Job, result: dict | None) -> None:
        """Persist side effects a sync handler could not perform itself."""
        if not result:
            return
        try:
            from app.queue import results

            await results.apply(job.type, result, owner=job.owner)
        except Exception as e:  # noqa: BLE001
            log.error(
                "result_apply_failed", job_id=str(job.id), type=job.type, error=str(e)
            )
```

This is the entire fix for this half of the trap: `job` was already in scope here, `job.owner` was already populated (every `Job` inherits it from `TimestampedDocument`, and Task 8 made `enqueue` set it from a real caller-supplied value rather than the `"local"` default), and the only thing missing was passing it one call further.

- [ ] **Step 5: Run test to verify it passes**

```bash
docker compose exec api python -m pytest tests/queue/test_results_owner.py -v
```

Expected: PASS (2 tests).

- [ ] **Step 6: Fix `_apply_index_bam`'s sidecar ingest and every other in-file call site, then run the full suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: failures at any remaining `ingest_local_file(...)` call inside `results.py` still missing `owner=owner` (the research found two such calls inside `_apply_call_variants` and two inside `_apply_annotate_variants` — confirm every one, not just the first, got the edit). Fix each the same way.

- [ ] **Step 7: Run the full suite to confirm nothing else broke**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: PASS, all tests green.

- [ ] **Step 8: Commit**

```bash
git add backend/app/queue/results.py backend/app/queue/executor.py backend/tests/queue/test_results_owner.py
git commit -m "feat: propagate job.owner into every object a pipeline result creates

executor._apply_result already had the full Job document and therefore
job.owner; it just never passed it the one call further into results.apply.
Every DataObject a pipeline handler creates now carries the launching
profile's owner instead of silently defaulting to \"local\" regardless of who
ran the job."
```

---

## Task 10: The profiles API

**Files:**
- Create: `backend/app/api/v1/profiles.py`
- Modify: `backend/app/api/v1/__init__.py`
- Modify: `backend/app/api/v1/schemas.py`
- Test: `backend/tests/api/test_profiles.py`

This exposes `profile_service` over HTTP: list profiles (for the startup picker), create one, select one (checks the password speed bump if set), get the current one's details, edit details, delete. This task also proves `get_current_owner` (Task 2) works end-to-end against a real route, which is this plan's scope boundary — see "What this plan does not include" for why the rest of the API surface is not rewired here.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_profiles.py`:

```python
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestProfilesApi:
    async def test_list_profiles_starts_empty(self, client):
        resp = await client.get("/api/v1/profiles")

        assert resp.status_code == 200
        assert resp.json() == []

    async def test_create_first_profile(self, client):
        resp = await client.post(
            "/api/v1/profiles", json={"username": "ada", "is_first_boot": True}
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["username"] == "ada"

    async def test_create_second_profile_rejects_is_first_boot(self, client):
        await client.post("/api/v1/profiles", json={"username": "first", "is_first_boot": True})

        resp = await client.post(
            "/api/v1/profiles", json={"username": "second", "is_first_boot": True}
        )

        assert resp.status_code == 422

    async def test_select_wrong_password_is_rejected(self, client):
        create = await client.post(
            "/api/v1/profiles",
            json={"username": "guarded", "password": "secret", "is_first_boot": True},
        )
        profile_id = create.json()["id"]

        resp = await client.post(
            f"/api/v1/profiles/{profile_id}/select", json={"password": "wrong"}
        )

        assert resp.status_code == 401

    async def test_select_correct_password_succeeds(self, client):
        create = await client.post(
            "/api/v1/profiles",
            json={"username": "guarded2", "password": "secret", "is_first_boot": True},
        )
        profile_id = create.json()["id"]

        resp = await client.post(
            f"/api/v1/profiles/{profile_id}/select", json={"password": "secret"}
        )

        assert resp.status_code == 200

    async def test_a_route_using_get_current_owner_rejects_missing_header(self, client):
        resp = await client.get("/api/v1/projects")

        assert resp.status_code == 400

    async def test_a_route_using_get_current_owner_accepts_a_real_profile(self, client):
        create = await client.post(
            "/api/v1/profiles", json={"username": "route-check", "is_first_boot": True}
        )
        profile_id = create.json()["id"]

        resp = await client.get(
            "/api/v1/projects", headers={"X-BioFlow-Profile": profile_id}
        )

        assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/api/test_profiles.py -v
```

Expected: FAIL — `404 Not Found` for `/api/v1/profiles` (route doesn't exist), and the last two tests fail too since `GET /api/v1/projects` does not yet require the header.

- [ ] **Step 3: Add profile schemas**

Add to `backend/app/api/v1/schemas.py` (follow the file's existing Pydantic-model style — read a couple of existing schemas in the file first for the exact conventions, e.g. field ordering and docstring style, before adding these):

```python
class ProfileCreate(BaseModel):
    username: str
    password: str | None = None
    email: str | None = None
    is_first_boot: bool = False


class ProfileSelect(BaseModel):
    password: str | None = None


class ProfileOut(BaseModel):
    id: str
    username: str
    email: str | None
    display: dict
    details: dict
    has_password: bool

    @classmethod
    def of(cls, profile) -> "ProfileOut":
        return cls(
            id=str(profile.id),
            username=profile.username,
            email=profile.email,
            display=profile.display.model_dump(),
            details=profile.details,
            has_password=profile.password_hash is not None,
        )
```

- [ ] **Step 4: Write the route**

Create `backend/app/api/v1/profiles.py`:

```python
"""Profile listing, creation, selection, and deletion.

See docs/superpowers/specs/2026-07-31-profiles-design.md. Selection here means
checking the optional password speed bump; it does not create a session or
token -- the client just remembers the returned profile id and sends it back
as X-BioFlow-Profile on every subsequent request (see api/deps.py).
"""

from fastapi import APIRouter, HTTPException, status

from app.api.v1.schemas import ProfileCreate, ProfileOut, ProfileSelect
from app.errors import ConflictError, ValidationError
from app.models import Profile
from app.services import profile_service

router = APIRouter(prefix="/profiles", tags=["profiles"])


@router.get("", response_model=list[ProfileOut])
async def list_profiles() -> list[ProfileOut]:
    profiles = await Profile.find({}).sort("username").to_list()
    return [ProfileOut.of(p) for p in profiles]


@router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
async def create_profile(body: ProfileCreate) -> ProfileOut:
    try:
        profile = await profile_service.create_profile(
            username=body.username,
            password=body.password,
            email=body.email,
            is_first_boot=body.is_first_boot,
        )
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.message) from e
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=e.message) from e
    return ProfileOut.of(profile)


@router.post("/{profile_id}/select", response_model=ProfileOut)
async def select_profile(profile_id: str, body: ProfileSelect) -> ProfileOut:
    from beanie import PydanticObjectId

    profile = await Profile.get(PydanticObjectId(profile_id))
    if profile is None:
        raise HTTPException(status_code=404, detail="Unknown profile")

    if profile.password_hash is not None:
        if not body.password or not profile_service.verify_password(
            body.password, profile.password_hash
        ):
            raise HTTPException(status_code=401, detail="Incorrect password")

    return ProfileOut.of(profile)


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(profile_id: str) -> None:
    from beanie import PydanticObjectId

    try:
        await profile_service.delete_profile(PydanticObjectId(profile_id))
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=e.message) from e
    except ValidationError as e:
        raise HTTPException(status_code=404, detail=e.message) from e
```

Register it in `backend/app/api/v1/__init__.py`. Read that file's existing router-inclusion list (found in the research: `search`, `projects`, `objects`, `uploads`, `jobs`, `pipelines`, `runs`, `sra`, `ncbi`, `schedules`, `system`, `events`) and add `profiles` to it — placement does not matter for path-shadowing here since `/profiles` does not collide with any existing prefix, but add it near `projects` for readability:

```python
from app.api.v1 import profiles
# ... existing imports ...
api_router.include_router(profiles.router)
```

- [ ] **Step 5: Wire `get_current_owner` into `GET /api/v1/projects` only, to prove the pattern end-to-end**

Edit `backend/app/api/v1/projects.py`. Find the route that lists projects (the one calling `project_service.list_projects`) and add the dependency:

```python
from fastapi import Depends

from app.api.deps import get_current_owner


@router.get("", response_model=list[ProjectOut])
async def list_projects_route(
    parent_id: str | None = None,
    include_archived: bool = False,
    owner: str = Depends(get_current_owner),
) -> list[ProjectOut]:
    projects = await project_service.list_projects(
        owner=owner,
        parent_id=PydanticObjectId(parent_id) if parent_id else None,
        include_archived=include_archived,
    )
    return [ProjectOut.of(p) for p in projects]
```

(Match this to the route's actual current name, parameter list, and response construction — the research did not capture this specific route's full body, only that `project_id` handling elsewhere in the file is inline path/body parsing. Read the file first, then apply the same edit shape: add `owner: str = Depends(get_current_owner)` as a parameter and pass `owner=owner` into the `project_service.list_projects` call.)

Also wire `create_project` (`POST /api/v1/projects`, shown fully in the research) the same way:

```python
@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate, owner: str = Depends(get_current_owner)
) -> ProjectOut:
    project = await project_service.create_project(
        name=body.name,
        owner=owner,
        description=body.description,
        parent_id=PydanticObjectId(body.parent_id) if body.parent_id else None,
        metadata=body.metadata,
        tags=body.tags,
    )
    return ProjectOut.of(project)
```

This plan deliberately wires **only** `projects.py`'s list and create routes — not every route in every file — as the proof that the dependency, the service layer, and the database index all agree end-to-end. Wiring the remaining ~12 route files (`objects.py`, `runs.py`, `jobs.py`, `pipelines.py`, `uploads.py`, `sra.py`, `ncbi.py`, `search.py`, and the rest of `projects.py`'s own routes: get/update/delete/breadcrumbs) is real, necessary work of the same shape, but is mechanical repetition of this exact pattern across ~14 files and is better done as its own follow-up pass (or the frontend plan's Task list, since the frontend cannot be usable until every route is wired) — see "What this plan does not include".

- [ ] **Step 6: Run test to verify it passes**

```bash
docker compose exec api python -m pytest tests/api/test_profiles.py -v
```

Expected: PASS (7 tests).

- [ ] **Step 7: Run the full suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: PASS, all tests green. If any other test hits `GET /api/v1/projects` or `POST /api/v1/projects` directly (via `httpx`/`TestClient` rather than calling `project_service` functions directly), it will now need an `X-BioFlow-Profile` header — fix those call sites by creating a profile first and passing its id, following the pattern in this task's own test file.

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/v1/profiles.py backend/app/api/v1/__init__.py backend/app/api/v1/schemas.py backend/app/api/v1/projects.py backend/tests/api/test_profiles.py
git commit -m "feat: add the profiles API and wire get_current_owner into project list/create"
```

---

## Final verification

- [ ] Run the complete suite one more time from a clean state:

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: PASS, all tests green, no skips introduced.

- [ ] Confirm no stray `owner="local"` literal was left where a real value was available. Search for the TODO markers Task 8 introduced and confirm each still has a reason to exist (i.e. its route genuinely isn't wired yet) rather than having been silently forgotten:

```bash
docker compose exec api grep -rn "TODO(profiles)" app/
```

Expected: matches only at the call sites this plan explicitly deferred (pipeline_service.py's ~9 sites, assembly_service.py, sra_service.py, upload_service.py, api/v1/jobs.py's dev endpoint, queue/scheduler.py's two global-schedule sites are permanent and should have no TODO). Confirm none remain in `results.py` — Task 9 should have removed that one.

- [ ] Restart the worker so any change to files it imports is live (per `CLAUDE.md`, `worker` does not hot-reload):

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose restart worker
```

(Run this from the main repo root, not this worktree — this plan's changes are not yet merged there, so this step is a reminder for after merge, not something to run mid-development against the worktree's own code.)

---

## What this plan does not include

Deliberately out of scope, so the plan stays reviewable and each task independently testable:

- **Wiring `get_current_owner` into every route.** Only `projects.py`'s list and create routes are wired (Task 10), to prove the pattern end-to-end. The remaining ~12 route files need the identical mechanical treatment — add `owner: str = Depends(get_current_owner)`, pass `owner=owner` into the service call — and are better done as a focused follow-up once this plan's foundation (model, dependency, service scoping, dedup-key fix, results propagation) is reviewed and merged.
- **Threading real `owner` through `pipeline_service.py`'s launch functions** (trim, QC, align, call_variants, annotate, build_index, assembly/SRA download launches). These currently pass `owner="local"` literally (Task 8) because their callers — the unwired API routes above — don't yet have a real owner to give them. This becomes real once those routes are wired.
- **The frontend.** Its own plan (`docs/superpowers/plans/2026-07-31-profiles-frontend.md`, not yet written), covering the startup picker, add-profile modal, header profile menu, `X-BioFlow-Profile` header injection in `api/client.ts`, and the upload-dedup message.
- **Sharing between profiles.** Named in the spec as its own future feature; this plan's global-blob design is what keeps it cheap later, but no share mechanism is built here.
