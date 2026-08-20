# StringTie `--merge` — implementation plan

Date: 2026-08-20.

Closes [#703](https://github.com/syntheticgio/bioflow/issues/703). Companion
to `docs/superpowers/specs/2026-08-20-stringtie-merge-and-quantify-design.md`
(decisions S1–S5).

**This plan covers stage 1 only.** Stage 2 (`stringtie -e` quantification,
into which #704 folds) gets its own plan once stage 1 has run against real
multi-sample data — its central guard (S5) depends on an answer only that run
produces.

#622 has landed, so `stringtie_runner.py`, `assemble_command`, `parse_gtf`,
and `ObjectRole.ASSEMBLED_TRANSCRIPTS` all exist. Re-read that module before
starting; this extends it.

## Spike first

- **S-1. Does the installed StringTie's `--merge` take GTFs positionally, or
  via a list file (`-o merged.gtf mergelist.txt`)?** Both forms exist across
  versions. The command builder's entire shape depends on it, and guessing
  produces a builder that unit-tests green against the wrong contract.
- **S-2. Does `-G` on merge accept the reference annotations this app actually
  downloads** — NCBI **GTF and GFF3** both? `counts_runner.attributes_for_format`
  exists precisely because those two differ in ways that break tools (no
  `gene_id` on a GFF3 exon line). Test with a real downloaded pair, not a
  hand-written fixture.
- **S-3. What `gene_id` does the merge emit for novel loci** on real data?
  S5's reasoning assumes `MSTRG.*`; confirm it. This is stage 2's input, but
  it is cheap to capture during stage 1's real run and expensive to come back
  for.

Record the answers in the spec as an "Amended" note.

## Files to touch

| File | Change |
|---|---|
| `backend/app/pipelines/stringtie_runner.py` | **Extend.** `merge_command(*, stringtie_path, gtfs, out, reference_gtf=None, min_len=None, min_cov=None)`, shaped by S-1. If S-1 says list-file, the builder returns the argv **and** the list-file content, so the handler does not have to know the format. |
| `backend/app/queue/<stringtie>_handlers.py` | **New handler** `merge_transcripts` — takes `gtf_object_ids` from `params` (S1), materializes each, writes the list file if needed, runs, parses with the existing `parse_gtf`. |
| `backend/app/queue/results.py` | `_apply_merge_transcripts` — ingest as **`ObjectRole.ANNOTATION`** (S3: it is a thing you quantify *against* now, not a per-sample assembly), `derived_from` = all N GTFs + the reference when `-G` was used. Facts: input count, transcript count, novel-transcript count. Per-key `facts.<key>` merge, never whole-dict (#606). |
| `backend/app/services/pipeline_service.py` | `launch_merge_transcripts(project_id, gtf_object_ids, owner, params)` — validates ≥2 inputs, all `ASSEMBLED_TRANSCRIPTS`, all same project. |
| `backend/app/api/v1/pipelines.py` | `POST /pipelines/merge-transcripts` → `JobOut`, 201. |
| `backend/app/pipelines/node_types.py` | `merge_transcripts` spec: **one representative** `assembled_transcripts` port, set travelling via `params["gtf_object_ids"]` (S1). The comment must cite `_launch_differential_expression` — a reader who does not know that precedent will read this as a bug. |
| `backend/app/services/running_now.py` | `ENDPOINT_JOB_TYPES["/pipelines/merge-transcripts"]`. |
| `backend/app/services/provenance_walker.py` | Narrative verb ("merged transcript assemblies") — it produces an object a person opens. |
| `frontend/` | Project-level entry point per S2: lists the project's `ASSEMBLED_TRANSCRIPTS` objects with checkboxes, defaulting to all. Follow the DE dialog's shape. **No per-object card.** |

## Ordered steps

1. **`merge_command`, shaped by S-1.** Pure, unit-tested: GTF ordering
   preserved, `-G` present only when a reference is passed, output flag
   correct. If S-1 says list-file, test the generated content too.
2. **Handler.** Materialize N objects, run, parse. Restart the worker after
   editing (`docker compose restart worker`, from the **main** repo root) —
   it does not hot-reload, so without it the job runs old in-memory code while
   appearing to run your fix.
3. **Applier.** The role is the thing to get right: **`ANNOTATION`, not
   `ASSEMBLED_TRANSCRIPTS`** (S3). Getting this wrong is not a crash — it
   makes the merged annotation eligible to be fed back into another merge as
   though it were a per-sample assembly, which produces a plausible-looking
   result from a meaningless operation.
4. **Launch + route**, with the ≥2-inputs and same-project validation. A
   one-input merge is a copy; refuse it with a reason rather than producing a
   duplicate object.
5. **Node type + registries**, then the whole `TestExhaustiveness` class and
   the provenance partition — these assert partitions, so a half-fix passes
   one test and fails its sibling (#355).
6. **Frontend entry point** per S2. Verify at `http://localhost:5273`
   (worktree stack via `./ops/worktree-up.sh`), not 5173.
7. **Real-data check.** Merge ≥3 real per-sample GTFs from #622 runs. Confirm:
   the merged GTF has fewer transcripts than the sum of its inputs (or the
   merge did nothing); `derived_from` lists every input; and capture S-3's
   `gene_id` answer while you are here.

## What this plan deliberately does not do

- **Implement `PortSpec.multiple`** (S1). The set travels through `params`,
  as DE's does. Making `multiple` real is a graph-model change that should be
  driven by a node a user wires by hand, and it would be validated here by
  exactly one consumer.
- **Add a per-object merge card** (S2). N cards for one action, each needing
  the other N−1 objects, is the per-object model applied where it does not fit.
- **Anything with `ObjectRole.COUNTS`.** That is stage 2, and S5 gates it: the
  `-e` output is a third gene universe whose novel loci (`MSTRG.*`) have no
  counterpart in the featureCounts universe. Nothing may emit counts until the
  incomparability guard exists.

## Verification

```bash
./backend/run-worktree-tests.sh tests/ -q
```

From the worktree, never `docker compose exec api`. Then `ruff check --config
backend/pyproject.toml backend/app backend/tests ops e2e`, fixing everything
it reports including pre-existing findings.
