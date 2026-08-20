# MetaBAT2 contig binning — implementation plan

Date: 2026-08-20.

Closes [#728](https://github.com/syntheticgio/bioflow/issues/728). Companion to
`docs/superpowers/specs/2026-08-20-metabat2-binning-design.md` (decisions
B1–B5, requirements R1–R6).

**Depends on [#727](https://github.com/syntheticgio/bioflow/issues/727)** —
needs a metagenome assembly to bin, and reads its `assembly_meta_mode` fact.

This is the epic's core and its largest child: a new tool *plus* the first
one-job-many-objects handler in the app.

## Spike first

- **S-1. MetaBAT2 on bioconda for `linux-aarch64`?** Check that subdir
  specifically before any GitHub release binary (CLAUDE.md). Check apt against
  a real container **with a control package in the same run** before believing
  "not packaged".
- **S-2. Does `jgi_summarize_bam_contig_depths` ship in the same package** as
  `metabat2`? B1 depends on having it; if it is packaged separately the install
  needs both.
- **S-3. Bin output naming and location** — `bins/bin.1.fa` style? And are
  unbinned contigs written by default, or only with `--unbinned`? B3 needs
  them.
- **S-4. License and citation**, from the project's own repository.
- **S-5. Typical bin counts** on a real community, to sanity-check B4's cap
  of 200.

## Files to touch

| File | Change |
|---|---|
| `backend/scripts/install-metabat2.sh` + `backend/Dockerfile` | Per S-1/S-2. End with a **real run**, not `--version` — a tool that dlopens its libraries passes `--version` with them deleted. |
| `backend/app/config.py` | `metabat2_path`, `jgi_depths_path`, and `metagenome_bin_cap: int = 200` (B4 — a setting, not a constant). |
| `backend/app/pipelines/tools.py` | `metabat2()` probe + `all_tools()` + `cache_clear`; `TOOL_META["metabat2"]` with S-4's fields. |
| `backend/app/pipelines/metabat_runner.py` | **New, pure.** `build_depths_command(...)`, `build_binning_command(...)`, `parse_depths(...)`, `bin_facts(...)`. |
| `backend/app/queue/<binning>_handlers.py` | **New.** Run depths → run binning → enumerate bins. |
| `backend/app/queue/results.py` | `_apply_binning` — the N-object applier (B2/B3/R3/R5). |
| `backend/app/services/pipeline_service.py` | `launch_binning(contigs_id, alignment_id, owner, params)`. |
| `backend/app/api/v1/pipelines.py` | `POST /pipelines/binning`. |
| `backend/app/services/suggestion_service.py` | `build_binning_card` per B5. |
| `backend/app/services/running_now.py` | `ENDPOINT_JOB_TYPES` entry. |
| `backend/app/services/provenance_walker.py` | Narrative verb ("binned contigs") — it produces objects a person opens. |
| `backend/app/pipelines/node_types.py` | Spec + adapter; the `EXCLUDED_LAUNCHES` **partition**. |

## Ordered steps

1. **Install + tool registration.** Per CLAUDE.md this is a tool with its own
   job type, so it touches six hand-maintained registries and **four fail
   silently**. Do them all in this branch, not "later": `tools.all_tools()`,
   `node_types.NODE_TYPES`, `running_now.ENDPOINT_JOB_TYPES`,
   `provenance_walker`, plus `TOOL_META` and `suggestion_service`.
2. **The depth step (B1) — get this right or the rest is worthless.**
   Run MetaBAT2's own `jgi_summarize_bam_contig_depths`, **not** mosdepth's
   per-contig output. The depth file carries mean depth *and variance*, and
   MetaBAT2 bins on coverage co-variance alongside tetranucleotide composition.
   A hand-built file from mosdepth's means is a file MetaBAT2 **accepts and
   bins from** — worse quality, no error, nothing to say so.
   Assert the command shape in a test, because the failure it guards is silent
   and an end-to-end assertion would pass either way.
3. **Command builders + depth parser.** Pure, unit-tested against a fixture
   captured from a real run (S-3).
4. **The N-object applier** (B2/B3), modelled on `_apply_assemble_reads` —
   which already ingests two objects from one job, contigs first and
   independently, precisely so a secondary failure cannot lose the expensive
   output. Generalize from 2 to N:
   - each bin `ObjectRole.REFERENCE`, `derived_from=[contigs.id, bam.id]`,
     assembly metadata carried forward;
   - **ingest independently** — a failure on bin 12 must not lose 1–11 and
     13–40 (R3). Log, continue, and report the count *actually ingested*, not
     the count produced;
   - per-bin facts: `bin_index`, `bin_source_assembly`, `bin_contig_count`,
     `bin_total_bases`, `bin_mean_depth`;
   - **unbinned contigs as their own object** (B3) with `bin_unbinned: true`,
     plus `binning_binned_bases` / `binning_unbinned_bases` /
     `binning_bin_count` on the source assembly. Both the number and the
     object: the number tells the user whether to care, the object lets them
     act on it.
5. **The cap (B4/R5), and it refuses rather than truncates.** More bins than
   `metagenome_bin_cap` → **fail the job naming both numbers**, ingesting
   nothing. Truncating would discard MAGs ordered by MetaBAT2's numbering
   rather than by quality, so the dropped set is arbitrary and invisible.
   **Test that nothing is ingested**, not merely that the job failed — the
   latter passes an implementation that ingests 200 objects and then fails.
6. **Card** (B5), failing direction first: not installed / not a FASTA
   reference / **no alignment of its own reads resolves** / not a `--meta`
   assembly. Patch `spec_for`, not `tools.metabat2`.
   The last gate is **soft**: offer binning on a non-`--meta` assembly with an
   explanation rather than refusing. A contaminated isolate is exactly a case
   someone might want to bin, and a hard gate here is the `protein.faa`
   mistake `build_consensus_card` already documents.
7. **Registries, then whole test classes.** `TestExhaustiveness` and the
   provenance partition are **partitions** — a half-fix passes one test and
   fails its sibling (#355).
8. **Real-data check.** A real metagenome assembly plus its alignment.
   The thing to confirm is not that bins appeared but that **a bin is usable by
   the completeness card without special-casing** — that is what B2's
   `REFERENCE` choice buys, and no unit test shows it.

## Verification

```bash
./backend/run-worktree-tests.sh tests/ -q
```

From the worktree, never `docker compose exec api`. Restart the worker after
handler edits (`docker compose restart worker`, from the **main** repo root) —
it does not hot-reload. Then `ruff check --config backend/pyproject.toml
backend/app backend/tests ops e2e`, fixing pre-existing findings too.

## Out of scope

Per the spec: bin QC (#729), taxonomic labelling (#730), ensemble binning
(DAS Tool), and multi-sample co-binning — the last is genuinely valuable and
genuinely bigger, needing the N-input representation #703's spec discusses.
