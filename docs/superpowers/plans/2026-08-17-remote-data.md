# Remote (non-local) data — implementation plan

Issue: [#523](https://github.com/syntheticgio/bioflow/issues/523)
Spec: [`docs/superpowers/specs/2026-07-31-remote-data-design.md`](../specs/2026-07-31-remote-data-design.md)

The spec is sound and this plan does not relitigate it. What this plan adds is
the parts the spec could not know: what the tree looks like on 2026-08-17,
which of its assumptions still hold, and three traps that only show up when you
go to write the code.

Per CLAUDE.md, **nothing ticks these checkboxes.** Verify against the code, not
against this file.

## What was verified before writing this

- `grep -rn "Locality\|RemoteSource\|locality\|fetch_remote" backend/app frontend/src`
  → zero hits. Nothing is built.
- `_resolve_readable` (`services/pipeline_service.py:156`) is still the single
  chokepoint the spec describes, and still branches on `BlobStorage.EXTERNAL`
  exactly as quoted.
- It has **56 call sites** (55 in `pipeline_service.py`, one in
  `api/v1/pipelines.py:2286`). This is the single most important number in the
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
away`. An offloaded object has, literally, no blob — so whatever code sets or
infers `MISSING` must not start firing on offloaded objects, or every offloaded
file reads as broken.

This is the same class of mistake the spec's central decision avoids
(`locality`, not a new `ObjectStatus`), arriving from the other direction:
there, a new status broke the guards; here, an *existing* status wrongly claims
an offloaded file. Audit every writer and reader of `MISSING` and make
`locality is REMOTE` win. Add the negative test: an offloaded object does not
become `MISSING`, and a genuinely-lost blob still does.

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

- **The reference picker** is now at `api/v1/pipelines.py:1842` (spec said
  529). The code is otherwise verbatim what the spec quotes, including the
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

- [ ] `Locality` StrEnum (`LOCAL`, `REMOTE`) in `models/object.py`, beside
      `ObjectStatus`.
- [ ] `RemoteSource` model: accession, component, size reported by the source.
- [ ] `DataObject.locality: Locality = Locality.LOCAL` and
      `remote_source: RemoteSource | None`. Defaulting to `LOCAL` is what makes
      every existing object correct without a migration.
- [ ] Test: an object loaded from a document with no `locality` key reads back
      as `LOCAL`.

## Stage 2 — The refusal gate (before any fetching)

Ship the refusals before the fetch. A half-built feature that refuses cleanly
is safe; one that silently returns a path to bytes that are not there is not.

- [ ] `_resolve_readable` raises for `locality is REMOTE`, **above** the
      `blob_sha256 is None` check (trap 3), naming the fetch action.
- [ ] Re-ingest / re-detect-format refuse, in the style `_check_fastq_ready`
      already uses.
- [ ] The sequence viewer and interactive reads refuse.
- [ ] Tests, all negative per the spec: each operation refuses a remote object
      with its own message. Assert the refusal direction, not the permissive
      one — per CLAUDE.md, a permissive assertion passes whether or not the
      mechanism did anything.

## Stage 3 — The regression guard

This is the one test the spec calls out as the one that would have caught the
`REMOTE`-status design being wrong. Write it before the UI exists.

- [ ] A remote object **still appears** in the reference picker
      (`api/v1/pipelines.py:1842`) and in Actions-tab suggestions
      (`suggestion_service.py:2047` and `:2132` — a query argument, not a
      comprehension; see trap 4).
- [ ] Per CLAUDE.md, also check both against a **real project** via
      `docker compose exec api python -c "..."`, not only fixtures — these
      exact rules previously passed a green suite while being wrong about real
      objects.

## Stage 4 — Dropping the bytes

- [ ] `blob_service.release_bytes_for_object` per trap 1.
- [ ] `object_service` action: flip `locality` to `REMOTE`, populate
      `remote_source` from `metadata.sra_run` / assembly accession, release the
      bytes. Refuse when no re-fetchable source exists — that is the whole
      precondition of the feature.
- [ ] API endpoint in `api/v1/objects.py`.
- [ ] Audit `MISSING` writers/readers per trap 2.
- [ ] Tests: `qc_reports/` intact, facts unchanged, `derived_from` intact on
      children, `object_count` **unchanged** while `total_bytes` drops, GC
      reclaims after `GC_GRACE`.

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

- [ ] `api/types.ts`: `locality`, `remote_source`.
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
