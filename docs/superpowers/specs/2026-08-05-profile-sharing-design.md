# Profile sharing

**Date:** 2026-08-05

Let one profile give another profile a file it already has, without copying the
bytes. Resolves the policy questions in
[epic #3](https://github.com/syntheticgio/bioflow/issues/3) so its three
execution slices can be built independently.

## The boundary: deduplication is not the work

This is the first thing to get straight, because the feature's name points at
the wrong layer.

Storage already shares bytes between profiles and has since before profiles
existed. `blobs` is a global collection keyed by SHA-256 with its own
`ref_count` (`models/blob.py`), deliberately separate from `objects` so that
content can be pointed at from more than one place:

> Putting a refcount on `objects` makes deduplication unrepresentable -- two
> objects sharing content would each believe they own it.

`storage/paths.blob_rel_path` builds a path from the digest and nothing else,
so there is no profile component to remove. The profiles design
(`2026-07-31-profiles-design.md`, "Storage: why blobs stay global") already
wrote down the consequence:

> Sharing between profiles, when built, becomes a second `DataObject` with a
> different `owner` pointing at the same digest. No bytes copied; the existing
> refcount governs lifetime.

**So a share is one document insert and one `$inc`.** Not a job, not a copy,
not a storage change. `blob_service.attach_blob_to_object` is the entire
mechanism and it already exists, already transactional. Everything below is
about metadata, placement, and what the two profiles are allowed to do to each
other -- and every hard question in this design is in that second category.

Two things follow that are worth stating before anyone estimates this:

- **A share moves no bytes, so it cannot fail slowly.** There is no progress
  to report, nothing to resume, no `dedup_key` to get wrong (see the profiles
  spec's "Trap: dedup keys are global" -- it does not apply here, because
  nothing is enqueued).
- **The GC slice is almost entirely tests.** See "Deletion and GC" below.

## What a share is

A `Share` document, plus a `DataObject` materialized on acceptance.

```python
class ShareState(StrEnum):
    OFFERED = "offered"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"   # sender revoked before acceptance


class Share(TimestampedDocument):
    from_owner: str            # sender's Profile.owner_id()
    to_owner: str              # recipient's Profile.owner_id()
    source_object_id: PydanticObjectId
    # Denormalized so the inbox renders without reading the sender's partition,
    # and so the offer still describes itself after the sender deletes their
    # copy. See "The sender may delete first".
    name: str
    size: int
    blob_sha256: str
    state: ShareState = ShareState.OFFERED
    accepted_object_id: PydanticObjectId | None = None
    message: str | None = None
```

`owner` (inherited) is meaningless on this document for the same reason it is
meaningless on `Profile`: a Share straddles two partitions and belongs to
neither. Queries use `from_owner` / `to_owner` explicitly. This is the second
collection in the codebase to sit outside the owner partition, and the reason
is the same both times -- it *defines* a relationship between partitions rather
than living inside one.

### Offer, accept, decline, revoke

- **Offer.** The sender names a recipient profile and one of their own objects.
  Ownership is checked through `object_service.get_object(..., owner=sender)`,
  so offering a file you do not own raises `NotFoundError` exactly as a missing
  one does.
- **Accept.** Materializes the copy (below) and sets `accepted_object_id`.
- **Decline.** Terminal. The recipient can be re-offered the same file; a new
  `Share` document is created rather than reviving the declined one, so the
  history stays readable.
- **Revoke.** **Withdraws an un-accepted offer only.** Revoking after
  acceptance is not possible.

That last decision is the one worth defending, because "revoke" in the epic's
title suggests otherwise.

Once accepted, the copy is an ordinary `DataObject` in the recipient's library.
Letting the sender delete it would mean one profile can destroy another
profile's file -- and any pipeline output derived from it -- through an
unauthenticated endpoint. It would also break the invariant every owner-scoped
service in the backend is built on, that an object has exactly one owner who
decides its fate.

And it would buy nothing. The API is unauthenticated by design (profiles spec,
"Passwords are a speed bump"): the recipient could re-fetch the file by sending
the sender's profile id in a header. A revoke that reaches into another
library is enforcement theater in a system that has explicitly declined to
enforce anything. Withdrawing an offer nobody has accepted yet is a real
action with a real effect; deleting an accepted copy only looks like one.

**A revoked-after-accept request is refused with a message that says why**, not
silently ignored. The UI should not offer the button at all.

## Materializing the copy

The naive implementation -- clone the document, change `owner` and
`project_id` -- produces a broken object. Four fields on `DataObject` hold
`PydanticObjectId`s that point into the *sender's* partition, and every one of
them resolves through an owner-scoped lookup that will now miss:

| Field | Naive result | Policy |
|---|---|---|
| `sidecar_of` | Sidecar arrives orphaned, or parent arrives without its index | **Cascade.** Sidecars come with the parent, re-pointed at the new copy |
| `mate_object_id` | A lone R1 that claims to have a mate | **Cascade.** Both mates share as a unit, re-paired to each other |
| `derived_from` | Provenance pointing at invisible objects | **Cleared**, replaced by `shared_from` |
| `produced_by_job` | Job in the sender's partition | **Cleared** |

The cascades are not symmetry for its own sake. A BAM without its BAI is a file
most tools refuse to open, and `delete_object` already treats sidecars as
inseparable from their parent for the mirror-image reason (an orphaned index
sits at refcount 1 forever). Sharing an R1 alone produces a file the pairing UI
reports as half of a pair that does not exist.

Provenance is not lost, it is retargeted. The copy carries:

```python
class SharedFrom(BaseModel):
    object_id: PydanticObjectId   # the sender's object, at share time
    owner: str
    share_id: PydanticObjectId
    at: datetime


class DataObject(TimestampedDocument):
    shared_from: SharedFrom | None = None
```

A typed field rather than a `metadata` key, for the reason `derived_from` gives:
metadata is user-owned and user-editable, and provenance that can be silently
retyped is not provenance.

`source` is left as the sender recorded it -- a shared file's `original_name`
is still where those bytes came from -- and `role`, `format`, `facts`, `tags`
and `metadata` copy across unchanged. `user_touched` copies too: the sender's
explicit role assignment is a fact about the file, and dropping it would let
re-ingest overwrite a role the recipient never chose to clear.

`status` is `READY` on the copy or the share is refused. Offering an object
mid-upload, mid-ingest, or `ERROR`/`MISSING` is rejected at offer time with a
message naming the state. The bytes must exist before they can be shared.

### The whole thing is one transaction

Each materialized object goes through `attach_blob_to_object`, which does the
`$inc` and the object write in a single transaction already. The cascade means
two or three of those per accept (parent, sidecars, mate). They must not
half-apply: an accept that creates a BAM and then fails before its BAI leaves
the recipient with a broken file and the blob ledger correct, which is the
worst combination to debug. The accept path opens its own session and passes it
down, or it retries idempotently keyed on `share_id`.

## Where it lands

An auto-created **"Shared with me"** project per recipient profile, with the
option to accept into a different project instead.

The alternative -- a separate "Shared" area in the explorer -- was rejected on
cost. Objects are reached through `project_id` everywhere: listing, counters,
deletion, pipeline launch, the Actions tab. A shared area outside the project
tree adds a second case to each of those. A real `Project` inherits all of it
and costs nothing: `uniq_sibling_name` is `(owner, parent_id, name)`, so an
auto-created project named "Shared with me" cannot collide with another
profile's.

Consequences, stated so they are not surprises:

- The project is ordinary. The recipient can rename it, move files out of it,
  delete it (which deletes the shared copies, correctly decrementing
  refcounts), or run pipelines against it.
- `counters.total_bytes` now counts the same bytes in two profiles. There is no
  quota mechanism and the profiles spec explicitly excludes one ("shared bytes
  have no single owner to charge"), so this is a display artifact, not an
  accounting bug. A per-project byte count answers "how much is in this
  project", which remains true.
- It is created lazily on first acceptance, not on profile creation. An empty
  "Shared with me" in every new library is clutter that explains nothing.

## Deletion and GC

**This mostly already works, and the slice is mostly tests.** Written out
because the epic asks for it explicitly and because it is easy to assume
otherwise.

- **Sender deletes their copy after a share was accepted.**
  `detach_blob_from_object` decrements the refcount from 2 to 1. The blob is
  not a GC candidate (`gc_candidates` filters `ref_count <= 0`). The
  recipient's file is untouched and keeps working. Nothing to build.
- **Both delete.** Refcount reaches 0, `GC_GRACE` (1 hour) elapses, the bytes
  are unlinked. Correct and unchanged.
- **Sender deletes with an offer still pending.** The offer is stale: its
  `source_object_id` no longer resolves. The denormalized `name`/`size`/
  `blob_sha256` on the `Share` are what let the inbox render it anyway, and
  acceptance is refused with "the sender deleted this file" rather than a
  crash. The blob may be gone entirely by then -- accept re-checks
  `find_present_blob(digest)` and refuses on a miss, which also covers a
  quarantined or missing blob.
- **Profile deletion.** Unchanged: refused while the profile owns any projects
  or objects. Its "Shared with me" project counts, which is correct -- those are
  its objects. Pending offers in either direction are deleted with the profile.

The one thing that does need building is that `Share` documents referencing a
deleted profile must not strand the inbox. Offers are deleted with the profile
on either side.

### Report directories do not follow the object

`qc_reports/`, `bam_stats/` and `vcf_stats/` are keyed by *object id*
(`object_service.remove_report_dirs`), not by digest. The recipient's copy has
a new id, so a shared BAM arrives with no QC report even though an identical
report exists on disk under the sender's id.

A lookup that falls back through `shared_from.object_id` is tempting and wrong:
it breaks the moment the sender deletes their copy, since `delete_object`
removes those directories. **The report directory is copied at share time**
(they are small -- HTML and JSON), making the recipient's copy independent.
Failure to copy is logged and does not fail the accept; a missing report is
recomputable, and refusing the share over it trades a working file for none.

Rekeying reports by digest is the principled fix and is deliberately not done
here: it touches every report writer and reader for a benefit this feature does
not need.

## External blobs are allowed, with the trap named

A `register_in_place` object points at a path BioFlow does not control and will
never unlink (`BlobStorage.EXTERNAL`). Sharing one hands the recipient a file
whose bytes can vanish without any refcount changing.

This is allowed. Profiles share one machine and one filesystem by construction,
so the recipient's access is exactly as good as the sender's -- and refusing
would block the most plausible sharing case there is, a big reference genome
someone registered from a data drive rather than uploading.

It is named here because the failure mode is confusing from the symptom: the
recipient's object goes `MISSING` through no action of their own, and nothing
in the UI says the file was never ours to begin with. The share dialog says so
when the source is external.

## Notification

The recipient learns about an offer through the existing SSE stream. `/events`
is already partitioned per profile (`api/v1/events.py`), and publishing to
`keys.events_channel(to_owner)` on offer needs no new plumbing.

The inbox itself is a badge on the profile menu in the header. That menu is
already the identity surface (profiles spec, "Profile menu in the header"), and
a share is an identity-shaped event, not an Activity-tab one -- Activity is
about jobs and runs.

## Sharing is advisory, like everything else about profiles

Stated plainly so nobody later mistakes any of this for access control:

- Any client can send any profile's id in `X-BioFlow-Profile` and read that
  profile's library. Sharing does not change what is reachable; it changes what
  is *organized into* a profile's own view.
- An offer is a piece of coordination between two people sharing a machine, not
  a grant of access.
- The password on a profile remains a speed bump.

This matches the repo's framing as a single-user, local-only tool for
non-critical work.

## Testing

Per `CLAUDE.md`, backend tests run in the `api` container from the main
checkout, or `./backend/run-worktree-tests.sh tests/ -q` from a worktree.

The tests that matter are the ones that fail when the feature is broken, which
here means asserting on the *recipient* and on the *ledger*, not on the sender:

- Accepting a share leaves the blob at `ref_count == 2` with one object under
  each owner.
- The sender deleting their copy leaves the recipient's object `READY` and the
  blob present at `ref_count == 1` -- and `gc_candidates` does not return it.
- Both deleting drives the refcount to 0 and *does* put it in `gc_candidates`
  once the grace window is passed.
- A shared BAM's copy has a BAI whose `sidecar_of` points at the *new* parent,
  and shared paired reads point at each other, not at the sender's objects.
- The copy's `derived_from` and `produced_by_job` are empty.
- Offering an object owned by someone else raises `NotFoundError`.
- Revoking an accepted share is refused and the recipient's object survives.
- Accepting an offer whose source object was deleted is refused cleanly.

Frontend verification is manual, at localhost:5173 (or 5273 from a worktree via
`./ops/worktree-up.sh`). It needs two real profiles with real data; the
interesting case is the adopted `"local"` profile on one side, since its
`owner_id()` is not its `str(id)`.

## Slices

| Slice | Issue | Contents |
|---|---|---|
| Offer/revoke | [#25](https://github.com/syntheticgio/bioflow/issues/25) | `Share` model, `share_service`, `api/v1/shares.py`, materialization with cascade + pointer sanitizing |
| Recipient visibility | (this design's child) | "Shared with me" project, inbox badge over SSE, share action in the detail panel, accept/decline UI |
| Delete + GC | (this design's child) | The refcount tests above, report-dir copy, stale-offer handling, profile-delete cleanup of `Share` documents |

## What this does not include

- **Real authentication.** Out of scope for profiles generally.
- **Sharing a whole project.** Objects only. A project share raises questions
  about nested projects and future edits that this design does not answer.
- **Any live link between the two copies.** They are independent documents
  after acceptance: renaming one does not rename the other, and editing
  metadata on one does not propagate. Only the bytes are shared.
- **Rekeying report directories by digest.** Noted above as the principled fix
  for a problem this feature works around.
- **Sharing to a profile on another machine.** Content-addressing makes this
  look adjacent; it is not. There is no transport.
