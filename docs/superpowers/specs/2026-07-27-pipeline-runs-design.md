# Grouping jobs into the run a user asked for

Date: 2026-07-27
Status: Approved, ready for implementation planning

## Problem

Clicking **Align** once produces seven rows in the activity view. Measured on a
real run: `build_index`, `align_reads`, `index_bam`, and four `ingest_headers`
jobs — one per produced file (the BAM, its `.bai`, the aligner index, the
`.fai`). A trim produces three. The view shows the machine's decomposition of
the work rather than the work the user asked for.

The information needed to describe the run is already present but stranded.
`align_reads` carries the reads, the reference, the aligner, the parameters and
the read group in its payload; no other job in the chain knows any of it.
`build_index` knows only its reference, `index_bam` only its BAM.

The existing linkage cannot express the grouping either, and is inconsistent
three ways:

- `index_bam` sets `parent_job_id` — the only job that does.
- `align_reads` has `depends_on` pointing *backwards* at its index build, which
  is a scheduling constraint rather than a statement of membership.
- `build_index` and `ingest_headers` have neither.

So a parent-chain walk finds almost nothing today, and even a complete one
would misrepresent the shape: `build_index` is deduplicated by content and
genuinely shared between runs, which a tree cannot express.

## Goal

The activity view lists **runs** — "align these reads against this reference
with these parameters" — each expandable into the jobs that carried it out. A
run shows its inputs, its parameters and its outputs, and remains a readable
record of what was run after its jobs have been pruned.

## Decisions

Recorded with their reasoning, because the reasoning constrains the
implementation.

**A run is a user intent, not an execution graph.** This is deliberately not
the generic DAG engine the alignment design put out of scope, and the
distinction is worth holding onto: a run records *what was asked for* and which
jobs served it. It does not describe execution order, express fan-out, or
schedule anything — `depends_on` already does the scheduling and stays
untouched. The test for future work is whether it describes a user's request or
the machine's plan; only the former belongs here.

**A first-class document rather than a correlation id.** A `run_id` stamped on
each job would group them with a single indexed field and no new collection,
which is cheaper. It was rejected for two reasons. A shared index build can
carry only one `run_id`, so the second alignment to reuse an index would show a
gap where the build was. And jobs are TTL-pruned after 30 days, so a run
described only by its jobs stops being describable — precisely when a record of
what was run is most valuable.

**Inputs and parameters are denormalized onto the run.** The label, the input
names, and the parameters are copied rather than referenced. A run must remain
readable after its jobs expire and after its input objects are deleted, and a
run whose description dissolves when a file is removed is not a record. The
object ids are kept alongside the names so a still-present input can be linked.

**Membership is a link collection, not an array on the run.** `RunJob` rows
carry `run_id`, `job_id`, a role, and a `shared` flag. An array of job ids on
the run would be simpler, but cannot express one job belonging to two runs —
which is exactly the deduplicated index case, and the case that ruled out the
correlation id. The `shared` flag is what lets the second run show the build as
*reused* rather than omitting it or claiming to have done it.

**Status is recomputed on read, never stored.** A stored status is a second
source of truth about something the jobs already know, and it drifts the first
time a write is lost — the failure mode the queue's own reconciler exists for.
Deriving it when serving costs one extra query for a run of about seven jobs,
at single-user scale, and removes the entire class of bug. The cost is that
"show me failed runs" cannot be an indexed Mongo query; if that becomes wanted,
a cached field can be added *behind* the derivation rather than instead of it.

**Ingest jobs are members, but collapsed.** `ingest_headers` on a produced file
is part of the run that produced it — the file would not exist otherwise. But
four near-identical rows would reproduce the noise this exists to remove, so
they fold into a single summary line ("4 files ingested"), expandable. The same
job type fired by an ordinary upload has no run and is unaffected.

**Trimming is included from the start.** A trim produces three rows for one
action and has the same problem in miniature. Covering both makes the activity
view uniformly grouped rather than half-grouped, and exercises the model
against two shapes before variant calling arrives to test it properly.

## Data model

```python
class RunKind(StrEnum):
    ALIGNMENT = "alignment"
    TRIM = "trim"


class RunStatus(StrEnum):
    """Derived, never stored. Listed here as the vocabulary of the API."""
    WAITING = "waiting"      # nothing started; at least one job blocked or queued
    RUNNING = "running"      # any member running
    SUCCEEDED = "succeeded"  # every member succeeded
    FAILED = "failed"        # any member failed, dead, or cancelled
    PARTIAL = "partial"      # finished, but an optional member did not succeed


class RunInputRole(StrEnum):
    READS = "reads"
    MATE = "mate"
    REFERENCE = "reference"


class RunInput(BaseModel):
    object_id: PydanticObjectId
    name: str                # copied: the object may be deleted later
    role: RunInputRole


class PipelineRun(TimestampedDocument):
    kind: RunKind
    project_id: PydanticObjectId
    # "specimen_R1.fastq.gz → ecoli_ref.fna". Built at launch, when every part
    # is known and present.
    label: str
    inputs: list[RunInput] = Field(default_factory=list)
    params: dict = Field(default_factory=dict)
    outputs: list[PydanticObjectId] = Field(default_factory=list)
```

Indexed on `(project_id, created_at DESC)` for the activity listing.

```python
class RunJobRole(StrEnum):
    INDEX = "index"
    ALIGN = "align"
    TRIM = "trim"
    INDEX_BAM = "index_bam"
    INGEST = "ingest"


class RunJob(TimestampedDocument):
    run_id: PydanticObjectId
    job_id: PydanticObjectId
    role: RunJobRole
    # True when this run reused a job another run created -- a deduplicated
    # index build. The run did not do this work but depended on it.
    shared: bool = False
```

Indexed on `run_id` and on `job_id`; the latter answers "which run does this
job belong to?" for the job detail view.

`Job` is unchanged. Nothing about scheduling moves into this model.

## Status derivation

```python
def derive_status(jobs: list[Job], *, optional_roles: set[RunJobRole]) -> RunStatus
```

Pure, and tested as such — the interesting cases are all orderings that are
awkward to reproduce against a live queue.

Rules, in precedence order:

1. Any member failed, dead, or cancelled in a **required** role → `FAILED`.
2. Any member running → `RUNNING`.
3. Any member queued, blocked, delayed, or pending → `WAITING`.
4. All required members succeeded, some optional member did not → `PARTIAL`.
5. Otherwise → `SUCCEEDED`.

A member job that no longer exists — pruned by the TTL — counts as succeeded.
The alternative is a run that spontaneously reports failure a month later,
which would be a lie about something that worked.

`INGEST` is the only optional role. An alignment whose BAM was produced but
whose header parse failed is not a failed alignment; the file is there and can
be re-ingested. `PARTIAL` says exactly that, rather than overstating with
`FAILED` or hiding it with `SUCCEEDED`.

## Creating runs

`launch_alignment` and `launch_trim` create the run before enqueueing anything
and link each job as it is created. Two cases need care:

**A deduplicated index build.** `_enqueue_build_index` already returns `None`
when an identical build is queued or running, and the launch path already looks
up the existing job to depend on. That existing job is linked to the new run
with `shared=True` — the one place the many-to-many link earns itself.

**Jobs enqueued later.** `index_bam` is enqueued from the `align_reads`
applier, not at launch, because it needs the BAM's digest. The applier resolves
the run from the `align_reads` job via `RunJob.job_id` and links the new job to
the same run. `ingest_headers` jobs, enqueued from `ingest_local_file`, are
linked the same way, with the producing job's run as the target.

A job whose run cannot be resolved is simply not linked. Grouping is a
presentation concern, and failing a pipeline over it would be absurd.

## API

```
GET  /api/v1/runs?project_id=&limit=      list, newest first, with derived status
GET  /api/v1/runs/{run_id}                one run, its jobs, and their states
```

The activity view fetches runs and loose jobs together. A job with no `RunJob`
row — `verify_files`, `reap_pipeline_scratch`, a manual re-ingest — is listed
individually exactly as today.

## UI

One row per run: label, derived status, and aggregate progress. Expanding shows
the member jobs with their individual states, the parameters the run was
launched with, and links to the objects it produced. Ingest members collapse
into a count.

A shared index build appears in the group marked *reused*, linking to the run
that built it.

Cancelling a run cancels its member jobs. The reverse — cancelling one job of a
run — already works and leaves the run `FAILED`, which is honest.

## Not in scope

Re-running a run from its record. It is the obvious next thing and the record
now makes it possible, but it wants its own thinking about what "same
parameters, new inputs" means.

Runs for anything not launched from the pipeline service: verification sweeps,
GC, uploads. They are not user-requested multi-step work and grouping them
would stretch the concept past what it means.

Filtering or searching runs by status, which the derived-status decision
deliberately trades away until there is a reason to want it.
