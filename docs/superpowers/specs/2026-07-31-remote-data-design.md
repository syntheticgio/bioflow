# Remote (non-local) data

Keep an NCBI download as a pointer instead of a local file, fetching the bytes
just-in-time when a pipeline actually needs them. Plus the inverse for files
already downloaded: drop the bytes of anything re-fetchable while keeping its
metadata, QC reports and provenance, so the disk space comes back without the
file leaving the library.

## Problem

Every file in BioFlow is local. Downloading a reference from NCBI means
committing its full size to the drive before knowing whether it will be used,
and the only way to reclaim that space is to delete the file — losing its
metadata, its QC reports, and its place in the provenance graph along with the
bytes.

Both halves of that are avoidable. NCBI content is re-fetchable by definition:
the accession is a durable address, and `assembly_service.launch_download`
already fetches the metadata over a separate path from the bytes.

## The core model: locality is not status

A new field on `DataObject`:

```python
class Locality(StrEnum):
    LOCAL = "local"    # the bytes are in the store
    REMOTE = "remote"  # we hold a pointer; the bytes are at the source
```

This is the central decision of the design, and it was forced by the code rather
than chosen on taste.

The obvious shape is a new `ObjectStatus.REMOTE`. It does not work.
`ObjectStatus.READY` is guarded in roughly fourteen places —
`pipeline_service.py` alone has six `if obj.status is not ObjectStatus.READY`
checks — and two of those sites *filter collections* rather than validating one
object:

```python
references = [
    o
    for o in objects
    if o.format.kind in pipeline_service.REFERENCE_KINDS
    and o.status is ObjectStatus.READY
]
```

(`api/v1/pipelines.py:529`; `suggestion_service.py:654` does the same for the
Actions tab.)

A remote file carrying a new status would fail every one of those checks. It
would disappear from the reference picker and from Actions-tab suggestions, and
be rejected by every launch path — the exact opposite of "usable on demand", and
failing *silently*: no error, the file simply is not in the dropdown.

So the two questions are separated:

- **`status`** answers *is this file understood?* — ingested, format detected,
  parsed. A remote file is `READY` in this sense.
- **`locality`** answers *are its bytes here?*

Every existing guard keeps working untouched, and remote files stay visible and
selectable.

### No blob row until first fetch

`blob_sha256` stays `None` while an object is remote, and no `Blob` document
exists for it.

This follows from the blob model rather than being a separate choice.
`models/blob.py` states that "the SHA-256 hex digest IS the primary key", and
the digest of a file that has never been downloaded is unknown — NCBI does not
publish one in the metadata we fetch. A placeholder id would make the primary
key a lie and weaken every digest-keyed lookup in the system.

On first fetch the ordinary ingest path runs and attaches a blob exactly as a
fresh download would, including deduplication if that content already exists in
the store.

### Where the pointer lives

A `RemoteSource` on the object records the accession, the component, and the
size NCBI reported, which is enough to reconstruct the fetch.

Metadata needs no special handling at all. `assembly_service.launch_download`
already resolves `meta.to_metadata()` and `meta.to_facts()` and puts them in the
job payload independently of the download, so NCBI metadata was never derived
from reading the local file. A remote object carries the same metadata a local
one would.

## Fetching: a real job, not a hidden step

`_resolve_readable` (`services/pipeline_service.py:129`) is the single function
that answers "where are this object's bytes", and it already branches on storage
mode:

```python
blob = await blob_service.find_present_blob(obj.blob_sha256)
if blob is not None and blob.storage is BlobStorage.EXTERNAL:
    if not blob.external_path:
        raise ValidationError(f"{obj.name!r} is registered in place but has no path")
    return None, blob.external_path
return obj.blob_sha256, None
```

Remote handling belongs here, not spread across the pipeline handlers. That one
chokepoint is why this feature does not touch `align_handlers.py`,
`pipeline_handlers.py`, or the runners at all.

When a launch path resolves a remote input it enqueues a `fetch_remote` job and
makes the pipeline job `depends_on` it. This is the pattern
`pipeline_service.launch_alignment` already uses for `build_index` →
`align_reads`, described in `models/job.py` as an exercised gate rather than new
machinery.

Three things follow from using a real job that an inline download inside the
handler would not give:

- The download is **visible in the queue** with its own progress. An inline
  fetch would leave the pipeline job sitting in `running` with no output for as
  long as a genome takes to transfer, which reads as hung.
- A failed fetch **fails the dependent with the fetch named as the reason**,
  rather than surfacing as a pipeline crash. `depends_on` already does this: an
  alignment whose index build died must not sit queued forever.
- Two pipelines needing the same remote file **deduplicate on `dedup_key`**
  instead of downloading it twice.

The launch dialog warns before submitting — "this will download ~3.2 GB first" —
using the size `assembly.component_availability` already reports and
`launch_download` already sums for its disk pre-flight.

On success the object becomes `locality=local` with a real blob attached, and is
thereafter an ordinary file.

## Badges

Computed from what the object already knows, not stored:

- **`Local`** — `locality is LOCAL`.
- **`NCBI`** — the object has a resolvable NCBI accession. This is already
  determined at ingest by `metadata/assembly.py` and `metadata/sra.py`, whose
  regexes are anchored at word boundaries so `MYGCA_000000001.1` does not match
  while `GCF_000002445.2_ASM244v1_genomic.fna` does.
- **`Remote`** — `locality is REMOTE` with a source that is not NCBI. Nothing
  produces this yet; it exists so the badge vocabulary does not have to change
  when another source is added.

A file therefore shows `Local` + `NCBI` when it was downloaded from NCBI or
uploaded with an accession in its name, `NCBI` alone when it is a pointer, and
`Local` alone when it is a user file with no accession. This matches the note's
description exactly, including the case of a user-uploaded file being recognised
as NCBI content.

## Dropping the bytes

An Actions entry on any object with a re-fetchable source. It decrements the
blob refcount and flips `locality` to `remote`; normal GC reclaims the space,
respecting the existing `GC_GRACE` window in `blob_service.py`.

**Everything derived survives, and this requires no work.** `qc_reports_dir`,
`bam_stats_dir` and `vcf_stats_dir` are keyed by *object id* and live outside
`objects/` deliberately — their docstrings in `config.py` explain that a
generated report "is derivative and regenerable, so content-addressing it would
buy deduplication of something that is never shared". Dropping a blob cannot
touch them. Facts, metadata, tags and `derived_from` all live on the object.

So the result is a file that keeps its QC report, its parsed facts, its NCBI
metadata and its place in the provenance graph, and has given back its disk
space. As the original note observed, this leaves *more* information than never
having downloaded it.

The confirmation dialog names any children — a trimmed FASTQ, an alignment —
that list this object in `derived_from`. They stay local and remain valid; the
user is told because a parent's bytes disappearing is worth knowing about, not
because it is a problem.

## What refuses instead of fetching

Some operations need bytes and must refuse rather than silently start a
multi-gigabyte transfer:

- **Re-ingest / re-detect format.** The entire operation is reading the file.
- **The sequence viewer and any interactive read.** Clicking a contig must not
  begin a 3 GB download.

These refuse with a message naming the fetch action, in the style
`_check_fastq_ready` already uses for a file that is not ready to trim.

Pipeline launches are the deliberate exception. There the download is a genuine
prerequisite of long-running work the user has explicitly committed to, it is
visible as its own queued job, and the dialog warned about the size first.

## Testing

Backend tests run inside the `api` container, per `CLAUDE.md`:

```bash
docker compose exec api python -m pytest tests/ -q
```

The tests that matter are **negative**, for the reason `CLAUDE.md` records about
tool-availability tests: asserting that something works in the permissive
direction passes whether or not the mechanism under test did anything.

- `_resolve_readable` **raises** for a remote object rather than returning a
  path that does not exist.
- The operations listed above **refuse** a remote object, each with its message.
- A remote object **still appears** in the reference picker and in Actions-tab
  suggestions. This is the regression a `REMOTE` status would have caused, and
  it is the one test that would have caught that design being wrong.
- Dropping bytes leaves `qc_reports/` intact, facts unchanged, and
  `derived_from` links intact on children.
- Fetching remote content whose bytes already exist in the store deduplicates to
  the existing blob rather than creating a second one.
- Two pipelines depending on one remote file produce **one** `fetch_remote` job.

Per `CLAUDE.md`, check the picker and suggestion rules against a real project
rather than only fixtures — those rules previously passed a full green suite
while being wrong about real objects, because the fixtures already looked the
way the rules expected. Frontend verification is manual at localhost:5173, and
`docker compose restart worker` is required after changing the handlers, which
this feature does.

## What this does not include

- **Non-NCBI remote sources** (S3, Google Cloud, HTTP URLs). The `Locality` and
  `RemoteSource` shapes leave room for them; no fetcher is built.
- **Automatic eviction.** Nothing drops bytes on its own — no LRU, no watermark.
  The governor's disk thresholds are already unreliable under Docker Desktop
  (see `docs/TODO.md`), so an automatic policy would be acting on a number the
  system knows to be wrong.
- **Partial or streaming reads.** A remote file is fetched whole or not at all.
- **Remote sidecars.** An index is derived from local bytes and is cheap to
  rebuild; it has no reason to live remotely.

## Files this touches

Backend: `models/object.py` (`Locality`, `RemoteSource`),
`services/pipeline_service.py` (`_resolve_readable` and the launch paths),
`services/object_service.py` (dropping bytes), `services/assembly_service.py`
and `services/sra_service.py` (the "keep remote" option),
`queue/pipeline_handlers.py` (the `fetch_remote` handler), `api/v1/objects.py`,
`api/v1/pipelines.py`.

Frontend: `api/types.ts`, `components/NcbiDownloadDialog.tsx` (the keep-remote
checkbox), `components/ProjectExplorer.tsx` and `components/FileHeadline.tsx`
(badges), `components/ManageFile.tsx` (the drop-bytes action),
`components/AlignDialog.tsx` (the download-size warning), `styles.css`.
