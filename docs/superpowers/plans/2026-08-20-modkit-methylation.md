# modkit ONT modified-base summarization — implementation plan

Date: 2026-08-20.

Closes [#631](https://github.com/syntheticgio/bioflow/issues/631). Companion
to `docs/superpowers/specs/2026-08-20-modkit-methylation-design.md`
(decisions K1–K5).

Three stages, one PR each, each independently mergeable and green. Stage 1
without stage 2 is a coherent product; do not start stage 2 until stage 1 has
been run against a real ONT BAM.

## Spike first (blocks stage 0)

- **S-1. Is modkit on bioconda for `linux-aarch64`?** Check
  `https://anaconda.org/bioconda/ont-modkit/files` and the
  `linux-aarch64` subdir specifically. ONT's GitHub release assets are
  routinely x86-64 only; taking one without checking is how a tool becomes an
  arm64 skip. Per CLAUDE.md, check apt against a real container **with a
  control package in the same run** before believing "not packaged".
- **S-2. License and citation**, read from ONT's own repository. ONT's
  licensing is not uniform across their repos, so this is a read, not a
  recall.
- **S-3. bedMethyl column layout** on whichever version S-1 selects. Capture a
  real output to `backend/tests/fixtures/modkit/pileup.bed`; the parser is
  written against that file. The layout has changed across modkit releases.
- **S-4. Does `pileup` require `--ref`?** If yes, the reference-resolution
  seam is `reference_assembly.resolve_alignment_target_for_bam` and the card
  gains a fourth UNAVAILABLE case (no resolvable reference).
- **S-5. Peak RSS** on the largest real ONT BAM available, to confirm or
  correct `mem_mb=4096` before the number reaches the timing models.

Record the answers in the spec as an "Amended" note. **A spike answer that
contradicts a decision supersedes it** — say so in the PR rather than
implementing around it.

## Stage 0 — tool registration

| File | Change |
|---|---|
| `backend/scripts/install-modkit.sh` | **New.** Per S-1. If bioconda: the `install-medaka.sh` own-venv pattern. If a release binary: note that this image ships without `curl` (install-meryl.sh and install-quast.sh purge it), so a late install script must reinstall and re-purge it. **End with a real run, not `--version`** — a tool that dlopens its libraries passes `--version` with its libraries deleted. |
| `backend/Dockerfile` | Wire the script in, with a comment recording the pinned version. |
| `backend/app/config.py` | `modkit_path: str = "modkit"`. |
| `backend/app/pipelines/tools.py` | `modkit()` probe; add to `all_tools()` and the `cache_clear` block. `TOOL_META["modkit"]` with `pipelines=(PipelineType.UTILITY,)` and the four bibliographic fields from S-2. `usage` as behaviour, not flags. |

**Done when** `/help/software` lists modkit with version, license, citation and
usage, and `test_every_tool_is_documented` passes — criterion 1.

## Stage 1 — end-to-end pileup

| File | Change |
|---|---|
| `backend/app/pipelines/modkit_runner.py` | **New.** `has_modification_tags(bam_path, *, limit=1000) -> ModTagProbe` (K1) — returns what it found **and how far it looked**; `build_pileup_command(...)`; `parse_bedmethyl(...)` per S-3; `summarize(...)`. All pure. |
| `backend/app/config.py` | `methylation_dir` property under `bioinfo_home`, mirroring `coverage_dir`. |
| `backend/app/queue/<modkit>_handlers.py` | **New.** `@handler("methylation", mode=SUBPROCESS, job_class=JobClass.COMPUTE, resources=JobResources(cpu=2, mem_mb=4096, io=IoClass.HEAVY), max_attempts=2)`. Imported for side effects in `handlers.py`. |
| `backend/app/queue/results.py` | `_apply_methylation` — ingest the bedMethyl (`derived_from=[bam.id]`), merge summary facts by per-key `facts.<key>` paths (**never** a whole-dict merge, #606), and enforce K3. |
| `backend/app/services/pipeline_service.py` | `launch_methylation(bam_id, owner, ...)` — **repeats the K1 check**; the card is a convenience, the launch is the gate. |
| `backend/app/api/v1/pipelines.py` | `POST /pipelines/methylation` (201) and `GET /pipelines/methylation/{object_id}/report`. |
| `backend/app/services/suggestion_service.py` | `build_methylation_card(obj)` per K2, category `ASSEMBLY_QC`, registered in `CARD_BUILDERS`. |
| `backend/app/services/running_now.py` | `ENDPOINT_JOB_TYPES["/pipelines/methylation"]`. |
| `backend/app/services/provenance_walker.py` | Narrative verb ("summarized base modifications"). **Not** `_NO_NARRATIVE_STEP` — unlike `coverage`, this produces an object a person opens. |
| `backend/app/pipelines/node_types.py` | `NodeTypeSpec` + `_launch_methylation` adapter, or the `EXCLUDED_LAUNCHES` side — whichever, it is a **partition**. |
| `frontend/src/lib/metricInfo.ts` | One entry per new `<Stat metric>`; missing, the InfoMarker renders nothing, silently. |

### Ordered steps

1. **Prefix probe (K1), first and alone.** `has_modification_tags` with its
   three fixtures: tags present, tags absent, and **tags present only after
   the sampled prefix**. That third test asserts the documented false negative
   rather than pretending it cannot happen — it is the honest boundary of the
   whole design, and a later reader who deletes it will "fix" the bound into a
   full scan of a 100 GB BAM.
   **Put this in `modkit_runner.py`, not `parsers.py`.** That module reads
   headers only, by a deliberate policy its docstring is built around; MM/ML
   are record tags, and extending it to scan records breaks the rule every
   other parser depends on.
2. **Command + parser.** `build_pileup_command` and `parse_bedmethyl` against
   the S-3 fixture. Add `--ref` iff S-4 says so.
3. **Handler + config dir.** Restart the worker after editing
   (`docker compose restart worker` from the **main** repo root) — it does not
   hot-reload, and without the restart the job runs old in-memory code while
   appearing to run your fix.
4. **Applier, with K3 enforced.** A pileup producing zero rows **fails the
   job**. Write that test before the happy path: a tool exiting zero is not a
   tool producing a result, and this is the exact silent-success the issue was
   filed about.
5. **Launch + route**, repeating the K1 check.
6. **Card, failing direction first.** Write the UNAVAILABLE tests before the
   builder exists:
   - probe patched off → names modkit. **Patch `spec_for`, not
     `tools.modkit`** — the registry captured the function object at import,
     so patching the name passes vacuously.
   - BAM with no MM tags → the message must explain that modified-base calling
     happens at basecalling time and cannot be added afterwards. **Assert on
     that explanation, not just on the UNAVAILABLE status** — criterion 3 is
     about what the user is told, so a test that only checks the status would
     pass against a bare "unavailable" that fails the criterion.
   - non-BAM → standard kind check.
   Only then the AVAILABLE case.
7. **Registries.** All of them, then the whole `TestExhaustiveness` class and
   the provenance partition — they assert partitions, so a half-fix passes one
   test and fails its sibling (#355).
8. **Real-data check.** A real ONT BAM with modification tags end to end,
   **and** one without, confirming the refusal message reaches the UI at
   `http://localhost:5273` (worktree stack via `./ops/worktree-up.sh`), not
   5173.

## Stage 2 — windowed methylation track

Only after stage 1 has run against real data.

1. Aggregate per-site methylation into windows using `gc_tracks.WINDOW_COUNT`
   and `MIN_WINDOW_BASES` — the same tiling mosdepth uses, so the methylation
   track shares an x-axis with the GC and coverage tracks already rendered per
   contig. Reuse `gc_tracks.windows()`; do not re-derive the scheme.
2. Store the windowed array where the report endpoint serves it, **not** in
   facts. A mammalian genome has tens of millions of CpG sites.
3. Render beside the existing per-contig tracks, following whatever
   #626's coverage track established.

## Verification

Backend tests from the worktree:

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Never `docker compose exec api` — from a worktree that silently tests main's
code. Then `ruff check --config backend/pyproject.toml backend/app
backend/tests ops e2e`, fixing everything it reports including findings the
diff did not cause.

## Out of scope

Per the spec: `modkit dmr` (differential methylation), other modkit
subcommands, re-basecalling, and interpretation of specific modification
codes.
