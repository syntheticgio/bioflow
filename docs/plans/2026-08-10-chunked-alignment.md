# Chunked/Sharded Alignment — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.
> **Spec:** `docs/superpowers/specs/2026-08-10-chunked-alignment-design.md`

**Goal:** Add chunked/shared alignment where a multi-sequence reference FASTA is split into memory-budget-aware buckets, reads are aligned against each bucket in parallel sub-jobs, and results are merged.

**Architecture:** A bucket planner packs reference sequences into memory-budget-aware groups. An orchestrator job dispatches one standard `align_reads` sub-job per bucket, tracks completion, and launches a merge step when all succeed. The existing AlignDialog gains a "Chunked alignment" toggle with estimator-driven defaults.

**Tech Stack:** Python 3.12 (backend), React/TypeScript (frontend), samtools, pytest, existing `resource_estimator` / `MemoryModel` / `LoadGovernor`

---

### Task 1: Add `chunking_supported` to `AlignerSpec`

**Objective:** Gate which aligners support chunked alignment.

**Files:**
- Modify: `backend/app/pipelines/aligner_registry.py`

**Step 1: Add field to AlignerSpec**

After `builder_tool` (line 86), add:

```python
    # True when this aligner's index works against a subset of the reference
    # FASTA. STAR's index is tied to the exact reference it was built against;
    # Winnowmap requires whole-reference meryl preprocessing.
    chunking_supported: bool = True
```

**Step 2: Set False for STAR and Winnowmap in REGISTRY**

In the REGISTRY dict, add `chunking_supported=False` to `Aligner.STAR` and `Aligner.WINNOWMAP` entries.

```python
    Aligner.STAR: AlignerSpec(
        aligner=Aligner.STAR,
        ...
        chunking_supported=False,
    ),
    Aligner.WINNOWMAP: AlignerSpec(
        aligner=Aligner.WINNOWMAP,
        ...
        chunking_supported=False,
    ),
```

**Step 3: Verify**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_aligner_registry.py -q`
Expected: All tests pass (new field defaults to True, no existing tests broken).

**Step 4: Commit**

```bash
git add backend/app/pipelines/aligner_registry.py
git commit -m "feat: add chunking_supported field to AlignerSpec"
```

---

### Task 2: Create bucket planner (`align_buckets.py`)

**Objective:** Greedy first-fit-decreasing bucket planner using per-aligner memory models.

**Files:**
- Create: `backend/app/pipelines/align_buckets.py`
- Create: `backend/tests/pipelines/test_align_buckets.py`

**Step 1: Write failing test**

```python
# tests/pipelines/test_align_buckets.py
import pytest
from app.pipelines.align_buckets import BucketSpec, pack_buckets

def test_single_sequence_returns_none():
    """A reference with one sequence doesn't need chunking."""
    result = pack_buckets(
        sequences=[("chr1", 100_000_000)],
        memory_budget_mb=4096,
        per_base_index_mb=3.2 / (1024 * 1024),  # bwa-mem2
    )
    assert result is None

def test_two_large_sequences_make_two_buckets():
    """When each sequence alone fills the budget, you get two buckets."""
    result = pack_buckets(
        sequences=[("chr1", 1_500_000_000), ("chr2", 1_200_000_000)],
        memory_budget_mb=4096,
        per_base_index_mb=3.2 / (1024 * 1024),
    )
    assert result is not None
    assert len(result) == 2
    assert result[0].sequences == ["chr1"]
    assert result[1].sequences == ["chr2"]

def test_small_sequences_pack_together():
    """Small sequences should pack into fewer buckets."""
    result = pack_buckets(
        sequences=[(f"ctg{i}", 10_000_000) for i in range(100)],
        memory_budget_mb=2048,
        per_base_index_mb=3.2 / (1024 * 1024),
    )
    assert result is not None
    assert len(result) < 100  # many should pack together
    assert all(bs.estimated_mb <= 2048 for bs in result)
```

**Step 2: Run test to verify failure**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_align_buckets.py -q
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.align_buckets'`

**Step 3: Write bucket planner implementation**

```python
# backend/app/pipelines/align_buckets.py
"""Split a multi-sequence reference into memory-budget-aware buckets."""

import math
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BucketSpec:
    index: int
    sequences: list[str]
    total_bases: int
    estimated_mb: int

    # Set after FASTA files are written
    fasta_path: Path | None = None


def pack_buckets(
    *,
    sequences: list[tuple[str, int]],  # (name, bases)
    memory_budget_mb: int,
    per_base_index_mb: float,
    fixed_overhead_mb: int = 256,
    bytes_per_thread_mb: int = 512,
    threads: int = 4,
    sort_memory_mb: int = 1024,
) -> list[BucketSpec] | None:
    """Pack reference sequences into memory-budget-aware buckets.

    Returns None when chunking is unnecessary (<= 1 bucket produced).
    """
    if len(sequences) <= 1:
        return None

    worker_mb = threads * bytes_per_thread_mb
    sort_mb = threads * sort_memory_mb
    # The aligner itself + samtools sort overhead
    per_bucket_overhead = fixed_overhead_mb + worker_mb + sort_mb
    effective_budget = memory_budget_mb - per_bucket_overhead

    if effective_budget <= 0:
        # Budget too tight for even the overhead — one bucket, will likely OOM
        total_bases = sum(b for _, b in sequences)
        return [
            BucketSpec(
                index=0,
                sequences=[name for name, _ in sequences],
                total_bases=total_bases,
                estimated_mb=memory_budget_mb,
            )
        ]

    # Sort descending by length — pack the big ones first
    sorted_seqs = sorted(sequences, key=lambda s: s[1], reverse=True)

    buckets: list[BucketSpec] = []

    for name, bases in sorted_seqs:
        seq_index_mb = math.ceil((bases * per_base_index_mb) + per_bucket_overhead)
        placed = False
        for bucket in buckets:
            if bucket.estimated_mb + seq_index_mb + per_bucket_overhead <= memory_budget_mb:
                bucket.sequences.append(name)
                bucket.total_bases += bases
                bucket.estimated_mb += seq_index_mb
                placed = True
                break
        if not placed:
            buckets.append(
                BucketSpec(
                    index=len(buckets),
                    sequences=[name],
                    total_bases=bases,
                    estimated_mb=seq_index_mb + per_bucket_overhead,
                )
            )

    if len(buckets) <= 1:
        return None
    return buckets
```

**Step 4: Run tests**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_align_buckets.py -q
```
Expected: PASS (all 3 tests)

**Step 5: Commit**

```bash
git add backend/app/pipelines/align_buckets.py tests/pipelines/test_align_buckets.py
git commit -m "feat: add memory-aware bucket planner for reference chunking"
```

---

### Task 3: Write per-bucket FASTA file writer

**Objective:** Write per-bucket FASTA files from an indexed reference, with content-hash caching.

**Files:**
- Modify: `backend/app/pipelines/align_buckets.py`
- Modify: `backend/tests/pipelines/test_align_buckets.py`

**Step 1: Write failing test**

Add to `test_align_buckets.py`:

```python
def test_write_bucket_fastas_creates_correct_files(tmp_path):
    """Each bucket gets a FASTA with only its sequences."""
    from app.pipelines.align_buckets import write_bucket_fastas

    # Create a simple 2-sequence FASTA
    fasta = tmp_path / "ref.fa"
    fasta.write_text(">chr1\nAAAA\n>chr2\nGGGG\n")

    buckets = [
        BucketSpec(index=0, sequences=["chr1"], total_bases=4, estimated_mb=100),
        BucketSpec(index=1, sequences=["chr2"], total_bases=4, estimated_mb=100),
    ]

    out_dir = tmp_path / "buckets"
    result = write_bucket_fastas(fasta, buckets, out_dir)

    assert len(result) == 2
    assert result[0].fasta_path == out_dir / "bucket_0.fa"
    assert result[1].fasta_path == out_dir / "bucket_1.fa"
    assert ">chr1" in result[0].fasta_path.read_text()
    assert ">chr2" in result[1].fasta_path.read_text()
```

**Step 2: Run to verify failure**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_align_buckets.py::test_write_bucket_fastas_creates_correct_files -q
```
Expected: FAIL — `write_bucket_fastas` not defined

**Step 3: Implement**

```python
def write_bucket_fastas(
    full_fasta: Path,
    buckets: list[BucketSpec],
    out_dir: Path,
) -> list[BucketSpec]:
    """Write per-bucket FASTA files by extracting sequences from `full_fasta`.

    Uses pyfaidx for indexed extraction. Returns updated BucketSpecs with
    fasta_path set.
    """
    import pyfaidx

    out_dir.mkdir(parents=True, exist_ok=True)
    ref = pyfaidx.Fasta(str(full_fasta))

    for bucket in buckets:
        path = out_dir / f"bucket_{bucket.index}.fa"
        with open(path, "w") as f:
            for name in bucket.sequences:
                seq = ref[name]
                f.write(f">{name}\n{str(seq)}\n")
        bucket.fasta_path = path

    return buckets
```

**Step 4: Run tests**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_align_buckets.py -q
```
Expected: 4 passed

**Step 5: Commit**

```bash
git add backend/app/pipelines/align_buckets.py tests/pipelines/test_align_buckets.py
git commit -m "feat: add per-bucket FASTA file writer with pyfaidx"
```

---

### Task 4: Create orchestrator handler (`align_reads_chunked`)

**Objective:** Handler that enqueues per-bucket sub-jobs, tracks completion, fires merge.

**Files:**
- Create: `backend/app/queue/chunked_align_handlers.py`
- Modify: `backend/app/queue/registry.py` (register handler)

**Step 1: Add handler implementation**

```python
# backend/app/queue/chunked_align_handlers.py
"""Chunked alignment: orchestrator and merge handlers."""

from app.models import IoClass, JobClass, JobResources
from app.queue.handlers import HandlerMode, handler


@handler(
    "align_reads_chunked",
    mode=HandlerMode.ORCHESTRATOR,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=128, io=IoClass.NONE),
    max_attempts=1,
)
def align_reads_chunked(ctx):
    """Orchestrate chunked alignment: enqueue per-bucket sub-jobs.

    The actual alignment work happens in standard `align_reads` sub-jobs.
    This handler only tracks completion and fires the merge step.
    """
    # Stub — filled in Task 5
    pass
```

**Step 2: Register handler**

In `backend/app/queue/handlers.py` (or `registry.py`), import:

```python
from app.queue.chunked_align_handlers import (  # noqa: F401 — registration side-effects
    align_reads_chunked,
)
```

**Step 3: Run handler registry test**

```bash
./backend/run-worktree-tests.sh tests/queue/test_handler_registry.py -q -k "align_reads_chunked"
```
Expected: Handler is registered and discoverable.

**Step 4: Commit**

```bash
git add backend/app/queue/chunked_align_handlers.py backend/app/queue/handlers.py
git commit -m "feat: add chunked alignment orchestrator handler skeleton"
```

---

### Task 5: Implement orchestrator dispatch logic

**Objective:** Full orchestrator: validate payload, enqueue sub-jobs, poll completion, fire merge.

**Files:**
- Modify: `backend/app/queue/chunked_align_handlers.py`

**Step 1: Implement orchestrator**

Replace the stub with:

```python
@handler(
    "align_reads_chunked",
    mode=HandlerMode.ORCHESTRATOR,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=128, io=IoClass.NONE),
    max_attempts=1,
)
def align_reads_chunked(ctx):
    """Orchestrate chunked alignment."""
    from app.queue import queue

    bucket_specs = ctx.payload.get("bucket_specs") or []
    if not bucket_specs:
        raise PermanentError("align_reads_chunked requires 'bucket_specs'")

    reference_id = ctx.payload.get("reference_id")
    owner = ctx.payload.get("owner")
    total = len(bucket_specs)

    # Enqueue one align_reads sub-job per bucket
    sub_job_ids = []
    for spec in bucket_specs:
        sub_payload = dict(ctx.payload)
        sub_payload["reference_path"] = spec["fasta_path"]
        sub_payload["parent_job_id"] = ctx.job_id
        sub_payload["chunk_bucket_index"] = spec["index"]
        sub_payload["chunk_total_buckets"] = total
        del sub_payload["bucket_specs"]  # sub-jobs don't need this
        sub_job_ids.append(
            queue.enqueue(
                "align_reads",
                payload=sub_payload,
                owner=owner,
                parent_job_id=ctx.job_id,
            )
        )

    ctx.progress(phase="aligning", pct=0.0, message=f"Buckets 0/{total}")

    # Poll sub-jobs (sync — orchestrator runs in a thread)
    import time
    from app.models.job import Job, JobState

    failed = None
    completed = 0
    deadline = time.time() + 86400  # 24h hard cap

    while time.time() < deadline:
        ctx.check_cancel()
        completed = 0
        for jid in sub_job_ids:
            job = Job.get(jid)
            if job and job.state == JobState.FAILED:
                failed = jid
                break
            if job and job.state == JobState.SUCCEEDED:
                completed += 1
        if failed:
            raise RetryableError(
                f"Bucket {failed} failed — chunked alignment cannot continue"
            )
        if completed == total:
            break
        ctx.progress(phase="aligning", pct=completed / total,
                     message=f"Buckets {completed}/{total}")
        time.sleep(5)

    ctx.progress(phase="merging", pct=1.0, message="Merging buckets")
    # Merge job will be enqueued by the applier after orchestrator succeeds
    return {
        "bucket_count": total,
        "sub_job_ids": sub_job_ids,
        "reference_id": reference_id,
        "project_id": ctx.payload.get("project_id"),
    }
```

**Step 2: No tests yet for handler (integration). Build to verify imports.**

```bash
make build
```
Expected: Build passes.

**Step 3: Commit**

```bash
git add backend/app/queue/chunked_align_handlers.py
git commit -m "feat: implement chunked alignment orchestrator dispatch"
```

---

### Task 6: Create merge handler (`merge_chunked_buckets`)

**Objective:** samtools merge + sort of per-bucket BAMs.

**Files:**
- Modify: `backend/app/queue/chunked_align_handlers.py`

**Step 1: Add merge handler**

```python
@handler(
    "merge_chunked_buckets",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=4, mem_mb=4096, io=IoClass.HEAVY),
    max_attempts=2,
)
def merge_chunked_buckets(ctx):
    """Merge per-bucket sorted BAMs into one coordinate-sorted BAM."""
    from app.pipelines import tools, align_runner

    samtools = tools.require(tools.samtools())
    bucket_bams = [Path(p) for p in ctx.payload["bucket_bam_paths"]]
    output_dir = Path(ctx.payload["workdir"])
    output_bam = output_dir / ctx.payload["output_name"]

    # merge
    merge_cmd = [samtools.path, "merge", "-c", "-p", str(output_bam.with_suffix(".unsorted.bam"))]
    merge_cmd.extend(str(b) for b in bucket_bams)
    code = run_subprocess(ctx, merge_cmd, log_path=str(output_dir / "merge.log"))
    if code != 0:
        raise RetryableError(f"samtools merge exited {code}")

    # sort
    sort_cmd = align_runner.build_sort_command(
        samtools_path=samtools.path,
        bam=output_bam.with_suffix(".unsorted.bam"),
        output=output_bam,
        threads=min(ctx.payload.get("threads", 4), 8),
        sort_memory_mb=ctx.payload.get("sort_memory_mb", 1024),
    )
    code = run_subprocess(ctx, sort_cmd, log_path=str(output_dir / "sort.log"))
    if code != 0:
        raise RetryableError(f"samtools sort exited {code}")

    # flagstat
    flagstat_cmd = align_runner.build_flagstat_command(
        samtools_path=samtools.path, bam=output_bam
    )
    flagstat_output = run_subprocess(ctx, flagstat_cmd, capture_stdout=True,
                                     log_path=str(output_dir / "flagstat.log"))

    return {
        "output_path": str(output_bam),
        "flagstat": align_runner.parse_flagstat(flagstat_output),
        "project_id": ctx.payload.get("project_id"),
        "reference_id": ctx.payload.get("reference_id"),
    }
```

**Step 2: Commit**

```bash
git add backend/app/queue/chunked_align_handlers.py
git commit -m "feat: add chunked alignment merge handler"
```

---

### Task 7: Create chunked alignment applier

**Objective:** Create the final merged BAM DataObject and chain post-alignment pipeline.

**Files:**
- Create: `backend/app/queue/chunked_align_results.py`
- Modify: `backend/app/queue/results.py` (register in `_APPLIERS`)

**Step 1: Write applier**

```python
# backend/app/queue/chunked_align_results.py
"""Applier for chunked alignment: create merged BAM, chain post-alignment steps."""

from pathlib import Path
from beanie import PydanticObjectId
from app.logging import get_logger
from app.models import ObjectRole, ObjectStatus
from app.models.object import DataObject
from app.services import object_service

log = get_logger(__name__)


async def _apply_chunked_alignment(result: dict, *, owner: str) -> None:
    """Take the merged BAM, create a DataObject, and chain index/stats/headers."""
    from app.queue import queue
    from app.pipelines import aligners

    output_path = Path(result["output_path"])
    project_id = PydanticObjectId(result["project_id"])
    reference_id = PydanticObjectId(result["reference_id"])
    reads_id = PydanticObjectId(result.get("reads_object_id") or result["reads_object_id"])
    name = result.get("output_name", output_path.name)
    aligner_name = result.get("aligner", "unknown")
    tool_version = result.get("tool_version", "")
    params = result.get("params", {})

    facts = {
        "aligned_by": aligner_name,
        "aligner_version": tool_version,
        "align_params": params,
        "chunked": True,
        "chunk_bucket_count": result.get("bucket_count", 0),
    }

    obj = await object_service.ingest_local_file(
        owner=owner,
        project_id=project_id,
        path=output_path,
        name=name,
        role=ObjectRole.ALIGNMENT,
        derived_from=[reads_id, reference_id],
        facts=facts,
    )

    # Chain the standard post-alignment pipeline
    queue.enqueue(
        "index_bam",
        payload={"object_id": str(obj.id), "project_id": str(project_id)},
        owner=owner,
    )
    queue.enqueue(
        "ingest_headers",
        payload={"object_id": str(obj.id), "project_id": str(project_id)},
        owner=owner,
    )
    queue.enqueue(
        "run_bam_stats",
        payload={"object_id": str(obj.id), "project_id": str(project_id)},
        owner=owner,
    )

    log.info("chunked_alignment_applied", object_id=str(obj.id), bucket_count=result.get("bucket_count"))
```

**Step 2: Register in _APPLIERS**

In `results.py`, add import:

```python
from app.queue.chunked_align_results import _apply_chunked_alignment
```

And in `_APPLIERS` dict:

```python
    "merge_chunked_buckets": _apply_chunked_alignment,
```

**Step 3: Build**

```bash
make build
```
Expected: Build passes.

**Step 4: Commit**

```bash
git add backend/app/queue/chunked_align_results.py backend/app/queue/results.py
git commit -m "feat: add chunked alignment applier with post-alignment chain"
```

---

### Task 8: Add align-envelope chunked variant

**Objective:** Extend `GET /pipelines/align-envelope` to return bucket estimates when `?chunked=true`.

**Files:**
- Modify: `backend/app/services/pipeline_service.py` (`align_envelope` function)
- Modify: `backend/app/api/v1/pipelines.py` (add `chunked` query param)

**Step 1: Add chunked query param to endpoint**

In `pipelines.py`, modify `align_envelope`:

```python
@router.get("/align-envelope")
async def align_envelope(
    object_id: PydanticObjectId,
    reference_id: PydanticObjectId,
    owner: OwnerDep,
    chunked: bool = False,
) -> dict:
    return await pipeline_service.align_envelope(
        object_id=object_id,
        reference_id=reference_id,
        owner=owner,
        chunked=chunked,
    )
```

**Step 2: Add chunking logic to pipeline_service.align_envelope**

```python
async def align_envelope(
    *, object_id, reference_id, owner, chunked=False
) -> dict:
    result = await _align_envelope(object_id, reference_id, owner)

    if chunked:
        ref = await object_service.get_object(reference_id, owner=owner)
        # Find the .fai sidecar
        sidecars = await object_service.list_sidecars(ref.id, owner=owner)
        fai = next((s for s in sidecars if s.sidecar_role == SidecarRole.FAI), None)

        if fai:
            # Read sequence lengths from .fai
            sequences = _read_fai(fai)
            spec = aligner_registry.spec_for(aligner)
            if spec.chunking_supported and len(sequences) > 1:
                governor = LoadGovernor()
                budget_mb = (governor.memory_limit_bytes or 8 * 1024**3) // (1024 * 1024)
                buckets = align_buckets.pack_buckets(
                    sequences=sequences,
                    memory_budget_mb=budget_mb,
                    per_base_index_mb=spec.memory_model.index_bytes_per_ref_base / (1024 * 1024),
                    fixed_overhead_mb=spec.memory_model.fixed_overhead_mb,
                    bytes_per_thread_mb=spec.memory_model.bytes_per_thread_mb,
                )
                if buckets:
                    result["chunking"] = {
                        "supported": True,
                        "buckets": len(buckets),
                        "per_bucket_mb": max(b.estimated_mb for b in buckets),
                        "per_bucket_bases": max(b.total_bases for b in buckets),
                        "total_sequences": len(sequences),
                    }
                else:
                    result["chunking"] = {"supported": False}
            else:
                result["chunking"] = {"supported": False}
        else:
            result["chunking"] = {"supported": False}
    return result
```

**Step 3: Build**

```bash
make build
```
Expected: Build passes.

**Step 4: Commit**

```bash
git add backend/app/api/v1/pipelines.py backend/app/services/pipeline_service.py
git commit -m "feat: add chunked alignment preview to align-envelope endpoint"
```

---

### Task 9: Branch launch_alignment for chunked path

**Objective:** When `chunked=true`, invoke bucket planner and enqueue orchestrator instead of direct `align_reads`.

**Files:**
- Modify: `backend/app/services/pipeline_service.py`

**Step 1: Add chunked branch**

In `launch_alignment`, after validation, add:

```python
    if params.get("chunked"):
        # --- Chunked path ---
        ref = await object_service.get_object(reference_id, owner=owner)
        sidecars = await object_service.list_sidecars(ref.id, owner=owner)
        fai = next((s for s in sidecars if s.sidecar_role == SidecarRole.FAI), None)
        if not fai:
            raise ValidationError("Reference has no .fai index — cannot chunk")

        sequences = _read_fai(fai)
        spec = aligner_registry.spec_for(aligner)
        governor = LoadGovernor()
        budget_mb = (governor.memory_limit_bytes or 8 * 1024**3) // (1024 * 1024)

        buckets = align_buckets.pack_buckets(
            sequences=sequences,
            memory_budget_mb=budget_mb,
            per_base_index_mb=spec.memory_model.index_bytes_per_ref_base / (1024 * 1024),
            fixed_overhead_mb=spec.memory_model.fixed_overhead_mb,
            bytes_per_thread_mb=spec.memory_model.bytes_per_thread_mb,
            threads=params.get("threads", 4),
            sort_memory_mb=params.get("sort_memory_mb", 1024),
        )

        if buckets is None:
            # Single bucket — fall through to normal path
            pass
        else:
            # Write per-bucket FASTAs
            cache_dir = settings.tmp_dir / "chunked-refs" / _ref_content_hash(fai)
            buckets = align_buckets.write_bucket_fastas(
                _find_fasta_path(ref), buckets, cache_dir
            )

            return await queue.enqueue(
                "align_reads_chunked",
                payload={
                    "bucket_specs": [asdict(b) for b in buckets],
                    "reference_id": str(reference_id),
                    "reference_path": _find_fasta_path(ref),
                    "reads": reads_payload,
                    "aligner": aligner.value,
                    "params": params,
                    "project_id": str(project_id),
                    "owner": owner,
                    "reads_object_id": str(reads_object_id),
                },
                owner=owner,
            )

    # --- Normal path (unchanged) ---
    return await queue.enqueue("align_reads", payload=..., owner=owner)
```

**Step 2: Build**

```bash
make build
```
Expected: Build passes.

**Step 3: Commit**

```bash
git add backend/app/services/pipeline_service.py
git commit -m "feat: branch launch_alignment for chunked path"
```

---

### Task 10: Add chunked toggle to AlignDialog

**Objective:** "Chunked alignment" checkbox with bucket preview in the React dialog.

**Files:**
- Modify: `frontend/src/components/AlignDialog.tsx`
- Modify: `frontend/src/api/types.ts`

**Step 1: Add chunking to AlignDefaults type**

```typescript
// types.ts
export interface AlignDefaults {
  // ... existing fields ...
  chunking?: {
    supported: boolean;
    buckets: number;
    per_bucket_mb: number;
    per_bucket_bases: number;
    total_sequences: number;
  } | null;
}
```

**Step 2: Add toggle to AlignDialog**

After the aligner selector, add:

```tsx
{defaults?.chunking?.supported && (
  <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 12 }}>
    <input
      type="checkbox"
      checked={chunked}
      onChange={(e) => setChunked(e.target.checked)}
    />
    <span>
      Chunked alignment — split the reference and align in parallel
      {chunked && defaults.chunking && (
        <span style={{ display: "block", fontSize: 11, color: "var(--text-faint)", marginTop: 2 }}>
          {defaults.chunking.buckets} buckets at ~{defaults.chunking.per_bucket_mb} MB each
          ({defaults.chunking.per_bucket_bases.toLocaleString()} bases/bucket)
        </span>
      )}
    </span>
  </label>
)}
```

Add state: `const [chunked, setChunked] = useState(false);`

**Step 3: Include chunked in launch payload**

When chunked is checked, add `chunked: true` to the launch request body.

**Step 4: Build**

```bash
make build
```
Expected: Build passes, no TypeScript errors.

**Step 5: Commit**

```bash
git add frontend/src/components/AlignDialog.tsx frontend/src/api/types.ts
git commit -m "feat: add chunked alignment toggle to AlignDialog"
```

---

### Task 11: Integration verification

**Objective:** Run full test suite, verify no regressions.

**Step 1: Run all tests**

```bash
./backend/run-worktree-tests.sh tests/ -q 2>&1 | tail -5
```

**Step 2: Run frontend build**

```bash
make build
```

**Step 3: Verify existing alignment tests pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_align_params.py tests/pipelines/test_align_defaults.py tests/pipelines/test_aligner_registry.py tests/pipelines/test_align_runner.py -q
```

**Step 4: Final commit**

```bash
git add -A
git commit -m "feat: complete chunked/sharded alignment implementation"
```

---

### Verification Checklist

- [ ] `aligner_registry.py` — STAR and Winnowmap have `chunking_supported=False`
- [ ] `align_buckets.py` — bucket planner passes unit tests (single seq, two seqs, packing)
- [ ] `chunked_align_handlers.py` — orchestrator and merge handlers registered
- [ ] `chunked_align_results.py` — applier creates BAM, chains post-alignment
- [ ] `pipeline_service.py` — chunked branch in launch_alignment, envelope returns bucket preview
- [ ] `AlignDialog.tsx` — toggle shown/hidden correctly, preview readout displays
- [ ] Existing alignment tests pass (no regressions)
- [ ] Full test suite passes
- [ ] `make build` succeeds
