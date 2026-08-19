# Plan: mosdepth per-region coverage-depth (issue #626)

Date: 2026-08-19. Implements the design in
`specs/2026-08-19-mosdepth-region-coverage-design.md`.

## Files

**Create**
- `backend/scripts/install-mosdepth.sh` — verified mosdepth install (mirror
  `install-quast.sh`). (MQ-2)
- `backend/app/pipelines/mosdepth_runner.py` — pure command-build + parse
  functions. (MQ-3)
- `backend/app/queue/mosdepth_handlers.py` — `run_mosdepth` region handler. (MQ-5)

**Modify**
- `backend/Dockerfile` (or image build) — run `install-mosdepth.sh` (mirror
  quast). (MQ-2)
- `backend/app/config.py` — add `mosdepth_path` + `mosdepth_dir`. (MQ-2, MQ-10)
- `backend/app/pipelines/tools.py` — mosdepth probe + `TOOL_META["mosdepth"]`.
  (MQ-1)
- `backend/app/pipelines/bam_stats_runner.py` — expose a mosdepth depth-source
  command. (MQ-4)
- `backend/app/queue/align_handlers.py` — `run_bam_stats` uses the mosdepth
  depth source. (MQ-4)
- `backend/app/services/pipeline_service.py` — `launch_mosdepth` (+ dedup);
  review `launch_bam_stats` for the depth-source switch. (MQ-11, MQ-4)
- `backend/app/api/v1/pipelines.py` — `POST /pipelines/mosdepth` +
  `GET /pipelines/mosdepth/{object_id}/report` + `MosDepthRequest`. (MQ-11, MQ-10)
- `backend/app/services/suggestion_service.py` — `build_coverage_depth_card`.
  (MQ-8)
- `frontend/src/...` — Coverage depth card + region view reusing
  `DepthHistogramChart` + a per-region table. (MQ-12)
- Tests as in MQ-14.

## Ordered steps

1. **Install + config.** Write `install-mosdepth.sh`; wire into the image build;
   add `mosdepth_path` / `mosdepth_dir` to `config.py`. Verify the image builds
   and `mosdepth --version` probes.
2. **Tool meta.** Add the mosdepth probe + `TOOL_META` entry with verified
   license / citation / homepage / usage. `test_every_tool_is_documented` passes.
3. **Runner.** `mosdepth_runner.py` pure functions + `test_mosdepth_runner.py`.
4. **bam_stats refactor (MQ-4).** Swap the depth source in `run_bam_stats` to
   mosdepth; reuse `bin_depth` / `DepthHistogram` / `cumulative_coverage`. Assert
   the `bam_stats_*` fact schema is unchanged via existing tests + a new
   schema-equivalence test.
5. **Region handler + launch + endpoint.** `mosdepth_handlers.run_mosdepth`,
   `launch_mosdepth`, `POST /pipelines/mosdepth`, report route + `MosDepthRequest`.
6. **Region sources.** Uploaded BED (resolve the BED annotation blob) and
   annotation-derived (GTF/GFF → gene intervals via `bedtools`).
7. **Card + frontend.** `build_coverage_depth_card` + the Actions-tab card +
   region view (DepthHistogramChart + table).
8. **End-to-end.** Run against a real BAM + BED (region mode) and a real BAM via
   the `bam_stats` refactor; confirm per-region facts + an unchanged birds-eye.
9. **Docs / release.** Software help page auto-updates; implementation PR uses
   `feat(pipelines): ...` and is labeled `type:feature` / `area:pipelines`.

## Test → success-criterion mapping

| Test | Success criterion |
|---|---|
| `test_every_tool_is_documented` + image build | SC1 (install + doc) |
| `test_mosdepth_handlers.py` + integration vs real BAM + BED | SC2 (region end-to-end) |
| `test_suggestion_service.py` card gating + distinct `kind`; frontend card | SC3 (card available + distinct from `bam_stats`) |
| existing `test_bam_stats_*` + schema-equivalence test | implicit refactor criterion (birds-eye unchanged) |

## Verification (against the real database, per repo guidance)

A unit test feeding `parse_regions` a hand-built `regions.bed.gz` string passes,
but the real check is running `launch_mosdepth` against a project that has a BAM
+ a BED/annotation and confirming the per-region facts and the served report.
Likewise, after MQ-4, confirm a real `bam_stats` run still produces the same
`bam_stats_*` facts as before the swap — the refactor's only acceptable outcome
is "identical output, faster."

## Risks

- **bam_stats coupling** — only the depth *source* changes; existing pure
  functions are reused and guarded by unchanged tests.
- **Annotation→region semantics** — gene-span is the v1; exon/CDS granularity
  deferred. Avoid double-counting overlapping features.
- **mosdepth output-format drift** — `regions.bed.gz` columns pinned by fixture
  tests.
- **Large inputs** — mosdepth is designed for genome scale; flag CRAM handling
  if a CRAM is ever substituted for a BAM.
