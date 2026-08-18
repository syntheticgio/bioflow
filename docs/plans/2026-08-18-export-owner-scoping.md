# Export Owner Scoping Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Scope `GET /exports` and `GET /exports/{name}/download` to the requesting profile by embedding the owner in the archive filename.

**Architecture:** Archive filenames change from `{slug}-{stamp}.tar.gz` to `{owner}__{slug}-{stamp}.tar.gz`. The list endpoint filters by owner prefix; the download endpoint validates the prefix before serving the file. The create endpoint already has the `owner` parameter threaded through to `export_project()` — it just needs to be used in the filename construction.

**Tech Stack:** Python/FastAPI, glob, re

**Spec:** `docs/superpowers/specs/2026-08-18-export-owner-scoping-design.md`

---

### Task 1: Add owner-prefix helper and update `_SAFE_NAME` regex in `exports.py`

**Objective:** Add a helper to build the owner prefix and expand the filename regex to accommodate longer names.

**Files:**
- Modify: `backend/app/api/v1/exports.py:18` (regex), add helper after line 18

**Step 1: Edit the file**

Replace the `_SAFE_NAME` regex and add an owner-prefix helper:

```python
# An export filename is "<owner>__<slug>-<timestamp>.tar.gz". The owner prefix
# is "local" or a 24-char hex ObjectId; the double-underscore delimiter is
# unambiguous since slugs use single underscores and ObjectIds are pure hex.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,200}\.tar\.gz$")


def _owner_prefix(owner: str) -> str:
    """Return the filename prefix for a given owner."""
    return f"{owner}__"
```

**Step 2: Verify the regex**

Run a quick Python check:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner/.worktrees/545-issue
python3 -c "
import re
r = re.compile(r'^[A-Za-z0-9._-]{1,200}\.tar\.gz$')
assert r.match('local__my-project-20240818T120000Z.tar.gz')
assert r.match('abc123def456abc789def012__my-project-20240818T120000Z.tar.gz')
assert not r.match('../secret.tar.gz')
print('regex OK')
"
```

Expected: `regex OK`

**Step 3: Commit**

```bash
git add backend/app/api/v1/exports.py
git commit -m "feat(export): add owner-prefix helper and expand filename regex"
```

---

### Task 2: Filter `list_exports` by owner prefix

**Objective:** Make `GET /exports` only return archives whose filename starts with the requesting profile's owner prefix.

**Files:**
- Modify: `backend/app/api/v1/exports.py:33-59`

**Step 1: Edit the function**

Replace the list comprehension with an owner-filtered version:

```python
@router.get("/exports")
async def list_exports(owner: OwnerDep) -> list[dict]:
    if not settings.exports_dir.exists():
        return []
    prefix = _owner_prefix(owner)
    return sorted(
        (
            {
                "name": p.name,
                "size_bytes": p.stat().st_size,
                "created_at": p.stat().st_mtime,
            }
            for p in settings.exports_dir.glob("*.tar.gz")
            if p.name.startswith(prefix)
        ),
        key=lambda e: e["created_at"],
        reverse=True,
    )
```

**Step 2: Verify with a quick check**

```bash
python3 -c "
# Simulate the logic
owner = 'abc123'
prefix = f'{owner}__'
files = ['abc123__proj-1.tar.gz', 'def456__proj-2.tar.gz', 'proj-3.tar.gz']
filtered = [f for f in files if f.startswith(prefix)]
assert filtered == ['abc123__proj-1.tar.gz'], f'got {filtered}'
print('filter logic OK')
"
```

Expected: `filter logic OK`

**Step 3: Commit**

```bash
git add backend/app/api/v1/exports.py
git commit -m "feat(export): filter list_exports by owner prefix"
```

---

### Task 3: Validate owner prefix in `download_export`

**Objective:** Make `GET /exports/{name}/download` reject requests for archives that don't belong to the requesting profile.

**Files:**
- Modify: `backend/app/api/v1/exports.py:62-72`

**Step 1: Edit the function**

Add owner prefix validation after the regex check, before the file existence check:

```python
@router.get("/exports/{name}/download")
async def download_export(name: str, owner: OwnerDep) -> FileResponse:
    if not _SAFE_NAME.match(name):
        raise HTTPException(status_code=400, detail="Invalid export name")
    if not name.startswith(_owner_prefix(owner)):
        raise HTTPException(status_code=404, detail="Export not found")
    path = settings.exports_dir / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Export not found")
    return FileResponse(path, media_type="application/gzip", filename=name)
```

**Step 2: Commit**

```bash
git add backend/app/api/v1/exports.py
git commit -m "feat(export): validate owner prefix in download_export"
```

---

### Task 4: Embed owner in archive filename in `export_service.py`

**Objective:** Change the archive filename from `{slug}-{stamp}.tar.gz` to `{owner}__{slug}-{stamp}.tar.gz`.

**Files:**
- Modify: `backend/app/api/v1/exports.py` (pass owner to `launch_project_export`)
- Modify: `backend/app/services/pipeline_service.py` (thread owner through to `export_project`)
- Modify: `backend/app/services/export_service.py:416-417` (use owner in filename)

**Step 1: Check the call chain**

The create endpoint already passes owner to `launch_project_export`. Let's verify:

```bash
grep -n 'launch_project_export' /Users/syntheticgio/Programming/local-bio-pipeliner/.worktrees/545-issue/backend/app/services/pipeline_service.py | head -5
```

**Step 2: Update `export_service.py` line 417**

Change the filename construction:

```python
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = settings.exports_dir / f"{owner}__{bundle.root.slug}-{stamp}.tar.gz"
```

**Step 3: Verify the change**

```bash
python3 -c "
# Simulate the new filename
owner = 'local'
slug = 'my-project'
stamp = '20240818T120000Z'
filename = f'{owner}__{slug}-{stamp}.tar.gz'
assert filename == 'local__my-project-20240818T120000Z.tar.gz'
print(filename)
"
```

Expected: `local__my-project-20240818T120000Z.tar.gz`

**Step 4: Commit**

```bash
git add backend/app/services/export_service.py
git commit -m "feat(export): embed owner in archive filename"
```

---

### Task 5: Remove KNOWN GAP comments from `exports.py`

**Objective:** Remove the two `# KNOWN GAP` comment blocks that are no longer relevant.

**Files:**
- Modify: `backend/app/api/v1/exports.py:35-45` (first KNOWN GAP)
- Modify: `backend/app/api/v1/exports.py:64-66` (second KNOWN GAP)

**Step 1: Remove first KNOWN GAP block**

Remove lines 35-45 (the long comment block in `list_exports`).

**Step 2: Remove second KNOWN GAP block**

Remove lines 64-66 (the short comment in `download_export`).

**Step 3: Commit**

```bash
git add backend/app/api/v1/exports.py
git commit -m "fix(export): remove KNOWN GAP comments (owner scoping fixed)"
```

---

### Task 6: Update existing test for list_exports

**Objective:** Update `test_list_exports_returns_a_list` to create test archive files with the new naming convention.

**Files:**
- Modify: `backend/tests/api/test_exports_api.py:70-74`

**Step 1: Read the current test to understand patterns**

```python
async def test_list_exports_returns_a_list(client, two_profiles):
    resp = await client.get("/api/v1/exports", headers=two_profiles["a_headers"])
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
```

This test passes because the exports directory doesn't exist (or is empty). We need to create a test file with the proper naming convention and verify it appears.

**Step 2: Update the test**

```python
async def test_list_exports_returns_a_list(client, two_profiles, tmp_path, monkeypatch):
    from app.config import settings

    owner = two_profiles["a"].owner_id()
    prefix = f"{owner}__"
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir()
    monkeypatch.setattr(settings, "exports_dir", exports_dir)

    # Create a test archive for profile A
    (exports_dir / f"{prefix}my-project-20240818T120000Z.tar.gz").write_text("fake archive")
    # Create an archive for another profile
    other_owner = two_profiles["b"].owner_id()
    (exports_dir / f"{other_owner}__other-project-20240818T120000Z.tar.gz").write_text("fake archive")

    resp = await client.get("/api/v1/exports", headers=two_profiles["a_headers"])
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == f"{prefix}my-project-20240818T120000Z.tar.gz"
```

**Step 3: Commit**

```bash
git add backend/tests/api/test_exports_api.py
git commit -m "test(export): update list_exports test with owner-scoped archives"
```

---

### Task 7: Add test that list_exports is owner-scoped

**Objective:** Verify that profile B cannot see profile A's archives.

**Files:**
- Modify: `backend/tests/api/test_exports_api.py`

**Step 1: Add the test**

```python
async def test_list_exports_is_owner_scoped(client, two_profiles, tmp_path, monkeypatch):
    from app.config import settings

    exports_dir = tmp_path / "exports2"
    exports_dir.mkdir()
    monkeypatch.setattr(settings, "exports_dir", exports_dir)

    # Create archives for both profiles
    a_owner = two_profiles["a"].owner_id()
    b_owner = two_profiles["b"].owner_id()
    (exports_dir / f"{a_owner}__a-project.tar.gz").write_text("a data")
    (exports_dir / f"{b_owner}__b-project.tar.gz").write_text("b data")

    # Profile B should only see their own
    resp = await client.get("/api/v1/exports", headers=two_profiles["b_headers"])
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == f"{b_owner}__b-project.tar.gz"
```

**Step 2: Commit**

```bash
git add backend/tests/api/test_exports_api.py
git commit -m "test(export): add list_exports owner scoping test"
```

---

### Task 8: Add test that download_export 404s for another owner's archive

**Objective:** Verify that profile B cannot download profile A's archive.

**Files:**
- Modify: `backend/tests/api/test_exports_api.py`

**Step 1: Add the test**

```python
async def test_download_export_404s_for_another_owners_archive(client, two_profiles, tmp_path, monkeypatch):
    from app.config import settings

    exports_dir = tmp_path / "exports3"
    exports_dir.mkdir()
    monkeypatch.setattr(settings, "exports_dir", exports_dir)

    a_owner = two_profiles["a"].owner_id()
    b_owner = two_profiles["b"].owner_id()
    a_file = exports_dir / f"{a_owner}__a-project.tar.gz"
    a_file.write_text("a data")

    # Profile B tries to download profile A's archive
    resp = await client.get(
        f"/api/v1/exports/{a_file.name}/download",
        headers=two_profiles["b_headers"],
    )
    assert resp.status_code == 404
```

**Step 2: Commit**

```bash
git add backend/tests/api/test_exports_api.py
git commit -m "test(export): add download_export owner scoping test"
```

---

### Task 9: Run the full test suite

**Objective:** Verify everything passes.

**Step 1: Run tests via worktree test runner**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner/.worktrees/545-issue
./backend/run-worktree-tests.sh tests/api/test_exports_api.py -q
```

Expected: All tests pass (look for a count of passed tests, e.g., "8 passed")

**Step 2: Also check no regressions in export service tests**

```bash
./backend/run-worktree-tests.sh tests/services/test_export_service.py -q
```

Expected: All tests pass

---

### Verification

- [ ] `GET /exports` returns only the requesting profile's archives
- [ ] `GET /exports/{name}/download` 404s for another profile's archive
- [ ] `POST /projects/{id}/export` creates archives with the new naming convention
- [ ] Both `KNOWN GAP` comments in `exports.py` are removed
- [ ] Old-format archives are excluded from listings (by design)
