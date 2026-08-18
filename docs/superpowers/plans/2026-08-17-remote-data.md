# Remote (non-local) data — implementation plan

Issue: [#523](https://github.com/syntheticgio/bioflow/issues/523)
Spec: [`docs/superpowers/specs/2026-07-31-remote-data-design.md`](../specs/2026-07-31-remote-data-design.md)

The spec is sound and this plan does not relitigate it. What this plan adds is
the parts the spec could not know: what the tree looks like on 2026-08-17,
which of its assumptions still hold, and three traps that only show up when you
go to write the code.

Per CLAUDE.md, **nothing ticks these checkboxes.** Verify against the code, not
against this file.

## Decisions settled on 2026-08-18

Three things the plan left implicit, resolved before implementation began.

**An offloaded object keeps `ObjectStatus.READY`.** `locality` carries the
remoteness; `status` continues to mean what it always meant. This is what makes
Stage 3's regression guard pass rather than merely be written: the reference
picker and the Actions rules both filter on `READY`, and the Actions filter is
at the *query* layer, so a non-`READY` offloaded object would be excluded in the
database and never reach Python. The refusal therefore comes from exactly one
new place -- the remote check in `_resolve_readable` -- and not from `status` at
all. `_check_fastq_ready` will pass an offloaded object; that is intended, and
the chokepoint catches it a moment later.

**v1 offloads SRA runs only.** `metadata.sra_run` is the one re-fetch key
verified to exist on downloaded objects (`services/sra_service.py:56`). The
plan's earlier mention of an "assembly accession" was never checked against the
tree. `release_bytes_for_object`'s precondition is therefore `metadata.sra_run`
is present and `parse_accession` recovers it; everything else refuses. Genome
assemblies are a follow-up, and need their own look at what accession an
assembly object actually stores and whether a single component can be re-fetched.

**Line numbers in this file drifted between 2026-08-17 and 2026-08-18.** See
the corrections below. Re-grep anyway.

## What was verified before writing this

- `grep -rn "Locality\|RemoteSource\|locality\|fetch_remote" backend/app frontend/src`
  → zero hits. Nothing is built.
- `_resolve_readable` (`services/pipeline_service.py:162` as of 2026-08-18) is still the single
  chokepoint the spec describes, and still branches on `BlobStorage.EXTERNAL`
  exactly as quoted.
- It has **57 call sites** (55 in `pipeline_service.py` plus its definition,
  two in `api/v1/pipelines.py`). This is the single most important number in the
  plan: it is why the fetch gate goes in the chokepoint and why no handler or
  runner is touched.
- `metadata.sra_run` is stored on downloaded runs
  (`services/sra_service.py:56`), and `metadata/sra.py:187` `parse_accession`
  recovers `ERR`/`SRR`/`DRR` accessions at a word boundary. The re-fetch key
  exists.
- `blob_service.GC_GRACE` is 1 hour (`:24`) and `gc_candidates` (`:263`) reaps
  on refcount, unchanged.

## Four traps

These are the reason this is a plan and not just "follow the spec".

### 1. `detach_blob_from_object` cannot be reused — it deletes the object

The obvious move for "drop the bytes" is
`blob_service.detach_blob_from_object` (`:195`), whose docstring even says
"decrement its blob's refcount". But its first transaction statement is
`db.objects.delete_one(...)`, and it also decrements
`counters.object_count`. It implements *delete the object and release its
blob*, which is the opposite of this feature: offloading must keep the object
and release only the bytes.

**Write a new `blob_service.release_bytes_for_object(object_id)`** that
decrements the blob refcount and `counters.total_bytes`, leaves the object row
and `counters.object_count` alone, and clears `blob_sha256`. Do not "fix"
`detach_blob_from_object` to take a flag — its delete-path callers are correct
as they stand, and a boolean that flips whether a function deletes a row is
exactly the kind of call site nobody can read later.

### 2. `ObjectStatus.MISSING` already means "the blob went away"

`models/object.py:24` defines `MISSING = "missing"  # underlying blob went
away`. An offloaded object has, literally, no blob, so the concern is that
whatever sets `MISSING` starts firing on offloaded objects and every offloaded
file reads as broken.

Verified on 2026-08-18, the risk is **narrower than it first looks, and the
mitigation is a specific ordering requirement rather than a broad audit.** Both
writers live in `verify_blobs` (`queue/handlers.py`) and both key off the
digest:

- `:688` sets objects to `MISSING` via `DataObject.blob_sha256 == blob.id`.
- `:753` heals back to `READY` via the same digest plus `status == MISSING`.

Because `release_bytes_for_object` **clears `blob_sha256`**, an offloaded object
matches neither query. It cannot be marked `MISSING` and it cannot be
spuriously healed. That is the mechanism -- not luck -- and it is what the test
must pin.

The real hazard is **ordering inside `release_bytes_for_object`**. Clearing
`blob_sha256` and decrementing the refcount must happen in one transaction. If
the refcount drops first and the digest is cleared second, a `verify_blobs` pass
landing in between sees an object still pointing at a blob whose bytes are on
their way out, which is precisely the `MISSING` mislabel this trap warns about.

- [ ] Test: an offloaded object does not become `MISSING` when `verify_blobs`
      runs against a store where its former blob has been reaped.
- [ ] Test: a genuinely-lost blob still marks its objects `MISSING` -- the
      permissive direction, which must keep working.

### 3. `blob_sha256 is None` currently means "not ready yet"

`_resolve_readable`'s **first** statement is:

```python
if obj.blob_sha256 is None:
    raise ValidationError(f"{obj.name!r} has no stored content yet (status={obj.status.value})")
```

Since a remote object has no blob, it hits this branch first and gets an error
message about hashing that is actively misleading. The remote check must go
*above* this, so the remote case is answered before the not-yet-ingested case.

### 4. One of the spec's two "filter collections" sites has changed shape

The spec's central argument rests on two sites that filter *collections* on
`ObjectStatus.READY`. Both still exist, but only one is where the spec says,
and the other is no longer a list comprehension at all:

- **The reference picker** is now at `api/v1/pipelines.py:1843` (spec said
  529; 1842 on 2026-08-17). The code is otherwise verbatim what the spec quotes, including the
  `and o.status is ObjectStatus.READY` line.
- **The Actions rules** no longer filter in Python. `suggestion_service.py`
  passes `status=ObjectStatus.READY` **into the query** at `:2047` and `:2132`
  (spec said a comprehension at 654). It also has nine separate
  `if obj.status is not ObjectStatus.READY` single-object guards.

This does not weaken the spec's conclusion — it strengthens it. A query-layer
filter excludes a remote object *in the database*, so it would not even reach
Python to be rescued, and the failure would still be silent. But it does mean
the fix site is a query argument, not a comprehension, so anyone following the
spec's line numbers literally will patch the wrong thing.

**Re-grep before editing.** Do not trust any line number in the spec or in this
plan; they were accurate on the date written and the file moves.

## Stage 1 — Model

- [x] `Locality` StrEnum (`LOCAL`, `REMOTE`) in `models/object.py`, beside
      `ObjectStatus`.
- [x] `RemoteSource` model: accession, component, size reported by the source.
- [x] `DataObject.locality: Locality = Locality.LOCAL` and
      `remote_source: RemoteSource | None`. Defaulting to `LOCAL` is what makes
      every existing object correct without a migration.
- [x] Test: an object loaded from a document with no `locality` key reads back
      as `LOCAL`.
- [x] Test: offloading does not change `status` — it stays `READY`.

## Stage 2 — The refusal gate (before any fetching)

Ship the refusals before the fetch. A half-built feature that refuses cleanly
is safe; one that silently returns a path to bytes that are not there is not.

- [x] `_resolve_readable` raises for `locality is REMOTE`, **above** the
      `blob_sha256 is None` check (trap 3), naming the fetch action.
- [x] Re-ingest / re-detect-format refuse, in the style `_check_fastq_ready`
      already uses.
- [ ] The sequence viewer and interactive reads refuse. **Deferred, and the
      plan's wording was wrong:** there is no sequence-viewer or interactive-
      reads route in `api/v1/` to patch — grepping for one on 2026-08-18 found
      nothing. The byte-reading routes that do exist are download, reingest,
      infer-molecule-type, and the DE results table; all four now call
      `object_service.check_local`. If a viewer route lands later it inherits
      the refusal from `_resolve_readable` only if it goes through the
      chokepoint, so this box stays open as a reminder to check that.
- [x] Tests, all negative per the spec: each operation refuses a remote object
      with its own message. Assert the refusal direction, not the permissive
      one — per CLAUDE.md, a permissive assertion passes whether or not the
      mechanism did anything.

## Stage 3 — The regression guard

This is the one test the spec calls out as the one that would have caught the
`REMOTE`-status design being wrong. Write it before the UI exists.

- [x] A remote object **still appears** in the reference picker
      (`api/v1/pipelines.py:1843`) and in Actions-tab suggestions
      (`suggestion_service.py:2099` and `:2184` — a query argument, not a
      comprehension; see trap 4).
- [x] Per CLAUDE.md, also check both against a **real project** via
      `docker compose exec api python -c "..."`, not only fixtures — these
      exact rules previously passed a green suite while being wrong about real
      objects.

## Stages 1-3 landed 2026-08-18

Commits `fe16a0fb` (model), `c7594ebc` (refusals + regression guard),
`ff2e4c1f` (accession fallback). Full suite 5457 passed, 7 skipped.

One thing the real-project check caught that the fixtures could not: every
SRA-backed object in the live database carries its accession in
`metadata.sra_run` and nothing in `remote_source`, because `remote_source`
is only written at offload time. The refusal therefore named no accession
on any real object until a fallback was added. The unit tests missed it by
hand-building a `RemoteSource` — fixtures shaped the way the code expects,
which is the exact failure mode CLAUDE.md describes for the suggestion rules.

Also worth carrying into Stage 4: the offload candidate in the real project
is `DRR1066343_1.fastq`/`_2.fastq`, 3.96 GB each, both already trimmed. That
is the motivating workflow with real bytes attached, and it is the pair to
test Stage 4 against.

## Stage 4 — Dropping the bytes

- [x] `blob_service.release_bytes_for_object` per trap 1.
- [x] `object_service` action: flip `locality` to `REMOTE`, populate
      `remote_source` from `metadata.sra_run`, release the bytes, and **leave
      `status` at `READY`** per the decisions above. Refuse when no
      `sra_run` is recoverable — that is the whole precondition of the
      feature, and in v1 an assembly is one of the things it refuses.
- [x] API endpoint in `api/v1/objects.py`.
- [x] Audit `MISSING` writers/readers per trap 2.
- [x] Tests: `qc_reports/` intact, facts unchanged, `derived_from` intact on
      children, `object_count` **unchanged** while `total_bytes` drops, GC
      reclaims after `GC_GRACE`.

## Stage 4 landed 2026-08-18

Commit `d07e7c28`. Full suite 5472 passed, 7 skipped.

Two things worth carrying into Stage 5:

**`ObjectOut` did not serialize `locality`.** The offload itself worked; the
field simply never reached the client, so the frontend could not have badged
anything. It surfaced only because the endpoint test asserted on the
response rather than on the database. Stage 6 would have found it as "the
badge never appears" with the backend looking correct.

**The real-data check ran against a copy, not the real object.** Offloading
the user's actual 3.96 GB FASTQ would release bytes nobody asked to lose, so
the probe copies the real document into a throwaway project, offloads the
copy, and restores the refcount afterwards. It still exercised a document
with real shape -- 41 fact keys -- which is the part a fixture cannot
provide. Stage 5's fetch check should follow the same pattern: a real
document, a disposable copy.

## Stage 5 — The fetch job

- [ ] `fetch_remote` handler in `queue/pipeline_handlers.py`. Model it on
      `download_sra_run` (`queue/sra_handlers.py:52`): `HandlerMode.SUBPROCESS`,
      `JobClass.USER_INTERACTIVE`, `max_attempts=3` (a failed download is
      usually the network, not the input).
- [ ] The applier re-attaches the blob to the **existing** object and flips
      `locality` back to `LOCAL`. This is where it differs from
      `_apply_sra_download`, which creates new objects — reuse the ingest path,
      not the object-creation path.
- [ ] Launch paths enqueue `fetch_remote` and set `depends_on`, reusing the
      `build_index` → `align_reads` pattern at
      `services/pipeline_service.py:1967-2092`.
- [ ] `dedup_key` so two pipelines needing one remote file produce **one** job.
- [ ] Tests: `_resolve_readable`'s dedup; a failed fetch fails the dependent
      naming the fetch; refetching content already in the store deduplicates to
      the existing blob rather than creating a second.
- [ ] `docker compose restart worker` before testing — per CLAUDE.md the worker
      does not hot-reload, and this stage changes a handler.

## Stage 6 — Frontend

- [ ] `api/types/object.ts` (not `api/types.ts`, which does not exist):
      `locality`, `remote_source`.
- [ ] Badges in `ProjectExplorer.tsx` / `FileHeadline.tsx`, computed not
      stored: `Local` + `NCBI` when downloaded, `NCBI` alone when offloaded.
- [ ] Drop-bytes action in `ManageFile.tsx`, with the confirmation dialog
      naming children that list this object in `derived_from` — they stay local
      and valid; the user is told because a parent's bytes disappearing is
      worth knowing, not because it is a problem.
- [ ] Download-size warning in `AlignDialog.tsx` ("this will download ~3.2 GB
      first").
- [ ] `styles.css`.
- [ ] Manual verification at localhost:5273 via `./ops/worktree-up.sh` (not
      plain `docker compose` from a worktree), then `--down` when finished.

## Deliberately out of scope

Carried from the spec: non-NCBI remote sources (S3/HTTP), automatic eviction
(no LRU, no watermark — the governor's disk thresholds are already unreliable
under Docker Desktop), partial/streaming reads, and remote sidecars.

The `NcbiDownloadDialog` "keep it remote at download time" checkbox is
**deferred to a follow-up**. It is a genuinely separate half — never-fetched
rather than fetched-then-released — and the offload path above is what the
motivating workflow (trim, then reclaim the raw FASTQ) actually needs. Splitting
it keeps the first PR reviewable.

## Sequencing note

Stages 2 and 3 before 4 and 5 is deliberate: the refusals and the
picker-visibility guard are what make a partially-landed feature safe. Stage 5
is the only stage that touches a queue handler, so it is the only one that
needs the worker restart.
