# QC Pipeline + Additional Trim Tools — Implementation Plan

> **Revised 2026-07-27.** The original version of this plan was written before
> the alignment merge (`4e15413`) and asserted that alignment did not exist.
> Most of it described work that had already shipped. The Current State table
> below was re-derived from the code; Part 2 (bwa-mem2 probe) was removed
> entirely because that probe exists. QC is the remaining real work.

## Current State

### What exists

| Component | Status |
|---|---|
| `backend/app/pipelines/tools.py` | Probes `fastp`, `fastqc`, `bwa-mem2`, `minimap2`, `samtools`. `all_tools()` returns all five as `list[Tool]`. |
| `backend/app/pipelines/fastp_runner.py` | Fastp command builder, progress parser, report parser. Complete. |
| `backend/app/pipelines/aligners.py` | Aligner definitions. Complete. |
| `backend/app/pipelines/align_runner.py` | Alignment command builder and runner. Complete. |
| `backend/app/pipelines/pairing.py` | Mate detection for R1/R2 pairs. |
| `backend/app/queue/pipeline_handlers.py` | `trim_reads` (SUBPROCESS), alignment handlers, `reap_pipeline_scratch` (ASYNC). |
| `backend/app/services/pipeline_service.py` | `launch_trim()`, `launch_alignment()`, `launch_build_index()`, plus reference/index helpers. |
| `backend/app/api/v1/pipelines.py` | `GET /tools`, `GET /defaults`, `GET /mate/{id}`, `POST /trim`, `GET /align/defaults/{id}`, `GET /references/{project_id}`, `POST /index`, `POST /align`. |
| `backend/app/models/run.py` | `PipelineRun` + `RunJob` + `RunJobRole`. `RunKind` is `ALIGNMENT` \| `TRIM`. |
| `frontend/src/components/TrimDialog.tsx` | Trim parameter dialog, hardcoded to fastp. |
| `frontend/src/components/AlignDialog.tsx` | Align parameter dialog. Aligner `<select>` is inside the `advanced` collapsible (AlignDialog.tsx:225-233). |
| `frontend/src/components/DetailPanel.tsx` | Tabbed: QC / Metadata / Actions. Trim and Align buttons in the panel header. |
| `backend/Dockerfile` | Installs `fastp`, `fastqc`, `default-jre-headless`, aligner packages. |

### What does NOT exist

- **No QC pipeline**: no `run_qc` handler, no `launch_qc()`, no `POST /pipelines/qc`, no QC button.
  (`grep -rn "run_qc\|launch_qc" backend/app frontend/src` returns nothing.)
- No `cutadapt` or `trimmomatic` tool definitions.
- No tool metadata (`summary` / `strengths` / pipeline attribution) on the tools API.
- No tool selection screen — see `tool-selector-implementation.md`.

---

## Ownership note — read before starting

Two plans in this directory touch overlapping surfaces. To avoid collisions:

- **`TOOL_META` is defined once, here** (Part 1.3). `tool-selector-implementation.md`
  consumes it and must not redefine it. The field is **`pipelines`, plural** — a
  tuple — because fastp genuinely belongs to both trim and QC. An earlier draft
  used a singular `pipeline` string in three places, which would have made fastp
  disappear from one of the two.
- **`run_qc` is defined once, here** (Part 2), as `HandlerMode.SUBPROCESS`.
  `sra-downloader-implementation.md` enqueues this same handler after a download
  rather than defining its own.

---

## Part 1 — cutadapt + Trimmomatic + tool metadata

### 1.1 Dockerfile

`cutadapt` is a Python package; Trimmomatic is a Debian package wrapping a JAR.

```dockerfile
# Existing apt line gains trimmomatic:
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        fastp \
        fastqc \
        default-jre-headless \
        trimmomatic \
    && rm -rf /var/lib/apt/lists/*

# Existing pip step gains cutadapt:
RUN pip install --no-cache-dir '.[dev]' cutadapt
```

### 1.2 `backend/app/config.py`

Existing tool paths are at config.py:49-53. Add two:

```python
cutadapt_path: str = "cutadapt"
trimmomatic_path: str = "trimmomatic"
```

### 1.3 `backend/app/pipelines/tools.py` — probes and metadata

Add two probes following the existing pattern (each `@lru_cache(maxsize=1)`,
each delegating to `_probe`):

```python
@lru_cache(maxsize=1)
def cutadapt() -> Tool:
    return _probe("cutadapt", settings.cutadapt_path, ["--version"])


@lru_cache(maxsize=1)
def trimmomatic() -> Tool:
    # The Debian package installs a wrapper at /usr/bin/trimmomatic that
    # forwards to the JAR. It prints its version to stderr and exits non-zero;
    # `_probe` ignores the exit code and reads whichever stream produced
    # output, the same accommodation bwa-mem2 already needs.
    return _probe("trimmomatic", settings.trimmomatic_path, ["-version"])


def all_tools() -> list[Tool]:
    return [
        fastp(), fastqc(), cutadapt(), trimmomatic(),
        bwa_mem2(), minimap2(), samtools(),
    ]
```

**Leave `all_tools()` returning `list[Tool]`.** An earlier draft changed it to
`list[dict]`; that breaks every internal caller for the benefit of one endpoint.
Enrich at the API boundary instead — `Tool.as_dict()` already exists
(tools.py:41).

Then the single metadata table. Note `pipelines` is a **tuple**:

```python
class PipelineType(StrEnum):
    TRIM = "trim"
    ALIGN = "align"
    QC = "qc"
    UTILITY = "utility"


@dataclass(frozen=True)
class ToolMeta:
    pipelines: tuple[PipelineType, ...]
    summary: str
    strengths: tuple[str, ...]


TOOL_META: dict[str, ToolMeta] = {
    "fastp": ToolMeta(
        # Both: fastp trims, and in --report-only mode it is also the QC tool.
        pipelines=(PipelineType.TRIM, PipelineType.QC),
        summary=(
            "All-in-one Illumina read QC and adapter trimming. Single-pass: "
            "quality filtering, adapter removal, poly-G tail trimming, length "
            "filtering, and duplicate detection. Produces structured JSON and "
            "HTML reports suitable for downstream charts and methods sections."
        ),
        strengths=(
            "Single-pass: trims and reports QC in one invocation",
            "Auto-detects adapter sequences from read overlap",
            "Handles NovaSeq/NextSeq two-colour poly-G artefacts",
            "Built-in per-base quality JSON for downstream visualization",
            "Fast C++ implementation, low memory footprint",
        ),
    ),
    "cutadapt": ToolMeta(
        pipelines=(PipelineType.TRIM,),
        summary=(
            "Flexible adapter, primer, and barcode trimmer for all sequencing "
            "platforms. Supports anchored adapters, linked adapters, "
            "demultiplexing by barcode, and adapter patterns fastp cannot "
            "express."
        ),
        strengths=(
            "Demultiplexing: split reads by barcode/index",
            "Linked adapter trimming for paired-end reads",
            "Anchored 5'/3' adapter matching for amplicon-seq",
            "Poly-A tail trimming for RNA-seq",
            "Works on any platform (Illumina, PacBio, ONT)",
        ),
    ),
    "trimmomatic": ToolMeta(
        pipelines=(PipelineType.TRIM,),
        summary=(
            "Classic sliding-window quality trimmer for Illumina paired-end "
            "and single-end reads. The longest-established tool in the field "
            "and still widely cited."
        ),
        strengths=(
            "Sliding-window quality trimming: aggressive on trailing bases",
            "Gold standard for legacy Illumina pipeline comparisons",
            "Simple paired-end model: keeps R1/R2 in sync",
            "Plays well with Nextera/TruSeq adapter FASTA files",
        ),
    ),
    "fastqc": ToolMeta(
        pipelines=(PipelineType.QC,),
        summary=(
            "The canonical per-file HTML QC report. Per-base quality, GC "
            "content, overrepresented sequences, adapter content, sequence "
            "duplication levels -- the standard artifact for publication "
            "supplementary materials."
        ),
        strengths=(
            "The publication-standard QC report format",
            "Rich per-base visualizations (quality, GC, N)",
            "Overrepresented sequence detection",
            "Zero configuration: runs on any FASTQ",
        ),
    ),
    "bwa-mem2": ToolMeta(
        pipelines=(PipelineType.ALIGN,),
        summary=(
            "The standard short-read aligner for human and model organism "
            "genomes. Optimized for Illumina paired-end reads up to ~500 bp."
        ),
        strengths=(
            "Gold standard for Illumina WGS/WES/resequencing",
            "Handles mated reads with proper insert-size modeling",
            "2x faster than original bwa-mem with the same accuracy",
            "x86-64 only (Intel compiler dispatch)",
        ),
    ),
    "minimap2": ToolMeta(
        pipelines=(PipelineType.ALIGN,),
        summary=(
            "Versatile aligner for long reads (PacBio, Nanopore) and "
            "any-vs-any comparisons. Splice-aware for RNA-seq. Works on short "
            "reads with the -x sr preset."
        ),
        strengths=(
            "Designed for PacBio CLR/HiFi and ONT reads",
            "Splice-aware for RNA-seq (junctions in BAM tags)",
            "Short-read alignment with the -x sr preset",
            "Runs on all architectures including arm64",
        ),
    ),
    "samtools": ToolMeta(
        # Utility and QC: flagstat is where the alignment report numbers come
        # from, and those already render in the detail panel.
        pipelines=(PipelineType.UTILITY, PipelineType.QC),
        summary=(
            "Universal BAM/CRAM/SAM toolkit. Sorting, indexing, flagstat, "
            "depth calculation. The common denominator of every alignment "
            "workflow."
        ),
        strengths=(
            "Universal BAM/CRAM manipulation",
            "Fast coordinate sorting and indexing",
            "Flagstat: comprehensive alignment statistics",
        ),
    ),
}


def tool_with_meta(tool: Tool) -> dict:
    """Probe result plus its static description, for the API."""
    meta = TOOL_META.get(tool.name)
    return {
        **tool.as_dict(),
        "pipelines": [p.value for p in meta.pipelines] if meta else [],
        "summary": meta.summary if meta else "",
        "strengths": list(meta.strengths) if meta else [],
    }


def all_tools_with_meta() -> list[dict]:
    return [tool_with_meta(t) for t in all_tools()]
```

### 1.4 API: enrich `GET /pipelines/tools`

Only the serialization changes; the internal `all_tools()` contract is
untouched, so existing callers are unaffected.

```python
@router.get("/tools")
async def list_tools() -> dict:
    tools_list = tools.all_tools_with_meta()
    return {
        "tools": tools_list,
        "all_available": all(t["available"] for t in tools_list),
    }
```

**Check before shipping:** `all_available` currently spans every probed tool.
With seven tools including two optional trimmers, a machine missing cutadapt
reports `all_available: false`, which may drive UI that only cares about fastp.
Either scope this per-pipeline or confirm no consumer treats it as "trim is
ready".

### 1.5 Frontend types — `frontend/src/api/types.ts`

```typescript
export type PipelineType = "trim" | "align" | "qc" | "utility";

export interface PipelineTool {
  name: string;
  path: string | null;
  version: string | null;
  available: boolean;
  error: string | null;
  pipelines: PipelineType[];   // plural: fastp is both trim and qc
  summary: string;
  strengths: string[];
}
```

### 1.6 Multi-tool trim (deferred)

Running cutadapt/trimmomatic needs a per-tool parameter model and one handler
each, and there is no UI to choose between them until the tool selector exists.
**Part 1 stops at making the tools probeable and described.** The runners are
sequenced after the tool selector — see `tool-selector-implementation.md`.

---

## Part 2 — QC Pipeline

The remaining genuinely-unbuilt work.

### 2.1 Overview

QC is read-only: it inspects a file and produces a report, unlike Trim which
derives new files. It should run on both raw and trimmed reads.

### 2.2 Handler — `backend/app/queue/pipeline_handlers.py`

`SUBPROCESS`, matching `trim_reads`: it spawns fastp and FastQC, and subprocess
mode is what gets the process group killed on cancel.

```python
@handler(
    "run_qc",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=2, mem_mb=1024, io=IoClass.MEDIUM),
    # Same reasoning as trim_reads: a QC failure is deterministic -- bad input
    # or a missing binary -- and retries only delay the error.
    max_attempts=2,
)
def run_qc(ctx: JobContext) -> dict:
    """Run fastp (report-only) and FastQC on one FASTQ.

    Synchronous: HandlerMode.SUBPROCESS runs off the event loop, so this body
    must not await. Follow how trim_reads resolves its input and writes back.
    """
    fastp_tool = tools.require(tools.fastp())
    fastqc_tool = tools.require(tools.fastqc())
    ...
```

Decisions:
- **fastp QC mode**: `fastp -i reads.fastq --report-only --json fastp_qc.json` —
  no output FASTQ, just the report. Seconds for typical files.
- **FastQC**: `fastqc -o qc_out/ reads.fastq` → `*_fastqc.html` + `*_fastqc.zip`.
- Both always run; the pair is the standard minimal QC package.
- Structured results land in the object's `facts`; the HTML is a derived file.

**Reuse rather than reinvent:** `fastp_runner.py` already builds fastp commands,
parses its progress output, and reads its JSON report. Report-only mode should
extend it, not fork it.

### 2.3 Service — `pipeline_service.launch_qc()`

```python
async def launch_qc(*, object_id: PydanticObjectId) -> Job:
    """Queue a QC run on a single FASTQ file."""
    obj = await DataObject.get(object_id)
    if obj is None:
        raise NotFoundError(f"Object not found: {object_id}")
    _check_trimmable(obj)  # same requirement: READY and FASTQ

    tools.require(tools.fastp())
    tools.require(tools.fastqc())

    r1_digest, r1_path = await _resolve_readable(obj)
    payload = {
        "object_id": str(obj.id),
        "project_id": str(obj.project_id),
        "name": obj.name,
    }
    if r1_digest:
        payload["sha256"] = r1_digest
    if r1_path:
        payload["path"] = r1_path

    return await queue.enqueue(
        "run_qc",
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=2, mem_mb=1024, io=IoClass.MEDIUM),
        max_attempts=2,
        # Re-running QC on unchanged content would produce an identical report.
        dedup_key=f"qc:{obj.id}",
        project_id=obj.project_id,
        object_id=obj.id,
    )
```

`_check_trimmable` (pipeline_service.py:87) and `_resolve_readable`
(pipeline_service.py:68) already exist and are the right helpers.

**Naming:** `_check_trimmable` is reused for a non-trim pipeline. Either rename
it `_check_fastq_ready` or add a thin alias, so the call does not read as a
copy-paste slip.

**Run grouping:** QC is a single job, so a `PipelineRun` is optional. If one is
wanted for the activity view, add `QC = "qc"` to `RunKind` (run.py:24) and a
`QC` role to `RunJobRole` — do **not** introduce a parallel run model.

### 2.4 API — `backend/app/api/v1/pipelines.py`

```python
class QCRequest(BaseModel):
    object_id: PydanticObjectId


@router.post("/qc", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_qc(body: QCRequest) -> JobOut:
    """Queue a QC run over a FASTQ file. Read-only: produces a report."""
    job = await pipeline_service.launch_qc(object_id=body.object_id)
    return JobOut.of(job)
```

### 2.5 Frontend — QC button

The detail panel header already holds Trim and Align (DetailPanel.tsx, in the
`panel-header` row). Add QC beside them with the same gating:

```tsx
const canQC = obj.status === "ready" && obj.format.kind === "fastq";
```

Rendered like its neighbours, disabled while pending. Note the button lives in
the **panel header**, which is outside the tab strip, so it stays reachable from
any tab.

### 2.6 API client — `frontend/src/api/client.ts`

```typescript
launchQC: (objectId: string) =>
  request<JobSummary>("/pipelines/qc", {
    method: "POST",
    body: JSON.stringify({ object_id: objectId }),
  }),
```

### 2.7 Where QC results render

The detail panel now has a **QC tab** (`QcTab` in DetailPanel.tsx) holding
parsed facts, the base-composition and quality charts, and the trim and
alignment reports. QC output belongs there — not in a new top-level section.

`BaseCompositionChart` and `QualityChart` (`SequenceCharts.tsx`) already render
from facts, so populating the right keys reuses them for free. A `QcReport.tsx`
following `TrimReport.tsx`'s shape can add what they do not cover: Q20/Q30,
duplication rate, and a link to the FastQC HTML.

**Serving the HTML is unsolved.** Nothing currently serves static files from
`BIOINFO_HOME`. Linking a FastQC report needs either a new endpoint that streams
it or a mounted static route, plus a decision about escaping — FastQC HTML is
generated from read data and should not be rendered same-origin without thought.
Resolve this before promising a link in the UI.

---

## Implementation Order

```
Phase 1 — Tool probes and metadata (no new runners):
  1. Add cutadapt + trimmomatic to Dockerfile and config
  2. Add probes; add TOOL_META and all_tools_with_meta() to tools.py
  3. Enrich GET /pipelines/tools; update frontend PipelineTool type
  4. Resolve the all_available question in 1.4

Phase 2 — QC pipeline (the real remaining work):
  5. run_qc handler, extending fastp_runner for --report-only
  6. launch_qc() service method
  7. POST /pipelines/qc endpoint
  8. QC button in the detail panel header + launchQC client method
  9. Decide report-serving (2.7) before any HTML link ships

Phase 3 — Multi-tool trim (requires the tool selector):
 10. cutadapt/trimmomatic handlers and per-tool parameter models
 11. launch_trim(tool_name=...)
 12. TrimDialog selectedTool prop
```

---

## Files changed

| File | Change |
|---|---|
| `backend/Dockerfile` | Add `cutadapt` (pip), `trimmomatic` (apt) |
| `backend/app/config.py` | Add `cutadapt_path`, `trimmomatic_path` |
| `backend/app/pipelines/tools.py` | Add two probes, `PipelineType`, `TOOL_META`, `all_tools_with_meta()` |
| `backend/app/pipelines/fastp_runner.py` | Extend for `--report-only` |
| `backend/app/api/v1/pipelines.py` | `POST /qc`; enrich `GET /tools` |
| `backend/app/services/pipeline_service.py` | `launch_qc()`; rename/alias `_check_trimmable` |
| `backend/app/queue/pipeline_handlers.py` | `run_qc` handler |
| `frontend/src/api/client.ts` | `launchQC()` |
| `frontend/src/api/types.ts` | `PipelineTool` with `pipelines`, `summary`, `strengths` |
| `frontend/src/components/DetailPanel.tsx` | QC button in the panel header |
| `frontend/src/components/QcReport.tsx` | New — QC facts display in the QC tab |

## Testing

- **Tool probes**: unit-test with `shutil.which` mocked, asserting `Tool`
  structs for found, missing, and non-zero-exit cases (trimmomatic exercises
  the last, like bwa-mem2 does today).
- **`TOOL_META` coverage**: assert every name in `all_tools()` has an entry, so
  a tool added later cannot silently ship without a description.
- **QC handler**: integration test against a small FASTQ fixture; verify the
  fastp JSON parses into facts and the FastQC HTML is produced.
- **API**: `POST /pipelines/qc` returns 201 with a job; a second identical call
  dedups to the same job id.
- **Frontend**: the QC button renders for FASTQ, is absent for BAM, and stays
  visible on every tab.
