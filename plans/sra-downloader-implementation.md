# SRA Downloader — Implementation Plan (V4)

> **Revised 2026-07-27.** Corrections against the current code:
> the §5 handlers were declared `HandlerMode.THREAD` but their bodies used
> `await`, which is a `SyntaxError` — sync handlers return dicts and the
> executor persists them (see "Handler contract" below); the blob-path helper
> flagged as an undefined blocker already exists; and the proposed `Run` model
> duplicates the `PipelineRun` that shipped in `5332055`.

## Overview

Add a hierarchical **Download from NCBI SRA** feature to the Add File flow. Users enter any INSDC accession (SRR/SRX/SRS/SRP/BioProject/BioSample) and drill down through a multi-step modal to select individual sequencing runs (SRR/ERR/DRR) for download via `fasterq-dump`. After download, QC tools auto-run per platform type and results are stored as object facts + HTML reports.

Supports Illumina, PacBio, and Nanopore data.

---

## 1. Docker Image Changes (`backend/Dockerfile`)

Already in the image: **fastp**, **FastQC** (+ JRE), **samtools**, **minimap2**, **pysam** (pip).

**Adding:**

```dockerfile
# SRA Toolkit for fasterq-dump and prefetch.
# Deliberately not pinned to an exact Debian revision: `=3.1.1-1` breaks the
# build the moment the archive supersedes that revision, which it does
# routinely. If reproducibility matters, pin the base image digest instead --
# that pins every package at once and does not rot.
RUN apt-get update && apt-get install -y --no-install-recommends \
        sra-toolkit \
    && rm -rf /var/lib/apt/lists/*

# NanoPlot for long-read QC (Nanopore / PacBio HiFi).
# Installed globally rather than in a venv because the worker already runs in
# the system Python and another entrypoint wrapper is not worth it.
RUN pip install --no-cache-dir NanoPlot==1.45.0
```

`sra-toolkit` in Debian trixie provides `fasterq-dump`, `prefetch`, and
`sam-dump`.

**Config directory — do this at runtime, not build time.** An earlier draft ran
`vdb-config --restore-defaults` in the Dockerfile. That writes to the *build*
user's home, which is not necessarily the runtime user's, so the first-run
failure it was meant to prevent can still happen. Set `NCBI_SETTINGS` to a path
under `BIOINFO_HOME/tmp/` in the compose environment, and have the download
handler ensure that directory exists before shelling out — the same
`require_home()` discipline the ingest path already uses.

Add the tool paths to `config.py` beside the existing ones (config.py:49-53):

```python
fasterq_dump_path: str = "fasterq-dump"
prefetch_path: str = "prefetch"
nanoplot_path: str = "NanoPlot"
```

and probe them in `tools.py` so a missing binary surfaces in the UI rather than
failing a job, consistent with every other tool.

---

## 2. QC Architecture

After a download completes and the FASTQ file is ingested into CAS, QC runs automatically per platform:

### Platform detection

The `RunInfo` from SRA resolution carries the `platform` field (`ILLUMINA`, `PACBIO_SMRT`, `OXFORD_NANOPORE`). This is stored on the object's metadata during ingest, so the QC handler can inspect it.

### Tool mapping

| Platform | QC Tool | Output |
|---|---|---|
| ILLUMINA | `fastp` (QC-only mode, no trimming) | Structured JSON → object facts (base comp, quality, GC, duplication, adapter content) |
| ILLUMINA | `FastQC` | HTML report → `qc_reports/<object_id>/fastqc.html` + structured data in facts |
| OXFORD_NANOPORE | `NanoPlot` | HTML report → `qc_reports/<object_id>/nanoplot/` + summary stats → facts |
| PACBIO_SMRT | `NanoPlot` | Same as Nanopore (works on any long-read data) |
| UNKNOWN / other | `fastp --quiet` | Basic stats only |

### QC data in facts

Structured QC data goes into the object's `facts` dict (alongside the existing format-extracted facts):

```python
{
  "qc_tool": "fastp",
  "qc_tool_version": "0.24.0",
  "qc_before_filtering": {
    "total_reads": 12345678,
    "total_bases": 1850000000,
    "q20_rate": 0.97,
    "q30_rate": 0.93,
    "gc_content": 0.42,
    "duplication_rate": 0.15
  },
  "qc_report_path": "qc_reports/<object_id>/fastqc.html",
  "qc_status": "ok"
}
```

The DetailPanel gets an optional "QC" section that renders known QC facts.

### QC job handler — owned by the QC plan

**`run_qc` is defined in `pipeline-tool-additions-qc.md` Part 2, not here.**
This plan enqueues that handler after a download; it must not define a second
one under the same name with a different mode.

The QC plan's version is `HandlerMode.SUBPROCESS`, `JobClass.COMPUTE`. Two
extensions are needed there to serve this plan:

1. **Platform dispatch.** The base version always runs fastp + FastQC. For
   long reads it should run NanoPlot instead, keyed on a `platform` payload
   field this plan supplies (defaulting to the Illumina path when absent).
2. **Resource ceiling.** NanoPlot on a large ONT run exceeds the 1024 MB the
   QC plan budgets. Measure before setting a number.

### Handler contract (the correction that matters)

The original draft declared `mode=HandlerMode.THREAD` and then wrote
`await object_service.ingest_local_file(...)` and `await queue.enqueue(...)`
inside a `def`. That is a `SyntaxError`, not a runtime edge case.

The modes are (`queue/registry.py:24-27`):

| Mode | Body | Runs |
|---|---|---|
| `ASYNC` | `async def`, may await | on the event loop; must not block |
| `THREAD` | plain `def`, **no await** | `asyncio.to_thread` |
| `SUBPROCESS` | plain `def`, **no await** | `asyncio.to_thread`; killed by process group |

**Sync handlers cannot touch the database.** `queue/results.py` says so
directly: handlers "return plain dicts, and the writes happen here on the
loop." The executor calls `results.apply(job.type, result)` after the handler
returns (`executor.py:61`), dispatching through the `_APPLIERS` map
(`results.py:601`).

So a download handler that ingests files and chains a QC job has two options:

- **`SUBPROCESS` + an applier** — the handler shells out to `fasterq-dump`,
  returns `{"staged_files": [...], "accession": ..., "platform": ...}`, and a
  new `_apply_sra_download` in `results.py` performs the `ingest_local_file`
  calls and the `queue.enqueue("run_qc", ...)`. This matches how `trim_reads`
  and `align_reads` already work, and gets process-group cancellation for a
  download that can run for an hour. **Recommended.**
- **`ASYNC`** — awaits are legal, but a long `fasterq-dump` would have to be
  driven through `asyncio.create_subprocess_exec` and carefully kept from
  blocking the loop. More rope, no benefit here.

Register the new applier in `_APPLIERS` alongside the existing five.

### Staging QC reports

HTML reports go to `BIOINFO_HOME/qc_reports/<object_id>/` (not CAS — they're derivative, not content-addressed). The object's facts reference the path so the UI can link to them.

---

## 3. SRA Resolution Engine (`backend/app/metadata/sra_resolver.py`)

New module that walks the NCBI hierarchy using E-utilities. Reuses the existing HTTP/throttle/client infrastructure in `sra.py`.

### Resolution strategy

| Input type | NCBI query | Steps |
|---|---|---|
| SRR (run) | `esearch db=sra` → `efetch rettype=xml` | Parse EXPERIMENT_PACKAGE → extract RunInfo from RUN element |
| SRX (experiment) | Same as SRR — EXPERIMENT_PACKAGE contains all runs | Extract all `<RUN>` elements from the XML |
| SRS (sample) | `elink dbfrom=sra db=sra` with SRS accession | Get linked SRA UIDs → fetch each EXPERIMENT_PACKAGE |
| SRP (study) | Same as SRX — study contains experiments | Fetch the STUDY's EXPERIMENT_PACKAGE |
| PRJNA/E/DB (BioProject) | `elink dbfrom=bioproject db=sra` with BioProject ID | Get linked SRA UIDs → fetch each → aggregate Runs |
| SAMN/E/D (BioSample) | `elink dbfrom=biosample db=sra` | Same as BioProject pattern |

### Key data structures

```python
@dataclass
class RunInfo:
    accession: str           # SRR1234567
    experiment: str | None   # SRX123456
    sample: str | None       # SRS123456 / SAMN...
    study: str | None        # SRP123456
    bioproject: str | None
    biosample: str | None
    platform: str | None     # "ILLUMINA"
    instrument: str | None   # "Illumina NovaSeq 6000"
    library_strategy: str | None  # "WGS"
    library_layout: str | None    # "PAIRED" | "SINGLE"
    library_source: str | None
    spots: int | None
    bases: int | None
    bytes: int | None        # Estimated FASTQ size (~2× total_bases)
    organism: str | None
    sample_attributes: dict
    experiment_title: str | None

@dataclass 
class HierarchyNode:
    accession: str
    kind: str                # "bioproject" | "biosample" | "study" | "experiment"
    title: str | None
    platform: str | None
    organism: str | None
    child_count: int         # Number of child runs
    total_bases: int | None
    attributes: dict

@dataclass
class SraResolution:
    accession: str
    kind: str                # The input accession type
    title: str | None
    organism: str | None
    hierarchy: list[HierarchyNode]  # Tree path for the drill-down UI
    runs: list[RunInfo]             # All resolved runs (flat)
    total_run_count: int
    total_bytes_estimate: int | None
    error: str | None
```

### Caching

Resolve results cached in Redis with key `sra:resolve:{accession}:{platform_filter}` and a 1-hour TTL. This prevents repeated NCBI queries when the user browses the hierarchy (going back and forth between screens).

**⚠️ Large response warning:** For BioProjects with thousands of runs (e.g., PRJNA with 2000+ SRRs), the current approach returns all `RunInfo` objects in a single JSON response — this can be 1-2 MB and slow to serialize. Consider adding server-side pagination to the `/resolve` endpoint (`?offset=0&limit=100`) so the frontend's page-at-a-time table doesn't require the full dataset. The Redis cache can store the complete resolution while the API paginates over it.

### Platform filter

All runs are resolved; the `platform_filter` parameter filters the returned `runs` list server-side. Available filters: `ILLUMINA`, `PACBIO_SMRT`, `OXFORD_NANOPORE`, or `None` (all).

---

## 4. API Endpoints (`backend/app/api/v1/sra.py`)

```python
@router.post("/resolve", response_model=SraResolveResponse)
async def sra_resolve(body: SraResolveRequest) -> SraResolveResponse:
    """
    Resolve any INSDC accession to its runs with metadata.
    Does NOT start a download.
    Cached in Redis for 1 hour.
    """
    
@router.post("/download", status_code=202, response_model=SraAccepted)
async def sra_download(body: SraDownloadRequest) -> SraAccepted:
    """
    Download selected runs from SRA.
    Creates a PipelineRun (kind=SRA_DOWNLOAD) plus one RunJob link per
    download job -- see section 4.

    Per run: fasterq-dump (handler) -> ingest + enqueue QC (applier).
    """
```

### Request/Response models

```python
class SraResolveRequest(BaseModel):
    accession: str
    platform_filter: str | None = None

class SraResolveResponse(BaseModel):
    accession: str
    kind: str
    title: str | None
    organism: str | None
    hierarchy: list[HierarchyNodeOut]
    runs: list[RunInfoOut]
    total_run_count: int
    total_bytes_estimate: int | None
    error: str | None

class SraDownloadRequest(BaseModel):
    project_id: str
    run_accessions: list[str]  # Selected runs from the checklist
    run_qc: bool = True        # Auto-run QC after download

class SraAccepted(BaseModel):
    run_id: str
    download_job_ids: list[str]  # One per selected run
    accession: str
```

### Run model — extend `PipelineRun`, do not add a second one

`backend/app/models/run.py` **already exists** and defines `PipelineRun`,
`RunJob`, `RunJobRole`, and `RunStatus` (shipped in `5332055`). A parallel
`Run` model would fork the activity view, which reads `PipelineRun`.

Two properties of the existing model are deliberate and worth not re-deciding:

- **`RunJob` is a link collection**, not a `job_ids` array on the run. A job can
  belong to more than one run — `build_index` is deduplicated by content, so a
  second alignment reuses the first one's build, and `shared=True` records that.
  An array cannot express it.
- **Status is derived, never stored** (`RunStatus`, run.py:29). A stored status
  is a second source of truth that drifts the first time a write is lost.

The changes needed:

```python
class RunKind(StrEnum):
    ALIGNMENT = "alignment"
    TRIM = "trim"
    SRA_DOWNLOAD = "sra_download"   # NEW


class RunJobRole(StrEnum):
    INDEX = "index"
    ALIGN = "align"
    TRIM = "trim"
    INDEX_BAM = "index_bam"
    INGEST = "ingest"
    DOWNLOAD = "download"           # NEW
    QC = "qc"                       # NEW
```

The root accession and the selected run accessions go in `PipelineRun.params`,
which is already a free-form dict denormalized for exactly this reason: a run
must stay describable after its jobs are TTL-pruned at 30 days.

**Decide:** whether a failed QC job should fail the whole run. `OPTIONAL_ROLES`
(run.py) currently holds only `INGEST`. QC is arguably the same case — the
download succeeded and produced its file, and QC is re-runnable — so `QC`
probably belongs there too. `DOWNLOAD` clearly does not.

---

## 5. Queue Handlers (`backend/app/queue/sra_handlers.py`)

### Handler 1: `download_sra_run`

The handler shells out and returns; **all database work happens in the
applier**. See "Handler contract" in §2.

```python
@handler("download_sra_run",
         mode=HandlerMode.SUBPROCESS,   # spawns fasterq-dump; sync body
         job_class=JobClass.USER_INTERACTIVE,
         resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
         max_attempts=3)
def download_sra_run(ctx: JobContext) -> dict:
    """Download one SRA run via fasterq-dump. Ingest happens in the applier.

    Synchronous: no await in this body. It shells out, stages files, and
    returns a description of what it staged.

    Idempotent via dedup_key=f"sra_download:{accession}:{project_id}".
    """
    accession = ctx.payload["accession"]
    run_metadata = ctx.payload.get("metadata", {})

    staging = settings.tmp_dir / "sra_download" / ctx.job_id
    staging.mkdir(parents=True)

    # prefetch first: some NCBI configurations require it before fasterq-dump,
    # and it is a no-op when the run is already cached. Cheaper than detecting
    # the vdb error and retrying.
    # subprocess.run([settings.prefetch_path, "--output-directory", str(staging), accession])

    cmd = [
        settings.fasterq_dump_path,
        "--outdir", str(staging),
        "--split-files",
        "--progress",
        "--threads", str(min(settings.pipeline_default_threads, 4)),
        accession,
    ]
    # Execute; parse "reads = 1234, bases = 5678" progress lines into
    # ctx.progress(). Follow how trim_reads drives fastp.

    fastq_files = sorted(staging.glob("*.fastq"))

    # Disk check happens *before* fasterq-dump using the resolver's byte
    # estimate, not after. Checking once the files already exist is too late:
    # the space is spent. (The pre-flight belongs above the cmd call.)
    free_space = shutil.disk_usage(staging).free
    estimated = ctx.payload.get("bytes_estimate") or 0
    if estimated and estimated > free_space * 0.9:
        raise PermanentError(
            f"Insufficient disk space: need ~{estimated / 1e9:.1f} GB, "
            f"only {free_space / 1e9:.1f} GB available"
        )

    # --split-files yields <acc>_1.fastq, <acc>_2.fastq, and <acc>.fastq for
    # unpaired singletons.
    paired = any(f.name.endswith(("_1.fastq", "_2.fastq")) for f in fastq_files)

    staged = []
    for fq in fastq_files:
        mate = None
        if paired:
            mate = ("read1" if fq.name.endswith("_1.fastq")
                    else "read2" if fq.name.endswith("_2.fastq")
                    else "unpaired")
        staged.append({"path": str(fq), "name": fq.name, "mate": mate})

    # No cleanup here: the applier consumes these paths. ingest_local_file
    # renames the file into the object store, so the staging dir is emptied by
    # the ingest itself; reap_pipeline_scratch handles a crashed run.
    return {
        "accession": accession,
        "staged": staged,
        "metadata": run_metadata,
        "platform": run_metadata.get("platform", "UNKNOWN"),
        "project_id": ctx.payload["project_id"],
        "run_qc": ctx.payload.get("run_qc", True),
        "staging_dir": str(staging),
    }
```

The applier, in `backend/app/queue/results.py`:

```python
async def _apply_sra_download(result: dict) -> None:
    project_id = PydanticObjectId(result["project_id"])
    for entry in result.get("staged", []):
        meta = {**result.get("metadata", {})}
        if entry["mate"]:
            meta["mate"] = entry["mate"]
        obj = await object_service.ingest_local_file(
            project_id=project_id,
            path=Path(entry["path"]),
            name=entry["name"],
            metadata=meta,
        )
        if result.get("run_qc"):
            await queue.enqueue(
                "run_qc",
                payload={
                    "object_id": str(obj.id),
                    "platform": result["platform"],
                    "sra_accession": result["accession"],
                },
                job_class=JobClass.COMPUTE,
                dedup_key=f"qc:{obj.id}",   # matches launch_qc's key
                project_id=project_id,
                object_id=obj.id,
            )

# Register alongside the existing five (results.py:601):
#   "download_sra_run": _apply_sra_download,
```

Note the dedup key is `qc:{obj.id}`, identical to `launch_qc`'s in the QC plan —
so a manual QC click and an automatic post-download QC collapse to one job
rather than running twice.

**`ingest_local_file` signature** (object_service.py:147) takes
`project_id`, `path`, `name`, and optional `role`, `derived_from`,
`produced_by_job`, `facts`, `metadata`, `sidecar_of`, `sidecar_role`. It
*consumes* `path` — renaming it into the object store on success, unlinking on
dedup — and requires the file to be under `tmp_dir` so the move is an atomic
rename rather than a copy. Staging under `settings.tmp_dir` above satisfies
that.

**Still to decide:** `mate` is written into free-form metadata here, but the
data model has a real `mate_object_id` field on `DataObject` and a
`pairing.py` module that detects R1/R2. The applier should set the actual
mate link after both files are ingested, not just a metadata string.

### Handler 2: `run_qc` — not defined here

Owned by `pipeline-tool-additions-qc.md` Part 2 (`HandlerMode.SUBPROCESS`,
`JobClass.COMPUTE`). This plan only enqueues it, with an extra `platform` field
in the payload.

The two extensions that plan needs in order to serve this one:

```python
# Inside the QC plan's run_qc, dispatching on the payload's platform:
platform = ctx.payload.get("platform", "UNKNOWN")

if platform in ("OXFORD_NANOPORE", "PACBIO_SMRT"):
    # NanoPlot: FastQC's per-base model is meaningless for reads that vary
    # from 200 bp to 100 kb in the same file.
    ...
else:
    # fastp --report-only + FastQC, the default path.
    ...
```

The same rule applies as for the download handler: this is a sync handler, so
it **returns** its facts and an applier writes them. It must not
`await DataObject.get(...)`.

**Resolving the input path is already solved.** An earlier draft flagged
`object_service.get_blob_path()` as an undefined API that "MUST be defined
before coding". It exists in a different shape:
`pipeline_service._resolve_readable(obj)` (pipeline_service.py:68) returns a
`(digest, path)` pair, and `app.storage.paths.blob_path(digest)` maps a digest
to its location. `launch_trim` and `launch_alignment` both use this to put
`sha256` and `path` in the job payload — the QC service method should do the
same, so the handler receives a path rather than looking one up.

---

## 6. Frontend: `SraDownloadDialog.tsx`

A multi-step wizard component with four screens:

### Screen 1: Accession Input
- Text field for accession
- Platform filter dropdown: Auto (all) | Illumina | PacBio | Nanopore
- "Resolve" button → calls `POST /sra/resolve`
- Validation: match against INSDC/BioProject/BioSample regex patterns

### Screen 2: Hierarchy View (conditional)
Shown only when the resolved accession is a container (BioProject/BioSample/SRP/SRS):
- Tree hierarchy: BioProject → BioSamples → Experiments
- Each node shows: title, organism, platform, run count, total bases
- Expand/collapse, selectable at run-level
- Back button to re-enter accession

### Screen 3: Run Selection Checklist
A paginated table (20 per page) with:

| ☐ | Accession | Platform | Instrument | Strategy | Layout | Spots | Bases | Size |
|---|---|---|---|---|---|---|---|---|

- Platform shown as colored badge chips
- Sortable by any column
- Select all / deselect all
- Total size summary bar at bottom
- "Download Selected (#)" button

### Screen 4: Download Progress
- Shows a `Run` entry (kind="sra_download") with per-run job progress
- Each run row: accession, status icon, progress bar (reads/bases downloaded), ETA
- After QC: a secondary "QC complete" indicator
- Click on a completed object: navigates to its detail panel

### Styles
Platform badges use distinct colors:
- **ILLUMINA** — blue `#2196F3`
- **PACBIO_SMRT** — green `#4CAF50`
- **OXFORD_NANOPORE** — orange `#FF9800`
- **UNKNOWN** — gray

---

## 7. QC Reports in the Detail Panel

The detail panel is **tabbed** as of `fc160b1`: `QcTab`, `MetadataTab`, and
`ActionsTab` inside `DetailPanel.tsx`. QC output belongs in `QcTab`, which
already holds the parsed facts, the charts, and the trim and alignment reports.
Do **not** add a new top-level section — that structure is gone.

`QcReport.tsx` goes into `QcTab` alongside the existing reports, following
`TrimReport.tsx`'s shape (self-suppressing when its facts are absent, so files
without QC render nothing):

- GC content, duplication rate, Q20/Q30
- A link to the FastQC/NanoPlot HTML report

`BaseCompositionChart` and `QualityChart` (`SequenceCharts.tsx`) already render
from facts and are already mounted in `QcTab` — populating the right fact keys
reuses them at no cost, so `QcReport` should only add what they do not cover.

**Serving the HTML report is unsolved.** Nothing currently serves static files
out of `BIOINFO_HOME`; there is no route for it. A link needs either a
streaming endpoint or a mounted static path, plus a decision about origin —
FastQC and NanoPlot HTML are generated from read data and embed it, so
rendering them same-origin deserves thought. This is shared with the QC plan
(§2.7 there); solve it once.

The species name also now appears in the panel header when
`metadata.organism` is set. The SRA resolver already returns `organism` per run
(§3), so passing it through at ingest populates that header for free.

---

## 8. Implementation Order

| Step | Area | Description |
|------|------|-------------|
| 0 | — | **Prerequisite:** `pipeline-tool-additions-qc.md` Phase 2 has landed, so `run_qc` exists to chain to. Without it, build steps 1-11 and leave `run_qc: false`. |
| 1 | Docker | Add `sra-toolkit` + `NanoPlot` to `backend/Dockerfile` |
| 2 | Config | Tool paths + `NCBI_SETTINGS` in `config.py`, `.env.example`, `docker-compose.yml`; probe the binaries in `tools.py` |
| 3 | Metadata | `sra_resolver.py` — hierarchical resolution with Redis caching |
| 4 | Models | `RunKind.SRA_DOWNLOAD`, `RunJobRole.DOWNLOAD`/`.QC` in `models/run.py` |
| 5 | Backend API | `sra.py` router: `POST /resolve` + `POST /download` |
| 6 | Queue | `download_sra_run` handler (SUBPROCESS, returns a dict) |
| 7 | Queue | `_apply_sra_download` in `results.py` + `_APPLIERS` entry — the ingest and QC chaining live here |
| 8 | QC extension | Platform dispatch (NanoPlot for long reads) in the QC plan's `run_qc` |
| 9 | Frontend | SRA types in `types.ts`; `sraResolve()`/`sraDownload()` in `client.ts` |
| 10 | Download dialog | `SraDownloadDialog.tsx` — multi-step wizard |
| 11 | + button | Split `ProjectExplorer.tsx`'s "+" into a split-button: primary action stays "Upload file" (the common case), a chevron opens "Upload file" / "Download from NCBI SRA" |
| 12 | QC reports | `QcReport.tsx` into `QcTab` — blocked on the report-serving decision (§7) |
| 13 | Tests | Backend + frontend |

---

## 9. File Inventory

### New files:
```
backend/app/metadata/sra_resolver.py           — NCBI hierarchy resolution
backend/app/queue/sra_handlers.py              — download_sra_run handler only
backend/app/api/v1/sra.py                      — resolve + download endpoints,
                                                 request models inline
frontend/src/components/SraDownloadDialog.tsx  — multi-step wizard
frontend/src/components/QcReport.tsx           — QC display inside QcTab
```

### Modified files:
```
backend/Dockerfile                      — add sra-toolkit + NanoPlot
backend/app/config.py                   — fasterq_dump_path, prefetch_path,
                                          nanoplot_path, NCBI_SETTINGS
backend/app/pipelines/tools.py          — probe the three new binaries
backend/app/api/v1/__init__.py          — register the sra router
backend/app/models/run.py               — RunKind.SRA_DOWNLOAD,
                                          RunJobRole.DOWNLOAD + .QC
backend/app/queue/results.py            — _apply_sra_download + _APPLIERS entry
frontend/src/api/types.ts               — RunInfo, SraResolveResponse, etc.
frontend/src/api/client.ts              — sraResolve, sraDownload
frontend/src/components/ProjectExplorer.tsx  — split "+" button into a dropdown
frontend/src/components/DetailPanel.tsx      — QcReport into QcTab
.env.example                            — SRA env vars
docker-compose.yml                      — SRA env vars
```

**Schema location — settled.** Request models go **inline in `sra.py`**. That
matches `pipelines.py`, which defines `TrimRequest` (:18), `AlignRequest` (:87),
and `BuildIndexRequest` (:96) in the router. `schemas.py` holds models shared
across routers (`ProjectOut`, `ObjectOut`, `JobOut`); if `RunInfoOut` ends up
consumed by more than this router, move that one and leave the rest.

`models/run.py` is listed as **modified, not new** — see §4. `run_qc` is not
listed at all: it belongs to `pipeline-tool-additions-qc.md`.

---

## 10. Edge Cases & Error Handling

| Scenario | Handling |
|---|---|
| BioProject with 2000 runs | Paginated at 20/page on the frontend; server returns all (Redis-cached) |
| Accession not found | Resolution returns error, modal shows "No records found for X" |
| NCBI rate-limited during resolve | E-utilities retry (already built in `sra.py`); Redis cache absorbs repeated lookups |
| NCBI rate-limited during download | `fasterq-dump` has its own retry; SRA Toolkit respects NCBI rate limits |
| Partial multi-run download | Per-run jobs are independent — 4/5 succeed, 1 fails |
| Disk full during download | `fasterq-dump` exits non-zero; job fails with stderr; staging cleaned up |
| Duplicate download request | Dedup key returns existing job_id; no re-download |
| Platform auto-detect fails | Default to `fastp` basic stats |
| QC tool not available | QC job reports error but doesn't fail the download |
| Interrupted download mid-file | `fasterq-dump` is not resumable; retry re-runs from scratch. Dedup at ingest level prevents double storage |
| Previously downloaded run | Resolver should check whether any of the resolved SRR accessions already exist as objects in the current project and flag them as "already downloaded" in the run selection checklist (greyed out, pre-deselected). Avoids redundant re-download. |
| NCBI returns malformed/missing XML | Resolution should wrap XML parsing in try/except; fall back to partial data (whatever fields were parsed successfully) plus an `error` string, rather than failing the entire resolution. Runs with missing fields should still be listable. |
| `fasterq-dump` needs `prefetch` | Some NCBI configurations require explicit `prefetch {accession}` before `fasterq-dump`. The download handler should catch the vdb error pattern and retry with a `prefetch` step first. Alternatively, always run `prefetch` before `fasterq-dump` — it's a no-op if already cached. |
