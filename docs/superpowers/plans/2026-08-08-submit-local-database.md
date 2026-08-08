# Submit Local Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user submit a URL + name + category for a database they care about, persisted and listed on the `/help/databases` page, and replace the ugly dice emoji on that same page with plain text.

**Architecture:** A new Beanie document + FastAPI router (`local_databases`), modeled directly on the existing `feedback` model/router pair — global (not owner-scoped), append-only (create + list, no edit/delete). The frontend adds a "Local Databases" section to the existing `HelpDatabases.tsx` page, above the static catalog, backed by a new `AddLocalDatabaseModal` built on the `AddProfileModal` pattern and fetched via `@tanstack/react-query`.

**Tech Stack:** FastAPI, Beanie (MongoDB ODM), Pydantic; React, TypeScript, `@tanstack/react-query`, Vite.

**Spec:** [docs/superpowers/specs/2026-08-08-submit-local-database-design.md](../specs/2026-08-08-submit-local-database-design.md)

---

### Task 1: Backend model — `LocalDatabase`

**Files:**
- Create: `backend/app/models/local_database.py`
- Modify: `backend/app/models/__init__.py`

- [ ] **Step 1: Write the model file**

```python
"""A user-submitted database, tracked locally.

Write-and-list only, like `feedback.py` -- no edit or delete route exists
yet, since a mistaken entry is rare enough that removing it by hand in Mongo
is an acceptable cost for now. Not owner-scoped: this app is single-user, and
a per-profile split would just be a filter nobody needs.
"""

from enum import StrEnum

from pydantic import Field
from pymongo import DESCENDING, IndexModel

from app.models.base import TimestampedDocument

NAME_MAX_LENGTH = 200
URL_MAX_LENGTH = 2000


class LocalDatabaseCategory(StrEnum):
    """What kind of thing a submitted database is.

    Deliberately a small, purpose-built set -- not the 26-value free-text
    `c` field in data/databases.json (sized for a 1000+ entry reference
    catalog) and not sources.py's SOURCE_KINDS (a different classification
    for a different concept). The `label` is what the submit form and the
    list show; it lives here so adding a category is a one-place change.
    """

    REFERENCE_ASSEMBLY = "reference_assembly"
    ANNOTATION = "annotation"
    VARIANT_CLINICAL = "variant_clinical"
    TAXONOMY_METADATA = "taxonomy_metadata"
    PIPELINE_TOOL_DATA = "pipeline_tool_data"
    OTHER = "other"

    @property
    def label(self) -> str:
        return _CATEGORY_LABELS[self]


_CATEGORY_LABELS = {
    LocalDatabaseCategory.REFERENCE_ASSEMBLY: "Reference / Assembly",
    LocalDatabaseCategory.ANNOTATION: "Annotation",
    LocalDatabaseCategory.VARIANT_CLINICAL: "Variant / Clinical",
    LocalDatabaseCategory.TAXONOMY_METADATA: "Taxonomy / Metadata",
    LocalDatabaseCategory.PIPELINE_TOOL_DATA: "Pipeline / Tool Data",
    LocalDatabaseCategory.OTHER: "Other",
}


class LocalDatabase(TimestampedDocument):
    name: str = Field(max_length=NAME_MAX_LENGTH)
    url: str = Field(max_length=URL_MAX_LENGTH)
    category: LocalDatabaseCategory

    class Settings:
        name = "local_databases"
        indexes = [IndexModel([("created_at", DESCENDING)], name="created_at_desc")]
```

- [ ] **Step 2: Register the model so Beanie creates its indexes**

In `backend/app/models/__init__.py`:

Add the import, alphabetically next to the other model imports:

```python
from app.models.job import (
```
becomes preceded by:
```python
from app.models.local_database import LocalDatabase, LocalDatabaseCategory
```
(insert this line alphabetically — after the `from app.models.job import (...)` block and before `from app.models.object import (...)`, matching the existing alphabetical-by-module ordering).

Add `LocalDatabase` to `ALL_MODELS`:

```python
ALL_MODELS = [
    AiProvider,
    AiRouting,
    Project,
    Blob,
    DataObject,
    Job,
    UploadSession,
    Schedule,
    JobRunTiming,
    PipelineRun,
    RunJob,
    OrganismBlurb,
    FailureExplanation,
    Profile,
    StructureLookup,
    Feedback,
    LocalDatabase,
    Share,
    ProjectConversation,
    ResourceLimits,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowNodeRun,
]
```

Add `"LocalDatabase"` and `"LocalDatabaseCategory"` to `__all__`, alphabetically:

```python
    "JobTiming",
    "LocalDatabase",
    "LocalDatabaseCategory",
    "OPTIONAL_ROLES",
```

- [ ] **Step 3: Verify the module imports cleanly**

Run: `docker compose exec api python -c "from app.models import LocalDatabase, LocalDatabaseCategory; print(LocalDatabaseCategory.OTHER.label)"`
Expected: prints `Other` with no traceback.

(If the stack isn't already running against this worktree, use `./ops/worktree-up.sh` first per this repo's CLAUDE.md, and substitute its API container name/port. Assume the worktree stack is up for the remainder of this plan's verification steps.)

- [ ] **Step 4: Commit**

```bash
git add backend/app/models/local_database.py backend/app/models/__init__.py
git commit -m "feat(models): add LocalDatabase document"
```

---

### Task 2: Backend router — create + list endpoints

**Files:**
- Create: `backend/app/api/v1/local_databases.py`
- Modify: `backend/app/api/v1/__init__.py`
- Test: `backend/tests/api/test_local_databases.py`

- [ ] **Step 1: Write the failing tests**

```python
"""The local-databases HTTP surface: submit and list."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.local_database import URL_MAX_LENGTH, LocalDatabase

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestSubmitLocalDatabase:
    async def test_persists_a_valid_submission(self, client):
        await LocalDatabase.find_all().delete()
        r = await client.post(
            "/api/v1/local-databases",
            json={
                "name": "Lab reference genome",
                "url": "https://example.org/genome.fasta",
                "category": "reference_assembly",
            },
        )

        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "Lab reference genome"
        assert body["url"] == "https://example.org/genome.fasta"
        assert body["category"] == "reference_assembly"
        assert "id" in body
        assert "created_at" in body
        assert await LocalDatabase.find_all().count() == 1

    async def test_rejects_a_url_over_the_limit(self, client):
        r = await client.post(
            "/api/v1/local-databases",
            json={
                "name": "Too long",
                "url": "https://example.org/" + ("x" * URL_MAX_LENGTH),
                "category": "other",
            },
        )

        assert r.status_code == 422

    async def test_rejects_an_empty_name(self, client):
        r = await client.post(
            "/api/v1/local-databases",
            json={"name": "", "url": "https://example.org", "category": "other"},
        )

        assert r.status_code == 422

    async def test_rejects_an_invalid_category(self, client):
        r = await client.post(
            "/api/v1/local-databases",
            json={"name": "X", "url": "https://example.org", "category": "not_a_real_category"},
        )

        assert r.status_code == 422

    async def test_rejects_a_malformed_url(self, client):
        r = await client.post(
            "/api/v1/local-databases",
            json={"name": "X", "url": "not-a-url", "category": "other"},
        )

        assert r.status_code == 422


class TestListLocalDatabases:
    async def test_lists_submissions_newest_first(self, client):
        await LocalDatabase.find_all().delete()
        await LocalDatabase(
            name="first", url="https://example.org/a", category="other"
        ).insert()
        # Distinct created_at: Mongo sorts newest-first, and identical
        # timestamps would make the sort order non-deterministic.
        await asyncio.sleep(0.01)
        await LocalDatabase(
            name="second", url="https://example.org/b", category="annotation"
        ).insert()

        r = await client.get("/api/v1/local-databases")

        assert r.status_code == 200
        names = [item["name"] for item in r.json()]
        assert names == ["second", "first"]

    async def test_returns_empty_list_when_none_submitted(self, client):
        await LocalDatabase.find_all().delete()

        r = await client.get("/api/v1/local-databases")

        assert r.status_code == 200
        assert r.json() == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker compose exec api python -m pytest tests/api/test_local_databases.py -v`
Expected: FAIL — `ModuleNotFoundError` or 404s, since `app/api/v1/local_databases.py` and its route registration don't exist yet.

- [ ] **Step 3: Write the router**

```python
"""Local-database endpoints: submit and list.

Not owner-scoped, like feedback.py -- a single-user install has no reason to
partition submissions per profile. Append-only: no PATCH or DELETE route
exists. A URL is validated for well-formedness (scheme + host) at the input
model, not fetched -- reachability checking is explicitly out of scope, so a
submission with a typo'd or since-dead URL is still accepted.
"""

from fastapi import APIRouter
from pydantic import AnyUrl, BaseModel, Field

from app.models.local_database import (
    NAME_MAX_LENGTH,
    URL_MAX_LENGTH,
    LocalDatabase,
    LocalDatabaseCategory,
)

router = APIRouter(prefix="/local-databases", tags=["local-databases"])


class LocalDatabaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    url: AnyUrl = Field(max_length=URL_MAX_LENGTH)
    category: LocalDatabaseCategory


class LocalDatabaseOut(BaseModel):
    id: str
    name: str
    url: str
    category: LocalDatabaseCategory
    created_at: str

    @classmethod
    def of(cls, d: LocalDatabase) -> "LocalDatabaseOut":
        return cls(
            id=str(d.id),
            name=d.name,
            url=d.url,
            category=d.category,
            created_at=d.created_at.isoformat(),
        )


@router.post("", status_code=201)
async def submit_local_database(body: LocalDatabaseCreate) -> LocalDatabaseOut:
    db = LocalDatabase(name=body.name, url=str(body.url), category=body.category)
    await db.insert()
    return LocalDatabaseOut.of(db)


@router.get("")
async def list_local_databases() -> list[LocalDatabaseOut]:
    items = await LocalDatabase.find_all().sort("-created_at").to_list()
    return [LocalDatabaseOut.of(d) for d in items]
```

- [ ] **Step 4: Register the router**

In `backend/app/api/v1/__init__.py`, add the import alphabetically:

```python
from app.api.v1 import (
    events,
    feedback,
    jobs,
    local_databases,
    ncbi,
```

And add the include, next to `feedback.router` since they're the closest analog:

```python
api_router.include_router(feedback.router)
api_router.include_router(local_databases.router)
api_router.include_router(settings.router)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/api/test_local_databases.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/local_databases.py backend/app/api/v1/__init__.py backend/tests/api/test_local_databases.py
git commit -m "feat(api): add local-databases submit and list endpoints"
```

---

### Task 3: Frontend types and API client methods

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Add the types**

In `frontend/src/api/types.ts`, add near `FeedbackSubmission`/`Feedback` (these mirror them closely):

```typescript
export type LocalDatabaseCategory =
  | "reference_assembly"
  | "annotation"
  | "variant_clinical"
  | "taxonomy_metadata"
  | "pipeline_tool_data"
  | "other";

export const LOCAL_DATABASE_CATEGORY_LABELS: Record<LocalDatabaseCategory, string> = {
  reference_assembly: "Reference / Assembly",
  annotation: "Annotation",
  variant_clinical: "Variant / Clinical",
  taxonomy_metadata: "Taxonomy / Metadata",
  pipeline_tool_data: "Pipeline / Tool Data",
  other: "Other",
};

export interface LocalDatabaseSubmission {
  name: string;
  url: string;
  category: LocalDatabaseCategory;
}

export interface LocalDatabaseEntry extends LocalDatabaseSubmission {
  id: string;
  created_at: string;
}
```

- [ ] **Step 2: Add the client methods**

In `frontend/src/api/client.ts`, add next to `submitFeedback`/`listFeedback`:

```typescript
  submitLocalDatabase: (body: LocalDatabaseSubmission) =>
    request<LocalDatabaseEntry>("/local-databases", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listLocalDatabases: () => request<LocalDatabaseEntry[]>("/local-databases"),
```

Add `LocalDatabaseSubmission` and `LocalDatabaseEntry` to the existing type-only import at the top of `client.ts` (find the import line that already brings in `Feedback`/`FeedbackSubmission` and add the two new names to it).

- [ ] **Step 3: Verify the frontend still type-checks**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no new errors (existing errors, if any predate this change, are unrelated — only check that nothing new appears referencing `client.ts` or `types.ts`).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat(api-client): add local-databases types and client methods"
```

---

### Task 4: `AddLocalDatabaseModal` component

**Files:**
- Create: `frontend/src/components/AddLocalDatabaseModal.tsx`

- [ ] **Step 1: Write the component**

```tsx
import { useState } from "react";
import { api, ApiRequestError } from "../api/client";
import { ModalBackdrop } from "./ModalBackdrop";
import {
  LOCAL_DATABASE_CATEGORY_LABELS,
  type LocalDatabaseCategory,
  type LocalDatabaseEntry,
} from "../api/types";

interface Props {
  onCreated: (entry: LocalDatabaseEntry) => void;
  onClose: () => void;
}

const CATEGORY_OPTIONS = Object.entries(LOCAL_DATABASE_CATEGORY_LABELS) as [
  LocalDatabaseCategory,
  string,
][];

/**
 * The form that submits a local database, following AddProfileModal's shape:
 * local state per field, inline validation, busy-state submit, errors shown
 * in an inline error-box rather than a toast (this modal can be opened from
 * a page with no toast host mounted, same reasoning as AddProfileModal).
 */
export function AddLocalDatabaseModal({ onCreated, onClose }: Props) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [category, setCategory] = useState<LocalDatabaseCategory>(CATEGORY_OPTIONS[0][0]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !url.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const entry = await api.submitLocalDatabase({
        name: name.trim(),
        url: url.trim(),
        category,
      });
      onCreated(entry);
    } catch (err) {
      setError(
        err instanceof ApiRequestError ? err.message : "Could not submit the database",
      );
      setBusy(false);
    }
  };

  return (
    <ModalBackdrop onClick={onClose} onKeyDown={(e) => e.key === "Escape" && onClose()}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>Submit a database</h2>

        <form onSubmit={submit}>
          <div className="modal-body">
            <label htmlFor="ldb-name">Name</label>
            <input
              id="ldb-name"
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Lab reference genome"
            />

            <label htmlFor="ldb-url">URL</label>
            <input
              id="ldb-url"
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://…"
            />

            <label htmlFor="ldb-category">Category</label>
            <select
              id="ldb-category"
              value={category}
              onChange={(e) => setCategory(e.target.value as LocalDatabaseCategory)}
            >
              {CATEGORY_OPTIONS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>

            {error && <div className="error-box">{error}</div>}
          </div>

          <div className="modal-actions">
            <button type="button" className="btn" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn primary" disabled={!name.trim() || !url.trim() || busy}>
              {busy ? "Adding…" : "Add database"}
            </button>
          </div>
        </form>
      </div>
    </ModalBackdrop>
  );
}
```

- [ ] **Step 2: Verify it type-checks**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no new errors referencing `AddLocalDatabaseModal.tsx`.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/AddLocalDatabaseModal.tsx
git commit -m "feat(frontend): add AddLocalDatabaseModal component"
```

---

### Task 5: Wire the Local Databases section into `HelpDatabases.tsx`, fix the dice icon

**Files:**
- Modify: `frontend/src/components/HelpDatabases.tsx`

- [ ] **Step 1: Add imports and state**

At the top of `frontend/src/components/HelpDatabases.tsx`, change:

```tsx
import { useState, useMemo, useEffect } from "react";
import { DATABASES } from "../data/databases";
```

to:

```tsx
import { useState, useMemo, useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { DATABASES } from "../data/databases";
import { api } from "../api/client";
import { LOCAL_DATABASE_CATEGORY_LABELS } from "../api/types";
import { AddLocalDatabaseModal } from "./AddLocalDatabaseModal";
```

Inside `export function HelpDatabases() {`, add alongside the existing `useState` calls:

```tsx
  const [showAddLocalDatabase, setShowAddLocalDatabase] = useState(false);
  const queryClient = useQueryClient();
  const localDatabasesQuery = useQuery({
    queryKey: ["local-databases"],
    queryFn: api.listLocalDatabases,
  });
  const localDatabases = localDatabasesQuery.data ?? [];
```

- [ ] **Step 2: Add the Local Databases section to the JSX**

In the returned JSX, immediately after the opening `<h1>Database Index</h1>` and its intro `<p className="db-intro">…</p>` (i.e. right before the existing `<p className="db-stats">` line), insert a new section:

```tsx
      <section className="db-local-section">
        <div className="db-local-header">
          <h2>Local Databases</h2>
          <button type="button" className="db-btn" onClick={() => setShowAddLocalDatabase(true)}>
            Submit a database
          </button>
        </div>

        {localDatabasesQuery.isLoading && <p className="db-empty">Loading…</p>}
        {localDatabasesQuery.isError && (
          <p className="db-empty">Could not reach the server to list local databases.</p>
        )}
        {!localDatabasesQuery.isLoading && !localDatabasesQuery.isError && localDatabases.length === 0 && (
          <p className="db-empty">No local databases yet — submit one above.</p>
        )}

        {localDatabases.length > 0 && (
          <div className="db-cards">
            {localDatabases.map((d) => (
              <article key={d.id} className="db-card">
                <div className="db-card-top">
                  <h3 className="db-card-name">
                    <a href={d.url} target="_blank" rel="noopener noreferrer">
                      {d.name} ↗
                    </a>
                  </h3>
                  <span className="db-card-cat">{LOCAL_DATABASE_CATEGORY_LABELS[d.category]}</span>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      {showAddLocalDatabase && (
        <AddLocalDatabaseModal
          onCreated={() => {
            setShowAddLocalDatabase(false);
            queryClient.invalidateQueries({ queryKey: ["local-databases"] });
          }}
          onClose={() => setShowAddLocalDatabase(false)}
        />
      )}
```

- [ ] **Step 3: Fix the dice icon**

Find this block (currently around line 160-173):

```tsx
        <button
          type="button"
          className="db-btn"
          title="Show a random database"
          onClick={() => {
            const d = DATABASES[Math.floor(Math.random() * DATABASES.length)];
            // scroll the user to a random card as the "surprise"
            const el = document.getElementById("db-" + encodeURIComponent(d.n));
            el?.scrollIntoView({ behavior: "smooth", block: "center" });
            el?.dispatchEvent(new MouseEvent("mouseenter", { bubbles: true }));
          }}
        >
          🎲 Surprise me
        </button>
```

Change only the button label (last line before the closing `</button>`) from:

```tsx
          🎲 Surprise me
```

to:

```tsx
          Surprise me
```

Everything else in that block (the `onClick` handler, `title`, `className`) stays unchanged — behavior is identical, only the emoji is dropped.

- [ ] **Step 4: Add minimal CSS for the new section header**

In `frontend/src/styles.css`, near the existing `.db-tools` rule (around line 2708), add:

```css
.db-local-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 0.75rem;
}

.db-local-section {
  margin-bottom: 2rem;
}
```

(`.db-card`, `.db-btn`, `.db-empty`, `.db-cards`, `.db-card-top`, `.db-card-name`, `.db-card-cat` are all reused as-is from the existing static-catalog styles — no new rules needed for those.)

- [ ] **Step 5: Verify it type-checks**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no new errors referencing `HelpDatabases.tsx`.

- [ ] **Step 6: Manual browser verification**

Run: `docker compose up -d --build api web worker` (or, from this worktree, `./ops/worktree-up.sh` per this repo's CLAUDE.md — use whichever the worktree is already running).

Open `localhost:5173/help/databases` (or `localhost:5273` if using the worktree stack). Verify:
- A "Local Databases" section appears above the "Database Index" intro/search tools, with a "Submit a database" button and an empty-state message.
- Clicking "Submit a database" opens the modal; filling name + URL + picking a category and submitting adds the entry to the list immediately with no manual refresh.
- Reloading the page still shows the submitted entry (confirms persistence).
- The "Surprise me" button reads as plain text with no dice emoji, and clicking it still scrolls to a random card in the static catalog below.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/HelpDatabases.tsx frontend/src/styles.css
git commit -m "feat(frontend): add Local Databases section and drop dice emoji"
```

---

### Task 6: Backend test suite sanity check

**Files:** none (verification only)

- [ ] **Step 1: Run the full backend test suite**

Run: `docker compose exec api python -m pytest tests/ -q` (or, from this worktree, `./backend/run-worktree-tests.sh tests/ -q` per this repo's CLAUDE.md — use whichever the worktree is already running against).

Expected: all tests pass, including the 7 new tests in `tests/api/test_local_databases.py`. Note the pass count.

- [ ] **Step 2: No commit needed for this task** — it's a verification checkpoint only. If anything fails, fix it in the relevant earlier task's files and re-run before proceeding.

---

## Self-Review Notes

- **Spec coverage:** backend model + category enum (Task 1), create/list router (Task 2), frontend types/client (Task 3), submit modal (Task 4), page section + dice fix (Task 5), full-suite check (Task 6) — all spec sections covered. Edit/delete and URL-reachability validation are explicitly out of scope per the spec and are not implemented here.
- **Type consistency:** `LocalDatabaseCategory` values (`reference_assembly`, `annotation`, `variant_clinical`, `taxonomy_metadata`, `pipeline_tool_data`, `other`) match exactly between the backend `StrEnum` (Task 1), the TypeScript union type (Task 3), and the test payloads (Task 2). `LocalDatabaseEntry`/`LocalDatabaseSubmission` field names (`name`, `url`, `category`, `id`, `created_at`) are consistent from the backend `LocalDatabaseOut` schema through the TS types, client methods, modal, and page component.
- **Query key consistency:** `["local-databases"]` is used identically in the `useQuery` call and the `invalidateQueries` call in Task 5 — a mismatch here would silently leave the list stale after a submission.
