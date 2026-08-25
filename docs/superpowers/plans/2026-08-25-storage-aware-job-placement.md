# Storage-aware job placement — implementation plan

Issue: [#845](https://github.com/syntheticgio/bioflow/issues/845)
Spec: `docs/superpowers/specs/2026-08-25-storage-aware-job-placement-design.md`

## Do not start until

1. **[#844](https://github.com/syntheticgio/bioflow/issues/844) is merged.**
   Confirm the field names as merged (`Node.storage_shared: bool | None`,
   `storage_location`, `storage_checked_at`). If #844 landed an enum instead of
   a tri-state bool, every `is True` below is an enum comparison — re-read the
   spec's "Verify before implementing" item 1 before writing any code.
2. **[#846](https://github.com/syntheticgio/bioflow/issues/846) is merged.**
   Hard ordering, not a preference (spec Q3). Merging this first stops
   filesystem-dependent work on every pre-existing node in the maintainer's
   deployment, for no gain. State the dependency in the PR description.
3. **The spec's five other pre-implementation checks are answered**, in
   particular item 3 (`install_tool`'s install root) — it decides a registry
   entry and there is no defensible default.

## Commit 1 — `feat(queue): classify every job type by whether it reads the primary's storage`

Registry and its tests only. No behaviour change; nothing reads the module yet.
Separable on purpose: this is the half a reviewer must check entry by entry,
and it must be revertable without unpicking the claim path.

1. **New `backend/app/queue/storage_dependence.py`.** Module docstring carries
   the inclusion rule verbatim from the spec's Q2, including the sentence that
   `io=IoClass.HEAVY` is not evidence either way and the `download_sra_run`
   counterexample. That comment is the whole defence against the next person
   deriving this from `IoClass`.
   - `SELF_CONTAINED_JOB_TYPES: frozenset[str]`, one comment per entry saying
     which half of the two-part rule earns it.
   - `FILESYSTEM_DEPENDENT_JOB_TYPES` computed at module scope from
     `registry.all_handlers()` minus the exempt set — materialized, not
     implicit, or the partition test has nothing to test.
   - `is_filesystem_dependent(job_type: str) -> bool`, defaulting **dependent**
     for an unknown type.

   Starting membership, per the spec: `download_sra_run`, `fetch_remote`,
   `download_assembly`, `download_uniprot`, `noop`, `sleep_test`. Everything
   else dependent, including `download_kraken_db`, `download_lineage`,
   `verify_files`, `gc_blobs`, `sweep_storage_drift`, `verify_blob` — each with
   its comment saying why a name that looks exempt is not.

2. **New `backend/tests/queue/test_storage_dependence.py`.** Modelled on
   `backend/tests/services/test_provenance_verbs.py`, which is the same shape
   of registry with the same failure mode. Four tests:
   - `test_every_registered_handler_is_classified` — with the
     `assert names, "registry empty -- handler modules were not imported"`
     guard against a vacuous pass (`test_provenance_verbs.py:25-27`).
   - `test_no_handler_is_both`.
   - `test_exempt_set_names_only_registered_handlers` — reachability the other
     way; a stale exemption is a typo that exempts nothing.
   - `test_io_class_does_not_imply_self_contained` — asserts
     `download_sra_run` is `IoClass.HEAVY` **and** self-contained. This is the
     test that fails when someone tries to collapse Q2.

   Module docstring: these are a pair, run the whole file, per CLAUDE.md.

## Commit 2 — `feat(queue): refuse jobs a node's storage cannot serve`

The enforcement. Depends on commit 1's module and on nothing else.

3. **`backend/app/queue/scripts/claim.lua`.**
   - New `ARGV[11]`, comma-separated denied job types; empty means deny nothing.
     Build a set from it exactly as `allowed` is built at `:51-54`.
   - Extend the HMGET at `:107` to fetch `type` as a **sixth** field, appended
     after `override`. `claim.lua:110-112` already documents that `override`
     was appended "so h[1]..h[5] keep their positions" — same reasoning, same
     placement, and say so in the comment.
   - Add `and not denied[jtype]` to the `fits` expression at `:115-119`.
   - Add the `storage` arm to the `i == 1` reason block (`:118-133`), **last**,
     after the `io` branch, so it never masks a resource reason:
     `why = {gate = 'storage'}`. No `need`/`free` — those are resource-shaped.
   - Update the header comment block (`:17-23`) which enumerates every ARGV
     slot; it is a contract and goes stale silently.

   `type` is already written by `_push_to_redis` (`queue.py:479`) and by
   `reconcile` (`queue.py:1004`), so no producer change is needed — **verify
   both**, because a job reconciled without `type` reads as nil in Lua and
   would be denied when the denied set is non-empty. If either omits it, that
   is part of this commit, not a follow-up.

4. **`backend/app/queue/queue.py:512-545` `claim()`** — new
   `denied_types: list[str] | None = None` keyword, forwarded as the eleventh
   ARGV as `",".join(...)`. Keyword-only with a `None` default so every
   existing call site keeps working unchanged.

5. **`backend/app/queue/blocked_reason.py:22`** — `GATES` gains `"storage"`,
   **appended**. The comment at `:22` says the order is "mirrored in claim.lua
   and in the frontend's wording", so items 3 and 7 must land in this same
   commit or the mirror is broken. `BlockedReason` needs no new field.

6. **`backend/app/queue/worker.py`.**
   - `_storage_shared()`: reads this worker's own `Node` document, caches the
     result with a 60s TTL, on the `_maintenance_starving` pattern
     (`:334-340`) — same reason (a Mongo read on a path that runs several times
     a second), same shape. Returns `False` on any exception, logging once
     rather than per call. Fail closed (spec R6).
   - A public cache-drop the enrollment/event path calls, so #844's probe takes
     effect without waiting out the TTL. Wire it into the existing event
     subscription; if #844 publishes no such event, say so in the PR and leave
     the TTL as the sole mechanism rather than pretending otherwise.
   - `_try_claim` (`:283-311`): compute
     `denied = sorted(FILESYSTEM_DEPENDENT_JOB_TYPES) if not await self._storage_shared() else []`
     once, and pass it to **both** `_try_claim_queue` calls. Missing the second
     one leaves the global-pool fallback wide open, which is precisely the path
     `chunked_align_handlers.py:56` uses — the bug this issue exists to fix.
   - `_try_claim_queue` (`:313-329`) grows the parameter and forwards it.

## Commit 3 — `feat(api): report each node's shared-storage status`

Serving the fields. Split from commit 4 because it is the one with the
fixture-poisoning hazard, and it must be revertable alone.

7. **`backend/app/api/v1/nodes.py:98-106`** — add `storage_shared`,
   `storage_location`, `storage_checked_at` to the `enumerate_nodes` mongo
   loop dict. **Read the warning at `:107-114` first**: the surrounding
   `except` discards *every* node accumulated so far, logs no doc id, and a
   field read added here has already silently emptied `mongo_nodes` for a stale
   fixture once. Real `Node` documents are safe (the fields are optional on the
   model); mocks and hand-built dicts standing in for one are not. Grep every
   fixture and `MagicMock` feeding this route and fix them **in this commit** —
   a fixture fixed in a later commit means this one merges green having broken
   nothing visible and everything silently.

8. **`frontend/src/api/types/system.ts:110-118`** — the three fields on
   `NodeInfo`, matching the API's null semantics exactly.

## Commit 4 — `feat(ui): show what a node's storage lets it run`

9. **`frontend/src/components/SettingsNodes.tsx`.**
   - Extract `storageBadge(node)` as a **pure function returning
     `{label, className, title}`**, exported for test. There is no jsdom setup;
     the pure-function pattern (`AlignerParamFields.test.tsx`) is how this
     component is testable at all.
   - New Storage column between Status and Version — the header list at
     `:144-152` and `NodeRow` must change together or the columns shift by one
     with no test failing.
   - Render it exactly like the Online/Offline/Unknown badge at `:515-521`:
     class-distinguished span, explanation in `title`. Copy that idiom rather
     than inventing a second one.
   - Wire the re-check control on non-`true` rows to #844's probe endpoint via
     `api/client.ts` (the nodes calls live at `:567-587`).
10. **`frontend/src/styles.css`** — `nodes-storage` variants alongside the
    existing `nodes-status` rules.
11. **New `frontend/src/components/SettingsNodes.test.tsx`** (or an addition to
    an existing sibling test) — three states, three labels, three non-empty
    titles, each naming a remedy.

## Commit 5 — `feat(ui): explain a job blocked because no node can read its data`

12. **The frontend's blocked-reason wording** — find the existing four-gate
    mapping (grep `"cpu"`/`"mem"` near the activity view; it mirrors
    `blocked_reason.GATES`) and add the `storage` arm with the spec's Q4
    string. Separated from commit 2 so the backend gate can ship and be
    verified against a real Redis before the copy is finalized.

## Verification

- `./backend/run-worktree-tests.sh tests/queue/ tests/api/test_node_provision.py -q`
  — from the worktree; `docker compose exec api` silently tests main's code.
- **The whole of `tests/queue/test_storage_dependence.py`**, never one named
  test — classified and not-double-classified are a pair (CLAUDE.md).
- `./backend/run-worktree-tests.sh tests/ -q` before the PR. Item 7 can break
  tests nowhere near it.
- `ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e`
  from the repo root, whole tree.
- **`claim.lua` against a real Redis, not a mock.** The Lua change is the one
  thing no Python test exercises directly. Enqueue an `align_reads` and a
  `download_sra_run`, claim with a denied set, assert which came back, then
  read `bp:why:bp:q:ready` and assert `gate == "storage"`. Also assert a
  memory-blocked job still reports `mem`.
- **Manual, at 5273** (`./ops/worktree-up.sh`): a node in each of the three
  storage states, badge and title correct in each; a queued `align_reads` with
  no shared node showing the storage reason rather than sitting mute.
- **Real database check** (CLAUDE.md): enumerate live `Node` docs and run the
  classifier over the job types in `job_timings`, not just the registry —
  catches a type the deployment has run that is no longer registered.

## What must change in the same commit or start lying

- `blocked_reason.GATES`, `claim.lua`'s reason arms, and the frontend wording
  are one contract in three files (`blocked_reason.py:22`). Commits 2 and 5
  split it deliberately and the PR must say so; do not split it further.
- `claim.lua`'s ARGV header comment (`:17-23`) versus its actual argument list.
- The `SettingsNodes.tsx` column header list versus `NodeRow`'s cells.
- Every fixture feeding `enumerate_nodes`, versus item 7's field reads.

## Out of scope

- **`target_node` not surviving `reconcile()` / `_release_dependents` /
  `rescue_orphans`** (`queue.py:1023`, `:461`, `:946`). Pre-existing, real, and
  deliberately not depended on by this design. **File an issue** per CLAUDE.md
  and link it from the PR.
- Steering work toward shared nodes (#835), the probe itself (#844), the
  migration (#846), share setup (#847/#848), `chunked_align_handlers.py:57`'s
  job-id bug (#851).
