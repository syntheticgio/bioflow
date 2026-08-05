# Profile share offer/accept/revoke — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one profile offer a stored file to another and let the recipient accept it, producing a second `DataObject` under the recipient's `owner` that points at the same blob. No bytes are copied and no job is enqueued. Implements [#25](https://github.com/syntheticgio/bioflow/issues/25), the first executable slice of [epic #3](https://github.com/syntheticgio/bioflow/issues/3).

**Architecture:** A new `Share` document straddling two owner partitions (queried by explicit `from_owner`/`to_owner`, never by the inherited `owner`), plus a materialization path that inserts the recipient's copies and increments each blob's refcount inside **one** transaction. The copy's four cross-partition pointer fields are sanitized: `sidecar_of` and `mate_object_id` cascade and are re-pointed at the new copies, `derived_from` and `produced_by_job` are cleared in favour of a new typed `shared_from` field on `DataObject`. Revoke withdraws an un-accepted offer only.

**Tech Stack:** FastAPI, Beanie/Motor (MongoDB replica set — the transaction is why), Python 3.12, pytest + pytest-asyncio. No new dependencies.

**Reference:** `docs/superpowers/specs/2026-08-05-profile-sharing-design.md` — read it before starting. This plan implements it and does not repeat its rationale except where a step needs it to make a call correctly.

**Out of scope, deliberately:** all UI ([#50](https://github.com/syntheticgio/bioflow/issues/50)), and report-directory copying / stale-offer edges / profile-delete cleanup ([#51](https://github.com/syntheticgio/bioflow/issues/51)). Task 8 leaves a named seam for each rather than half-building them.

---

## Before you start

### Running tests from a worktree

`docker compose exec api python -m pytest` **silently tests main's code** from a
worktree — the `api` container bind-mounts the main checkout. Use:

```bash
./backend/run-worktree-tests.sh tests/ -q            # whole suite
./backend/run-worktree-tests.sh tests/services -v    # one directory
```

Record the baseline count before touching anything. If the baseline is red,
stop and report rather than starting against it.

### The transaction needs the replica set

`blob_service` already requires it (`get_client().start_session()`), and the
worktree test script provides its own single-node replica set. Nothing new is
needed — but a test that fails with "Transaction numbers are only allowed on a
replica set member" means the script was bypassed, not that the code is wrong.

### After merge

`worker` does not hot-reload, but **this plan changes nothing the queue
handlers import** — a share enqueues no job. `docker compose up -d --build api
web worker` from the main repo root is still the way to see it, for the `api`
process.

---

## Task 1: The `Share` model and `SharedFrom` on `DataObject`

**Files:**
- Create: `backend/app/models/share.py`
- Modify: `backend/app/models/object.py`, `backend/app/models/__init__.py`
- Test: `backend/tests/models/test_share.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/models/test_share.py`:

```python
import pytest
from beanie import PydanticObjectId

from app.models import DataObject, Share, ShareState

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


async def test_share_defaults_to_offered():
    share = Share(
        from_owner="local",
        to_owner="65f000000000000000000001",
        source_object_id=PydanticObjectId(),
        name="reads.fastq.gz",
        size=1234,
        blob_sha256="a" * 64,
    )
    await share.insert()
    assert share.state is ShareState.OFFERED
    assert share.accepted_object_id is None


async def test_duplicate_pending_offer_is_rejected():
    """The unique partial index is the guard, not a service-level read."""
    from pymongo.errors import DuplicateKeyError

    source = PydanticObjectId()
    common = dict(
        from_owner="local",
        to_owner="65f000000000000000000002",
        source_object_id=source,
        name="ref.fna",
        size=99,
        blob_sha256="b" * 64,
    )
    await Share(**common).insert()
    with pytest.raises(DuplicateKeyError):
        await Share(**common).insert()


async def test_a_declined_offer_does_not_block_re_offering():
    """Only OFFERED participates in the index, so history never blocks a retry."""
    source = PydanticObjectId()
    common = dict(
        from_owner="local",
        to_owner="65f000000000000000000003",
        source_object_id=source,
        name="ref.fna",
        size=99,
        blob_sha256="c" * 64,
    )
    first = Share(**common)
    await first.insert()
    first.state = ShareState.DECLINED
    await first.save()

    await Share(**common).insert()  # must not raise


async def test_shared_from_is_a_typed_field_not_metadata():
    obj = DataObject(project_id=PydanticObjectId(), name="x.bam")
    assert obj.shared_from is None
```

Run it — it fails on the import. Good.

- [ ] **Step 2: Implement**

`backend/app/models/share.py`:

```python
"""Shares: an offer of one profile's file to another profile.

Deliberately outside the owner partition, for the same reason `Profile` is:
a Share describes a relationship *between* two partitions and belongs to
neither, so the inherited `owner` field is meaningless here. Every query names
`from_owner` or `to_owner` explicitly -- reading `owner` on this collection is
always a bug.

A share moves no bytes. It is one document plus, on acceptance, a second
`DataObject` pointing at a blob that already exists; see
docs/superpowers/specs/2026-08-05-profile-sharing-design.md.
"""

from enum import StrEnum

from beanie import PydanticObjectId
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models.base import TimestampedDocument


class ShareState(StrEnum):
    OFFERED = "offered"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"  # sender revoked, before acceptance


class Share(TimestampedDocument):
    from_owner: str
    to_owner: str
    source_object_id: PydanticObjectId

    # Denormalized from the source object at offer time. Three reasons, and
    # the third is the load-bearing one: the inbox renders without reading
    # into the sender's partition; the list endpoint needs no per-row join;
    # and the offer still describes itself after the sender deletes their
    # copy, which is what lets acceptance refuse with "the sender deleted
    # this file" instead of dereferencing a missing object.
    name: str
    size: int
    blob_sha256: str

    state: ShareState = ShareState.OFFERED
    accepted_object_id: PydanticObjectId | None = None
    message: str | None = None

    class Settings:
        name = "shares"
        indexes = [
            # The recipient's inbox and the sender's outbox.
            IndexModel([("to_owner", ASCENDING), ("state", ASCENDING)], name="inbox"),
            IndexModel([("from_owner", ASCENDING), ("created_at", DESCENDING)], name="outbox"),
            # One pending offer per (sender, recipient, object). Partial on
            # OFFERED so that declining and re-offering the same file works,
            # and so a long history of accepted shares never blocks a new one.
            # Enforced here rather than by a read-then-insert in the service:
            # two concurrent offers both read "no pending offer" before either
            # writes, and the recipient gets the same file twice in their inbox.
            IndexModel(
                [("from_owner", ASCENDING), ("to_owner", ASCENDING), ("source_object_id", ASCENDING)],
                name="uniq_pending_offer",
                unique=True,
                partialFilterExpression={"state": ShareState.OFFERED.value},
            ),
        ]
```

Add to `backend/app/models/object.py`:

```python
class SharedFrom(BaseModel):
    """Where a shared copy came from.

    A typed field rather than a `metadata` key, for the reason `derived_from`
    gives: metadata is user-owned and user-editable, and provenance that can be
    silently retyped is not provenance.

    `object_id` and `owner` describe the source *at share time* and are not
    kept live. The sender may delete their copy, rename it, or move it; none of
    that reaches here, and the recipient's file keeps working regardless --
    which is the whole point of the copy being an independent document.
    """

    object_id: PydanticObjectId
    owner: str
    share_id: PydanticObjectId
    at: datetime
```

and on `DataObject`, beside `derived_from`:

```python
    # Set only on a copy materialized by accepting a share. Mutually exclusive
    # with `derived_from`/`produced_by_job` in practice: those are cleared on
    # the copy because they name objects and jobs in the sender's partition,
    # which the recipient's owner-scoped lookups can never resolve.
    shared_from: SharedFrom | None = None
```

Export `Share`, `ShareState`, `SharedFrom` from `models/__init__.py` and add
`Share` to `ALL_MODELS` — **that is what creates the indexes**; a model missing
from that list has none, and `uniq_pending_offer` is the only thing preventing
duplicate offers.

- [ ] **Step 3: Verify** — `./backend/run-worktree-tests.sh tests/models/test_share.py -v`

---

## Task 2: A refcount increment that does not lie about verification

**Files:**
- Modify: `backend/app/services/blob_service.py`
- Test: `backend/tests/services/test_blob_share_attach.py`

This is the subtlest task in the plan and the reason it comes before the
service. **Do not reuse `attach_blob_to_object` for a share.**

That function is written for the path that *places bytes*, so it
unconditionally `$set`s verification state it has just earned honestly:

```python
"$set": {
    "last_verified_at": now,
    "observed_size": size,
    "observed_mtime": observed_mtime,
    "state": BlobState.PRESENT.value,
    "miss_count": 0,
}
```

Called from a share, every one of those is wrong:

- **`observed_mtime` would be nulled.** A share has no file to stat, so it
  would pass `None` — and for an `EXTERNAL` blob that field is the drift
  baseline (`queue/handlers.py:509`). Nulling it silently downgrades drift
  detection to size-only, so an external file edited in place stops being
  quarantined. Nothing fails; the check just stops working.
- **`last_verified_at = now` is a claim nobody checked.** The verifier rotates
  oldest-verified-first (`verify_rotation` index), so a shared blob jumps to
  the back of the queue and its real verification is deferred — the opposite of
  what sharing a file should do to the attention it gets.
- **`state: PRESENT` would heal a `MISSING` or `QUARANTINED` record** on the
  word of a caller that looked at nothing.

- [ ] **Step 1: Write the failing test**

`backend/tests/services/test_blob_share_attach.py`:

```python
import pytest
from beanie import PydanticObjectId

from app.models import Blob, BlobState, BlobStorage, DataObject, ObjectStatus
from app.services import blob_service

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


async def _external_blob(digest: str) -> Blob:
    blob = Blob(
        id=digest,
        size=10,
        state=BlobState.PRESENT,
        storage=BlobStorage.EXTERNAL,
        external_path=f"/data/ext/{digest}.fa",
        ref_count=1,
        observed_size=10,
        observed_mtime=1_700_000_000.0,
    )
    await blob.insert()
    return blob


async def test_share_attach_preserves_the_external_drift_baseline():
    digest = "d" * 64
    await _external_blob(digest)
    obj = DataObject(project_id=PydanticObjectId(), name="ref.fa", owner="b")
    await obj.insert()

    await blob_service.attach_existing_blob_to_object(
        object_id=obj.id, digest=digest, size=10
    )

    blob = await Blob.get(digest)
    assert blob.ref_count == 2
    # The whole point: these are untouched.
    assert blob.observed_mtime == 1_700_000_000.0
    assert blob.observed_size == 10


async def test_share_attach_does_not_claim_a_verification_it_did_not_do():
    digest = "e" * 64
    blob = await _external_blob(digest)
    before = blob.last_verified_at
    obj = DataObject(project_id=PydanticObjectId(), name="ref.fa", owner="b")
    await obj.insert()

    await blob_service.attach_existing_blob_to_object(
        object_id=obj.id, digest=digest, size=10
    )

    assert (await Blob.get(digest)).last_verified_at == before


async def test_share_attach_refuses_a_blob_that_is_not_present():
    digest = "f" * 64
    blob = await _external_blob(digest)
    blob.state = BlobState.QUARANTINED
    await blob.save()
    obj = DataObject(project_id=PydanticObjectId(), name="ref.fa", owner="b")
    await obj.insert()

    with pytest.raises(Exception):  # narrow to the real type once implemented
        await blob_service.attach_existing_blob_to_object(
            object_id=obj.id, digest=digest, size=10
        )
    assert (await Blob.get(digest)).ref_count == 1  # not incremented


async def test_share_attach_sets_the_object_ready():
    digest = "0" * 64
    await _external_blob(digest)
    obj = DataObject(project_id=PydanticObjectId(), name="ref.fa", owner="b")
    await obj.insert()

    await blob_service.attach_existing_blob_to_object(
        object_id=obj.id, digest=digest, size=10
    )
    refreshed = await DataObject.get(obj.id)
    assert refreshed.status is ObjectStatus.READY
    assert refreshed.blob_sha256 == digest
```

- [ ] **Step 2: Implement**

Add to `blob_service.py`:

```python
async def attach_existing_blob_to_object(
    *,
    object_id: PydanticObjectId,
    digest: str,
    size: int,
    session=None,
) -> Blob:
    """Point an object at a blob that already exists, and take a reference.

    The share path's counterpart to `attach_blob_to_object`, and separate from
    it on purpose. That function is for callers that just *placed bytes*, so it
    writes `last_verified_at`, `observed_size`, `observed_mtime`, `state` and
    `miss_count` -- verification facts it earned by touching the file. A share
    touches no file and has earned none of them:

    - Writing `observed_mtime=None` destroys the drift baseline an EXTERNAL
      blob is checked against (`queue/handlers.py`), silently reducing drift
      detection to size-only.
    - Writing `last_verified_at=now` pushes the blob to the back of the
      verifier's oldest-first rotation without anything having been verified.
    - Writing `state=PRESENT` would heal a MISSING or QUARANTINED record on
      the strength of a caller that looked at nothing.

    So this touches `ref_count` and `updated_at` and nothing else on the blob.

    The blob must exist and be PRESENT; a share of content we cannot vouch for
    is refused rather than handed over. `session` is accepted so an acceptance
    cascade -- parent, sidecars, mate -- lands as one transaction.
    """
    blob = await Blob.get(digest)
    if blob is None or blob.state is not BlobState.PRESENT:
        raise NotFoundError(
            f"Content is no longer available for sharing (blob {digest[:12]}...)"
        )

    now = datetime.now(UTC)
    db = get_db()

    async def _apply(s):
        await db.blobs.update_one(
            {"_id": digest}, {"$inc": {"ref_count": 1}, "$set": {"updated_at": now}}, session=s
        )
        await db.objects.update_one(
            {"_id": object_id},
            {
                "$set": {
                    "blob_sha256": digest,
                    "size": size,
                    "status": ObjectStatus.READY.value,
                    "updated_at": now,
                }
            },
            session=s,
        )

    if session is not None:
        await _apply(session)
    else:
        async with await get_client().start_session() as s:
            async with s.start_transaction():
                await _apply(s)

    return await Blob.get(digest)  # type: ignore[return-value]
```

`ObjectStatus.READY` rather than `INGESTING`: there is nothing to ingest. The
format, facts and role are copied from a source object that was already parsed,
so enqueuing an ingest job would re-derive facts we already have — and would
put this feature back in the business of enqueuing jobs, which the design says
it is not.

- [ ] **Step 3: Verify** — the new test file, then `tests/services -q` for regressions.

---

## Task 3: `share_service.offer_share`

**Files:**
- Create: `backend/app/services/share_service.py`
- Test: `backend/tests/services/test_share_offer.py`

- [ ] **Step 1: Write the failing test**

Cover, in this order (the negative cases are the ones that fail when the
feature is broken):

```python
async def test_offer_records_a_denormalized_snapshot(): ...
async def test_offering_an_object_you_do_not_own_raises_not_found(): ...
async def test_offering_to_yourself_is_rejected(): ...
async def test_offering_to_an_unknown_profile_is_rejected(): ...
async def test_offering_a_non_ready_object_is_rejected(): ...     # UPLOADING, ERROR, MISSING
async def test_a_second_pending_offer_of_the_same_object_conflicts(): ...
```

`test_offering_an_object_you_do_not_own_raises_not_found` is the important one.
Creating an object as A and offering it as A passes whether or not the
ownership check exists — the same trap `CLAUDE.md` records for tool-availability
tests. Assert the *wrong* owner is refused.

- [ ] **Step 2: Implement**

```python
async def offer_share(
    *,
    owner: str,
    object_id: PydanticObjectId,
    to_profile_id: str,
    message: str | None = None,
) -> Share:
    """Offer one of your objects to another profile.

    Resolves the target through the same `deps.resolve_owner` the header goes
    through, so "share with profile X" and "act as profile X" agree on what a
    valid profile id is, including the `"local"` special case for the adopted
    profile -- whose `owner_id()` is not its `str(id)`.
    """
    obj = await object_service.get_object(object_id, owner=owner)  # owner-scoped: 404 for others

    to_owner = await resolve_owner(to_profile_id)
    if to_owner == owner:
        raise ValidationError("Cannot share a file with yourself")

    if obj.status is not ObjectStatus.READY:
        raise ConflictError(
            f"Only a ready file can be shared; this one is {obj.status.value}.",
            details={"status": obj.status.value},
        )
    if not obj.blob_sha256:
        raise ConflictError("This file has no stored content yet")

    share = Share(
        from_owner=owner,
        to_owner=to_owner,
        source_object_id=obj.id,
        name=obj.name,
        size=obj.size,
        blob_sha256=obj.blob_sha256,
        message=message,
    )
    try:
        await share.insert()
    except DuplicateKeyError as e:
        raise ConflictError(
            f"{obj.name!r} is already offered to that profile and awaiting a response",
            details={"object_id": str(obj.id)},
        ) from e
    return share
```

Note what is *not* checked: whether the recipient already has an object with
this digest. They may well — dedup is global — and refusing would make the
common "I already have this, but not in this project" case unshareable. The
recipient sees the name and can decline.

Also not checked: that the source is not `EXTERNAL`. The design allows it; the
warning is the UI's job (#50), and the offer response carries `storage` so the
UI can render it.

- [ ] **Step 3: Verify**

---

## Task 4: Listing — inbox and outbox

**Files:** modify `share_service.py`; test `backend/tests/services/test_share_listing.py`

- [ ] **Step 1: Write the failing test**

The mandatory negative: create shares in both directions between three
profiles, and assert A's inbox contains only shares *to* A and A's outbox only
shares *from* A. A test with two profiles and one share passes on a query that
ignores direction entirely.

- [ ] **Step 2: Implement**

```python
async def list_inbox(*, owner: str, state: ShareState | None = ShareState.OFFERED) -> list[Share]:
    query = {"to_owner": owner}
    if state is not None:
        query["state"] = state.value
    return await Share.find(query).sort("-created_at").to_list()


async def list_outbox(*, owner: str) -> list[Share]:
    """Every state, not just pending: the sender's view is a history, and an
    accepted share is the row they most want to see."""
    return await Share.find({"from_owner": owner}).sort("-created_at").to_list()
```

Neither touches `Share.owner`. Add a comment saying so — the field is inherited
and reading it here is always a bug.

- [ ] **Step 3: Verify**

---

## Task 5: `accept_share` — the materialization cascade

**Files:** modify `share_service.py`; test `backend/tests/services/test_share_accept.py`

The largest task. Read the design note's "Materializing the copy" first.

- [ ] **Step 1: Write the failing test**

```python
async def test_accepting_creates_a_second_object_on_the_same_blob(): ...
async def test_the_copy_clears_cross_partition_provenance(): ...
async def test_the_copy_records_shared_from(): ...
async def test_a_shared_bam_brings_its_bai_repointed_at_the_new_parent(): ...
async def test_shared_paired_reads_point_at_each_other_not_at_the_source(): ...
async def test_accepting_twice_is_refused(): ...
async def test_a_wrong_recipient_cannot_accept(): ...
async def test_destination_project_must_belong_to_the_recipient(): ...
async def test_counters_move_on_the_destination_project(): ...
```

Two of these carry the weight:

- `test_the_copy_clears_cross_partition_provenance` — assert `derived_from ==
  []` and `produced_by_job is None`. A naive `obj.model_copy()` passes every
  other test in this file and fails only this one.
- `test_a_shared_bam_brings_its_bai_repointed_at_the_new_parent` — assert the
  copied BAI's `sidecar_of` equals the **new** BAM's id, not the source's.
  Copying the field verbatim leaves a sidecar pointing into the sender's
  partition, where `list_sidecars` (owner-scoped) will never find it: the
  recipient sees a BAM with no index and a BAI that belongs to nothing.

- [ ] **Step 2: Implement**

```python
async def accept_share(
    *,
    owner: str,
    share_id: PydanticObjectId,
    project_id: PydanticObjectId | None = None,
) -> DataObject:
    """Materialize a shared file into the recipient's library.

    `project_id` is the recipient's choice of destination; omitted, the copy
    lands in a lazily created "Shared with me" project (#50 renders it; the
    creation lives here because acceptance cannot complete without a project).
    """
    share = await _load_for_recipient(share_id, owner=owner)
    if share.state is not ShareState.OFFERED:
        raise ConflictError(
            f"This share is already {share.state.value}",
            details={"state": share.state.value},
        )

    source = await DataObject.get(share.source_object_id)
    if source is None or source.owner != share.from_owner:
        raise ConflictError("The sender deleted this file before it was accepted")

    if project_id is None:
        project = await _shared_with_me_project(owner)
    else:
        project = await project_service.get_project(project_id, owner=owner)  # 404 for others

    # Build the whole set before writing anything: the parent, its sidecars,
    # and its mate (with the mate's own sidecars). Sharing a BAM without its
    # BAI hands over a file most tools refuse to open, and sharing one half of
    # a pair produces a file the pairing UI reports as half of a pair that does
    # not exist.
    group = await _share_group(source, owner=share.from_owner)
    ...
```

Sketch of the two helpers, which is where the care goes:

```python
async def _share_group(source: DataObject, *, owner: str) -> list[DataObject]:
    """Source, its sidecars, its mate, and the mate's sidecars -- deduplicated,
    source first so the parent id exists before a sidecar needs to point at it.
    """


def _copy_for(
    src: DataObject, *, owner: str, project_id: PydanticObjectId, share: Share, now: datetime
) -> DataObject:
    """One recipient-side copy of one source object.

    Carried over: name, size, format, facts, metadata, tags, role, source, and
    `user_touched`. That last one is not obvious and is deliberate -- it records
    which fields a *person* set, and dropping it would let the recipient's next
    re-ingest overwrite a role the sender chose and the recipient never cleared.

    Cleared: `derived_from` and `produced_by_job`, which name objects and jobs
    in the sender's partition; the recipient's owner-scoped lookups resolve
    neither, so carrying them forward would render provenance links that 404.
    `shared_from` replaces both.

    Rewritten by the caller once ids exist: `sidecar_of`, `mate_object_id`.
    """
    return DataObject(
        project_id=project_id,
        owner=owner,
        name=src.name,
        size=src.size,
        status=ObjectStatus.INGESTING,  # attach_existing_blob_to_object sets READY
        format=src.format,
        facts=src.facts,
        metadata=src.metadata,
        tags=list(src.tags),
        role=src.role,
        user_touched=list(src.user_touched),
        sidecar_role=src.sidecar_role,
        read_number=src.read_number,
        source=src.source,
        shared_from=SharedFrom(
            object_id=src.id, owner=src.owner, share_id=share.id, at=now
        ),
    )
```

Then the write, all of it in one transaction:

```python
    async with await get_client().start_session() as session:
        async with session.start_transaction():
            # Insert first so ids exist, then rewrite the intra-group pointers,
            # then take the blob references.
            ...
```

**Why one transaction and not three sequential `attach` calls:** a cascade that
creates the BAM and then fails before its BAI leaves the recipient holding a
broken file beside a *correct* blob ledger — nothing is inconsistent enough for
any check to notice, and the only symptom is a BAM that will not open. Pass the
session into `attach_existing_blob_to_object` (Task 2 accepts it for exactly
this).

Then, **outside** the transaction — matching `ingest_local_file`, which also
bumps counters outside the blob transaction:

```python
    await project_service.bump_counters(
        project.id, objects=len(copies), total_bytes=sum(c.size for c in copies)
    )
```

And the destination project helper:

```python
async def _shared_with_me_project(owner: str) -> Project:
    """Lazily create the recipient's "Shared with me" project.

    An ordinary Project, not a special kind: it inherits listing, counters,
    cascade delete and pipeline launch with no second case anywhere. Created on
    first acceptance rather than at profile creation -- an empty "Shared with
    me" in every new library is clutter that explains nothing.

    `uniq_sibling_name` is (owner, parent_id, name), so two profiles can each
    have one. The ConflictError catch handles two acceptances racing.
    """
```

- [ ] **Step 3: Verify** — the new file, then the whole `tests/services` directory.

---

## Task 6: `decline_share` and `revoke_share`

**Files:** modify `share_service.py`; test `backend/tests/services/test_share_revoke.py`

- [ ] **Step 1: Write the failing test**

```python
async def test_revoking_a_pending_offer_withdraws_it(): ...
async def test_revoking_an_accepted_share_is_refused(): ...
async def test_revoking_an_accepted_share_leaves_the_recipient_object_intact(): ...
async def test_only_the_sender_can_revoke(): ...
async def test_only_the_recipient_can_decline(): ...
async def test_a_declined_offer_can_be_re_offered(): ...
```

`test_revoking_an_accepted_share_leaves_the_recipient_object_intact` is the
one that encodes the policy decision. Assert both that the call raises *and*
that the recipient's object and the blob's refcount are unchanged — a raise
that happens after a partial delete passes the first half alone.

- [ ] **Step 2: Implement**

```python
async def revoke_share(*, owner: str, share_id: PydanticObjectId) -> Share:
    """Withdraw an offer the recipient has not accepted.

    An *accepted* share cannot be revoked, and the refusal is explicit rather
    than a silent no-op. Once materialized, the copy is an ordinary DataObject
    in the recipient's library: deleting it would let one profile destroy
    another's file -- and anything derived from it -- through an
    unauthenticated endpoint, and would break the one-owner-per-object
    invariant every owner-scoped service is built on.

    It would also buy nothing. The API is unauthenticated by design (see the
    profiles spec, "Passwords are a speed bump"): a recipient who wanted the
    file back could send the sender's profile id in a header. Withdrawing an
    unaccepted offer is a real action; deleting an accepted copy only looks
    like one. See the design note, "Offer, accept, decline, revoke".
    """
    share = await _load_for_sender(share_id, owner=owner)
    if share.state is ShareState.ACCEPTED:
        raise ConflictError(
            "This file was already accepted and now belongs to the other "
            "profile. Ask them to delete their copy.",
            details={"state": share.state.value},
        )
    ...
```

- [ ] **Step 3: Verify**

---

## Task 7: The API surface

**Files:**
- Create: `backend/app/api/v1/shares.py`
- Modify: `backend/app/api/v1/schemas.py`, `backend/app/api/v1/__init__.py`
- Test: `backend/tests/api/test_shares_api.py`

- [ ] **Step 1: Write the failing test**

Route-level tests asserting the status codes: 201 on offer, 409 on a duplicate
pending offer, 409 on revoking an accepted share, 404 on offering someone
else's object, 400 on a missing `X-BioFlow-Profile` header.

That last one matters: **every route here takes `OwnerDep`**, unlike
`profiles.py`, which is deliberately outside the partition because it is what a
client calls before it has a profile. Sharing always has a caller with a
profile.

- [ ] **Step 2: Implement**

Routes:

| Method | Path | Meaning |
|---|---|---|
| `POST` | `/shares` | Offer `{object_id, to_profile_id, message?}` |
| `GET` | `/shares/inbox` | Offers to me |
| `GET` | `/shares/outbox` | Offers I made, all states |
| `POST` | `/shares/{share_id}/accept` | `{project_id?}` |
| `POST` | `/shares/{share_id}/decline` | |
| `DELETE` | `/shares/{share_id}` | Revoke — un-accepted only |

`ShareOut` is hand-enumerated like every other response model in `schemas.py`
(a generic serializer is what publishes a `password_hash` one day). It carries
the denormalized `name`/`size`, the state, `from_profile`/`to_profile` display
blocks so the inbox needs no second call, and `storage` so #50 can warn on an
external source.

Register in `api/v1/__init__.py` after `profiles`. Order does not matter here —
no route collides with a path parameter the way `search`/`objects` do.

- [ ] **Step 3: Verify**

---

## Task 8: Publish the offer event, and name the seams #50/#51 will use

**Files:** modify `share_service.py`; test `backend/tests/services/test_share_events.py`

- [ ] **Step 1: Write the failing test**

Assert `queue.publish_event` is called with `owner=to_owner` — the *recipient's*
channel. Publishing to the sender's channel is the natural typo and produces a
notification that reaches everyone except the person who needs it.

- [ ] **Step 2: Implement**

```python
    await queue.publish_event("share.offered", {"share_id": str(share.id), ...}, owner=to_owner)
```

`/events` is already partitioned per profile, so this needs no new plumbing.

- [ ] **Step 3: Leave the seams explicit**

Three things belong to #51 and must not be half-built here. Add a short comment
at each site saying so, rather than leaving a future reader to wonder whether
it was forgotten:

- **Report directories.** `accept_share` does not copy `qc_reports/` etc. A
  shared BAM arrives with no QC report. Comment at the end of the cascade
  naming #51 and the reason a `shared_from` fallback is the wrong fix (it
  breaks when the sender deletes, because `delete_object` removes those dirs).
- **Stale offers.** `accept_share` already refuses when the source is gone
  (Task 5). What is missing is the sweep that marks such offers stale for the
  inbox rather than leaving them to fail on click.
- **Profile deletion.** Nothing deletes `Share` documents naming a deleted
  profile. Comment in `profile_service.delete_profile`.

- [ ] **Step 4: Verify**

---

## Task 9: Full suite, real-data check, and close-out

- [ ] **Step 1: Full suite from the worktree**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Read the count, not the exit code. Compare against the baseline from "Before
you start": it should be baseline + the new tests, with nothing lost.

- [ ] **Step 2: Check the cascade against the real database, not only fixtures**

`CLAUDE.md` records this specifically ("Check a rule against the real database,
not only its unit tests"): the Actions-tab rules passed a green suite while
being wrong about real objects, because the fixtures were hand-built to look
the way the rules expected. The same risk applies here — `_share_group` is a
guess about what a real BAM's sidecar set looks like.

After merging, from the main repo root:

```bash
docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models import DataObject
async def main():
    await connect_to_mongo()
    bam = await DataObject.find_one({'format.kind': 'bam'})
    sidecars = await DataObject.find({'sidecar_of': bam.id}).to_list()
    print(bam.name, [(s.name, s.sidecar_role) for s in sidecars])
asyncio.run(main())
"
```

Confirm the group a real alignment output produces is what the cascade expects
— particularly that a BAM's BAI is a `sidecar_of` and not something else, and
that a reference's aligner index sidecars (eight files for STAR) come through
as a set.

- [ ] **Step 3: Merge and push**

Per `CLAUDE.md`: once green, commit, merge to `main`, and push. Keep the
mechanical parts separable from the behavioural ones.

- [ ] **Step 4: Close out the issue and the backlog**

- Comment on #25 with what shipped and, specifically, **what this
  implementation did differently from this plan** — `CLAUDE.md` notes that
  every entry closed so far departed from its own plan somewhere, and that
  delta is the most valuable sentence in the write-up.
- Move #25 to closed; leave #50 and #51 open.
- `docs/TODO.md`'s "Sharing between profiles" entry stays open — two slices
  remain — but update its status paragraph to say the offer/accept/revoke
  backend landed and name the commit.
- Do **not** trust this plan's checkboxes as evidence of completion. Nothing
  ticks them automatically; verify against the code.

---

## Traps, collected

Every one of these is silent — no exception, no failing test unless you write
the one that catches it.

1. **`attach_blob_to_object` nulls `observed_mtime`.** Task 2 exists entirely
   for this. Reusing the placement path for a share degrades external-blob
   drift detection to size-only and lies about `last_verified_at`.
2. **A copied `sidecar_of` points into the sender's partition.** The recipient
   gets a BAM with no index and an orphan BAI; `list_sidecars` is owner-scoped
   and will never connect them.
3. **`Share.owner` is inherited and meaningless.** Any query that reads it
   instead of `from_owner`/`to_owner` returns nothing, or worse, everything.
4. **A `Share` missing from `ALL_MODELS` has no indexes**, so
   `uniq_pending_offer` does not exist and duplicate offers go through.
5. **The adopted profile's `owner_id()` is `"local"`, not `str(id)`.** Resolve
   target profiles through `deps.resolve_owner`, never `str(profile.id)`.
6. **Publishing the offer event to the sender's channel** notifies everyone
   except the recipient.
7. **A test that shares A→B and then asserts B can see it** passes whether or
   not the ownership checks work. The tests that matter assert the *refusal*.
