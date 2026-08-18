# Export Archive Owner Scoping

Scoping `GET /exports` and `GET /exports/{name}/download` to the requesting
profile by embedding the owner in the archive filename.

- **Issue:** #545
- **Source:** I5 in the final whole-branch review of #476 (per-project export archives)
- **Author:** John (syntheticgio)
- **Date:** 2026-08-18
- **Status:** Design approved

## Problem

`backend/app/api/v1/exports.py`: both `GET /exports` and
`GET /exports/{name}/download` accept `owner: OwnerDep` and never use it.
Export archive filenames (`{slug}-{stamp}.tar.gz`) carry no owner-derived
component, so every profile can currently list and download every other
profile's export archives — inconsistent with how the rest of the codebase
(including this feature's own create endpoint) partitions by owner.

## Approach

Embed the owner in the archive filename and filter list/download by owner
prefix. This is Approach 1 from the brainstorming session: no sidecar records,
no new collections, minimal code surface.

## Filename format

Change from `{slug}-{stamp}.tar.gz` to `{owner}__{slug}-{stamp}.tar.gz`.

- `owner` is always either `"local"` (5 chars) or a 24-char hex ObjectId —
  both are filesystem-safe.
- Double underscore `__` as delimiter: unambiguous since slugs use single
  underscores and hyphens, and ObjectIds are pure hex.
- Example: `abc123def456abc789def012__my-project-20240818T120000Z.tar.gz`

## Changes

### 1. `backend/app/services/export_service.py` — line 417

Change the archive filename to include the owner:

```python
# Before:
dest = settings.exports_dir / f"{bundle.root.slug}-{stamp}.tar.gz"
# After:
dest = settings.exports_dir / f"{owner}__{bundle.root.slug}-{stamp}.tar.gz"
```

The `owner` parameter is already threaded through `export_project()` — it is
passed from the create endpoint via `launch_project_export` and used in
`collect()` for the project lookup. It just needs to be forwarded to the
filename construction.

### 2. `backend/app/api/v1/exports.py`

#### `_SAFE_NAME` regex

Expand the max length to accommodate the owner prefix:

```python
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,200}\.tar\.gz$")
```

Worst-case filename length: 24 (owner) + 2 (delimiter) + 112 (slug max) + 1
(hyphen) + 15 (stamp) = 154 chars. 200 leaves comfortable headroom.

#### `list_exports` — filter by owner

```python
prefix = f"{owner}__"
return sorted(
    (
        {"name": p.name, "size_bytes": p.stat().st_size, "created_at": p.stat().st_mtime}
        for p in settings.exports_dir.glob("*.tar.gz")
        if p.name.startswith(prefix)
    ),
    key=lambda e: e["created_at"],
    reverse=True,
)
```

#### `download_export` — validate owner prefix

After the regex name validation, before the file existence check:

```python
if not name.startswith(f"{owner}__"):
    raise HTTPException(status_code=404, detail="Export not found")
```

#### Remove KNOWN GAP comments

Both `# KNOWN GAP` comments (lines 35–45 and lines 64–66) are removed once
the above changes are in place.

### 3. Tests (`backend/tests/api/test_exports_api.py`)

- Update `test_list_exports_returns_a_list` to create test archive files with
  the new naming convention for the requesting profile
- Add `test_list_exports_is_owner_scoped` — profile B cannot see profile A's
  archives
- Add `test_download_export_404s_for_another_owners_archive` — profile B
  cannot download profile A's archive
- Add `test_old_format_archives_are_not_visible` (optional) — archives without
  owner prefix are not shown in listings

## Backwards compatibility

Existing archives without the owner prefix won't appear in listings for any
profile, and cannot be downloaded by name (the prefix check rejects them).
Since this is a single-user tool and the bug was always present, existing
archives are either nonexistent or trivially re-creatable. No migration needed.

## Self-review

- **Placeholders:** None. All sections are complete.
- **Consistency:** The approach matches the architecture — owner is already a
  first-class concept threaded through every partitioned query.
- **Scope:** Single, focused fix. Does not decompose further.
- **Ambiguity:** None. The owner prefix rule is unambiguous: `name.startswith(f"{owner}__")`.
