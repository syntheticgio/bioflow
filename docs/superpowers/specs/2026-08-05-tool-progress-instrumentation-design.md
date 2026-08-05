# Tool progress instrumentation and display

Per-tool progress parsers that can be shown to have stopped working, a
model-derived estimate for tools with nothing countable, and a UI that renders
what the model already collects.

Second executable slice of epic
[#6](https://github.com/syntheticgio/bioflow/issues/6), following
[#24](https://github.com/syntheticgio/bioflow/issues/24) (spec:
`docs/superpowers/specs/2026-08-05-job-progress-model-design.md`).

## Why this, and what already exists

#24 built the pipe. Almost nothing is pushed through it, and the parts that
are, are not displayed. The inventory that matters, measured against this
tree rather than the issue text:

- `run_subprocess(ctx, cmd, log_path, on_line=...)`
  (`app/queue/executor.py:500`) is the single seam every external tool runs
  through. **Roughly forty call sites; seven pass `on_line`.** Everything
  else streams to a log file and reports nothing.
- Four parsers exist -- `TrimProgress` (fastp), `AlignProgress`,
  `AssemblyProgress` (Flye stages), `VariantProgress` -- and
  `VariantProgress.pct` is hardcoded `None`.
- **minimap2, named in the backlog entry as the motivating example, is not
  parsed.** `align_runner.py:46`'s `_PROCESSED_RE` matches `Processed N
  reads`, which is bwa-mem2's line. minimap2 writes a different one. So the
  tool this epic was filed about shows an indeterminate bar for its entire
  run, and nothing anywhere says so.
- **The frontend renders about a third of the model.** `JobList.tsx` shows
  `pct` and `rss_bytes`; `ActivityView.tsx:353` shows `message`. Nothing
  renders `eta_seconds`, `units_done`/`units_total`, `phase_index`/
  `phase_total`, `cpu_percent`, or live `peak_rss_bytes` -- all typed in
  `api/types.ts`, all unread. `ActivePipelineJobs.tsx:60`, the most visible
  surface of the three, does `job.progress?.pct ?? 0`, which collapses
  "unknown" back to 0% -- undoing the exact distinction #24 made `pct`
  nullable to preserve.

So the remaining half of the epic is two things, not one: parsers, and a
display for what is already being collected.

## The problem this design is actually built around

Not "how do we parse tool output" -- that is regex. It is **how a parser
proves it still works.**

This repo has already paid for getting that wrong once. `AssemblyProgress`'s
original stage table matched on prose banners ("Assembly draft", "Building
repeat graph"); only one of five patterns would ever have fired. The tests
were green throughout, because they fed the parser hand-built lines that
already looked the way the parser expected. The symptom in the app is a
progress display that silently stops updating -- indistinguishable from a
tool that is merely slow, on a job where "is it stuck?" is the only question
anyone is asking.

That failure mode gets worse, not better, as this epic proceeds: every parser
added is another regex over a third-party tool's output format, which that
tool is free to change in its next release. Unit tests over invented strings
cannot catch any of it.

Everything below is arranged around that. Two mechanisms, in order of how
much they catch:

1. **Golden log fixtures.** A parser is tested against stderr captured from a
   real run, not against lines written from memory. Logs already land at
   `settings.logs_dir/{job_id}.log`, so capturing one is a matter of running
   the tool once and copying the file.
2. **Silence detection at the seam.** `_run_streaming` counts the updates a
   parser produced. A job that ran past a threshold with a parser attached
   that never fired once logs `progress_parser_silent` with the tool name.
   This is the only check that catches a tool upgrade changing its output,
   because it does not depend on knowing what the new output looks like.

## Scope

In scope:

- A `ctx.attach()` wiring helper and a parser protocol (slice F).
- Silence detection and the golden-fixture convention (slice D).
- minimap2 read counting in `AlignProgress` (slice A).
- Estimated-progress rendering derived from the duration model, and a UI
  pass over the three job surfaces (slice B).

Out of scope, filed separately:

- Clair3 per-chunk `units_done`/`units_total` (`VariantProgress` currently
  returns `None` always).
- The sweep of currently uninstrumented long jobs -- standalone `samtools
  sort`/`index`/`markdup`, `featureCounts`, BUSCO/CheckM, `prefetch`.
- Flye phase structure, already tracked as
  [#55](https://github.com/syntheticgio/bioflow/issues/55).
- DAG-level aggregation, which is
  [#18](https://github.com/syntheticgio/bioflow/issues/18)'s, per this epic's
  stated boundary.

## The parser seam

Today each handler hand-writes the same closure:

```python
progress = align_runner.AlignProgress(expected_reads=...)

def on_line(line: str) -> None:
    if progress.feed(line):
        ctx.progress(pct=progress.pct, phase=progress.phase, ...)

code = run_subprocess(ctx, cmd, log_path=str(log_path), on_line=on_line)
```

Written five times, and it has already drifted: `align_handlers.py` passes
`phase_index`/`phase_total`, `assembly_handlers.py` passes neither,
`variant_handlers.py` passes neither. A parser that grows a new field does
not reach the UI until every call site is found and edited, and nothing
fails when one is missed.

Replace with a protocol and one helper. Parsers stay pure objects in
`pipelines/` -- no `ctx`, no I/O -- which is the runner/handler split this
repo already enforces and what makes golden-fixture tests possible at all.

```python
class ProgressParser(Protocol):
    """Pure line-to-progress translation. No ctx, no I/O."""

    name: str  # tool name, for progress_parser_silent

    def feed(self, line: str) -> bool:
        """True if the caller should publish an update."""

    def snapshot(self) -> dict:
        """Fields for ctx.progress(). Only keys this parser knows."""
```

`snapshot()` is what removes the drift: a parser that learns `units_done`
starts emitting it everywhere at once, because the helper forwards whatever
the parser returns rather than each call site naming fields by hand.

```python
code = run_subprocess(ctx, cmd, log_path=str(log_path), parser=progress)
```

`on_line` stays, for the callers that want raw lines and no progress
(`variant_handlers.py:621`'s `_csq_line_logger` logs without reporting).
`parser=` is the sugar over it; passing both is an error.

Migrating the four existing parsers to the protocol is mechanical and belongs
in this slice -- leaving two conventions in the tree is how the next parser
picks the wrong one.

## Silence detection

`_run_streaming` already wraps `on_line` in a try/except that swallows parser
exceptions (correct -- progress is advisory and must never fail real work).
Extend that wrapper to count.

- Count updates published, not lines consumed. A parser is not silent because
  the tool was quiet; it is silent because it saw output and recognized none
  of it.
- On exit, if the run exceeded `PARSER_SILENCE_FLOOR_S` (120s -- long enough
  that a real tool has certainly printed something, short enough to catch a
  broken parser on a test run) and the count is zero, log
  `progress_parser_silent` at **warning**, with the parser name, job type,
  duration, and line count.
- Line count in the log line is what makes it diagnosable: zero lines and
  zero updates means the tool was quiet or output was redirected; forty
  thousand lines and zero updates means the parser is broken.
- Warning, not an error, and never a failure. A broken parser must not fail
  a six-hour assembly that produced a perfectly good FASTA -- the same rule
  `parse_assembly_info` already follows.

This is deliberately a log line and not a metric or an alert. Single-user,
local-only tool; the person who will read it is the person debugging "why did
the bar stop", and `docker compose logs worker` is where they will be.

## Golden log fixtures

`backend/tests/fixtures/tool_logs/{tool}-{version}.log`, each a real captured
stderr, trimmed to the lines that matter plus enough surrounding noise to be
representative. Version in the filename because the whole point is knowing
which release the parse was verified against.

Each parser gets one test that replays its fixture line by line and asserts
the parse *arrived somewhere* -- final `units_done`, the phase sequence
observed -- rather than asserting on individual lines. Hand-built lines stay
allowed for edge cases (malformed input, counter rollover); they are not
allowed as the only coverage.

**Do not write the minimap2 regex from memory.** Capture a real log first and
write the pattern against it. Writing a plausible-looking regex from recall
is precisely what produced the Flye stage bug, and it fails silently in
exactly the same way.

## Estimated progress for tools with nothing countable

Flye, bcftools, BUSCO and most of the uninstrumented sweep have no countable
unit and never will. Today they render an indeterminate bar for their whole
run, which is honest but tells the user nothing they did not already know.

The duration model can fill that gap, and the machinery is already in place:
`JobContext.eta_model_ms` is resolved once at claim time, and
`timing_service.eta_seconds()` already falls back to it whenever measured
`pct` is below `ETA_PCT_FLOOR`. An elapsed/predicted fraction is arithmetic
over numbers both emit paths already hold. No new collection read, no new
field on the job document.

Three constraints, each of which is the reason this is safe to do at all:

**It is not `pct`.** `JobProgress.pct` means "measured, or null", and #24's
design rests on that -- `job_timings`' consumers, provenance, and the
duration model itself must never see a fabricated number fed back in. So this
is a **derived, never-persisted** sibling, computed in the two places
`eta_seconds` already is (`executor.py:487`, `api/v1/jobs.py:270`):

```python
pct_estimated: float | None   # response/event field only, never stored
```

A tool that later grows a real parser starts emitting `pct`, and the estimate
yields to it with no migration and no call-site change.

**The UI cannot be allowed to confuse the two.** Different treatment, not a
different shade: a hatched or striped fill plus an explicit "estimated"
label, against a solid fill for measured. If a user cannot tell at a glance
which they are looking at, the transparency claim this epic is named for is
false.

**It degrades honestly at both ends.** `MIN_SAMPLES = 5` already gates the
duration model, so a job type with no history yields `None` and the bar stays
indeterminate exactly as today -- the estimate appears only once there is
real history behind it. At the other end, once elapsed passes the prediction
the bar stops growing and the label becomes **"longer than expected"** rather
than creeping toward 100% and pinning there. A bar silently parked at 99% is
the stalled-job ambiguity #24 set out to kill, reintroduced by the back door.

Cap the estimate at `MAX_MEASURED_PCT` (0.95), the ceiling `TrimProgress` and
`AlignProgress` already use for the same reason: a number that cannot be
verified must not claim completion.

This gets better on its own. Every completed job writes to `job_timings`, so
the prediction sharpens with use, and "longer than expected" becomes a real
signal rather than a hedge.

## UI

Three surfaces, currently inconsistent, all reading the same payload.

- `ActivePipelineJobs.tsx:60` -- fix `pct ?? 0` first. It is a bug, not a
  gap: an unknown-progress job renders identically to one that has not
  started.
- `JobList.tsx` -- add ETA, step N of M when `phase_index` is set, units with
  their label when set, and the estimated-bar treatment.
- `ActivityView.tsx` -- currently `message` alone; same additions.

Resource display is the other half of the epic's title and is nearly free:
`rss_bytes` and `cpu_percent` arrive at 1 Hz already, and `JobList` shows
only RSS on running jobs. Live peak alongside current is what answers "did
this already touch the ceiling", which is the question asked immediately
after an unexplained failure -- and `last_attempt_progress.peak_rss_bytes`
already proves the shape works, on the retry path only.

Verification is the browser at localhost:5273 via `./ops/worktree-up.sh`.
There is no headless component-testing setup in this repo and none is
expected.

## Testing

- Parser unit tests over golden fixtures, per above.
- Silence detection: a fake parser that matches nothing, over a run past the
  floor, asserts the warning fires; one that matches asserts it does not.
  This test is the thing that keeps the safety net itself working.
- `ctx.attach()`/`parser=`: assert `snapshot()` keys reach `ctx.progress()`
  unmodified, including a key no current parser emits -- that is the drift
  this slice exists to remove.
- `pct_estimated`: no history yields `None`; mid-run yields a fraction;
  elapsed past prediction clamps and flags; it never appears on a persisted
  document. That last assertion is the one protecting the duration model from
  its own output.
- Migrated parsers keep their existing tests passing unchanged.

Run from a worktree with `./backend/run-worktree-tests.sh tests/ -q`, never
`docker compose exec api`, which would test main's code instead.

## Follow-ups

- Clair3 per-chunk units (`VariantProgress`).
- Uninstrumented sweep: samtools, featureCounts, BUSCO/CheckM, prefetch.
- Flye phase structure -- [#55](https://github.com/syntheticgio/bioflow/issues/55).
