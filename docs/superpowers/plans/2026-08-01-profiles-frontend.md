# Profiles (Frontend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the profiles backend a face — a startup picker, an add-profile modal, a header profile menu, and an `X-BioFlow-Profile` header on every request — so the partition built across the backend becomes something a person can actually use.

**Architecture:** A `profileStore` (zustand, persisted to localStorage) holds the selected profile. `App.tsx` gates on it: no profile selected means the picker renders instead of `Shell`, so no `api.*` call can fire before a profile exists. The header is injected at `request<T>`'s single chokepoint in `api/client.ts`, plus two paths that bypass it — the XHR upload and the SSE `EventSource`.

**Tech Stack:** React 18, TypeScript, zustand, @tanstack/react-query, react-router-dom, Vite. No component-testing setup exists in this repo and none is expected — verification is manual at localhost:5173, per `CLAUDE.md`.

**Reference:** `docs/superpowers/specs/2026-07-31-profiles-design.md` — read the "UI flow" section before starting. This plan implements it.

---

## Before you start

### The backend is ready and strict

Every user-data route now requires the header. Verified against the running app:

```
GET /api/v1/projects        -> 400 profile_unresolved
GET /api/v1/jobs            -> 400 profile_unresolved
GET /api/v1/runs            -> 400 profile_unresolved
GET /api/v1/uploads         -> 400 profile_unresolved
GET /api/v1/search/objects  -> 400 profile_unresolved
GET /api/v1/pipelines/tools -> 200   (global: describes the container)
GET /api/v1/profiles        -> 200   (what you call before you have a profile)
```

So **the app is currently broken end to end** until this plan lands: the
frontend sends no header, so every data request 400s. That is expected, and it
is why this plan is the last step rather than the first.

### The API you are consuming

- `GET /api/v1/profiles` — list, sorted by username. Returns
  `{id, username, email, display: {emoji, colour}, details, has_password}`.
  Never returns `password_hash`.
- `POST /api/v1/profiles` — `{username, password?, email?, is_first_boot?}`.
  422 on validation, 409 on duplicate username.
- `POST /api/v1/profiles/{id}/select` — body `{password?}` optional. 403
  `wrong_profile_password` on mismatch. Sets `last_used_at`. **Returns no
  token and sets no cookie** — the client simply starts sending the id.
- `DELETE /api/v1/profiles/{id}` — 409 with one of three distinct codes:
  `last_profile`, `adopted_legacy_owner`, `profile_not_empty` (the last
  carries counts in `details`). Only the third is actionable.

Errors arrive as `{code, message, details}` and `ApiRequestError` in
`api/client.ts` already parses that shape, so `err.code` is available.

### Running the app

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner
docker compose up -d --build api web worker
```

Always from the main repo root, never a worktree — see `CLAUDE.md`. The app is
at **localhost:5173** and that is the only instance; there is no dev/prod
split. `web` runs `vite dev`, so frontend edits take effect on save with no
rebuild.

Backend tests, if you touch anything server-side (**one invocation at a time** —
all test processes share `biopipe_test` and concurrent runs corrupt each
other):

```bash
docker compose exec api python -m pytest tests/ -q      # baseline 2012 passed
```

Frontend typecheck and tests:

```bash
docker compose exec web npx tsc --noEmit
docker compose exec web npm test
```

---

## Task 1: `profileStore` — the selected profile, persisted

**Files:**
- Create: `frontend/src/stores/profileStore.ts`
- Test: none (no component-test setup; verified through use in later tasks)

- [ ] **Step 1: Read the existing store convention**

Read `frontend/src/stores/uiStore.ts`. It is plain zustand with no persistence
middleware, and its docstring explains what deliberately does *not* live in a
store (navigation lives in the URL). Match that voice.

Check whether `zustand/middleware`'s `persist` is already a dependency:

```bash
grep -n "zustand" frontend/package.json
```

`persist` ships inside the `zustand` package, so no new dependency is needed.

- [ ] **Step 2: Write the store**

Create `frontend/src/stores/profileStore.ts`:

```ts
import { create } from "zustand";
import { persist } from "zustand/middleware";

export interface Profile {
  id: string;
  username: string;
  email: string | null;
  display: { emoji: string; colour: string };
  details: Record<string, unknown>;
  has_password: boolean;
}

interface ProfileState {
  /** The profile whose data every request is scoped to, or null for "show the
   *  picker". This is the one piece of client state the API cannot recover
   *  from the URL: the backend issues no token and sets no cookie, so the id
   *  held here IS the session. */
  current: Profile | null;
  /** Whether to skip the picker on the next launch. Persisted alongside the
   *  profile because "stay logged in" is meaningless without remembering who. */
  autoLogin: boolean;
  setCurrent: (p: Profile) => void;
  logout: () => void;
  setAutoLogin: (v: boolean) => void;
}

export const useProfileStore = create<ProfileState>()(
  persist(
    (set) => ({
      current: null,
      autoLogin: false,
      setCurrent: (current) => set({ current }),
      // Clears the profile but keeps `autoLogin` as the user set it: logging
      // out to switch profiles should not silently also turn the preference
      // off, since the next selection is what re-arms it.
      logout: () => set({ current: null }),
      setAutoLogin: (autoLogin) => set({ autoLogin }),
    }),
    { name: "bioflow-profile" },
  ),
);
```

- [ ] **Step 3: Typecheck**

```bash
docker compose exec web npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/stores/profileStore.ts
git commit -m "feat: hold the selected profile in a persisted store"
```

Trailer on every commit in this plan:
```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

## Task 2: Send the header — all three paths

**Files:**
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/hooks/useEvents.ts`

This is the task most likely to be done half-way, because two paths bypass the
obvious one. Do all three.

- [ ] **Step 1: The `request<T>` chokepoint**

`request<T>` at `frontend/src/api/client.ts:67` already merges a `headers`
object and every one of the ~60 `api.*` methods goes through it. Add the header
there:

```ts
import { useProfileStore } from "../stores/profileStore";

function profileHeaders(): Record<string, string> {
  const id = useProfileStore.getState().current?.id;
  return id ? { "X-BioFlow-Profile": id } : {};
}
```

…and spread `...profileHeaders()` into the `headers` object in `request<T>`,
**before** `...init?.headers` so an explicit per-call header can still override
it.

Read the state via `useProfileStore.getState()` rather than the hook: this is a
plain function, not a component, and `getState()` is how zustand is read
outside React.

- [ ] **Step 2: The XHR upload path**

`api.uploadObject` (around `client.ts:544-581`) uses raw `XMLHttpRequest` and
never touches `request<T>`, so Step 1 does not cover it. It already calls
`xhr.setRequestHeader` twice (`X-Filename`, `Content-Type`). Add the profile
header the same way, guarding for the null case.

**Verify you found every bypass** rather than trusting this list:

```bash
grep -n "fetch(\|XMLHttpRequest\|EventSource" frontend/src/**/*.ts frontend/src/**/*.tsx
```

Report anything else that talks to the API without going through `request<T>`.

- [ ] **Step 3: The SSE stream — and read this carefully**

`frontend/src/hooks/useEvents.ts` opens `new EventSource("/api/v1/events")`.
**`EventSource` cannot send custom headers.** There is no option for it in the
browser API. So the header approach does not work here.

The backend's `/events` route is currently *unscoped* — it subscribes to one
global Redis channel and forwards everything to everyone. That is tracked
separately (see `docs/TODO.md`, "Profiles: events and schedules are the last
unscoped routes") and is **not this plan's job to fix**.

What this plan must do is not make it worse and not break the stream. Pass the
profile as a **query parameter** instead:

```ts
const id = useProfileStore.getState().current?.id;
const source = new EventSource(`/api/v1/events?profile=${encodeURIComponent(id ?? "")}`);
```

and add the `useEffect` dependency so the stream reconnects when the profile
changes — otherwise switching profiles leaves you subscribed as the old one.

The backend ignores that parameter today. Passing it anyway means the server
side can start honouring it without a second frontend change, and it makes the
intent visible at the call site. Add a comment saying exactly that, including
that the query parameter exists because `EventSource` cannot carry a header.

- [ ] **Step 4: Typecheck and commit**

```bash
docker compose exec web npx tsc --noEmit
git add frontend/src/api/client.ts frontend/src/hooks/useEvents.ts
git commit -m "feat: send the profile on every request, including the two paths that bypass request()"
```

---

## Task 3: The startup picker

**Files:**
- Create: `frontend/src/components/ProfilePicker.tsx`
- Create: `frontend/src/components/AddProfileModal.tsx`
- Modify: `frontend/src/api/client.ts` (profile methods)
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Add the API methods**

In `frontend/src/api/types.ts`, add the `Profile` shape (import or re-declare
to match `profileStore.ts` — pick one home and have the other import it; do not
maintain two copies).

In `frontend/src/api/client.ts`, add to the `api` object, following the style of
its neighbours:

```ts
  listProfiles: () => request<Profile[]>("/profiles"),
  createProfile: (body: {
    username: string;
    password?: string;
    email?: string;
    is_first_boot?: boolean;
  }) => request<Profile>("/profiles", { method: "POST", body: JSON.stringify(body) }),
  selectProfile: (id: string, password?: string) =>
    request<Profile>(`/profiles/${id}/select`, {
      method: "POST",
      body: JSON.stringify({ password: password ?? null }),
    }),
  deleteProfile: (id: string) =>
    request<void>(`/profiles/${id}`, { method: "DELETE" }),
```

- [ ] **Step 2: Build the picker**

`ProfilePicker.tsx` renders a clickable square per profile — emoji and
username — plus a `+` square that opens the add modal. Per the design spec:

- A profile with `has_password: false` enters directly on click.
- A profile with `has_password: true` reveals a password field; submit calls
  `selectProfile`. On 403 (`err.code === "wrong_profile_password"`) show the
  message inline and keep the field focused. Do not clear the whole picker.
- An **auto-login checkbox**, wired to `profileStore.setAutoLogin`.
- On a successful select, call `setCurrent(profile)`.

**First boot:** when `listProfiles()` returns `[]`, do not show an empty grid
with a lone `+`. Show a short welcome explaining what a profile is, and open
the add-profile form directly — this is the state a brand-new install lands in,
and an empty grid does not explain itself.

That first profile must be created with `is_first_boot: true`. It is what
adopts the pre-existing library: every document already carries
`owner: "local"`, and the adopted profile's `owner_id()` returns `"local"` so
nothing is migrated. Getting this flag wrong on a populated installation means
the user's whole library appears to vanish — it will still be there, owned by
`"local"` with no profile claiming it. Send `is_first_boot: true` **only** when
the list came back empty.

- [ ] **Step 3: Build the add-profile modal**

`AddProfileModal.tsx`: username (required), password (optional), email
(optional), and an expandable **Details** section for name, institution and
research areas, which post as the `details` object.

Look at `frontend/src/components/NewProjectModal.tsx` for the house modal
pattern — structure, focus handling, and how `.modal-actions` is pinned (a
previous fix made `.modal-body` scroll with the actions pinned via
`margin-top: auto`; do not reintroduce a modal whose submit button scrolls
away).

Handle 409 (duplicate username) and 422 (validation) inline on the form.

- [ ] **Step 4: Style it**

Add to `frontend/src/styles.css`. Reuse existing custom properties
(`--bg-panel`, `--border`, `--accent`, `--text-dim`) rather than new colours —
read the `:root` block at the top of the file. The picker is full-screen; the
squares are a responsive grid.

Support light and dark: the file has a `@media (prefers-color-scheme: light)`
block that redefines the same variables, so using the variables gets both for
free.

- [ ] **Step 5: Verify in the browser**

This is the real verification step for anything UI-facing, per `CLAUDE.md`.
Open localhost:5173 and check the picker renders, a profile can be created, and
selecting it enters the app.

- [ ] **Step 6: Commit**

---

## Task 4: The gate in `App.tsx`

**Files:**
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Gate on the store**

`App()` currently renders `QueryClientProvider > BrowserRouter > Shell`. The
gate goes **inside `QueryClientProvider` but outside `Shell`**, because
`Shell`'s children fire `api.*` calls on mount — rendering it before a profile
exists means a burst of 400s.

```tsx
function Gate() {
  const current = useProfileStore((s) => s.current);
  const autoLogin = useProfileStore((s) => s.autoLogin);

  if (!current) return <ProfilePicker />;
  return <Shell />;
}
```

- [ ] **Step 2: Auto-login**

Per the design spec, auto-login **skips the picker entirely** — the picker
reappears only after an explicit logout, or if the remembered profile no longer
exists.

Because the store is persisted, `current` is already populated on reload, so
the gate above naturally skips the picker. What `autoLogin: false` must add is
the opposite: **clear `current` on startup** so the picker shows even though a
profile is remembered.

Implement that as an effect that runs once on mount, before the gate renders
`Shell`. Be careful not to create a loop where clearing the profile
re-triggers the effect.

- [ ] **Step 3: Handle a stale remembered profile**

The persisted profile id can name a profile that has since been deleted. The
backend answers `404 not_found` for that id — deliberately distinguished from
`400 profile_unresolved` (malformed) precisely so the picker can tell "that
profile is gone, choose another" from "that is not an id at all".

On startup, validate the remembered profile against `listProfiles()`. If its id
is absent, clear it and show the picker rather than letting every subsequent
request fail. Do not silently pick a different profile.

- [ ] **Step 4: Verify in the browser**

Check: fresh load shows the picker; selecting enters the app; reload with
auto-login on skips the picker; reload with it off shows the picker; deleting
the remembered profile from another tab and reloading shows the picker rather
than a wall of errors.

- [ ] **Step 5: Commit**

---

## Task 5: The header profile menu

**Files:**
- Modify: `frontend/src/components/Header.tsx`

- [ ] **Step 1: Add the menu**

`Header.tsx` already renders two `<Menu>` instances (File, Help).
`components/Menu.tsx` is a generic dropdown taking `label` and
`items: {label, onSelect, disabled?}[]` — its docstring says it was written to
be reused, so no changes to `Menu.tsx` should be needed.

Add a third with the current profile's emoji and username as its label, and
items:
- **Switch profile** — `logout()`, which returns to the picker
- **Edit details** — opens the add-profile modal in edit mode, or defer with a
  `disabled: true` item if the backend has no update endpoint (**check** —
  `PATCH /profiles/{id}` may not exist; if it does not, say so and leave the
  item out rather than shipping a dead control)
- **Logout** — `logout()`

Per the design spec this lives in its own menu rather than under Activity:
Activity is about jobs and runs, and having the current profile permanently
visible in the header is what stops someone working for ten minutes in the
wrong library.

- [ ] **Step 2: Verify in the browser, then commit**

---

## Task 6: The upload dedup message

**Files:**
- Modify: `frontend/src/components/UploadTray.tsx`

- [ ] **Step 1: Say why an instant upload was instant**

`UploadCreated` already carries `dedup_hit: boolean` (`api/types.ts`), set when
the client's digest matched a blob already in the store. No new plumbing is
needed.

Blobs are global — deliberately, so two profiles holding the same reference
genome store it once. So `dedup_hit` now fires for content **another profile**
uploaded, and an upload that completes instantly reads as a bug.

When `dedup_hit` is true, say the file already existed locally and has been
added to the library. Frame it as success, not as a skipped step. Do not
mention the other profile — that would leak the one fact the partition hides.

- [ ] **Step 2: Verify in the browser, then commit**

---

## Final verification

Manual, at localhost:5173, per `CLAUDE.md`. There is no headless
component-testing setup in this repo and none is expected.

- [ ] **Against a real, populated library.** The interesting cases involve the
      adopted `"local"` profile, not a fresh one. Create the first profile and
      confirm your existing projects are all there — that is the zero-migration
      claim, and it is the one worth checking by eye.
- [ ] Create a second profile. Confirm it sees an empty library, and that the
      first still sees everything.
- [ ] Switch between them from the header menu without reloading.
- [ ] Run a pipeline (QC is quickest) as the second profile; confirm its job
      appears in *its* Activity view and the output lands in *its* library.
- [ ] Search as the second profile; confirm it does not return the first's
      files.
- [ ] Confirm no console errors and no 400s in the network tab.

```bash
docker compose exec web npx tsc --noEmit
docker compose exec web npm test
```

After merging, per `CLAUDE.md`, `worker` does not hot-reload — but this plan
touches no queue handler, so a restart is only needed if you deviate.

---

## What this plan does not include

- **Scoping the SSE event stream.** `/events` still broadcasts every profile's
  events to every client. Task 2 passes the profile as a query parameter so the
  server can start honouring it without a second frontend change, but the
  server-side fix is tracked separately.
- **Editing profile details**, if no `PATCH /profiles/{id}` exists. Task 5 says
  to check and leave the control out rather than ship a dead one.
- **Sharing between profiles.** Its own feature; the global-blob design keeps it
  cheap, but nothing here builds it.
- **Any backend change.** If you find you need one, stop and say so rather than
  widening this plan — the backend is at 2012 passing tests and its partition
  has been reviewed task by task.
