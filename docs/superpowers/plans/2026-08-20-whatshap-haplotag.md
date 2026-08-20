# WhatsHap haplotag read-level BAM haplotagging — implementation plan

Date: 2026-08-20.

Closes [#710](https://github.com/syntheticgio/bioflow/issues/710). Companion to
`docs/superpowers/specs/2026-08-20-whatshap-haplotag-design.md` (decisions
D1–D5).

**Blocked by [#628](https://github.com/syntheticgio/bioflow/issues/628).** Do
not start until #628 has merged to `main`. This plan assumes it landed:
`settings.whatshap_path`, the `tools.whatshap()` probe,
`TOOL_META["whatshap"]`, the Dockerfile install, `RunKind.PHASE_VARIANTS`,
`whatshap_runner.py` with `phased_name`, the `phase_set` variant column, and
the phase card's BAM-picker dialog. **Rebase onto `main` and re-read
`whatshap_runner.py` before writing a line** — #628's implementation may have
deviated from its own spec, and this plan extends that file rather than
creating it.

## Spike first (blocks everything below)

The spec's "Verify before implementing" list is not optional preamble; three
of its four items change the code that gets written. Do this against the
worktree stack with a real phased VCF from a #628 run:

- **S-1.** Run `whatshap haplotag --output-haplotag-list` on a real phased VCF
  + its source BAM. Capture the TSV to
  `backend/tests/fixtures/whatshap/haplotag_list.tsv` and record its exact
  column layout — `parse_haplotag_list` is written against this fixture, not
  against the manual.
- **S-2.** Check whether BAMs this app produces carry `MD` tags
  (`samtools view <bam> | head -1 | grep -o 'MD:Z:'`). If they do not,
  `--reference` is mandatory and its absence is a silent failure, so the
  reference-resolution seam is load-bearing rather than defensive.
- **S-3.** Run once **without** `--ignore-read-groups` on a BAM whose `@RG`
  sample name differs from the VCF's. Confirm whether it errors or exits zero
  having tagged nothing. This determines whether the flag is a dialog option
  or an unconditional default.
- **S-4.** Record peak RSS on the largest real BAM available, to confirm or
  correct `mem_mb=4096` before the number reaches the timing models.

Write the findings into the spec as an "Amended" note, the way the MultiQC
spec records SF-1..SF-6. **A spike answer that contradicts a decision below
supersedes the decision** — say so in the PR rather than implementing around
it.

## Files to touch

| File | Change |
|---|---|
| `backend/app/pipelines/whatshap_runner.py` | **Extend.** `build_haplotag_command(*, whatshap_path, reference, phased_vcf, bam, out, haplotag_list, ignore_read_groups=False, sample=None)` — output is a **flag** (`--output`), not positional; do not copy `phase`'s positional-output shape. `haplotagged_name(bam_name) -> "<stem>.haplotag.bam"`, mirroring `phased_name`. `parse_haplotag_list(path) -> dict` per S-1. |
| `backend/app/queue/variant_handlers.py` | **New handler** `_haplotag(ctx)` — `@handler("haplotag", mode=SUBPROCESS, job_class=JobClass.COMPUTE, resources=JobResources(cpu=1, mem_mb=4096, io=IoClass.HEAVY), max_attempts=2)`. Resolve phased VCF + BAM + reference, run, parse the list, return `{object_id, bam_object_id, output, facts, tool, tool_version}`. |
| `backend/app/queue/results.py` | **New applier** `_apply_haplotag` — ingest the new BAM (`role=ObjectRole.ALIGNMENT`, `derived_from=[bam.id, vcf.id]`, `metadata=dict(bam.metadata)`), `run_service.record_outputs`, **chain `index_bam`** gated on `blob_sha256`, then merge facts with per-key `facts.<key>` paths. Model on `_apply_align_reads` (L1440). |
| `backend/app/services/pipeline_service.py` | `launch_haplotag(object_id, alignment_id, params, owner, ...)` — eligibility, payload, `enqueue`; records both VCF and BAM as run inputs. |
| `backend/app/api/v1/pipelines.py` | `POST /pipelines/haplotag` → `JobOut`, 201. |
| `backend/app/pipelines/node_types.py` | `haplotag` `NodeTypeSpec` (inputs `variants` + `alignment`, output `alignment`, `run_tool="whatshap"`, `run_kind=RunKind.PHASE_VARIANTS`) + `_launch_haplotag` adapter. |
| `backend/app/services/suggestion_service.py` | `build_haplotag_card(obj, alignments)` — kind `"haplotag"`, category `"VARIANTS"`. Register in `CARD_BUILDERS` **and** `_CONFIGURE_DIALOGS`. |
| `backend/app/services/running_now.py` | `ENDPOINT_JOB_TYPES["/pipelines/haplotag"] = frozenset({"haplotag"})`. |
| `backend/app/services/provenance_walker.py` | Narrative verb ("haplotagged") for `haplotag`. **Not** a `_NO_NARRATIVE_STEP` entry — it produces an object a person opens. |
| `frontend/src/components/BamResults.tsx` | Haplotag facts panel, untagged count rendered beside the tagged count. |
| `frontend/src/lib/metricInfo.ts` | One `METRIC_INFO` entry per new `<Stat metric>`; `metricInfo.test.ts` enforces it and a missing entry renders nothing, silently. |
| `backend/tests/pipelines/test_whatshap_runner.py` | Command-shape tests + `parse_haplotag_list` against the S-1 fixture. |
| `backend/tests/services/test_suggestion_service.py` | New class, failing direction first (see step 6). |
| `backend/tests/queue/test_variant_handlers.py` | Handler + applier tests, including the chained `index_bam`. |

## Implementation steps (ordered)

Each numbered step is one commit. Steps 1–3 are pure and land without any
running tool; the stack is only needed from step 4.

1. **Runner (pure).** `build_haplotag_command`, `haplotagged_name`,
   `parse_haplotag_list`. Unit-test the argv and the parser against the S-1
   fixture. Include the case that matters: **a list where every read is
   untagged parses to `haplotagged_reads=0`** rather than raising or reporting
   nothing — that is the shape of the silent-failure this whole fact pair
   exists to catch (D4).
2. **Handler.** `_haplotag` in `variant_handlers.py`. Resolve the reference via
   `reference_assembly.resolve_alignment_target_for_bam` (the same seam
   `build_consensus_card` uses); pass `--reference` when it resolves, per S-2.
   Remember `docker compose restart worker` after editing this file — it does
   not hot-reload, and without the restart the job runs the old in-memory code
   while appearing to run your fix.
3. **Applier.** `_apply_haplotag` in `results.py`. Three things here are each
   independently easy to omit and each fails silently:
   - `derived_from` carries **both** parents (D2);
   - the **`index_bam` chain** after ingest, gated on `blob_sha256` (D3) —
     without it `facts.has_index` is never stamped and the BAM is permanently
     unusable in a viewer, with no error anywhere;
   - facts merged as per-key `facts.<key>` paths, never a whole-dict merge
     (the #606 erasure).
4. **Launch + route.** `launch_haplotag` and `POST /pipelines/haplotag`.
   Eligibility rejects a VCF with no populated `phase_set` here too, not only
   in the card — the card is a convenience, the launch is the gate.
5. **Registries, all four, in one commit.** `node_types` spec + adapter;
   `ENDPOINT_JOB_TYPES`; the provenance narrative verb. Then run the **whole**
   `TestExhaustiveness` class in `test_node_types.py` and the provenance
   partition test — these assert partitions, so a half-fix passes one test and
   fails its sibling (#355).
6. **Card, failing direction first.** Write the three UNAVAILABLE tests before
   `build_haplotag_card` exists:
   - probe patched off → UNAVAILABLE naming whatshap. **Patch `spec_for`, not
     `tools.whatshap`** — the registry captured the function object at import
     time, so patching the name does nothing and the test passes vacuously.
   - VCF whose `phase_set` is entirely NULL → UNAVAILABLE saying the VCF is
     not phased.
   - no selectable BAM → UNAVAILABLE saying so.
   Only then the AVAILABLE case. The image ships whatshap, so an "available"
   assertion passes whether or not any patch worked; it proves nothing alone.
   Gate on `phase_set` evidence, **never on the object name** (D1).
7. **Frontend.** `BamResults` panel + `metricInfo` entries. Verify manually at
   `http://localhost:5273` (worktree stack via `./ops/worktree-up.sh`), not
   5173.
8. **Real-database check.** Not a formality — this is the step that catches
   everything the fixtures cannot:
   ```bash
   samtools view <haplotagged.bam> | grep -c 'HP:i:'
   ```
   must be non-zero and must match `facts.haplotagged_reads`; the object must
   have a `.bai` sidecar and `facts.has_index == true`; and the phase blocks
   in the BAM must match those in the source VCF. A run that tags nothing and
   exits zero passes every unit test in this plan and fails only here.

## Verification

```bash
./backend/run-worktree-tests.sh tests/ -q
```

From the worktree, never `docker compose exec api` — that silently tests
main's code with nothing saying so. Then `ruff check --config
backend/pyproject.toml backend/app backend/tests ops e2e`, and fix everything
it reports, including findings the diff did not cause.

## Out of scope

Per the spec: `whatshap split`, in-app read-level haplotype rendering,
auto-running haplotag after phasing, and any re-registration of the WhatsHap
tool (#628 owns that entry).
