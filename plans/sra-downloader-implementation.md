# SRA Downloader — Implementation Plan (V3)

## Overview

Add a hierarchical **Download from NCBI SRA** feature to the Add File flow. Users enter any INSDC accession (SRR/SRX/SRS/SRP/BioProject/BioSample) and drill down through a multi-step modal to select individual sequencing runs (SRR/ERR/DRR) for download via `fasterq-dump`. After download, QC tools auto-run per platform type and results are stored as object facts + HTML reports.

Supports Illumina, PacBio, and Nanopore data.

---

## 1. Docker Image Changes (`backend/Dockerfile`)

Already in the image: **fastp**, **FastQC** (+ JRE), **samtools**, **minimap2**, **pysam** (pip).

**Adding:**

```dockerfile
# SRA Toolkit for fasterq-dump
RUN apt-get update && apt-get install -y --no-install-recommends \
        sra-toolkit \
    && rm -rf /var/lib/apt/lists/*

# NanoPlot for long-read QC (Nanopore / PacBio HiFi)
# Installed globally rather than in a venv because the worker already runs in
# the system Python and adding another entrypoint wrapper is not worth it.
RUN pip install --no-cache-dir NanoPlot==1.45.0
```

Note: `sra-toolkit` in Debian trixie includes `fasterq-dump`, `prefetch`, and `sam-dump`. A writable config dir is needed; set via `NCBI_SETTINGS` env pointing to a path under `BIOINFO_HOME/tmp/`.

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

### QC job handler

```python
@handler("run_qc", mode=HandlerMode.THREAD, ...)
def run_qc(ctx: JobContext) -> dict:
    """
    Run platform-appropriate QC on a file.
    Enqueued as a chained job after download+ingest succeeds.
    """
    object_id = ctx.payload["object_id"]
    platform = ctx.payload.get("platform", "UNKNOWN")
    
    # 1. Read the file from CAS (by digest or external path)
    # 2. Determine tool based on platform
    # 3. Run tool, capture output
    # 4. Parse structured data into facts
    # 5. Store HTML report to qc_reports/<object_id>/
    # 6. Update object facts
    # 7. Return summary
```

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
    Creates a Run record and one job per selected run.
    Each job: fasterq-dump → ingest → enqueue QC
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

---

## 5. Queue Handlers (`backend/app/queue/sra_handlers.py`)

### Handler 1: `download_sra_run`

```python
@handler("download_sra_run", mode=HandlerMode.THREAD,
         job_class=JobClass.USER_INTERACTIVE,
         resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
         max_attempts=3)
def download_sra_run(ctx: JobContext) -> dict:
    """
    Download one SRA run via fasterq-dump and ingest the result.
    
    Idempotent via dedup_key=f"sra_download:{accession}:{project_id}"
    """
    accession = ctx.payload["accession"]
    project_id = ctx.payload["project_id"]
    run_metadata = ctx.payload.get("metadata", {})
    platform = run_metadata.get("platform", "UNKNOWN")

    # 1. Create staging directory
    staging = settings.tmp_dir / "sra_download" / ctx.job_id
    staging.mkdir(parents=True)

    # 2. Run fasterq-dump
    cmd = [
        settings.fasterq_dump_path,
        "--outdir", str(staging),
        "--split-files",
        "--progress",
        "--threads", str(min(settings.pipeline_default_threads, 4)),
        accession,
    ]
    # Execute, parse progress lines like "reads = 1234, bases = 5678"
    # Report via ctx.progress()

    # 3. Find output FASTQ files
    fastq_files = list(staging.glob("*.fastq"))

    # 4. For each FASTQ, ingest into CAS as a managed blob
    object_ids = []
    for fq in fastq_files:
        obj = await object_service.ingest_local_file(
            project_id=project_id,
            path=fq,
            name=fq.name,
            metadata=run_metadata,  # pre-attach SRA metadata
        )
        object_ids.append(str(obj.id))

        # 5. Enqueue QC
        if ctx.payload.get("run_qc", True):
            from app.queue import queue
            await queue.enqueue(
                "run_qc",
                payload={
                    "object_id": str(obj.id),
                    "platform": platform,
                    "sra_accession": accession,
                    "instrument": run_metadata.get("instrument"),
                },
                job_class=JobClass.USER_BACKGROUND,
                resources=JobResources(cpu=1, mem_mb=256, io=IoClass.LIGHT),
                dedup_key=f"run_qc:{obj.id}",
                project_id=project_id,
                object_id=obj.id,
            )

    # 6. Clean up staging
    shutil.rmtree(staging, ignore_errors=True)

    return {"accession": accession, "object_ids": object_ids, "file_count": len(fastq_files)}
```

### Handler 2: `run_qc`

```python
@handler("run_qc", mode=HandlerMode.THREAD,
         job_class=JobClass.USER_BACKGROUND,
         resources=JobResources(cpu=1, mem_mb=256, io=IoClass.LIGHT))
def run_qc(ctx: JobContext) -> dict:
    """
    Run platform-appropriate QC on an ingested file.
    Runs automatically after download_sra_run.
    """
    object_id = ctx.payload["object_id"]
    platform = ctx.payload.get("platform", "UNKNOWN")
    
    # 1. Find the object and its blob path
    obj = await DataObject.get(PydanticObjectId(object_id))
    path = ...  # resolve to filesystem path
    
    # 2. Platform-dependent QC
    qc_results = {}
    if platform == "ILLUMINA":
        # Run fastp in QC-only mode
        # Run FastQC for HTML report
        pass
    elif platform in ("OXFORD_NANOPORE", "PACBIO_SMRT"):
        # Run NanoPlot
        pass
    else:
        # Run fastp --quiet for basic stats
        pass
    
    # 3. Store QC report HTML
    qc_dir = settings.bioinfo_home / "qc_reports" / object_id
    qc_dir.mkdir(parents=True, exist_ok=True)
    # ... copy reports
    
    # 4. Update object facts with structured QC data
    # await obj.set({DataObject.facts: {**obj.facts, **qc_facts}})
    
    return {"object_id": object_id, "tool": tool_name, "qc_status": "ok"}
```

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

The existing `DetailPanel.tsx` gets a new optional section:

```tsx
{qc_facts && (
  <div className="section">
    <div className="section-title">Quality Control</div>
    <QcReport facts={obj.facts} />
  </div>
)}
```

New component `QcReport.tsx`:
- Shows per-base quality curve (reuse existing `QualityChart`)
- Shows base composition (reuse existing `BaseCompositionChart`)
- Shows GC content, duplication rate, Q20/Q30
- Links to FastQC/NanoPlot HTML report (opens in new tab)

The existing `SequenceCharts.tsx` components already render quality curves and composition from facts. QC integration just means populating the right facts keys.

---

## 8. Implementation Order

| Step | Area | Description |
|------|------|-------------|
| 1 | Docker | Add `sra-toolkit` + `NanoPlot` to `backend/Dockerfile` |
| 2 | Config | Add SRA download settings to `config.py`, `.env.example`, `docker-compose.yml` |
| 3 | Metadata | Build `sra_resolver.py` — hierarchical resolution engine with Redis caching |
| 4 | Backend API | `sra.py` router: `POST /resolve` + `POST /download` |
| 5 | Queue handlers | `sra_handlers.py`: `download_sra_run` handler |
| 6 | Queue handlers | `run_qc` handler in `sra_handlers.py` or a new `qc_handlers.py` |
| 7 | QC reports | `QcReport.tsx` component for the detail panel |
| 8 | Frontend types | Add SRA + QC types to `types.ts` |
| 9 | Frontend API | Add `sraResolve()`, `sraDownload()` to `client.ts` |
| 10 | Download dialog | `SraDownloadDialog.tsx` — multi-step wizard |
| 11 | + button | Split `ProjectExplorer.tsx` + button into a dropdown |
| 12 | Tests | Backend + frontend tests |

---

## 9. File Inventory

### New files:
```
backend/app/metadata/sra_resolver.py    — NCBI hierarchy resolution
backend/app/queue/sra_handlers.py       — download_sra_run + run_qc handlers
backend/app/api/v1/sra.py               — resolve + download endpoints
frontend/src/components/SraDownloadDialog.tsx  — multi-step wizard
frontend/src/components/QcReport.tsx           — QC facts display
```

### Modified files:
```
backend/Dockerfile                      — add sra-toolkit + NanoPlot
backend/app/config.py                   — SRA download settings
backend/app/api/v1/__init__.py          — register sra router
backend/app/api/v1/schemas.py           — SRA response/request models
frontend/src/api/types.ts               — RunInfo, SraResolveResponse, etc.
frontend/src/api/client.ts              — sraResolve, sraDownload methods
frontend/src/components/ProjectExplorer.tsx  — dropdown + button
frontend/src/components/DetailPanel.tsx      — QC section
.env.example                            — SRA download env vars
docker-compose.yml                      — SRA download env vars
```

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
