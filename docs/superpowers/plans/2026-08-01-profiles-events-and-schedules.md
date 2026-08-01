# Profiles: Events and Schedules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the last two unscoped routers. `events.py` gets a per-owner SSE
stream so profile B's browser stops receiving profile A's job progress and
filenames; `schedules.py` gets a written decision that it is deliberately
global, so nobody has to rediscover the question.

**Reference:** `docs/superpowers/specs/2026-07-31-profiles-design.md`, and the
TODO entry "Profiles: events and schedules are the last unscoped routes" in
`docs/TODO.md`, which this plan closes.

**Prerequisite:** the profiles backend (`api/deps.py`, `models/profile.py`) and
the profiles frontend (`stores/profileStore.ts`, the header injection in
`api/client.ts`) are both merged. Verified present in this tree:
`get_current_owner` exists at `backend/app/api/deps.py:28`, and
`frontend/src/hooks/useEvents.ts:36` already appends `?profile=` to the
EventSource URL with a comment saying the backend ignores it. This plan is what
stops it being ignored.

---

## The shape of the change, and why this shape

**Per-owner Redis channels, not a filtered global channel.** Both work. They
fail differently, and that is the whole decision. With one channel plus an
`owner` field on the payload, a publisher that forgets to stamp an owner leaks
that event to every profile — silently, exactly like the `dedup_key` trap the
spec already records. With `bp:events:{owner}`, a publisher that forgets emits
into a channel nobody is subscribed to: the UI misses a refetch and polls
instead. A missed refresh is recoverable; a leaked filename is not. Fail closed.

**`owner` becomes a required keyword argument on `publish_event`.** This is the
enforcement mechanism, and it is the reason the change is worth doing in one
pass rather than route by route: after it, every one of the twelve call sites
has to name an owner or fail to import. A default value would put the failure
back at runtime and lose the entire benefit.

**Ownerless events go to an explicit system channel.** Four of the twelve call
sites are genuinely installation-wide — `system.starvation_override`,
`storage.unavailable`, `blob.drifted`, `blob.missing`. They describe the
machine, not anyone's library. They publish to `bp:events:system`, and every
SSE client subscribes to *two* channels: its own and system. Passing a
sentinel is a decision the author had to make; omitting an argument is not.

**Not touched:** `blobs` and `objects/` stay global per the spec, and nothing
here changes what any query returns. This is purely the notification path.

---

## Phase 1 — Channel keys

### Task 1.1: Add channel helpers to `backend/app/queue/keys.py`

- [ ] Keep `EVENTS = f"{PREFIX}:events"` as the channel *prefix* and add:
      `SYSTEM_OWNER = "system"` and
      `def events_channel(owner: str) -> str: return f"{EVENTS}:{owner}"`.
- [ ] Write the comment that says why the bare `EVENTS` string is no longer
      published to or subscribed to by anything: a leftover subscriber on the
      old channel would receive nothing and look like a broken stream.
- [ ] Note in the comment that `"system"` can never collide with a real owner:
      owners are either `"local"` or a 24-hex `ObjectId` string.

**Verify:** `grep -rn "keys.EVENTS" backend/app` returns only `keys.py` itself
and the two files Phase 2 and 3 rewrite. Nothing else may reference it after
this plan lands.

---

## Phase 2 — Publishers

### Task 2.1: Make `owner` required on `publish_event`

`backend/app/queue/queue.py:789`.

- [ ] Change the signature to
      `async def publish_event(event_type: str, data: dict, *, owner: str) -> None`
      and publish to `keys.events_channel(owner)`.
- [ ] Keep the existing swallow-everything `except` — telemetry still must not
      fail a job — and keep the docstring's "events are advisory" note, which
      is now doing double duty: it is also why a missed event on an unread
      channel is an acceptable failure mode.

### Task 2.2: Thread the owner through `queue.py`'s six call sites

Each already has, or can cheaply get, the owning job:

- [ ] `enqueue`, both publishes (`:163`, `:167`) — the `owner` parameter is
      already in scope.
- [ ] `_fail_blocked_job` (`:259`) — takes `job`; use `job.owner`.
- [ ] `request_cancel`, both publishes (`:583`, `:589`) — loaded `job` above.
- [ ] `complete` (`:508`) — it already does `job = await Job.get(...)` for the
      duration calculation, and that lookup is nullable. Do **not** fall back
      to `"local"` when it is None: that attributes a stranger's job to the
      adopted profile. Publish to `keys.SYSTEM_OWNER` in that branch and log at
      `warning`, because a completing job with no document is itself a bug
      worth seeing.

### Task 2.3: Give `JobContext` an owner, and use it for progress

`backend/app/queue/executor.py:237` publishes `job.progress` from
`_write_progress(job_id, epoch, update)`, which has no owner anywhere in scope.

- [ ] Add `owner: str` to `JobContext` (`backend/app/queue/registry.py:31`).
      Give it no default — same reasoning as `publish_event`.
- [ ] Set it from `job.owner` where the executor builds the context. The
      executor already reads `job.owner` for `results.apply` at
      `executor.py:160`, so the value is proven available at that point.
- [ ] Thread `owner` through `_schedule_progress` and `_write_progress` as an
      explicit parameter rather than reading it back off a job document — this
      path runs several times a second per job and must not add a Mongo read.

### Task 2.4: The four installation-wide publishers

- [ ] `worker.py:212` `system.starvation_override` — `owner=keys.SYSTEM_OWNER`.
      The queue is one queue; a starving maintenance job is not anyone's.
- [ ] `handlers.py:459` `storage.unavailable` — system. The drive is gone for
      everyone.
- [ ] `handlers.py:514` `blob.drifted` and `handlers.py:554` `blob.missing` —
      system. Blobs are global by the spec, so there is no single owner to
      attribute one to, and the set of profiles referencing it is not worth
      computing to decorate a refetch hint.
- [ ] At each, leave one line of comment saying *why* system rather than an
      owner. Four identical `owner=keys.SYSTEM_OWNER` arguments with no
      rationale is how the next person concludes it was laziness.

**Note for the implementer:** the TODO entry names `queue/results.py` as a
`publish_event` call site. It is not one — `grep -rn "publish_event"
backend/app` finds `queue.py`, `worker.py`, `executor.py` and `handlers.py`
only. Trust the grep; correct the TODO entry when closing it.

---

## Phase 3 — The SSE route

### Task 3.1: Extract owner resolution from the header dependency

`backend/app/api/deps.py`. `get_current_owner` currently reads the header and
resolves it in one function. SSE needs the same resolution from a query
parameter, because `EventSource` cannot send custom headers — the comment at
`frontend/src/hooks/useEvents.ts:25` already says so.

- [ ] Extract the body into `async def resolve_owner(value: str | None) -> str`
      and have `get_current_owner` call it with the header value.
- [ ] Keep every existing behaviour intact: the `"local"` adoption branch, the
      `InvalidId`-not-`ValueError` catch (the file's comment explains why that
      one is load-bearing), and `ProfileUnresolvedError` for all three failure
      shapes.
- [ ] Do not add a second dependency alias. The SSE route calls `resolve_owner`
      directly, and its docstring should say that the query parameter is a
      browser-API limitation rather than a second sanctioned way in.

### Task 3.2: Scope the stream

`backend/app/api/v1/events.py`.

- [ ] Take `profile: str | None = Query(None)` and resolve it with
      `resolve_owner` **before** returning `EventSourceResponse` — an
      unresolved profile must be a 400 the picker can act on, not a stream that
      opens and then errors. Resolving inside the generator would produce a
      200 followed by a dead stream.
- [ ] Subscribe to both `keys.events_channel(owner)` and
      `keys.events_channel(keys.SYSTEM_OWNER)`; unsubscribe from both in the
      `finally`. Redis `pubsub.subscribe` takes both in one call.
- [ ] Keep the keepalive, the JSON-decode guard, and the `CancelledError`
      re-raise exactly as they are.
- [ ] Docstring: say that resolving the profile here is organizational, not
      authentication — any client may pass any profile id and read that
      profile's stream, same as every other route. The `deps.py` module
      docstring already sets this precedent; do not let a scoped SSE endpoint
      read as a security boundary.

### Task 3.3: Frontend guard

`frontend/src/hooks/useEvents.ts`.

- [ ] Return early without opening an `EventSource` when `profileId` is
      undefined. Today the hook sends `profile=` (empty), which after Task 3.2
      becomes a 400 — and `EventSource` reconnects automatically on error, so
      that is a reconnect loop rather than a single failure. `Shell` only
      mounts with a profile selected, so this is a guard against a state that
      should not occur, which is exactly when a reconnect loop is hardest to
      spot.
- [ ] Rewrite the stale comment at `:30` that says "the backend ignores the
      parameter today". Replace it with what is now true, including the two
      channels.

---

## Phase 4 — Schedules: decide and document

`backend/app/api/v1/schedules.py`, 5 routes, 0 scoped. The TODO says "probably
correct as-is — but nobody has said so."

**Say so: they stay global.** The evidence is already in the tree, not a
judgement call. `scheduler.py:121` and `:161` both enqueue with `owner="local"`
under a comment reading that this maintenance "runs against the whole
installation, not any one profile's library. There is no owner to inherit here
and there never will be." The schedules are GC and file verification. Making
`GET /schedules` per-profile would mean either every profile seeing an empty
list, or five identical copies of one cron table.

### Task 4.1: Document it in the route docstrings

- [ ] Add a module-level docstring paragraph naming these as installation-wide
      and pointing at `scheduler.py`'s `owner="local"` as the reason, in the
      shape `search.py:94`'s `/metadata/schemas` docstring uses. That is the
      established pattern in this codebase for "deliberately unscoped", and
      matching it is what makes the next sweep's grep find this one.
- [ ] State the line being held, as `search.py` does: schedules describe *the
      machine's* periodic work. A per-profile scheduled task, if one is ever
      added, is a different feature and would need its own owner field on
      `Schedule` — say that, so the door is visibly left open rather than
      appearing to have been overlooked.

### Task 4.2: Record the wrinkle this exposes

- [ ] Maintenance jobs carry `owner: "local"`, and `/api/v1/jobs` is
      owner-scoped — so GC and verify jobs appear in the *adopted* profile's
      job list and nobody else's, and (after Phase 2) their job events reach
      only that profile's channel. This is pre-existing, not caused by this
      plan, and not worth fixing here. Add it to `docs/TODO.md` as a deferred
      finding so it is discovered by reading rather than by someone wondering
      why their GC jobs vanished after switching profiles.

---

## Phase 5 — Tests

Run from this worktree with `./backend/run-worktree-tests.sh tests/ -q` —
**not** `docker compose exec api`, which tests main's code from here.
`CLAUDE.md` explains both.

### Task 5.1: Channel routing tests

New `backend/tests/queue/test_event_channels.py`.

- [ ] Assert `publish_event` with owner A lands on `bp:events:{A}` and **not**
      on `bp:events:{B}` — both directions. The TODO records ten shipped-green
      isolation tests that only asserted "B sees nothing", which also passes
      against a route hardcoded to `"local"`.
- [ ] Use non-`"local"` owners throughout, for the reason
      `test_queue_owner.py`'s module docstring gives: `"local"` is the default
      every document inherits, so asserting against it proves nothing.
- [ ] Assert the four system publishers land on `bp:events:system`.

### Task 5.2: SSE isolation test

Add to `backend/tests/api/` alongside `test_route_owner_scoping.py`, using the
existing `client` and `two_profiles` fixtures from `tests/api/conftest.py`.

- [ ] Publish one event as A and one as B; assert A's stream yields A's event
      and never B's, within a bounded read. Both directions again.
- [ ] Assert a system event reaches both streams — this is the assertion that
      fails if someone later "simplifies" the two-channel subscription.
- [ ] Assert `GET /events?profile=<garbage>` is a 400 `profile_unresolved`
      before any stream body, and that a missing `profile` is the same.

### Task 5.3: Mutation check — the step that is easy to skip

- [ ] For each new isolation test, break the code it covers (hardcode the
      channel to `"local"`; drop the `not` from the negative assertion) and
      confirm the test fails. Every one of the ten bad tests in the TODO's
      record was caught this way and no other way. A test that passes against
      broken code is worse than no test, because it is also a claim.

### Task 5.4: Full suite

- [ ] `./backend/run-worktree-tests.sh tests/ -q` green. Expect fallout in any
      existing test that calls `publish_event` positionally or stubs it —
      `tests/queue/test_queue_owner.py` stubs it in its `_no_redis` fixture.
      Fix the stubs; do not add a default to the signature to make them pass.

---

## Phase 6 — Manual verification

- [ ] `./ops/worktree-up.sh` from this worktree (UI on 5273, API on 8100). Not
      plain `docker compose`, which would repoint the main stack at this tree.
- [ ] Create a second profile. Open the app in two browser profiles or a normal
      and a private window, each on a different BioFlow profile.
- [ ] Start a job as A — an upload or a QC run. Watch B's network tab: B's
      `/events` stream must show only pings for the duration. Before this
      change it shows A's `job.progress` with A's filename in it.
- [ ] Confirm A's own UI still live-updates. A stream that leaks nothing
      because it delivers nothing passes the test above.
- [ ] Restart the worker first if a queue handler changed:
      `docker compose -p <worktree project> restart worker` — the worker does
      not hot-reload, and a stale worker makes a correct fix read as a failure.

---

## Phase 7 — Close the TODO entry

Per `CLAUDE.md`, finishing the work is not finishing the entry, and this has
gone wrong three times.

- [ ] Append ` — FIXED` to the "Profiles: events and schedules are the last
      unscoped routes" heading. Keep the body: the two-shapes analysis is the
      reasoning behind the channel layout and the next person needs it.
- [ ] Under it, note what shipped, when, and where: per-owner channels plus a
      system channel, `owner` required on `publish_event`, `resolve_owner`
      extracted in `deps.py`, schedules documented as global.
- [ ] **Say what this did differently from the plan.** Known deltas already:
      the entry names `results.py` as a `publish_event` call site and it is
      not; the entry treats schedules as an open question and this plan closes
      it as global; the system channel is not in the entry at all, and it is
      the piece that makes "per-owner channels" actually workable.
- [ ] Add the Task 4.2 finding as a new deferred entry.
- [ ] Do not tick the checkboxes in this plan as evidence of anything — nothing
      ticks them, and `CLAUDE.md` records two merged plans sitting at zero of
      115 boxes. Verify against the code.
