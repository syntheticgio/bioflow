# Share deletion and GC behaviour — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the deletion edges of profile sharing correct and *provably* correct — the refcount behaviour that already works gets tests that fail if it regresses, and the three genuinely unbuilt edges get built. Implements [#51](https://github.com/syntheticgio/bioflow/issues/51), the third slice of [epic #3](https://github.com/syntheticgio/bioflow/issues/3), following [#25](https://github.com/syntheticgio/bioflow/issues/25) and [#50](https://github.com/syntheticgio/bioflow/issues/50).

**Architecture:** Backend only. One new function in `object_service` (copy report directories), one post-commit call site in `share_service.accept_share`, one guard added to the existing stale-source check, one cleanup query in `profile_service.delete_profile`, and a characterization test file for the refcount/GC path that nothing currently covers.

**Tech Stack:** FastAPI/Beanie/Motor, pytest. No new dependencies.

**Reference:** `docs/superpowers/specs/2026-08-05-profile-sharing-design.md` — "Deletion and GC" and "Report directories do not follow the object". Read both before starting; the second one rules out the obvious implementation.

**Out of scope:** Rekeying report directories by digest (the principled fix, deliberately deferred by the spec — it touches every report writer and reader). Any UI change: the stale-offer refusal surfaces through the existing 409 toast, and profile deletion has no share-facing UI. Revoking an *accepted* share, which the spec refuses on purpose.

---

## Before you start

### Two claims in the issue are already true in the code

Verify these rather than re-deriving them, and do not "fix" them:

- `accept_share` **already** refuses a deleted source, at
  `backend/app/services/share_service.py:331` — `ConflictError("The sender
  deleted this file before it was accepted")`. Task 3 adds the *blob* re-check
  beside it, it does not add the source check.
- `blob_service.detach_blob_from_object` already decrements, and
  `gc_candidates` already filters `ref_count <= 0` + `GC_GRACE` +
  `MANAGED`. Task 1 is tests only; if a test in Task 1 fails, that is a real
  bug and worth stopping over.

`profile_service.delete_profile` ends with a "Not built here … see #51" comment
at `backend/app/services/profile_service.py:224`. Task 4 replaces it.

### Tests run from the worktree, not through `docker compose exec`

Per `CLAUDE.md` ("Verifying changes") — `docker compose exec api python -m
pytest` inside a worktree silently tests *main's* code:

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Record the baseline count before starting. If it is red, stop and report
rather than starting against it.

### Assert on the ledger and the recipient, never on the deleter

This is the whole reason the issue exists. A test that deletes the sender's
copy and then checks the sender can no longer see it passes whether or not the
refcount was decremented correctly — it passes with the refcount left at 2, and
it passes with the blob's bytes already unlinked. Every assertion in Task 1
lands on the `blobs` document, `gc_candidates()`, or the *recipient's* object.

`backend/tests/services/helpers_share.py` already drives real profiles and real
`ingest_local_file` ingestion; use it rather than hand-built `DataObject`s, for
the same reason it was written (an owner value a factory made up is not the one
production stamps).

---

## Task 1: The refcount is right — prove it

**Files:**
- Test: `backend/tests/services/test_share_deletion_gc.py` (new)

Pure characterization. No source changes in this task; if one turns out to be
needed, that is a finding to report, not a step to take quietly.

Setup for each test is the shipped path end to end: two real profiles, a READY
object with stored bytes on the sender, `offer_share`, `accept_share`.

- [ ] **Sender deletes after acceptance.** `object_service.delete_object` on the
      sender's copy, then assert *all four*: the recipient's object still
      resolves through `object_service.get_object(..., owner=recipient)`; its
      `status is ObjectStatus.READY`; `Blob.get(digest).ref_count == 1`; and the
      digest is absent from `blob_service.gc_candidates()`.
- [ ] **The recipient deleting instead is symmetric.** Same four assertions with
      the roles swapped. Cheap, and it catches an implementation that special-
      cases the `shared_from` copy on the way out.
- [ ] **Both delete → the blob becomes collectable.** After both deletes assert
      `ref_count == 0`. `gc_candidates` filters `updated_at < now - GC_GRACE`,
      so a freshly decremented blob is correctly *not* a candidate yet — assert
      that too, then backdate `updated_at` past `GC_GRACE` with a direct
      `blobs.update_one` and assert the digest now appears. Do not monkeypatch
      `GC_GRACE`; backdating exercises the real comparison.
- [ ] **The sidecar cascade keeps the recipient's index.** Share a parent with a
      sidecar (`sidecar_of`), accept, delete the sender's parent — which
      cascades to the sender's sidecar — and assert the recipient's *sidecar*
      copy is still READY at `ref_count == 1`. `accept_share` re-points
      `sidecar_of` inside the group, and this is the assertion that fails if a
      future cascade follows the pointer across partitions.

**Verification:** `./backend/run-worktree-tests.sh tests/services/test_share_deletion_gc.py -q`

---

## Task 2: Report directories are copied to the recipient's object id

**Files:**
- Modify: `backend/app/services/object_service.py`, `backend/app/services/share_service.py`
- Test: `backend/tests/services/test_share_reports.py` (new)

**Why not the obvious fix:** a read-time fallback through
`shared_from.object_id` breaks the moment the sender deletes their copy, because
`delete_object` calls `remove_report_dirs`. The recipient's report has to be
independent bytes under the recipient's id. See the spec, "Report directories do
not follow the object".

- [ ] Add `copy_report_dirs(src_object_id, dst_object_id) -> None` to
      `object_service`, directly beside `remove_report_dirs` and mirroring its
      shape: sync (called through `asyncio.to_thread`), iterating the same three
      roots (`settings.qc_reports_dir`, `bam_stats_dir`, `vcf_stats_dir`), with
      the same `relative_to` containment check on **both** source and
      destination before touching anything.
- [ ] Best-effort per root, exactly like `remove_report_dirs`: a missing source
      directory is the common case and is skipped silently; an existing
      destination is skipped rather than merged; `shutil.copytree` failure logs
      `report_dir_copy_failed` and continues to the next root. **Nothing in this
      function raises.** A missing report is recomputable; refusing the share
      over one trades a working file for no file.
- [ ] Call it from `accept_share` **after the transaction commits**, for every
      `(src, copy)` pair in the group — not just the source object, since a
      shared BAM's stats live under the BAM and a mate's under the mate. Put it
      after the `bump_counters` call and before or beside the `share.accepted`
      event. Filesystem work does not belong inside a Mongo transaction, and a
      copy failure must not roll back an otherwise-correct accept.
- [ ] Delete the "Not built here, deliberately -- see #51" comment block at the
      end of `accept_share` (`share_service.py:394-400`) and replace it with a
      one-line note on why the copy is post-commit.

**Two things worth knowing while implementing:**

- The recipient can already *serve* the report once the directory exists —
  `GET /pipelines/qc/report/{object_id}/{path}` is owner-scoped by an
  `object_service.get_object` lookup on the recipient's own id, and `_copy_for`
  already carries `facts` (which is where the report filename lives).
- The orphan-report reaper (`queue/handlers.py:731`) removes directories whose
  object id no longer resolves. The recipient's id resolves, so the copy is
  safe from it; the *sender's* directory is removed by `delete_object` as
  before.

**Tests:**

- [ ] Write a fake report file into `settings.qc_reports_dir / str(source.id)`
      before accepting; assert the identical file exists under the recipient's
      new object id afterwards, with the same bytes.
- [ ] **Assert it survives the sender's deletion** — delete the sender's object
      and re-assert the recipient's report file is still there. This is the test
      that fails if someone later "simplifies" this into a `shared_from`
      fallback.
- [ ] Accepting with no report directory present succeeds and creates nothing.
- [ ] A copy failure does not fail the accept: monkeypatch `shutil.copytree` to
      raise `OSError`, assert `accept_share` still returns a READY object.

**Verification:** `./backend/run-worktree-tests.sh tests/services/test_share_reports.py tests/services/test_share_accept.py -q`

---

## Task 3: Acceptance re-checks the blob, and says why it refused

**Files:**
- Modify: `backend/app/services/share_service.py`
- Test: `backend/tests/services/test_share_accept.py`

The source-object check exists. What is missing is the case where the source
object is fine but the *bytes* are not — the blob was collected after both
earlier owners dropped it, or the verifier moved it to `QUARANTINED` or
`MISSING`. Without this, `attach_existing_blob_to_object` fails inside the
transaction and the recipient gets a 500 for a condition the app understands.

- [ ] After the existing source check in `accept_share`, add
      `blob_service.find_present_blob(share.blob_sha256)` — the denormalized
      digest on the `Share`, not the source object's, so the check is meaningful
      even as the source drifts. A `None` result raises `ConflictError` naming
      the cause: the content is no longer available, and the file may need to be
      shared again after re-upload. `find_present_blob` already treats any
      non-`PRESENT` state as a miss, so quarantined and missing are covered by
      the one call.
- [ ] **The offer row is left `OFFERED`.** A refused accept changes no state;
      the recipient clears it by declining, through the path that already
      exists. (Decided deliberately — a rejected request should not write, and
      the recipient should not have a row vanish under them.) Add a comment
      saying so, or the next reader will assume it was an oversight.

**Tests:**

- [ ] Accepting after the sender deleted the source raises `ConflictError` whose
      message names the sender's deletion — and the inbox still *renders* the
      offer (`list_inbox` returns it with its denormalized `name`/`size`), which
      is the thing the denormalization exists for.
- [ ] Accepting when the blob has been flipped to `QUARANTINED` raises
      `ConflictError` naming the content, not the sender.
- [ ] After either refusal, the share is still `OFFERED` and no `DataObject` was
      created for the recipient — assert the object count, since a transaction
      that half-committed would show up here and nowhere else.

**Verification:** `./backend/run-worktree-tests.sh tests/services/test_share_accept.py tests/api/test_shares_api.py -q`

---

## Task 4: Deleting a profile takes its offers with it

**Files:**
- Modify: `backend/app/services/profile_service.py`
- Test: `backend/tests/services/test_profile_service.py`

- [ ] After `await profile.delete()`, delete every `Share` naming
      `profile.owner_id()` as `from_owner` **or** `to_owner` whose state is not
      `ACCEPTED`. `owner_id()`, never `str(profile.id)` — the adopted profile's
      shares carry the literal `"local"`, and a query built from the id would
      match none of them and silently leave every offer behind. This is the same
      trap the `count_owned_documents` call above it already documents.
- [ ] **`ACCEPTED` rows are kept.** The surviving profile's copied object carries
      `shared_from.share_id`, and that row is the only record of where their file
      came from. Nothing reads it today; it costs one clause to not destroy it.
      Say that in the comment, since "delete them all" is the obvious next
      simplification.
- [ ] Log the count deleted (`profile_shares_deleted`) on the existing
      `profile_deleted` log line or beside it. A profile delete that silently
      removes rows in another partition should say how many.
- [ ] Replace the "Not built here" comment block with a short note on what is
      now done and why `ACCEPTED` survives.

**Tests:**

- [ ] A profile with a pending *outgoing* offer: delete it, assert the offer is
      gone from the other profile's `list_inbox`.
- [ ] A profile with a pending *incoming* offer: delete it, assert the offer is
      gone from the sender's `list_outbox`. Both directions, because one query
      with a missing `$or` branch passes the first test alone.
- [ ] An `ACCEPTED` share survives the deletion of either party.
- [ ] The delete still refuses a non-empty profile *before* touching any share —
      assert the `profile_not_empty` `ConflictError` and that the share rows are
      untouched. (A profile that owns objects cannot be deleted, so this only
      matters if the cleanup is ever moved above the guard.)

**Verification:** `./backend/run-worktree-tests.sh tests/services/test_profile_service.py -q`

---

## Task 5: Close it out

- [ ] Full suite: `./backend/run-worktree-tests.sh tests/ -q`. Compare against
      the baseline recorded at the start — read the count, not the exit code.
- [ ] Sanity-check against real data, per `CLAUDE.md` ("Check a rule against the
      real database"): from the main checkout, `docker compose exec api python
      -c "..."` listing any `shares` rows whose `from_owner`/`to_owner` names no
      existing profile. On a clean install this is empty; if the user's real
      database has strays from before this task, say so rather than deleting
      them — there is no migration in scope here.
- [ ] Merge to `main` and push (`CLAUDE.md`: commit and merge once green,
      without asking). Keep the commits separable — the characterization tests
      from Task 1 are their own commit, and they are the one commit that should
      be true both before and after the rest.
- [ ] Update [#51](https://github.com/syntheticgio/bioflow/issues/51): tick the
      five acceptance criteria, note anything the implementation did differently
      from this plan, and close it.

---

## What is deliberately not here

- **Report directories keyed by digest.** The spec names this as the principled
  fix and rejects it for this slice.
- **A stale-offer sweeper.** Considered and dropped: a new scheduled handler for
  a condition the accept path already refuses cleanly.
- **Anything about external blobs.** A shared `register_in_place` object can go
  `MISSING` under the recipient with no refcount change; that is allowed, named
  in the spec, and surfaced by the share dialog rather than by this work.
