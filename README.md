# local-bio-pipeliner

A local, single-user web application for managing bioinformatics data files: projects,
uploads, metadata, and a priority- and load-aware background job queue. Built as the
foundation for later work — assigning rich metadata to files and launching computations
and pipelines (alignment, variant calling).

## Quick start

```bash
cp .env.example .env
make up
```

Then open <http://localhost:5173>. The API is at <http://localhost:8000/docs>.

## Required macOS setup

**You must share the external drive with Docker Desktop before the stack will work.**

Docker Desktop on macOS runs a Linux VM. It cannot see `/Volumes/*` unless you grant it
access explicitly:

1. Docker Desktop → **Settings → Resources → File Sharing**
2. Add `/Volumes/ModelExtension`
3. **Apply & Restart**

Also recommended, under **Settings → General**: enable **VirtualizationFramework** and
**VirtioFS**. The older gRPC-FUSE backend is substantially slower for the large sequential
reads this application performs.

If the drive is not shared, `/readyz` fails with an explicit message naming the problem
rather than the application silently writing into the VM's own filesystem.

## Architecture

| Component | Choice | Notes |
|---|---|---|
| API | FastAPI (Python 3.12) | async; large-file work never runs on the event loop |
| Worker | Custom, Redis-backed | priority classes + load-aware admission (Phase 4) |
| Database | MongoDB **single-node replica set** | replica set is required for transactions |
| Queue | Redis sorted sets + Lua | atomic claim; Mongo remains the record of truth |
| Frontend | React + TS + Vite | TanStack Query (server state) + Zustand (UI state) |
| Storage | Content-addressed | `objects/<sha256[:2]>/<sha256>` |

### Storage layout

Everything lives under `BIOINFO_HOME` (`/data` inside containers):

```
objects/ab/abcdef...     content-addressed blobs, mode 0444
staging/                 in-progress chunked uploads
tmp/                     job scratch (same filesystem as objects/, for atomic rename)
logs/                    per-job captured output
.biopipe/VERSION         mount sentinel — see below
.biopipe/lock            flock; prevents two stacks sharing one home
```

Files are stored by the SHA-256 of their content, so uploading the same reference genome
twice costs one copy. Human-facing names, projects, and metadata live in MongoDB. A
separate `blobs` collection refcounts each unique piece of content; garbage collection
only unlinks a blob after its refcount has been zero for over an hour.

### The mount sentinel

If the external drive unmounts, the container sees an **empty `/data`**, not an error.
Without a guard, one verification pass would flip the entire library to "missing."

`.biopipe/VERSION` is written at initialization. Every verification batch checks it first
and aborts the whole batch if it is absent, emitting a single `storage.unavailable` event.
Individual files additionally require **two consecutive misses at least 60s apart** before
being marked missing.

## Job queue

Four priority classes; lowest score dispatches first. Within a class, FIFO.

| Class | Use |
|---|---|
| `user_interactive` | UI-initiated actions, upload assembly |
| `user_background` | header parsing after the user's own upload |
| `maintenance` | file verification, blob GC |
| `bulk` | full-library sweeps |

Jobs are **at-least-once, never exactly-once — every handler must be idempotent.**

A discrete promotion sweep ages jobs upward every 30s, so low-priority work cannot starve
indefinitely. Leases carry a fencing token (epoch): if the Docker VM is paused (laptop lid
close) long enough for a lease to expire while the job is genuinely still running, the
resumed worker's writes are rejected rather than corrupting state.

### Load-aware admission

The queue refuses to launch work when the machine is already strained. The naive version
of this oscillates — the job you start *causes* the spike that blocks the next one — so
four mechanisms damp it: EWMA smoothing (never decide on one sample), hysteresis (close at
85% CPU, reopen at 70%), a 20-second minimum dwell between state changes, and token-bucket
ramp-up after reopening.

| State | Admits |
|---|---|
| `OPEN` | everything |
| `THROTTLED` | user work only |
| `CLOSED` | `user_interactive` only |

**`user_interactive` is admitted in every state, including CLOSED.** A UI that goes dead
under load is a worse outcome than a briefly oversubscribed machine.

Only the leader worker samples and publishes the decision, so all workers act on one
consistent state. Sustained *external* load (an aligner running in your terminal) would
otherwise hold the governor closed forever and maintenance would never run — so any
maintenance job starved past 30 minutes is admitted anyway, with a visible event.

### Periodic jobs

| Schedule | Interval | Purpose |
|---|---|---|
| `verify_files` | 60s | confirm stored files still exist |
| `reap_uploads` | 300s | delete abandoned staging directories |
| `gc_blobs` | 600s | unlink unreferenced blobs past the grace window |

Manage these at `/api/v1/schedules` — enable, disable, change interval, or force a run.
Exactly one worker wins each tick via an atomic compare-and-advance in Lua, and
`catchup=false` means a four-hour laptop sleep produces one tick on resume, not 240.

**`verify_files` does not stat every file every minute.** Taken literally that would be
100k stat calls per minute across a FUSE mount — itself the load problem this system
exists to avoid. Each tick checks a batch ordered oldest-verified-first, covering the whole
library on a rotation. Two guards make it safe: the mount sentinel is checked first and the
entire batch aborts if the drive is gone, and no file is marked missing until two
consecutive misses at least 60 seconds apart. Files registered in place are additionally
watched for size/mtime drift and quarantined if they change underneath us.

## Metadata and search

Each format has a suggested field set — FASTQ gets library prep and platform, BAM gets
reference build and aligner, VCF gets caller and variant type — on top of common sample
fields. The schema drives a proper form (dropdowns for enums, number inputs with units,
date pickers) and gives values a declared type so they sort and compare correctly.

**Schemas suggest; they do not restrict.** An unknown key is stored as-is. A value that
does not match its declared type produces a *warning* and is still saved — refusing to
record what someone typed loses information, while telling them it looks wrong does not.
An aligner outside the suggested list stays selectable in the dropdown rather than being
silently dropped on the next save.

Search from the ⌕ button, or by URL:

```
/search?meta=sample_id%3DP-041&kind=bam&tag=qc-pass
```

The whole query lives in the URL, so a search is shareable and survives a reload.
Metadata filters use a compact syntax — `key=value`, `key>=30`, `key!=x`, `key~text`,
`key=*` for "has any value" — and numeric-looking values are stored and compared as
numbers, so `lane>=2` is a real range query rather than a string comparison. Queries hit
the `metadata.$**` wildcard index (verified `IXSCAN`, not a collection scan).

Shift-click or ⌘-click to multi-select, then set a field or add tags across the whole
selection. **Bulk edits merge**: assigning `batch` to a cohort cannot silently erase the
other fields those files already carry.

### SRA auto-population

Upload `SRR11768093_1.fastq` and its metadata fills itself in. During ingest, FASTQ and
FASTA filenames are checked for an INSDC accession (`SRR`/`SRX`/`ERR`/`ERX`/`DRR`/`DRX`),
and a match is looked up at NCBI via E-utilities — giving organism, instrument, library
strategy and layout, BioProject/BioSample, study title, and submitter sample attributes
such as strain and tissue.

**If the filename does not carry the accession**, enter it under **Archive → SRA run** and
click **re-ingest**. A manually entered accession always takes priority over whatever the
filename parses to, which is the escape hatch for renamed or oddly-named files.

**Enrichment never overwrites anything you typed.** Public records contain mistakes, and a
correction must survive re-ingest. SRA fills only empty fields; where it disagrees with a
value you set, your value stands and the difference is reported in the SRA panel. Ingest
also never fails because of enrichment — an unreachable NCBI, a rate limit, or a retired
accession is recorded as a note and the file ingests normally.

Set `SRA_ENRICHMENT_ENABLED=false` to disable all outbound lookups.

## Common commands

```bash
make up          # build and start, waits for readiness
make down        # stop (data volumes preserved)
make logs        # tail all services
make test        # backend test suite
make check-home  # verify the drive is mounted and writable
make clean       # delete mongo/redis volumes (does NOT touch BIOINFO_HOME)
```

## Known platform caveats

- **`inotify` does not propagate** from host writes into the container. A filesystem
  watcher is impossible; polling is the only option — which is why file verification is a
  periodic job.
- **Hardlinks are unreliable** across the VirtioFS boundary. Deduplication uses
  rename + refcount, never `os.link`.
- **`fsync` durability is weaker** than on a native filesystem. Acceptable for a
  single-user local tool; the CAS design tolerates a lost tail.
- **APFS is case-insensitive.** SHA-256 hex is always lowercase so blob paths are safe,
  but user-supplied filenames are never used as paths.
- **Clock jumps after sleep** are expected. Durations use monotonic clocks; schedules never
  backfill missed ticks (a 4-hour sleep produces one tick, not 240).

## Running the tests

The backend suite needs no containers — it runs the real Lua scripts against an
in-process Redis and uses temp directories for storage:

```bash
cd backend && pip install -e '.[dev]' && pip install 'fakeredis[lua]' && pytest -v
```

Frontend typecheck and build:

```bash
cd frontend && npm install && npm run lint && npm run build
```

## Dependency pins

Two upstream versions are pinned deliberately, both because a newer release
broke the running stack:

- `beanie>=2.0,<2.1` — beanie 2.1.0 calls `client.append_metadata()`, which the
  pinned motor does not provide; startup fails at `init_beanie`.
- `redis>=5.2,<9` — the `Script` type moved to `redis.commands.core.AsyncScript`
  in redis-py 8.0. The code no longer references it directly, so this bound is
  precautionary.

## Build status

- **Phase 0** — walking skeleton: projects, simple upload (≤100 MB), CAS, two-pane UI
- **Phase 1** — queue core: Redis dispatch, worker, leases, cancellation, SSE
- **Phase 2** — chunked resumable uploads, register-in-place, blob GC, staging reaper
- **Phase 3** — format detection and header parsing (pysam) surfaced as file facts
- **Phase 4** — periodic jobs and the load-aware admission governor
- **Phase 5** — metadata schemas, search, and bulk editing

All three phases are verified running under `docker compose` against the real
drive. Phase 0/1: an uploaded FASTQ lands at `objects/a3/a31741…` with a matching
SHA-256, identical bytes deduplicate to one blob with refcount 2, priority
classes dispatch in order, a killed worker's job is requeued by the reaper and
reclaimed, and removing the mount sentinel makes `/readyz` return 503 rather than
marking the library missing. Phase 2: a 40 MB file uploaded in 3 chunks,
interrupted before the last chunk, resumed from `missing_chunks`, and assembled
to a byte-exact digest; a corrupted chunk is rejected on digest mismatch; a
pre-flight `client_sha256` deduplicates with zero bytes transferred; a
registered-in-place file is hashed without being copied, and path-escape
attempts (`/etc/passwd`, `/data/../etc/hosts`, relative paths) are all refused.

## Uploading

Three paths, chosen by size and where the file already is:

| Path | Use when | Cost |
|---|---|---|
| `POST /uploads` → chunks → `complete` | Browser upload of any size | One copy; resumable |
| `POST /projects/{id}/objects/register` | The file is already on the drive | **Zero copy** |
| `POST /projects/{id}/objects/upload` | Small files, scripts | One copy; no resume |

Chunked uploads are resumable by design: the client asks `GET /uploads/{id}` for
`missing_chunks` and sends only those, so a transfer that dies at 90% of a 30 GB
file resumes rather than restarting. Chunks are written `.tmp` then atomically
renamed, so a file named `.part` is always complete. Assembly hashes in the same
pass as the copy — reading a 100 GB file twice across VirtioFS would cost minutes
for nothing.

**Register-in-place does not copy or move your file.** The object records a
pointer to where it already lives, marked `external`. The tradeoff is ownership:
we do not control that file, so garbage collection never unlinks it (only the
database record is removed), and verification watches size and mtime for drift.

## File facts

After ingest, an `ingest_headers` job extracts format-specific metadata with
pysam. Everything reads **headers and a small prefix only** — a 100 GB BAM
carries its full metadata in the first few kilobytes, and scanning the body
would cost minutes to learn almost nothing.

| Format | Extracted |
|---|---|
| BAM/SAM/CRAM | sort order, reference contigs and lengths, samples, platform, read groups, aligner chain, read length, pairing |
| VCF/BCF | VCF version, samples, contigs, INFO/FORMAT/FILTER fields, variant types |
| FASTQ | read length, estimated read count, first read IDs, R1/R2 mate hint |
| FASTA | sequence count and names, total bases |
| BED/GFF/GTF | column shape, contigs seen, header lines |

**Estimates are labelled as estimates.** An exact FASTQ read count means
decompressing and scanning the entire file; the displayed value is extrapolated
from the first 1000 records and shown as `~2,004 (estimated)` with a note. A
number that is really an extrapolation must never look like a measurement,
because it will end up in someone's methods section.

Where the filename and the file contents disagree about format — a `.bam` that
is really gzipped FASTQ — both signals are recorded and the conflict is shown in
the UI rather than silently resolved. Click **re-ingest** on any file to re-run
detection and parsing after a parser improves.
- Phase 2 — chunked/resumable uploads, register-in-place, GC
- Phase 3 — format detection and header parsing (pysam)
- Phase 4 — periodic jobs and the load-aware governor
- Phase 5 — metadata schemas and search
- Phase 6 — pipeline execution (alignment, variant calling)
