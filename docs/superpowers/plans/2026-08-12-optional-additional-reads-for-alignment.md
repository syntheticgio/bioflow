# Optional additional reads for reference alignment

**Goal:** A file-level "Align to reference" launch can include additional FASTQ read
files alongside the primary reads, with the aligner receiving every selected file —
and the run's provenance recording every selected file.

**Source:** GitHub issue #268 (`feat(pipelines): allow optional additional reads for
reference alignment`). Decisions recorded in [Design decisions](#design-decisions).

**Architecture:** The backend already carries `extra_reads` machinery built for the
workflow editor's multi-input `reads` port: `launch_alignment` accepts flat extra ids
via `params["extra_reads"]`, validates them, resolves them to `{name, sha256, path}`
payload entries, and `align_reads` concatenates their bytes into the R1 stream
(`_concatenate_reads`). What #268 needs on top of that is: pair-aware additional
*sets* (a set is an R1 and optionally its mate), a UI to add/remove them, complete
provenance, and a dedup key that distinguishes launches by their full input set.
This plan replaces the flat params list with a typed `additional_read_sets` contract
and makes the whole run — primary and extras alike — one internally uniform list of
read sets.

## Design decisions

Source: user choice on the three brainstorm questions (paired run ⇒ every set
paired; mates resolved like the primary; new `EXTRA_READS`/`EXTRA_MATE` roles), plus
the transport and workflow-compat calls below. Where a decision is a judgement call
rather than a user choice it is marked **decided here**.

1. **Pairing is a property of the run, set by the primary pair.** A launch is
   *paired* iff the primary read has a mate (explicit or auto-discovered). In a
   paired run every additional set must be paired (its R1 and its mate); in a
   single-end run every additional set is single-end. A request that mixes the two
   is rejected with a message naming the offending set. (User choice; the issue's
   existing workflow chunk path, which concatenates R1-only chunks into a paired
   run, now fails validation with an actionable message instead of silently
   concatenating — see the workflow note under Task 5.)
2. **An additional set's mate is resolved exactly like the primary's.** An explicit
   `mate_object_id` wins; otherwise `suggest_mate` runs on the set's R1. If the run
   is paired and a set has no mate, launch fails naming the set. The same R1-leads
   swap applied to the primary pair is applied per set, so a set added R2-first is
   normalized before payload assembly. (User choice: "same as primary".)
3. **Transport is a typed top-level request field**, `additional_read_sets`:
   a list of `{object_id, mate_object_id?}` entries, a sibling of `mate_object_id`
   on `AlignRequest`. Not a `params` key — `params` is aligner configuration, and
   a flat id list there cannot express sets. The workflow node stops producing
   `params["extra_reads"]`; `launch_alignment` hard-errors if it still sees it, so a
   stale caller fails loudly instead of silently losing files. (**decided here**)
4. **Every set is estimated and recorded.** The memory estimate sums every R1 and R2
   byte in the run; the dedup key lists every set's R1 and R2 ids in order; the run's
   `inputs` record every set's R1 as `EXTRA_READS` and every mate as `EXTRA_MATE`
   (primary keeps `READS`/`MATE`/`REFERENCE`). (User choice on roles.)
5. **The aligner still receives two byte streams.** All set R1s concatenate into the
   R1 stream (primary R1 first), all set R2s into the R2 stream (primary R2 first).
   This is the existing `_concatenate_reads` approach, extended to a second stream;
   it remains the only approach that works across every aligner `align_runner`
   drives. (**decided here** — follows the existing design note in `align_handlers.py`.)

## Global constraints

- The primary file remains the first, non-removable set.
- One launch = one job (or one chunked fan-out); additional reads never split into
  separate jobs or merged BAMs.
- Every id in a launch is unique across the whole request: no file may be the
  primary, a primary mate, and/or a member of two sets.
- Every set member is owner-scoped, `READY`, FASTQ, and in the primary's project —
  the same checks `_check_alignable` already applies to the primary.
- The API is the authoritative validator; the dialog is a convenience that mirrors
  the same rules for immediacy.
- Requirements here are stated per the repo's spec conventions: testable, one
  obligation each, permanent identifiers, actor named.

## Requirements

| ID | Requirement |
|---|---|
| R-1 | A user opening the file-level "Align to reference" dialog can add FASTQ files from the same project as additional reads and remove them again before launch. |
| R-2 | A user can add at most one of a file that is already the primary, the primary's mate, or a member of another additional set; the picker omits or disables such files. |
| R-3 | When the run is paired, adding an additional R1 automatically includes its mate (explicit link, else `suggest_mate`), the same rule as the primary. |
| R-4 | A launch whose sets mix pairing is rejected: a paired run with a set that has no mate, or a single-end run with a set that declares a mate, fails with a message naming the set and the fix. |
| R-5 | The backend resolves and validates every set member — owner scope, `READY`, FASTQ kind, same project as the primary, whole-request uniqueness — before enqueueing. |
| R-6 | The job payload carries every set's R1 and, for paired sets, R2 as named, content-addressed entries. |
| R-7 | The alignment handler concatenates all set R1s into the R1 stream and all set R2s into the R2 stream, primary first in both. |
| R-8 | The memory estimate for a launch sums the sizes of every set member, R1 and R2. |
| R-9 | Two launches that differ only in their additional sets are distinct jobs (dedup key covers every set member id, in order). |
| R-10 | The run record lists every set's R1 as `EXTRA_READS` and every mate as `EXTRA_MATE`, alongside the primary's `READS`/`MATE` and the `REFERENCE`. |
| R-11 | A chunked alignment launch accepts and records additional sets exactly as a single-shot launch does. |
| R-12 | The workflow editor's align node continues to launch one run over its multi-input `reads` files, now via `additional_read_sets`. |
| R-13 | A run summary shows every reads input of a launch — primary, mates, and additional sets — in its "Reads" fact. |
| R-14 | A stale caller sending `params["extra_reads"]` receives a validation error naming the replacement field. |

## File structure

| File | Change |
| --- | --- |
| `backend/app/models/run.py` | Add `EXTRA_READS`, `EXTRA_MATE` to `RunInputRole`. |
| `backend/app/api/v1/pipelines.py` | `AdditionalReadSetIn` model; `AlignRequest.additional_read_sets`; route forwards it. |
| `backend/app/services/pipeline_service.py` | `_resolve_alignment_read_sets`; `launch_alignment` uses it in both paths; validation, memory, dedup key, payload, `_alignment_inputs` take the set list; reject stale `params.extra_reads`. |
| `backend/app/queue/align_handlers.py` | `align_reads` concatenates extra R1s into r1 and extra R2s into r2. |
| `backend/app/pipelines/node_types.py` | `_launch_align` emits `additional_read_sets` from the `reads` port. |
| `frontend/src/api/types.ts` | `AdditionalReadSet`; `AlignRequest.additional_read_sets`; `RunInputRole` gains the two roles. |
| `frontend/src/components/AlignDialog.tsx` | "Additional reads" section: picker, per-set rows with mate note, remove; mode gating; launch body includes sets. |
| `frontend/src/lib/runFormat.ts` | Include `EXTRA_READS`/`EXTRA_MATE` in the run's "Reads" fact. |
| `backend/tests/api/test_pipelines_align_schema.py` | Transport-schema tests. |
| `backend/tests/pipelines/test_align_launch.py` | Service validation, estimates, provenance, dedup. |
| `backend/tests/pipelines/test_align_extra_reads.py` | Payload shape and two-stream concatenation. |
| `frontend/src/lib/runFormat.test.ts` | Reads fact covers extra roles. |

---

### Task 1: Transport contract

**Files:** `backend/app/api/v1/pipelines.py`, `frontend/src/api/types.ts`,
`backend/tests/api/test_pipelines_align_schema.py`

- [ ] **Step 1:** Add failing schema tests: a request with
  `additional_read_sets: [{object_id, mate_object_id}, {object_id}]` validates in
  order; a request without the field defaults to `[]`.
- [ ] **Step 2:** Run `./backend/run-worktree-tests.sh tests/api/test_pipelines_align_schema.py -q`
  — expect failure (field missing).
- [ ] **Step 3:** Add `AdditionalReadSetIn` (`object_id`, `mate_object_id: | None = None`),
  `AlignRequest.additional_read_sets: list[AdditionalReadSetIn] = Field(default_factory=list)`,
  and forward it as a named argument in the route. Mirror both in
  `frontend/src/api/types.ts` (`AdditionalReadSet`, `AlignRequest.additional_read_sets`).
- [ ] **Step 4:** Re-run the schema tests — expect pass. Commit:
  `feat(api): accept ordered additional alignment read sets`

### Task 2: Set resolution and launch validation

**Files:** `backend/app/services/pipeline_service.py`,
`backend/tests/pipelines/test_align_launch.py`

- [ ] **Step 1:** Write failing service tests: same-project, READY, FASTQ, and
  uniqueness (primary/mate/other-set collisions) rejections; stale
  `params["extra_reads"]` rejection (R-14); paired-run-missing-mate and
  single-run-declared-mate rejections (R-4); mate auto-inclusion for a set without
  explicit mate (R-3); R1-leads swap per set.
- [ ] **Step 2:** Implement `_resolve_alignment_read_sets(*, primary, primary_mate,
  requested_additions, owner) -> list[ResolvedReadSet]` — the primary pair is set 0;
  each addition resolves its own mate (explicit wins, else `suggest_mate`), applies
  the R1-leads swap, and validates per R-5. Call it once in `launch_alignment`
  before the chunked branch, replacing the two duplicated mate-resolution blocks.
- [ ] **Step 3:** Memory: `total_input_bytes` sums every set's R1 and R2 (R-8).
  Dedup key: list every set's R1 then R2 id (R-9) — both the single-shot and
  chunked enqueues. Raise on `params["extra_reads"]` (R-14).
- [ ] **Step 4:** Re-run — expect pass. Commit:
  `feat(pipelines): resolve and validate additional read sets at launch`

### Task 3: Payload and handler

**Files:** `backend/app/services/pipeline_service.py`,
`backend/app/queue/align_handlers.py`,
`backend/tests/pipelines/test_align_extra_reads.py`

- [ ] **Step 1:** Extend payload assembly (both paths) so each `extra_reads` entry
  carries the set's R1 `{name, sha256, path}` plus, for paired sets, `{mate_name,
  mate_sha256, mate_path}` (R-6). The chunked branch inherits R-11 automatically via
  the existing payload spread.
- [ ] **Step 2:** In `align_reads`, split the extras into R1 paths and R2 paths;
  concatenate R1 extras into the R1 stream and R2 extras into the R2 stream (R-7).
  Guard: R2 extras with no primary R2 are impossible by validation, but the handler
  should fail loudly rather than silently drop them if the payload is hand-built.
- [ ] **Step 3:** Extend `test_align_extra_reads.py` for the new payload shape and
  two-stream concatenation (byte-level, gzip round-trip as the existing tests do).
- [ ] **Step 4:** Re-run `./backend/run-worktree-tests.sh tests/pipelines/test_align_extra_reads.py tests/pipelines/test_align_launch.py -q`
  — expect pass. Commit: `feat(pipelines): align additional read sets on both streams`

### Task 4: Provenance

**Files:** `backend/app/models/run.py`, `backend/app/services/pipeline_service.py`,
`frontend/src/api/types.ts`, `frontend/src/lib/runFormat.ts`,
`backend/tests/pipelines/test_align_launch.py`, `frontend/src/lib/runFormat.test.ts`

- [ ] **Step 1:** Add `EXTRA_READS = "extra_reads"`, `EXTRA_MATE = "extra_mate"` to
  `RunInputRole`; mirror both strings in the frontend union.
- [ ] **Step 2:** Change `_alignment_inputs` to take the resolved sets: set 0 keeps
  `READS`/`MATE`; each additional set emits `EXTRA_READS` for its R1 and `EXTRA_MATE`
  for its mate (R-10). Confirm the run label still reads sanely with sets present.
- [ ] **Step 3:** `runFormat.ts` includes the two roles in the "Reads" fact (R-13);
  add a unit test.
- [ ] **Step 4:** Provenance test asserts one `RunInput` per set member with the
  right roles. Commit: `feat(provenance): record additional alignment read sets in run inputs`

### Task 5: Workflow node

**Files:** `backend/app/pipelines/node_types.py`, tests in
`backend/tests/pipelines/test_node_types.py` (if launch wiring is testable there)

- [ ] **Step 1:** `_launch_align` builds `additional_read_sets` from the `reads`
  port's extra files (`{object_id}` entries, no mates — the port is documented as
  chunked/split reads, not mates) and passes it as the named argument, dropping the
  `params["extra_reads"]` key (R-12).
- [ ] **Step 2:** Note the behavior change: a workflow that feeds R1-only chunks
  *and* a mate now fails at launch with the R-4 message rather than silently
  concatenating chunks into a paired run. State this in the commit body.
- [ ] **Step 3:** Re-run the affected tests; commit:
  `refactor(pipelines): emit additional read sets from the workflow align node`

### Task 6: AlignDialog additional reads

**Files:** `frontend/src/components/AlignDialog.tsx`

- [ ] **Step 1:** Fetch `api.listObjects(object.project_id)` and filter to FASTQ +
  `READY` + not already used; render an "Additional reads" picker and per-set rows
  (name, mate note, Remove). Detect each added set's mate via `api.detectMate`,
  mirroring the primary's existing query (R-1, R-3).
- [ ] **Step 2:** Mode gating: in a paired run, a set with no mate blocks launch
  with an explanation; in a single-end run, sets are sent without mates and the
  picker explains when a candidate's mate will not be included. The primary pair
  checkbox remains the only mode switch (R-4 preview).
- [ ] **Step 3:** Launch bodies (normal and `launchAnyway`) include
  `additional_read_sets`.
- [ ] **Step 4:** Manual check at localhost:5273 via `./ops/worktree-up.sh`: add a
  single-end set, add a paired set, mix modes, verify the refusal messages, verify
  the run's activity entry lists every file. Commit:
  `feat(frontend): let alignment launches include additional read sets`

### Task 7: Verification and PR

- [ ] **Step 1:** `./backend/run-worktree-tests.sh tests/ -q` — read the count, not
  the exit code.
- [ ] **Step 2:** Frontend: `npm run build` and `npm test` (vitest) in `frontend/`.
- [ ] **Step 3:** Re-run the API-against-real-objects sanity check (AGENTS.md) for
  one paired launch with one paired extra and one single launch with one single
  extra, and confirm the worker log line and the run's inputs.
- [ ] **Step 4:** `git push -u origin HEAD`, `gh pr create --base main --fill` with
  `Closes #268` and `feat:`/`pipelines`+`frontend` labels. Do not merge.
