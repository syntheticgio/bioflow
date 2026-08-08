# Submit a local database (issue #84)

**Issue:** [#84](https://github.com/syntheticgio/bioflow/issues/84) — "Add submit database to database page"

## Problem

`/help/databases` (`frontend/src/components/HelpDatabases.tsx`) is a static,
read-only catalog of ~famous external reference databases (NCBI, SILVA, etc.),
sourced from `frontend/src/data/databases.json`. It's explicitly documented as
"not things BioFlow integrates with... just a catalog to browse and follow
links out from."

There is currently no concept of a *user-submitted, locally-tracked* database
anywhere in the app — no backend model, no persistence, no UI. Issue #84
wants a way to submit a URL + name + category for a database the user cares
about, and have it show up in a "local databases" list.

Separately, the page's "🎲 Surprise me" button uses a literal emoji glyph,
which the issue calls "ugly."

## Scope

This spec covers:
1. A new backend model + API for storing user-submitted local databases.
2. A new "Local Databases" section on the existing `/help/databases` page,
   with a submit form.
3. Replacing the emoji dice icon with plain text.

Out of scope (deliberately, per YAGNI): editing or deleting submitted
entries, URL reachability validation on submit, per-profile scoping,
multi-category selection. All of these can be added later if they turn out
to matter; none are needed for the issue as written.

## Backend

### Model — `backend/app/models/local_database.py`

A new Beanie `Document`, following the `feedback.py` convention
(`backend/app/models/feedback.py`): extends `TimestampedDocument`
(`app/models/base.py`) for `created_at`/`updated_at`, global (not
owner-scoped) — matching `feedback` and `schedules`, since this app is
single-user.

Fields:
- `name: str` — `Field(max_length=200)`
- `url: str` — `Field(max_length=2000)`, validated as a well-formed URL
  (scheme + host) at the Pydantic input-model level, not by fetching it
- `category: LocalDatabaseCategory` — new enum (see below)

`Settings` inner class: collection name `local_databases`, one `IndexModel`
sorted by `created_at` descending (list is always newest-first).

### Category enum

A new, small, purpose-built enum — **not** reused from `databases.json`'s
`c` field (free-text, 26 values, sized for a 1000+ entry reference index) or
from `sources.py`'s `SOURCE_KINDS` (a different, narrower classification for
a different concept). Single-select, one category per submission.

```python
class LocalDatabaseCategory(str, Enum):
    REFERENCE_ASSEMBLY = "reference_assembly"
    ANNOTATION = "annotation"
    VARIANT_CLINICAL = "variant_clinical"
    TAXONOMY_METADATA = "taxonomy_metadata"
    PIPELINE_TOOL_DATA = "pipeline_tool_data"
    OTHER = "other"
```

Display labels ("Reference / Assembly", "Variant / Clinical", etc.) live
alongside the enum the same way other enums with UI labels in this codebase
do (check for an existing `_LABELS`-style dict pattern and match it).

### Router — `backend/app/api/v1/local_databases.py`

Following the `feedback.py` router pattern:

- `LocalDatabaseCreate` (input): `name` (`min_length=1, max_length=200`),
  `url` (`min_length=1, max_length=2000`, URL-shape validated), `category`
  (enum, required).
- `LocalDatabaseOut` (response): `id`, `name`, `url`, `category`,
  `created_at`, with a `.of(model)` classmethod.
- `POST ""` → 201, `await LocalDatabase(**payload.model_dump()).insert()`,
  returns `LocalDatabaseOut`.
- `GET ""` → `LocalDatabase.find_all().sort("-created_at").to_list()`,
  returns `list[LocalDatabaseOut]`.

No `PATCH`/`DELETE` routes — append-only for this pass.

Wire the router into the API app the same way `feedback.py`'s router is
registered (same module, same pattern).

## Frontend

### `HelpDatabases.tsx` changes

Add a "Local Databases" section **above** the existing static catalog
intro/tools (your own submissions are more immediately relevant than the
reference index below them). Contents:

- A heading ("Local Databases") and a "Submit a database" button that opens
  `AddLocalDatabaseModal`.
- The list of submitted databases, fetched via `@tanstack/react-query`
  (matching how `HelpSources.tsx` fetches its data), rendered as simple
  cards: name (linked to `url`), category badge, submitted date. Reuses the
  existing `db-card`-family CSS where it fits; add scoped classes only for
  what doesn't (e.g. a "Local Databases" section wrapper) rather than
  reworking the static catalog's styling.
- Empty state: a short line ("No local databases yet — submit one above.")
  when the list is empty, consistent with the empty-state pattern already
  used for the filtered static list (`db-empty`).

### New API client methods — `frontend/src/api/client.ts`

- `listLocalDatabases(): Promise<LocalDatabaseOut[]>`
- `createLocalDatabase(input: LocalDatabaseCreate): Promise<LocalDatabaseOut>`

Following the existing method/type conventions already in this file (check
neighboring methods for exact shape before adding).

### New `AddLocalDatabaseModal.tsx`

Built directly on the `AddProfileModal.tsx` pattern
(`frontend/src/components/AddProfileModal.tsx`):

- `ModalBackdrop` wrapper, click-outside/Escape closes.
- Local `useState` per field: `name`, `url`, `category` (defaults to the
  first enum option).
- Inline validation: submit disabled unless `name.trim()` and `url.trim()`
  are non-empty and `category` is selected; no reachability check.
- `async submit()` calls `api.createLocalDatabase(...)`, catches
  `ApiRequestError`, shows `error-box` on failure, `busy` state disables the
  button and swaps its label to "Adding…".
- On success: closes the modal and invalidates the react-query cache key
  used by the local-databases list so the new entry appears immediately
  without a manual refetch.

Category picker: a `<select>` with the six category labels (matches
existing `<select>` usage in `HelpDatabases.tsx`'s access-method filter).

## Dice icon fix

`HelpDatabases.tsx:160-173` — the "🎲 Surprise me" button. The app has no
icon library anywhere (`lucide-react`, `heroicons`, etc. are all absent from
`frontend/package.json`), and no other button in the app uses an icon.
Replace the button label with plain text: **"Surprise me"** — drop the emoji
entirely rather than introduce an icon dependency or hand-roll an SVG for
one button. Behavior (random scroll-to-card) is unchanged.

## Testing

- Backend: `pytest` coverage for the new router — create returns 201 with
  the right shape, list returns newest-first, invalid category/empty
  name/malformed URL are rejected with 422. Follow the test file layout
  used for `feedback`'s router tests.
- Frontend: no automated component tests exist in this repo (no
  jsdom/testing-library) and none are expected here. Manual verification in
  the browser at `localhost:5173`: submit a database, confirm it appears in
  the list immediately, confirm the dice button reads as plain text and
  still scrolls to a random static-catalog card.
