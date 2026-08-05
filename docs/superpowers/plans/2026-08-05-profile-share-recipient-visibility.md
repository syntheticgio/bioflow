# Profile share recipient visibility — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the sharing backend that shipped in [#25](https://github.com/syntheticgio/bioflow/issues/25) reachable from the UI — a sender can offer a file, a recipient sees the offer and accepts it, and the accepted file shows up somewhere they can find it. Implements [#50](https://github.com/syntheticgio/bioflow/issues/50), the second slice of [epic #3](https://github.com/syntheticgio/bioflow/issues/3).

**Architecture:** Mostly frontend — a share dialog launched from `ManageFile`, a `/shares` route holding inbox and outbox, and a count badge on the header profile menu fed by the existing per-profile SSE stream. Two backend changes are required first, and the first of them is the reason this slice is not frontend-only: `ShareOut` currently returns raw `owner` strings that **the frontend cannot resolve to a person**, and the case where it fails is the adopted profile.

**Tech Stack:** React 18 + TypeScript, TanStack Query, Zustand, React Router, plain CSS. FastAPI/Beanie for the two backend tasks. No new dependencies.

**Reference:** `docs/superpowers/specs/2026-08-05-profile-sharing-design.md` ("Where it lands", "Notification", "Sharing is advisory") and the shipped backend in `backend/app/services/share_service.py` / `backend/app/api/v1/shares.py`. Read both before starting.

**Out of scope:** report-directory copying, stale-offer sweeping, and `Share` cleanup on profile delete — all [#51](https://github.com/syntheticgio/bioflow/issues/51). The mobile shell (`frontend/src/mobile/`) gets no share UI; it is a focused activity/download view and sharing is not one of its jobs.

---

## Before you start

### Verification is manual, in a browser

There is no headless component-testing setup in this repo and none is expected
(`CLAUDE.md`, "Verifying changes"): zero `.test.tsx` files, no jsdom, no
testing-library. **Every frontend task in this plan ends at a browser**, and the
backend tasks are the only ones that end at `pytest`.

From this worktree:

```bash
./ops/worktree-up.sh        # UI on 5273, API on 8100, its own database
./ops/worktree-up.sh --down # stop it, delete its volumes
```

Do **not** use plain `docker compose` from a worktree — a `PreToolUse` hook
blocks it, because it would silently repoint the 5173 stack at this branch.

### You need two real profiles, and one of them must be the adopted one

This is not optional colour. The adopted profile (`adopted_legacy_owner: true`)
is the one whose `owner_id()` returns the literal `"local"` rather than its own
id, it is the profile holding the pre-existing library on any real
installation, and it is therefore both the most likely sharer *and* the one case
where the obvious client-side implementation breaks. See Task 1.

`worktree-up.sh` copies the main stack's database on first launch, so the
adopted profile comes with it. Create a second, ordinary profile in the picker.

### Baseline

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Record the count. As of #25 merging it is **2772 passed**. If the baseline is
red, stop and report rather than starting against it.

> **Note on the test image:** `run-worktree-tests.sh`'s fallback image tag
> (`biopipe-api`) went stale when [#37](https://github.com/syntheticgio/bioflow/issues/37)
> changed how Compose tags the api build, so it may silently run against a
> days-old image missing recent dependencies. If tests fail on
> `ModuleNotFoundError` for something in `backend/pyproject.toml`, that is the
> cause, not your change. There is a separate task open to fix the script.

---

## Task 1: `ShareOut` carries who, not just which owner string

**Files:**
- Modify: `backend/app/api/v1/schemas.py`, `backend/app/api/v1/shares.py`, `backend/app/services/share_service.py`
- Test: `backend/tests/api/test_shares_api.py`

**This task exists because the frontend cannot do this join itself.**

`ShareOut` returns `from_owner` and `to_owner` as raw owner strings. To render
"alice wants to share reads.fastq.gz", the UI needs a username and an emoji, so
the obvious implementation is: fetch `/profiles`, match `profile.id ===
share.from_owner`.

That match **silently fails for the adopted profile**, and only for it.
`Profile.owner_id()` returns `"local"` for that one profile, never its `_id`, so
its shares carry `from_owner: "local"` — a value that appears in no profile
listing. The frontend's `Profile` type (re-exported from
`stores/profileStore.ts`) does not even carry `adopted_legacy_owner`, so the
client has no way to reconstruct the mapping. The offer would render with a
blank or "Unknown" sender, on the profile most likely to be doing the sharing.

Resolve it server-side, where `owner_id()` is available.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/api/test_shares_api.py`:

```python
async def test_share_out_names_the_other_profile(client, two_profiles):
    """The inbox has to say who, and the raw owner string cannot answer that."""
    a, b = two_profiles["a"], two_profiles["b"]
    obj = await _ready_object(a.owner_id())

    r = await client.post(
        "/api/v1/shares",
        json={"object_id": str(obj.id), "to_profile_id": b.owner_id()},
        headers=two_profiles["a_headers"],
    )

    body = r.json()
    assert body["from_profile"]["username"] == a.username
    assert body["from_profile"]["emoji"] == a.display.emoji
    assert body["to_profile"]["username"] == b.username


async def test_share_out_resolves_the_adopted_profile(client):
    """The regression this task exists for. The adopted profile's owner string
    is the literal "local", which matches no profile id -- so a resolver keyed
    on `str(profile.id)` returns nothing for exactly the profile holding the
    pre-existing library."""
    adopter = await profile_service.create_profile(username="sh-adopter", is_first_boot=True)
    other = await profile_service.create_profile(username="sh-other")
    obj = await _ready_object(adopter.owner_id())  # owner is "local"
    assert adopter.owner_id() == "local"

    r = await client.post(
        "/api/v1/shares",
        json={"object_id": str(obj.id), "to_profile_id": other.owner_id()},
        headers={"X-BioFlow-Profile": adopter.owner_id()},
    )

    assert r.json()["from_profile"]["username"] == "sh-adopter"

    await adopter.delete()
    await other.delete()
```

The second test is the one that fails against a naive implementation. Write it
first and watch it fail before fixing.

- [ ] **Step 2: Implement**

A batch resolver in `share_service.py`, because the inbox renders many rows and
one lookup per row is an N+1:

```python
async def resolve_owner_profiles(owners: Iterable[str]) -> dict[str, Profile]:
    """Map owner strings to the profiles that answer for them.

    Keyed by `owner_id()`, not by `str(profile.id)`. For every profile but one
    those are the same string; for the adopted profile `owner_id()` is the
    literal "local", and a map built from ids simply has no entry for the
    profile holding the pre-existing library. Nothing raises -- the row just
    renders with no sender.

    One query for the whole set rather than one per share: the inbox is a list.
    """
    wanted = set(owners)
    profiles = await Profile.find_all().to_list()
    return {p.owner_id(): p for p in profiles if p.owner_id() in wanted}
```

Reading every profile is fine here — this is a single-user tool with a handful
of profiles, and filtering server-side would need the same `"local"` special
case expressed as a query.

Then a nested schema, hand-enumerated like every other response model in
`schemas.py`:

```python
class SharePartyOut(BaseModel):
    """Just enough to name the other side in a list row. Deliberately not
    `ProfileOut`: an inbox does not need `email`, `details`, or
    `has_password`, and sharing is not a reason to publish them."""

    owner: str
    username: str
    emoji: str
    colour: str
```

`ShareOut.of` gains a `parties: dict[str, Profile]` argument. Where a profile is
missing (deleted between the offer and the read — #51's territory), fall back to
a placeholder rather than raising: an inbox that 500s because one sender was
deleted is worse than one row reading "(deleted profile)".

- [ ] **Step 3: Verify** — the new tests, then `tests/api -q`.

---

## Task 2: Tell the sender when their offer is answered

**Files:** modify `backend/app/services/share_service.py`; test `backend/tests/services/test_share_events.py`

`offer_share` publishes `share.offered` to the recipient. Nothing publishes
anything when the recipient accepts or declines, so the sender's outbox shows
"offered" until they reload the page by hand — which reads as the accept not
having worked.

- [ ] **Step 1: Write the failing test**

Mirror the existing `test_offer_publishes_to_the_recipients_channel`, asserting
the *opposite direction*: accepting publishes to `share.from_owner`, declining
too. Publishing these to the acceptor's own channel is the natural mistake and
notifies the one person who already knows.

- [ ] **Step 2: Implement**

`share.accepted` and `share.declined`, both `owner=share.from_owner`, published
after the transaction commits rather than inside it — an event for a
transaction that then rolled back is a lie, and `publish_event` already
swallows its own failures so it cannot break the accept.

- [ ] **Step 3: Verify**

---

## Task 3: Frontend API client and types

**Files:** modify `frontend/src/api/types.ts`, `frontend/src/api/client.ts`

- [ ] **Step 1: Types**

```ts
export interface ShareParty {
  owner: string;
  username: string;
  emoji: string;
  colour: string;
}

export type ShareState = "offered" | "accepted" | "declined" | "withdrawn";

export interface Share {
  id: string;
  from_owner: string;
  to_owner: string;
  from_profile: ShareParty | null;
  to_profile: ShareParty | null;
  source_object_id: string;
  name: string;
  size: number;
  state: ShareState;
  accepted_object_id: string | null;
  message: string | null;
  created_at: string;
  updated_at: string;
}
```

- [ ] **Step 2: Client methods**

`offerShare`, `shareInbox`, `shareOutbox`, `acceptShare`, `declineShare`,
`revokeShare` — all through the existing `request<T>()` chokepoint, which
attaches `X-BioFlow-Profile` already. Nothing here needs `profileQuery()`: these
are `fetch` calls, not `<a href>` navigations.

- [ ] **Step 3: Verify** — `npx tsc --noEmit` from `frontend/`.

---

## Task 4: Wire the SSE events

**Files:** modify `frontend/src/hooks/useEvents.ts`

- [ ] **Step 1: Implement**

Add a `"shares"` coalescing key alongside `jobs`/`objects`/`stats`, invalidating
`["shares"]`. Subscribe `share.offered`, `share.accepted`, `share.declined`.

`share.accepted` must **also** schedule `objects` and `projects`: acceptance
creates the recipient's copies and may lazily create the "Shared with me"
project, and without those invalidations the accepted file is invisible until a
manual reload — the exact symptom this whole slice exists to prevent.

There is no `projects` key today; add one that invalidates `["projects"]`.

- [ ] **Step 2: Verify** — covered by the manual pass in Task 8; nothing to
      assert here on its own.

---

## Task 5: The share dialog

**Files:** create `frontend/src/components/ShareFileModal.tsx`; modify `frontend/src/components/ManageFile.tsx`

- [ ] **Step 1: Implement the modal**

Follow `NewProjectModal.tsx` exactly — `ModalBackdrop`, a `useMutation`,
`notify.success`/`notify.error`, `onClose`. That file is the pattern; do not
invent a second modal shape.

Contents: a recipient `<select>` (from `api.listProfiles()`, **with the current
profile filtered out** — the backend 422s on a self-share, and offering the
option only to reject it is a worse experience than not offering it), an
optional message field, and the submit.

When `obj.blob?.storage === "external"`, show a warning before the submit:
BioFlow does not own those bytes and will never unlink them, so the recipient's
copy can go `MISSING` through no action of their own. `ManageFile` already
branches on this exact field for its delete note — match that wording's register.

- [ ] **Step 2: Add the row to `ManageFile`**

A `manage-label` / control pair in the existing grid, between Tags and Delete.
Guard it the way the Pairing and Role rows are guarded — `ManageFile`'s comment
records that an unguarded row strands its label over nothing:

```ts
// Mirrors the backend's own precondition (offer_share raises 409 on a
// non-READY object), so the button is absent rather than present-and-failing.
const shareable = obj.status === "ready" && Boolean(obj.blob_sha256);
```

- [ ] **Step 3: Verify in the browser** — offer a file to the other profile,
      confirm the toast, confirm a second offer of the same file is refused
      with the backend's own conflict message rather than a generic error.

---

## Task 6: The `/shares` route — inbox and outbox

**Files:** create `frontend/src/components/SharesView.tsx`; modify `frontend/src/App.tsx`

- [ ] **Step 1: Implement**

A route rather than a modal, matching how `/settings` hangs off the same profile
menu: it can be linked to, it survives a reload, and the outbox needs more room
than a dropdown gives.

Two sections in one view:

**Shared with me** (`api.shareInbox()`) — one row per pending offer: sender
emoji + username, filename, size, optional message, and **Accept** / **Decline**.
Accept takes an optional destination project; default to "Shared with me" with a
project `<select>` for choosing otherwise. On success, `notify.success` naming
the project it landed in, and a link to the accepted object via
`accepted_object_id`.

**Shared by me** (`api.shareOutbox()`) — every state, newest first. A pending row
gets **Withdraw**; an accepted row gets no action and says so. Do not render a
withdraw control on an accepted share: `revoke_share` refuses it with a 409, and
a button whose only outcome is an error is worse than no button. The design note
is explicit that the UI should not offer it.

Query keys must include the profile id (`["shares", "inbox", profileId]`), or
switching profiles serves the previous profile's offers from cache before the
refetch lands.

- [ ] **Step 2: Register the route and the menu entry**

`<Route path="/shares" element={<SharesView />} />`, and a "Shared with me"
item in the header profile menu beside Settings.

- [ ] **Step 3: Verify in the browser** — accept as the recipient; confirm the
      file appears in a "Shared with me" project in the explorer **without a
      manual reload** (that is Task 4 doing its job), and that the sender's
      outbox flips to accepted on its own (that is Task 2).

---

## Task 7: The inbox badge

**Files:** modify `frontend/src/components/Menu.tsx`, `frontend/src/components/Header.tsx`

- [ ] **Step 1: Widen `Menu`'s label**

`Menu` types `label: string` and renders `{label}`. A count badge needs markup,
so widen it to `ReactNode`. One line, and every existing caller keeps working
because a `string` is a `ReactNode`.

- [ ] **Step 2: Implement**

The header already renders the profile menu; give it a badge when
`shareInbox()` is non-empty. Reuse the same `["shares", "inbox", profileId]`
query the route uses, so opening `/shares` costs no second request and the badge
clears from the same invalidation.

The badge belongs on the **profile** menu, not Activity — Activity is about jobs
and runs, and a share is identity-shaped. That is the design note's reasoning
and it matches where "Switch profile" already lives.

- [ ] **Step 3: Verify** — with both profiles open in two browsers (or two
      windows), offer from one and watch the badge appear on the other
      **without a reload**. That is the end-to-end proof that the SSE path
      works; a badge that only appears on refresh means Task 4 is not wired.

---

## Task 8: Full verification and close-out

- [ ] **Step 1: Backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Read the count, not the exit code. Expect baseline + the tests from Tasks 1–2.

- [ ] **Step 2: `npx tsc --noEmit`** from `frontend/`, and check the browser
      console is clean — there is no type-checking in CI to catch it later.

- [ ] **Step 3: The manual pass, with the adopted profile on the sending side**

Not a fresh pair of test profiles. Task 1 exists entirely because the adopted
profile behaves differently, and a manual pass between two ordinary profiles
cannot see the bug it fixes. Offer from the adopted profile and confirm the
recipient's inbox names it correctly rather than showing a blank sender.

Also exercise: offering a paired FASTQ (the mate should arrive too, from #25's
cascade) and offering a BAM with a BAI (the index should arrive and the file
should be usable as a pipeline input in the recipient's library).

- [ ] **Step 4: Merge and push**

Per `CLAUDE.md`: once green, commit, merge to `main`, push. Re-run the suite
after merging if `main` moved.

- [ ] **Step 5: Close out**

- Comment on #50 with what shipped and **what differed from this plan** —
  `CLAUDE.md` notes every closed entry departed from its plan somewhere, and
  that delta is the most valuable sentence in the write-up.
- Close #50. Leave #51 open.
- Update `docs/TODO.md`'s "Sharing between profiles" entry: it stays open for
  #51, but the recipient-visibility half is now done.

---

## Traps, collected

1. **The adopted profile's owner string is `"local"`, never its id.** A
   client-side join of `share.from_owner` against `profile.id` silently renders
   no sender for the one profile holding the pre-existing library. Task 1 is
   this trap; the rest of the plan depends on it being fixed first.
2. **Accepting must invalidate `projects` as well as `objects` and `shares`.**
   The "Shared with me" project is created lazily *by the accept*, so without
   it the file lands somewhere the explorer has never heard of.
3. **`Menu`'s `label` is typed `string`.** A badge needs `ReactNode`.
4. **Query keys must carry the profile id**, or a profile switch serves the
   previous profile's inbox from cache.
5. **Nothing tells the sender their offer was answered** until Task 2 —
   an outbox stuck on "offered" reads as a broken accept.
6. **A withdraw button on an accepted share can only produce a 409.** Guard on
   `state === "offered"`.
7. **The recipient picker must exclude the current profile.** The backend
   rejects a self-share, so offering the option only sets up a refusal.
8. **There are no component tests here and none are expected.** A task that
   ends without a browser check is a task that was not verified.
