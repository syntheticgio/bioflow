# Chunked/Sharded Alignment — Design Spec

> **Issue:** [#121](https://github.com/syntheticgio/bioflow/issues/121) — Add chunked/sharded alignment: split references into buckets, align in parallel, combine results

**Goal:** Enable alignment jobs to split a multi-sequence reference FASTA into memory-aware buckets, align reads against each bucket in parallel sub-jobs, and merge the results — so large references (e.g. human) that don't fit in memory as a single alignment can still be aligned on the same machine.

**Architecture:** A bucket planner packs reference sequences into memory-budget-aware groups. An orchestrator job dispatches one standard `align_reads` sub-job per bucket, tracks completion, and launches a merge step when all succeed. The existing AlignDialog gains a "Chunked alignment" toggle with estimator-driven defaults. All-or-nothing failure: one exhausted bucket fails the whole job.

**Tech Stack:** Python (backend), React/TypeScript (frontend), samtools merge/sort, existing `resource_estimator` / `MemoryModel` / `LoadGovernor`

---

## 1. Architecture

```
User checks "Chunked" → AlignDialog → align-envelope?chunked=true
                                            ↓
                                   BucketPlanner.pack()
                                   (reads .fai, uses MemoryModel
                                    + LoadGovernor budget)
                                            ↓
                                   Returns {buckets, per_bucket_mb}
                                            ↓
launch_alignment(chunked=True)
    → BucketPlanner writes per-bucket FASTA files (tmp/, content-hash cached)
    → Enqueue orchestrator: align_reads_chunked
                                  ↓
         ┌────────────────────────┼────────────────────────┐
         ↓                        ↓                        ↓
    align_reads              align_reads              align_reads
    (bucket 1)               (bucket 2)               (bucket N)
    ─────────                ─────────                ─────────
    parent_job_id            parent_job_id            parent_job_id
         ↓                        ↓                        ↓
    bucket_1.bam             bucket_2.bam             bucket_N.bam
         └────────────────────────┼────────────────────────┘
                                  ↓
                         merge_chunked_buckets
                         (samtools merge → sort)
                                  ↓
                         index_bam → flagstat
                                  ↓
                         Final merged BAM DataObject
```

**New files:**

| File | Purpose |
|---|---|
| `backend/app/pipelines/align_buckets.py` | Bucket planner: FAI parse, greedy pack, budget math |
| `backend/app/queue/chunked_align_handlers.py` | `align_reads_chunked` handler + `merge_chunked_buckets` handler |
| `backend/app/queue/chunked_align_results.py` | `_apply_chunked_alignment` applier |

**Modified files:**

| File | Change |
|---|---|
| `backend/app/pipelines/aligner_registry.py` | Add `chunking_supported: bool` to `AlignerSpec` (False for STAR, Winnowmap) |
| `backend/app/services/pipeline_service.py` | `launch_alignment` branches to chunked path; new `align_envelope` chunked variant; new `reference_index_status` unchanged |
| `backend/app/queue/registry.py` | Register two new handlers |
| `backend/app/queue/results.py` | Register new applier; wire up `APPLIERS` dict |
| `frontend/src/components/AlignDialog.tsx` | Chunked toggle, bucket preview readout |
| `frontend/src/api/types.ts` | `AlignDefaults` gains optional `chunking` field |

---

## 2. Bucket Planner (`align_buckets.py`)

### 2.1 Algorithm

```
Input:  FASTA path (.fai available), Aligner, memory_budget_mb
Output: list[BucketSpec] | None (None = single bucket, chunking unnecessary)

1. Parse .fai → [(name, length), ...]
2. If len(sequences) <= 1: return None
3. Sort sequences by length descending
4. per_seq_mb = estimate_mb(aligner, reference_bases=seq_length, threads=1, sort_memory_mb=0, building_index=False)
5. effective_budget = memory_budget_mb - aligner_memory_model.fixed_overhead_mb
6. Greedy first-fit-decreasing:
   - For each sequence (largest first):
     - Find first bucket where (bucket.total_mb + per_seq_mb) <= effective_budget
     - If none: open new bucket
7. If len(buckets) == 1: return None (single bucket → no chunking needed)
8. Return buckets
```

### 2.2 Per-bucket FASTA files

- Written under `settings.tmp_dir / "chunked-refs" / <content_hash_of_full_ref> /`
- Named `bucket_<N>.fa` where N is 0-indexed
- Content-hash caching: if the full reference FASTA hasn't changed, the bucket FASTAs are reused. Hash = SHA-256 of the sorted concatenation of `(bucket_index, bucket_sequence_names)`.
- Cleanup by `reap_pipeline_scratch` (age-based, same as all tmp/ content).

### 2.3 BucketSpec

```python
@dataclass
class BucketSpec:
    index: int
    sequences: list[str]       # sequence names from .fai
    total_bases: int
    estimated_mb: int
    fasta_path: Path           # per-bucket FASTA file
```

---

## 3. Resource Estimation

### 3.1 Budget source

The `memory_budget_mb` comes from `LoadGovernor.memory_limit_bytes` (reads cgroup v1/v2 inside Docker, or total system memory on bare metal) minus a 10% headroom margin. This is the same source `classify()` uses for the BLOCK band — consistent with the existing estimator.

### 3.2 Per-bucket estimate

Each bucket's `estimated_mb` is `estimate_mb()` for the aligner against just that bucket's sequences, with the user's chosen thread count and sort memory. The orchestrator sends each sub-job's resource reservation as the per-bucket estimate rather than the whole-reference estimate.

### 3.3 Minimum bucket size

If a single sequence's estimated memory exceeds `effective_budget`, the planner still creates a single-sequence bucket with a warning flag. The orchestrator shows: "chr1 is 248M bases — the estimator says it needs ~5 GB but the budget is 4 GB. It will still run; the estimator may be conservative." The existing `classify()` BLOCK vs WARN distinction applies per-bucket.

### 3.4 AlignEnvelope integration

The existing `GET /pipelines/align-envelope` endpoint gains an optional `?chunked=true` query parameter. When set, it runs the planner and returns:

```json
{
  "chunking": {
    "supported": true,
    "buckets": 4,
    "per_bucket_mb": 6144,
    "per_bucket_bases": 750000000,
    "total_sequences": 24
  }
}
```

When `supported` is false (1 sequence, STAR, Winnowmap), the field is `null` and the frontend hides the toggle.

---

## 4. Orchestrator Job

### 4.1 `align_reads_chunked` handler

```
Payload:
  bucket_specs: list[dict]     # from BucketPlanner, serialized
  reads: list[dict]            # read file paths (same as normal align)
  aligner: str
  params: dict                 # full AlignParams (shared across buckets)
  reference_id: str            # the original full reference (for provenance)

Flow:
1. Validate: bucket_specs non-empty, reads present, aligner supports chunking
2. For each bucket_spec:
   - Enqueue align_reads job with:
       payload.reference_path = bucket_spec.fasta_path  # per-bucket FASTA
       payload.parent_job_id = orchestrator_job_id
       payload.chunk_bucket_index = bucket_spec.index
       payload.chunk_total_buckets = len(bucket_specs)
   - Record sub-job ID
3. Report progress: "Bucket 1/4 starting" → "Bucket 1/4 aligning" → ...
4. Poll sub-job status via the queue's job-status endpoint or MongoDB
5. When all succeed: enqueue merge_chunked_buckets
6. When any fails (after retries): mark orchestrator FAILED, error names the bucket
```

### 4.2 Concurrency

Sub-jobs are enqueued all at once. The queue's `LoadGovernor` naturally serializes them — only as many as the machine can handle run simultaneously. No artificial sequencing.

### 4.3 Cancel propagation

Cancelling the orchestrator cancels all sub-jobs. The existing `cancel_requested` mechanism handles this: the orchestrator's cancel handler iterates sub-job IDs and sets `cancel_requested` on each.

---

## 5. Merge Job (`merge_chunked_buckets`)

### 5.1 Handler

```
Input:
  bucket_bam_paths: list[Path]   # per-bucket sorted BAM paths
  output_name: str               # final BAM name

Steps:
1. samtools merge -c -p output.unsorted.bam bucket_*.bam
   (-c = combine @PG headers, -p = create @PG header for merge step)
2. samtools sort -@ N output.unsorted.bam -o output.bam
3. Cleanup: remove output.unsorted.bam, per-bucket bams (moved by applier)

Returns: {output_path, flagstat: {...}}
```

The merge handler does NOT build the index — `index_bam` is a separate follow-on job (same as normal alignment), queued by the applier with `produced_by_job` pointing to the merge job.

### 5.2 Flagstat

The existing `flagstat` extraction in `align_runner.py` runs as a final step. The flagstat from the merged BAM is recorded on the final DataObject, exactly as normal alignment does.

---

## 6. Applier (`_apply_chunked_alignment`)

### 6.1 Object model

The final merged BAM becomes a DataObject with:
- `derived_from` = the reads file(s) + the full reference
- `produced_by_job` = the merge job ID
- `role` = `ObjectRole.ALIGNMENT`
- Facts: `aligned_by`, `aligner_version`, `align_params`, `chunked: true`, `chunk_bucket_count: N`
- Per-bucket BAMs: registered as intermediate outputs, GC'd once the merged BAM exists

### 6.2 Post-merge pipeline

After the merge applier creates the BAM DataObject, the normal post-alignment chain fires:
1. `index_bam` — builds `.bai`
2. `ingest_headers` — extracts BAM header for the explorer
3. `run_bam_stats` — computes BAM statistics (coverage, etc.)

This is the exact same chain `_apply_align_reads` uses — no new code.

### 6.3 Provenance

The methods-paragraph renderer already handles branching (from #119 fix). The merge step is a natural branch-convergence point: "These per-bucket alignments were merged with samtools merge."

---

## 7. UI

### 7.1 AlignDialog changes

New checkbox below the aligner selector:

```
☐ Chunked alignment — split the reference and align in parallel
   4 buckets at ~6 GB each (750M bases/bucket)
```

- Checkbox hidden when `chunking.supported === false`
- The readout appears when checked, populated from the align-envelope response
- No additional configuration fields — the estimator drives everything
- Existing threads/sort-memory sliders apply per-bucket (unchanged behavior)

### 7.2 Launch payload

When chunked is checked, the launch request includes `chunked: true`. The backend branches to the chunked path in `launch_alignment`.

### 7.3 Activity tab

The orchestrator appears as one activity row. Expanding it shows per-bucket sub-jobs nested underneath, each with its own progress, same as the workflow run view already does for pipeline nodes. The `parent_job_id` field already supports this hierarchy.

---

## 8. Edge Cases & Failure Modes

### 8.1 Single-sequence reference

Planner returns `None`. The toggle is hidden. Launch goes through the normal path. No behavior change.

### 8.2 STAR / Winnowmap

Gated out. `AlignerSpec.chunking_supported` is `False` for these. The planner is never invoked. The toggle is hidden.

### 8.3 All buckets succeed except memory estimate was wrong

The estimator is a heuristic, and a bucket that "should" fit might still OOM. The sub-job fails → retried (max_attempts from handler registration). If it keeps failing, the orchestrator fails with the bucket name. The user sees which bucket failed and can adjust. On the next attempt with manual thread/sort-memory reduction (fewer threads = less worker memory), the bucket fits.

### 8.4 Disk space for per-bucket FASTAs and BAMs

Per-bucket FASTAs: human genome as 24 buckets → 24 files totalling ~3.2 GB (same as the full FASTA, just split). Per-bucket BAMs: roughly proportional to reads divided by buckets. The merge produces one final BAM (same size as normal alignment's BAM). Total temp disk: ~1.5× the normal alignment's disk usage, mostly the per-bucket BAMs. Acceptable — same order of magnitude.

### 8.5 Bucket planner on very fragmented references

10K contigs, greedy FFD: O(n × m) where n=10K and m is bucket count (~10-20). Sort-by-length: O(n log n). FAI parse: O(n). Total: milliseconds. Not a performance concern.

### 8.6 Concurrent chunked alignments

Two users aligning different samples against the same reference with chunking: each gets its own orchestrator, its own sub-jobs, its own per-bucket FASTAs (content-hash cached — only written once). The queue governor serializes CPU/memory-intensive work naturally. No new locking needed.

---

## 9. Aligner Gating

| Aligner | Chunking | Reason |
|---|---|---|
| bwa-mem2 | ✓ | Index built for full ref; accepts subsets |
| minimap2 | ✓ | Index built for full ref; accepts subsets |
| bowtie2 | ✓ | Index built for full ref; accepts subsets |
| HISAT2 | ✓ | Index built for full ref; accepts subsets |
| STAR | ✗ | Index tied to exact reference FASTA |
| Winnowmap | ✗ | Requires whole-reference meryl preprocessing |

---

## 10. Verification

- [ ] Single-sequence reference: toggle hidden, launch uses normal path
- [ ] Multi-sequence reference, chunking unchecked: launch uses normal path
- [ ] Multi-sequence reference, chunking checked: N buckets shown in preview, launch creates orchestrator + N sub-jobs
- [ ] All sub-jobs succeed: merge runs, final BAM produced, index + flagstat applied
- [ ] One sub-job fails (after retries): orchestrator fails, error names the bucket, no partial BAM produced
- [ ] Cancel orchestrator: all sub-jobs cancelled
- [ ] STAR aligner: toggle hidden regardless of reference
- [ ] Memory budget unavailable (bare metal): planner defaults to conservative budget, alignment runs
- [ ] Two concurrent chunked alignments: separate orchestrators, no interference
- [ ] Methods paragraph: merge step renders with branch-join verb (from #119 fix)
- [ ] Existing normal alignment: no behavior change, all existing tests pass
