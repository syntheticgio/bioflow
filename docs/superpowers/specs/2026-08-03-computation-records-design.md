# Computation records

One durable record per completed job, capturing what the run cost and what it
ran on. Three readers off one write: the duration model, a new memory model,
and per-object provenance.

## Why this, and what already exists

A meaningful part of this already ships. `app/models/timing.py` defines
`JobRunTiming` (`job_type`, `input_bytes`, `duration_ms`, plus never-populated
`format_kind`/`compression`), `app/services/timing_service.py` fits duration
against bytes per job type, `queue/executor.py:126` writes one row per
successful job, and `api/v1/jobs.py:244` surfaces the estimate.

So wall time and a thin slice of input size are done. Peak CPU, peak RAM, the
invocation, the machine, and richer input features are not.

Two other modules are relevant. `queue/governor.py` already samples CPU and
memory via psutil for admission control, and knows the difference between what
psutil reports and what a cgroup actually allows. `pipelines/resource_estimator.py`
predicts peak memory from coefficients taken from published tool documentation
rather than from measurement -- its own docstring says so. Measured peaks are
what would eventually replace those coefficients.

This design widens `JobRunTiming` rather than adding a parallel system.

## Scope

In:

- Widen the record to cover resources, machine, invocation, and input features.
- Sample peak CPU and RSS per job from the executor.
- Promote `threads` into the duration model.
- A memory model alongside the duration model.
- Surface all three (provenance, duration estimate, memory estimate) through
  endpoints that already exist.

Out, deliberately:

- **Upload and aggregation.** The eventual goal is an opt-in upload to a
  central server that aggregates across users. Nothing here implements it; the
  design only ensures records are portable and free of local identifiers so
  that work is possible later without a migration.
- **Replacing `resource_estimator.py`'s coefficients.** The measured model runs
  alongside so the two can be compared. Cutting over before there is evidence
  would trade a known-imprecise number for an unvalidated one.
- **Per-job-type structured feature schemas.** Features are an opportunistic
  dict; formalizing per handler would stall the whole change behind a decision
  for each one.
- **Retention and pruning.** Records are kept indefinitely. At a few hundred
  bytes per job on a single-user tool, thousands of runs is single-digit MB.

## The record

One collection, one document per completed job. Fields group as follows.

### Identity

`job_type`, `job_id`, `object_id`, `project_id`, `finished_at`, `outcome`,
`schema_version`.

`schema_version` exists for the upload plan specifically: a server aggregating
rows written by many BioFlow versions needs to know which fields a given row
can be trusted to mean.

`outcome` is a change from current behavior. `executor.py:129` records
successful runs only, on the reasoning that a job which failed after 200ms
would drag every future estimate down. That reasoning is correct for the
*model* and wrong for *provenance*: a failed run is the most valuable record a
user can read, and an OOM kill is the single best memory signal available. So
failures are recorded and tagged, and the predictors filter to successes.

The cost of this is worth stating. Today the collection is clean by
construction -- `executor.py` writes on the success path and nowhere else, so
`_samples()` needs no outcome filter and has none. Merging failures in moves
that invariant from one write path to every read path, and the failure mode is
silent: a job that OOM-killed at ninety seconds reads as a fast, cheap run
whose peak RSS is the ceiling it hit rather than what it needed. A few of those
in a fit drag estimates downward, which is the direction that causes the next
OOM. Nothing raises.

The containment is to keep the invariant in one place rather than four:
**callers do not query the collection directly.** `timing_service._samples()`
applies the outcome filter once and every model fits through it; provenance,
the one reader that wants failures, uses a separate explicitly-named accessor.
This is also recorded in `CLAUDE.md` so a future change adding a fourth
consumer gets the warning without having to find this document.

### Timing

`duration_ms`, `queued_ms`.

Queue wait is separated from run time because it is what makes a user's
wall-clock experience diverge from the model's prediction. `job.timing.enqueued_at`
already exists to derive it.

### Resources

`peak_rss_bytes`, `peak_cpu_percent`, `mean_cpu_percent`, `sample_count`.

Sampled by a poller in the executor walking the job's process subtree at ~1s
intervals, retaining the max.

**Resource fields are null for runs under 60 seconds.** Short jobs yield a
handful of samples, and a peak derived from that is noise. They are not
interesting for this feature -- the point is substantial work -- so they are
excluded rather than recorded with an unreliable number. The record is still
written; only the resource block is empty. This removes the failure mode
instead of labeling it.

A minute is a high floor deliberately. At a ~1s interval it guarantees roughly
sixty samples behind every peak, which is enough that the number means
something. It also sets the floor for what the memory model can ever speak
about: estimates exist only for work measured in minutes, which is the only
work where "will this fit on my machine" is a question worth asking.

`sample_count` remains as the honesty field for runs above the floor.

Why polling rather than the alternatives. `resource.getrusage(RUSAGE_CHILDREN)`
gives the kernel's own exact high-water mark for free, but it is cumulative
across all children of the worker process -- and the governor admits multiple
jobs concurrently, so a number cannot be attributed to one job. That is
disqualifying. `/usr/bin/time -v` is exact per tool invocation but covers only
subprocess tools, missing in-Python work like `de_runner`, and would require
editing every runner. Polling the subtree is the only approach uniform across
handler modes, it yields CPU as a side effect, and two concurrent jobs' subtrees
remain cleanly separable.

Accuracy is adequate for the use: it replaces coefficients that may be off by a
factor of two.

### Machine

`cpu_model`, `physical_cores`, `logical_cores`, `total_ram_bytes`,
`cgroup_cpu_budget`, `cgroup_mem_limit`, `platform`, `machine_id`.

Captured once at worker start, stamped on every record rather than re-probed.

Both the raw totals and the cgroup budgets are recorded because inside Docker
they differ and the budget is what binds. `governor.py` already makes this
distinction (`_read_cgroup_cpu`, `_read_cgroup_mem`) precisely because psutil
reports the Linux VM's resources, not the host Mac's. A record claiming 32 GB
when the container was capped at 8 would poison the local memory model and
poison an aggregated corpus worse.

`machine_id` is a stable local hash, not a hostname or serial. It lets a future
aggregation server segment by hardware without identifying anyone.

### Invocation and inputs

`tool`, `tool_version`, `params`, `input_bytes`, `features`.

`tool_version` is what lets the corpus survive time. A tool getting faster
between releases is invisible without it, and `pipelines/tool_cache.py` already
probes versions.

`params` is the **sanitized** payload -- thread count, preset, aligner choice --
with file paths, project names, and other local identifiers stripped at write
time. Doing this on write is far cheaper than retrofitting a scrubber onto an
existing corpus on the day upload ships.

`features` is a free-form dict the handler populates with whatever it happens to
know: `read_count`, `reference_bases`, `n_variants`. Nothing blocks on plumbing
every runner; fields accumulate and appear in provenance immediately even before
any model consumes them. Bytes alone is a weak predictor -- a 500 MB gzipped
FASTQ and a 500 MB BAM cost very different amounts -- so this is the field that
makes the corpus worth aggregating.

## Models

**Duration.** `threads` is promoted into the fit now. It is read from
`job.payload` in the executor, where `align_handlers`, `assembly_handlers`,
`expression_handlers`, and `assembly_qc_handlers` already put it -- so no runner
changes. The fit becomes duration against bytes segmented by thread count, with
a fallback to today's bytes-only fit for job types with no thread data.

**Memory.** A new model mirroring the duration one: peak RSS against input size
per job type, same `MIN_SAMPLES` silence rule, same outlier trimming. It reads
only records above the duration floor, since those are the only ones with
resource data.

Both keep the existing principle: below `MIN_SAMPLES`, report no estimate at
all. A confidently wrong number is worse than an honest absence.

### Extrapolation flagging

Both models also report whether the input being asked about falls inside the
range of sizes they have actually observed, by comparing against the min and
max of their own samples.

This matters more than it sounds, because of how this app has been used so far.
Every existing row comes from test data -- small inputs, one machine, whatever
thread counts happened to get tried. The first serious alignment or variant
call will be one or two orders of magnitude larger than anything in the
collection, and a linear fit extrapolated that far is precisely where it is
least trustworthy. Existing rows are being kept rather than wiped, so the model
will be extrapolating from toy inputs for a while.

"Estimated 40 minutes, but this input is 8x larger than anything measured" is a
materially different claim from an estimate inside the observed range, and the
distinction costs one comparison. `r_squared` already tells the caller how well
the fit describes its own samples; this tells them whether the question is even
inside the fit's evidence.

The predictors now select against a collection that grows without bound rather
than one kept small by construction, so their queries need indexes on
(`job_type`, `outcome`, `finished_at`).

## Reading it back

Three surfaces, none of which requires a new page:

- **Provenance on the object.** "Aligned in 41 min, 12.3 GB peak, 8 threads,
  minimap2 2.28, on 10 cores / 32 GB." Queried by `object_id`.
- **`api/v1/jobs.py:244`** gains a memory estimate beside its duration estimate.
- **`api/v1/jobs.py:186`** already returns per-type model summaries; it gains
  the memory model and its sample counts.

## Failure behavior

Unchanged from `timing_service.record`: **telemetry never fails a job.** The
sampler is a supervised task that logs and dies on error. Every write sits in a
`try/except` logging at debug. A missing record is a lost sample, never a failed
pipeline run.

## Testing

Backend behavior is covered by pytest, run from a worktree via
`./backend/run-worktree-tests.sh`. Worth testing specifically:

- The duration floor: a run under 60s writes a record with null resource
  fields, not a record with a handful-of-samples peak.
- The outcome filter: a failed run is recorded and is excluded from both fits.
- Extrapolation flagging: an input well above the largest observed sample is
  reported as outside the measured range; one inside it is not.
- Param sanitization: a payload containing file paths yields a `params` field
  containing none.
- Subtree attribution: two concurrent jobs do not sum into each other's peak.

Per `CLAUDE.md`, the models are also worth checking against the real database
rather than fixtures alone -- hand-built objects that already look the way the
code expects are how the suggestion rules passed green while being wrong.
