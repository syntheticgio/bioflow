# Profiles

Segregate the library into named profiles chosen at startup, so several people
sharing one machine each see their own projects, files, and runs. Not a security
mechanism: profiles are an organizational boundary, and the optional password
exists to stop someone entering the *wrong* profile by accident, not to keep
them out of it.

## Problem

BioFlow has one library. Everything a user creates lands in the same explorer
alongside everything anyone else created. Two people sharing a machine — or one
person keeping teaching material apart from real analysis — have no way to
separate their work.

The data model has been waiting for this from the beginning.
`TimestampedDocument` (`models/base.py`) gives every collection an `owner`
field:

```python
class TimestampedDocument(Document):
    """Every collection carries owner + timestamps + schema_version.

    `owner` is unused today (single-user, no auth) but present from the start so
    that adding accounts later is a code change, not a data migration.
    """

    owner: str = "local"
```

That promise holds up. `owner` is written on every document and indexed on two
collections already — `uniq_sibling_name` and `recent` on `projects`
(`models/project.py:41`), `by_status` on `objects` (`models/object.py:221`) all
lead with it. Every one of the eleven references to `owner` in the backend is a
write, an index definition, or a comment. **Nothing reads it as a filter.**
`project_service.list_projects` (`services/project_service.py:62`) queries on
`parent_id` and `archived` and never mentions the owner.

So this feature is not a schema change. It is the filter the schema was shaped
for.

## Storage: why blobs stay global

The original note asked whether storage should nest under a profile directory —
`<profile>/objects/...` with the current layout beneath it. It should not, and
the storage layer says why.

Files live at `objects/ab/abcdef...`, built by `blob_rel_path`
(`storage/paths.py`) from the SHA-256 digest and nothing else. The path *is* the
content hash. There is no room for a profile in it without abandoning
content-addressing altogether.

That addressing is load-bearing. `models/blob.py` keeps refcounts in their own
collection specifically so content can be shared:

> One document per unique SHA-256. This is deliberately a separate collection
> from `objects`: refcounting must be atomic and independent of how many objects
> point at a piece of content. Putting a refcount on `objects` makes
> deduplication unrepresentable.

Nesting per profile would store the same *T. brucei* reference twice when two
profiles download it — duplicating exactly the files most likely to be shared,
since reference genomes and NCBI assemblies are the same bytes for everyone. It
would also make the planned cross-profile sharing feature impossible rather than
nearly free.

**The cut is therefore between layers, not directories:**

| Layer | Scope | Reason |
|---|---|---|
| `projects`, `objects`, `runs`, `jobs`, `schedules` | Per-profile | What a user means by "my stuff" |
| `blobs`, `objects/` on disk | **Global** | Content-addressed and refcounted |
| `organisms` | Global | Cached NCBI taxonomy; reference data, not user data |
| Tool probes, `TOOL_META` | Global | Describes one container's installed software |

`qc_reports/`, `bam_stats/` and `vcf_stats/` sit outside `objects/` but are keyed
by object id, so they inherit partitioning from the object they describe and need
no change.

Sharing between profiles, when built, becomes a second `DataObject` with a
different `owner` pointing at the same digest. No bytes copied; the existing
refcount governs lifetime.

## The Profile model

A new collection, deliberately outside the partition — it is the thing that
defines partitions.

```python
class Profile(TimestampedDocument):
    username: str                      # unique, required
    password_hash: str | None = None   # None means no password
    email: str | None = None           # optional, for future notifications
    display: ProfileDisplay            # emoji + colour
    details: dict                      # name, institution, research areas...
    last_used_at: datetime | None = None
```

The profile's `ObjectId`, stringified, becomes the `owner` value on every
document it creates.

**Emoji are safe.** The original note worried that emoji in a profile name would
break the filesystem, and proposed a numeric id to map paths. The concern is
real but does not apply: `owner` never becomes a path component, because storage
stays digest-addressed. Display names may contain anything Unicode allows. A
stable id is still wanted — so renaming a profile does not rewrite every
document — and an `ObjectId` supplies one without the `findAndModify` counter a
sequential integer would need.

`username` is unique and is what the picker matches on; `display.emoji` and
`display.colour` are cosmetic.

### First boot adopts `"local"`

Every existing document has `owner: "local"`. On first boot with no profiles,
the setup screen creates a profile that **claims `"local"` as its owner value**
— so the existing library belongs to it immediately and **not one document is
rewritten**.

Note what that does *not* mean. The profile's own `_id` is an ordinary
`ObjectId`; Beanie rejects a `Profile(id="local", ...)` outright with
`ValidationError: Id must be of type PydanticObjectId`, verified against the
running stack. (`Blob` does override `id` to a `str` for its digest, so a
string key is achievable — but conflating the two here would be wrong anyway.)

A profile's identity and the owner value its documents carry are separate
facts, and only the adopted profile has them differ. That difference has to be
stored, because nothing else distinguishes the adopted profile once a second
one exists:

```python
class Profile(TimestampedDocument):
    adopted_legacy_owner: bool = False

    def owner_id(self) -> str:
        """The value this profile's documents carry in their `owner` field."""
        return "local" if self.adopted_legacy_owner else str(self.id)
```

Every caller asks `profile.owner_id()` and never `str(profile.id)`. That is the
whole reason the accessor exists rather than reading `.id` at each call site.

This is worth a special case because the alternative is a data migration across
`objects`, `projects`, `runs` and `jobs`, and `docs/TODO.md` records that this
project has no migrations mechanism — and that the last index-definition change
took the API down on startup until the index was dropped by hand. A design that
needs no migration avoids that class of failure entirely. Profiles created after
the first get ordinary `ObjectId`s.

## Passwords are a speed bump

Stated plainly because the code should not imply otherwise: the password stops
someone from *accidentally* entering the wrong profile. It is not a security
boundary.

- Stored as a hash, so it is not sitting in the database in plaintext.
- Checked at profile selection, in the profile-resolution endpoint.
- **The rest of the API stays unauthenticated.** Any client can send any
  `X-BioFlow-Profile` header and get that profile's data.

This matches the repo's framing in `CLAUDE.md` — a single-user, local-only tool
for non-critical work — and the user's own description: profiles are
organizational, and in the ordinary no-password case anyone could simply select
the other profile and look. Making the API genuinely enforce identity would mean
session tokens on every endpoint, which is a different and much larger feature.

The spec says this out loud so nobody later mistakes the hash for protection.

## Request scoping

The frontend sends `X-BioFlow-Profile: <owner>` on every request. There is a
single `fetch` chokepoint — `request<T>` at `api/client.ts:66`, which already
merges a `headers` object — so this is a few lines on the frontend.

A FastAPI dependency resolves the header to an owner id and rejects an unknown
one. Service functions take `owner` as an **explicit parameter** rather than
reading a context variable: a filter someone forgets to apply then shows up as a
missing argument at the call site, instead of silently returning another
profile's data.

### Trap: dedup keys are global

`queue.enqueue` (`queue/queue.py:53`) deduplicates through a unique partial index
on `dedup_key`. When a key collides, `enqueue` returns `None` and the job is
never created.

Some existing keys embed a per-profile id and are safe by construction —
`f"sra_download:{accession}:{project_id}"` (`services/sra_service.py:144`)
differs between profiles because projects are per-profile. But any key built only
from a tool name, an accession, or a digest would let profile A's in-flight job
**silently cancel** profile B's identical request. B's work would simply never
happen, with no error.

Fix: prefix `owner` into every dedup key, and audit the existing keys as part of
the implementation rather than assuming they are all project-scoped.

### Trap: the worker has no request

`enqueue` takes no owner, and `Job` inherits `owner: str = "local"` from
`TimestampedDocument`. Left alone, **every job in the system would be attributed
to the first profile**, and every object a handler creates would land in that
profile's library regardless of who launched it.

Fix: `enqueue` gains an `owner` parameter, and the handlers that create objects —
`queue/results.py` and the pipeline handlers — propagate it from the job rather
than taking the default. This is the subtlest part of the feature and gets
explicit tests.

## UI flow

**Startup picker.** A clickable square per profile showing emoji and name, plus a
`+` square to add one. A profile with a password prompts for it; one without
enters directly.

**Auto-login.** A checkbox on the picker. When armed, the next launch skips the
picker entirely and enters the remembered profile — matching "stay logged in,
even on restarts". The picker reappears only after an explicit logout, or if the
remembered profile no longer exists.

**Add-profile modal.** Username (required, unique), password (optional), email
(optional), and an expandable **Details** section for name, institution, and
research areas.

**Profile menu in the header.** Shows the current profile's emoji and name, and
opens to *Switch profile* / *Edit details* / *Logout*. Its own menu rather than a
slot under Activity: Activity is about jobs and runs, and an identity action
placed there is both incongruous and hard to find. Having the current profile
permanently visible in the header is also what stops someone working for ten
minutes in the wrong library.

**Upload deduplication message.** `UploadCreated` already carries
`dedup_hit: boolean` (`api/types.ts:270`), set when a client's digest matches a
blob already in the store. Cross-profile dedup means this now fires for content
another profile uploaded, and an upload that completes instantly reads as a bug.
When `dedup_hit` is true the UI says the file already existed locally and has
been added to the library — framing it as success. No new plumbing; the flag is
already there.

**Profile deletion.** Refused while the profile owns any projects or objects,
reporting the counts and directing the user to delete its projects first. This
reuses `project_service.delete_project`, which already cascades to sidecars and
decrements refcounts correctly, instead of adding a second destructive path that
would have to rediscover the same rules. A cascading profile delete is a large
irreversible action behind one button, and refusing is both safer and less code.

Two consequences worth stating, since "refuse unless empty" interacts with the
adopted `"local"` profile:

- The profile holding the pre-existing library is effectively undeletable until
  that library is deleted. That is the correct outcome — it is the user's whole
  data set — but the refusal message should name the profile rather than reading
  as a generic error.
- Deleting the **last** profile is refused outright, empty or not. A BioFlow with
  no profiles would drop into the first-boot setup screen, and a first-boot
  screen that appears on an installation with existing blobs is a confusing
  state to design around.

## Testing

Backend tests run with `pytest` inside the `api` container, per `CLAUDE.md`:

```bash
docker compose exec api python -m pytest tests/ -q
```

The important tests are **negative**, and this is the discipline that decides
whether the suite is worth anything here. Creating data under one profile and
asserting that profile can see it passes whether or not the filter was applied —
the same trap `CLAUDE.md` records for tool-availability tests, where "the image
ships most tools as installed, so a test asserting a card is *available* passes
whether or not its patch worked."

So each partitioned collection gets a test that creates documents under **two**
profiles and asserts each query returns only its own. Plus:

- **First boot:** with documents already at `owner: "local"`, creating the first
  profile leaves every existing document untouched and visible.
- **Dedup keys:** the same logical job enqueued by two profiles produces two
  jobs, not one deduplicated away.
- **Worker attribution:** a job enqueued by profile B produces objects owned by
  B, not by the first profile.
- **Blobs stay global:** two profiles uploading identical content share one blob
  at refcount 2, and one deleting its object leaves the other's intact.

Frontend verification is manual at localhost:5173, per `CLAUDE.md` — there is no
headless component-testing setup and none is expected. Worth exercising against
a real profile with real data rather than a fresh one, since the interesting
cases involve the adopted `"local"` library.

Per `CLAUDE.md`, `worker` does not hot-reload; `docker compose restart worker`
is required after changing anything the queue handlers import, which this
feature does.

## What this does not include

- **Sharing between profiles.** Its own feature. This design keeps it cheap by
  leaving blobs global, but adds no share mechanism.
- **Real authentication.** Explicitly out of scope; see "Passwords are a speed
  bump".
- **Per-profile tool configuration.** One container, one set of installed tools.
- **Profile-scoped storage quotas.** No mechanism exists to attribute disk to a
  profile, and content-addressing means shared bytes have no single owner to
  charge.

## Files this touches

Backend: `models/base.py` (unchanged, but its promise is now honoured), new
`models/profile.py`, `api/v1/` (new `profiles.py` plus the resolution
dependency), `services/project_service.py`, `services/object_service.py`,
`services/run_service.py`, `services/sra_service.py`,
`services/pipeline_service.py`, `queue/queue.py` (owner on `enqueue`, dedup key
prefixing), `queue/results.py`.

Frontend: `api/client.ts` (the header, one place), `api/types.ts`, new profile
picker and add-profile modal, `components/Header.tsx`, `components/Menu.tsx`,
`components/UploadTray.tsx` (the dedup message), `App.tsx` (the picker gate).
