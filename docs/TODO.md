# TODO

Deferred work, with enough context to pick up cold. Newest first.

## `JobContext.extend_lease` is inert

Raised: 2026-07-27, during read preparation.

`JobContext.extend_lease` in `backend/app/queue/registry.py` calls
`self._extend_cb`, which is never assigned anywhere in the codebase. Only
`_progress_cb` is set (in `worker.py` and `executor.py`), so the method
silently does nothing. Its docstring promises the opposite: "A multi-hour
alignment sets a long lease and keeps heartbeating; without this the reaper
would treat it as hung."

Nothing is broken today. `_heartbeat_loop` renews every in-flight job's lease
every 10s regardless of duration, and because a thread-mode handler blocks only
its own worker thread the event loop keeps turning -- a multi-hour `trim_reads`
run is safe. The hazard is the API's existence: it reads as the tool for long
phases, and someone will eventually rely on it instead of the heartbeat.

Either wire `_extend_cb` to a real lease extension or delete the method. Delete
is probably right: the heartbeat already handles the case the docstring
describes, and a second mechanism for the same thing is a way to get them out of
step. Deferred because it changes a public-looking handler API that this feature
did not otherwise touch.

Touches: `backend/app/queue/registry.py`, `backend/app/queue/executor.py`,
`backend/app/queue/worker.py`.

## `bp:cancel` grows without bound

Raised: 2026-07-27, during read preparation.

`queue.request_cancel` adds a job id to the `bp:cancel` Redis set. The queued
path removes it again (`queue.py`, in the branch that cancels a job before it
starts), but the *running* path never does -- when a running job observes
cancellation and terminates, nothing SREMs its id.

Every worker calls `SMEMBERS bp:cancel` once a second in `_cancel_watch_loop`,
so the cost of each stale entry is paid forever, by every worker. At single-user
scale this is a slow leak rather than a problem: hundreds of cancellations would
still be a small set. It is worth fixing before anything drives cancellations
automatically.

The fix belongs wherever a job reaches a terminal state -- `queue.complete` and
the reaper both already write there. Deferred because it is a correctness
cleanup in code this feature only read.

Touches: `backend/app/queue/queue.py`, `backend/app/queue/worker.py`.

## Claim-time resource accounting ignores its own reservations

Raised: 2026-07-27, during read preparation.

`claim.lua` reserves a job's declared cpu/mem into `bp:conc:cpu` and
`bp:conc:mem_mb`, and `release` gives them back. But `worker._free_resources`
computes headroom from `in_flight` -- a *count of running jobs* -- and never
reads those counters. So the reservation is maintained and never consulted.

Before this feature every handler declared `cpu=1`, which made the count and the
sum identical and the discrepancy invisible. `trim_reads` declares the user's
thread count (4 by default, up to 16), so they now diverge: four single-CPU jobs
and one 16-thread fastp look the same to admission.

The fix is to read `bp:conc:cpu` in `_free_resources` rather than deriving from
`in_flight`. The reason to be careful: the counters are the thing that can leak
if a release is ever missed, whereas a job count cannot -- which may be why it
was written this way. Any change wants a test that a crashed worker's
reservations do not permanently shrink capacity.

Touches: `backend/app/queue/worker.py`, `backend/app/queue/scripts/claim.lua`,
`backend/app/queue/scripts/release.lua`.

## The `io_heavy` cap counts all jobs, not heavy ones

Raised: 2026-07-27, during read preparation.

`worker._free_resources` computes `io_heavy` free capacity as
`max(2 - in_flight, 0)`, where `in_flight` is every running job of any kind. The
intent (documented on `IoClass.HEAVY`) is a throughput cap: more than two
concurrent heavy readers on a FUSE mount is slower in aggregate than two.

But with `worker_max_concurrent` at 4, four running *light* jobs -- header
parsing, verification -- drive `io_heavy` to zero and block heavy claims
entirely. A trim job can be starved by four trivial ones. The
`reap_pipeline_scratch` and `verify_files` schedules make that combination
routine rather than hypothetical.

The fix is to track heavy jobs separately, either by counting them in the worker
or by reading `bp:conc:io_heavy` (which `claim.lua` already maintains, and which
has the same leak consideration as the entry above). Deferred because it is a
pre-existing scheduling flaw rather than something this feature introduced --
though this feature is what makes it reachable.

Touches: `backend/app/queue/worker.py`.

## Mate detection is filename-only

Raised: 2026-07-27, during read preparation.

`app/pipelines/pairing.py` matches paired-end files by stripping an R1/R2 token
from the end of the name. Read IDs inside the files would be authoritative, but
checking them means decompressing two files to compare their first records, and
the naming convention is near-universal.

Two consequences. Files named outside the convention (`foo_fwd.fastq.gz` /
`foo_rev.fastq.gz`, or a sample whose mate marker sits mid-name) never pair, and
the user has to link them by hand. And two genuinely unrelated files could in
principle pair if their names collide after the token is removed -- guarded
against by requiring the naming *scheme* to match and by refusing an ambiguous
match, but not impossible.

Worth revisiting only if a real dataset trips it. The launch dialog already
shows the detected mate and allows overriding it, and `mate_object_id` is never
overwritten once set, so a wrong guess is visible and correctable rather than
silent.

Touches: `backend/app/pipelines/pairing.py`, `backend/app/queue/results.py`.

## Re-ingest re-asserts a reference role the user cleared

Raised: 2026-07-26, during assembly-accession enrichment.

`should_assign_reference_role` in `backend/app/queue/results.py` assigns the
reference role when an assembly accession is found and `role is None`. A role
the user *cleared* is indistinguishable from one never set, so converting a
reference back to reads and then re-ingesting will silently re-assign it.

Rare in practice — it needs a deliberate conversion plus a re-ingest of a file
whose name carries a GCA/GCF accession — but it quietly contradicts the promise
that an explicit choice is never overruled.

The fix needs a way to record that a user has touched the role: either a
nullable `role_set_by` field (`"user"` vs `"ingest"`), or a general
`user_touched: list[str]` on the object. The second generalizes to the same
problem for metadata fields, so it is probably the better shape. Deferred
because it is a schema change that this feature does not otherwise need.

Touches: `backend/app/models/object.py`, `backend/app/queue/results.py`,
`backend/app/services/object_service.py`.

## Warn before a role conversion discards in-progress metadata edits

Raised: 2026-07-26, during the object-role implementation.

`SchemaMetadataEditor` keeps local form state and its resync effect bails while
`dirty`, so converting a file mid-edit would otherwise save the previous role's
values against the new role's schema. The fix shipped is a
`key={obj.role ?? "none"}` remount in `DetailPanel.tsx`, which discards that
local state — correct, but **silent**: a user who types into the metadata form
and then clicks Convert loses the typing with no warning.

The honest fix is a dirty-state confirmation in `RoleConverter` before
mutating, which needs `SchemaMetadataEditor` to expose its dirty flag (lift it
to the parent, or accept an `onDirtyChange` callback). Deferred because it
means re-architecting a component that otherwise works, for an edge case that
requires an unsaved edit plus a conversion in the same visit.

Touches: `frontend/src/components/SchemaMetadataEditor.tsx`,
`frontend/src/components/RoleConverter.tsx`,
`frontend/src/components/DetailPanel.tsx`.

## Sample GC content across the file instead of a prefix

Raised: 2026-07-26, during the object-role design.

`sequence_stats.fasta_stats` caps at `max_bases=50_000_000` and reads from the
start of the file. On a multi-GB reference that means the reported
`gc_content_percent` describes chr1, not the assembly — and GC content varies
enough between chromosomes that the number is misleading when compared across
references.

The cap itself is a deliberate performance guard and should stay. The fix is to
make the sample representative rather than larger: read strided blocks across
the file (seek to N offsets, take a chunk at each, skip partial lines) and
aggregate. Same cost, far better estimate.

Blocked on nothing. Until it lands, the reference detail panel labels the row
"GC content (sampled)" and shows `stats_sampled_bases`, so the figure is not
presented as genome-wide.

Touches: `backend/app/storage/sequence_stats.py`,
`backend/tests/storage/test_sequence_stats.py`. Once fixed, revisit the
"(sampled)" label in the Assembly section of `DetailPanel.tsx`.

## Extract per-sequence lengths for FASTA

Raised: 2026-07-26, during the object-role design.

`_parse_fasta` collects sequence *names* only, and `fasta_stats` counts bases in
aggregate, so there is no way to report the longest or shortest sequence in an
assembly. The reference detail panel wants a longest/shortest row; it was cut
from the initial implementation rather than adding parser work.

Fix: accumulate per-sequence base counts in the `_parse_fasta` loop and store
them bounded, mirroring how `reference_lengths` is already capped at
`MAX_STORED_CONTIGS` for BAM headers. Note the existing 256 MB exact-count
limit — when parsing truncates, the lengths are partial and must be flagged
as such rather than reported as final.

Touches: `backend/app/storage/parsers.py`,
`backend/tests/storage/test_parsers.py`, then add the row to the Assembly
section of `frontend/src/components/DetailPanel.tsx`.
