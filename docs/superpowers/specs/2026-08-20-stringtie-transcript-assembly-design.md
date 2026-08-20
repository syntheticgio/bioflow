# StringTie transcript assembly — design

Design for [#622](https://github.com/syntheticgio/bioflow/issues/622),
"Add StringTie transcript assembly (named consumer of align_params.py's
HISAT2 formatting)".

This design adds **reference-guided transcript assembly** to the RNA-seq
path. Today the app can align RNA-seq reads (HISAT2/STAR), count them
against a known annotation (featureCounts), and quantify against a known
transcriptome (Salmon) — but every one of those requires transcripts to
already be annotated. Nothing can propose a transcript model that is not
already in the reference. StringTie fills that gap: it takes a spliced
alignment plus a reference annotation and emits a GTF of assembled
transcripts, including novel isoforms.

It also closes a loop the codebase left open. `align_params.py` already
carries a `--dta` flag whose comment names StringTie as its intended
consumer; until now nothing downstream consumed it.

## What exists today

Verified against this worktree on 2026-08-20:

- **`align_params.py:463`** documents `dta` as "Formats output for
  downstream transcript assembly (StringTie et al)" — but `dta: bool =
  False`. It is an opt-in checkbox
  (`aligner_registry.py:908`, help text: "only useful if that is the next
  step"), and the auto-suggested RNA-seq card
  (`suggestion_service.py:404`) sets only `aligner: "hisat2"` and never
  `dta`. So the comment describes intent that the default path does not
  take.
- **`salmon_runner.py` + `expression_handlers.salmon_quantify` +
  `results._apply_salmon_quantify`** are the closest structural analog and
  the template followed throughout: a `SUBPROCESS` handler, a runner split
  out so command construction and output parsing are pure functions, and a
  `_apply_*` in `results.py` that ingests the produced file.
- **`facts.aligned_by`** already records which aligner produced a BAM,
  written by `results.py:1331` and `chunked_align_results.py:27`, and
  already read as a gate by
  `pipeline_service._group_gci_candidates_by_aligner:7591`. A BAM lacking
  it groups as `"unknown"` there, deliberately never merged into a named
  aligner. This is the gate #622's third success criterion needs; no new
  plumbing is required to obtain it.
- **`pipeline_service._is_annotation:4210`** is the annotation predicate,
  and it is **format-first on purpose**: its docstring records that every
  annotation in a real project arrives from `download_assembly` with
  `role=None`, verified against the live database (29 objects, 4 GFF/GTF,
  **0** carrying `ObjectRole.ANNOTATION`). A rule written against the role
  matches nothing in production while passing any hand-built fixture.
- **Two paths already produce GFF/GTF objects** —
  `results._apply_export_annotation_subset:1894` and
  `_apply_materialize_annotation_edits:1952` — both tagging
  `ObjectRole.ANNOTATION`. Neither creates novel biology; both are
  derivatives of an authoritative annotation.
- **`ObjectRole`** has no member for produced transcript models.
  `tests/storage/test_metadata_schemas.py:321` asserts
  `set(ObjectRole) == set(ROLE_FIELDS) | FORMAT_DERIVED_ROLES`, so a new
  member fails that test until `schemas.py` is updated.
- **`node_types.py:767`** shows the `(run_kind, run_tool)` uniqueness trap:
  `salmon_quantify` must claim `run_tool="salmon"` because it shares
  `RunKind.QUANTIFY` with featureCounts, "otherwise a salmon run derives
  back as a featureCounts node with ports it never had."
- **`Dockerfile:95`** installs tools from Debian trixie. **StringTie is not
  installed** and must be added.

## Decisions (with rationale)

**D1. Assembly only — not `--merge`, not `-e`.** StringTie has three modes.
Only reference-guided assembly is in scope.
`--merge` (N per-sample assemblies into one annotation) inherits an
unsolved design problem: `PortSpec` is scalar, so `differential_expression`
already smuggles its N inputs through `params`
(`node_types.py:277`). `-e` (quantify-only, restricted to the reference)
overlaps featureCounts and Salmon and would put a third "count RNA-seq"
card on the Actions tab with no rule for choosing between them. Both are
legitimate follow-up issues, filed separately.

**D2. `-G` required.** Reference-guided only. This matches the issue's own
scope language, reuses the `annotations` prefetch and `_is_annotation`
resolution that `quantify` already performs, and produces stable gene IDs
matched to the annotation. De novo assembly (no `-G`) produces transcript
models with generated IDs that nothing downstream in this app can consume.

**D3. New `RunKind.TRANSCRIPT_ASSEMBLY`, not a reuse of `QUANTIFY`.**
`RunKind`'s own docstrings state it is a display and grouping vocabulary;
"assembled transcripts" is not the same activity line as "counted one
sample". A distinct kind also keeps the `(run_kind, run_tool)` pair unique
without a discriminator, avoiding the trap documented at
`node_types.py:772`.

**D4. New `ObjectRole.ASSEMBLED_TRANSCRIPTS`.** A StringTie GTF is a
*hypothesis*; an NCBI GFF3 is *authoritative*. Conflating them is the same
hazard that split `COUNTS` from `DE_RESULTS` and that justifies `ALIGNMENT`
and `VARIANTS` existing at all. Note the honest limit: because every real
gate here is format-first (D5), this role is display-and-provenance, not
protection — the protection is D5.

**D5. `_is_annotation` excludes the new role.** Without this, StringTie
output is GTF and therefore becomes a candidate reference for featureCounts
*and* for StringTie's own `-G`. This exclusion is the one place the new role
does load-bearing work.

**D6. `dta` default flips to `True`.** This is what makes
`align_params.py:463`'s comment true rather than merely updated — a real
consumer now exists *and* the default path feeds it. `--dta` requires longer
anchors either side of a junction, trading some alignment sensitivity for
output a transcript assembler can use; for a path that exists to feed
expression analysis, that is the right default. It cannot affect DNA-seq:
the RNA card selects `hisat2` only for `_SPLICED_ASSAYS` on eukaryotes, and
DNA alignments never construct `Hisat2Params`.

**D7. The aligner gate returns `None`, not `UNAVAILABLE`.** A bwa-mem2 BAM
shows no transcript-assembly card at all. The issue calls such a card
"nonsensical," and an `UNAVAILABLE` card advertising a capability that can
never apply to this object is worse than silence. `UNAVAILABLE` is reserved
for the two states a user can act on: tool not installed, no annotation in
the project.

**D8. A BAM with no `aligned_by` gets no card.** A deliberate
false-negative: an uploaded HISAT2 BAM will not be offered the card. This
matches `_group_gci_candidates_by_aligner`'s refusal to merge `"unknown"`
into a named aligner, and an unknown-provenance BAM may well be DNA-seq.

**D9. This card is stricter than its `quantify` sibling, deliberately.**
`build_quantify_card` offers on *any* BAM because "whether an alignment is
RNA-seq is not knowable from the file"
(`suggestion_service.py:2166`). Here it **is** knowable, because
`aligned_by` records it. The asymmetry is justified by that difference, not
an inconsistency to be smoothed away.

**D10. No index step, therefore no `SidecarRole`.** Unlike Salmon,
StringTie runs directly against the BAM. There is no index-caching question.

## Staging

Each stage is independently mergeable and leaves the app working.

## Stage 0 — tool registration

1. `Dockerfile`: add `stringtie` to the trixie `apt-get install` block
   (alongside `hisat2`, `rna-star`, `salmon`).
2. `config.py`: `stringtie_path: str = "stringtie"`.
3. `tools.py`: `stringtie()` probe via `_probe`, added to the probe list,
   and `cache_clear()` registration alongside `salmon.cache_clear()`.
4. `tools.py`: `TOOL_META["stringtie"]` with
   `pipelines=(PipelineType.EXPRESSION,)`, `license="MIT"` — **verified
   2026-08-20 via `gh api repos/gpertea/stringtie`, not recalled** — and the
   Pertea et al. 2015 *Nature Biotechnology* citation.

**Verification:** `test_every_tool_is_documented` passes; `stringtie
--version` resolves inside the image.

## Stage 1 — runner and handler

1. **`pipelines/stringtie_runner.py`** (new). Pure functions only, split
   from the handler for the reason `salmon_runner`'s docstring gives:
   - `assemble_command(bam, annotation, out_gtf, *, stringtie_path,
     threads) -> list[str]` → `stringtie <bam> -G <ann> -o <out> -p <n>`.
   - `parse_gtf(text) -> dict` → transcript count, gene count, and novel
     transcript count (transcripts whose attributes carry no `reference_id`,
     which is how StringTie marks a model absent from `-G`).
2. **`models/run.py`**: `RunKind.TRANSCRIPT_ASSEMBLY`, with a comment
   stating why it is not `QUANTIFY` (D3).
3. **`models/object.py`**: `ObjectRole.ASSEMBLED_TRANSCRIPTS`, with a
   comment stating the hypothesis-vs-authoritative distinction (D4).
4. **`metadata/schemas.py`**: add the new role to **`FORMAT_DERIVED_ROLES`**,
   not `ROLE_FIELDS`. It belongs beside `DE_RESULTS` for that member's own
   stated reason: everything describing an assembled-transcripts GTF -- which
   BAM, which annotation, which tool and version -- is provenance the applier
   already records from the run that produced it, and an assembly nobody
   produced here is not a thing that exists. There is no question to ask a
   user about it. `test_metadata_schemas.py:321` is the forcing function.
5. **`queue/expression_handlers.py`**: `@handler("transcript_assembly",
   mode=HandlerMode.SUBPROCESS, ...)`, mirroring `salmon_quantify`'s
   workdir/`_failure` shape.
6. **`queue/results.py`**: `_apply_transcript_assembly` — ingest the GTF
   with `role=ObjectRole.ASSEMBLED_TRANSCRIPTS`, `derived_from=[bam.id]`,
   `facts={"assembled_by": "stringtie", "transcript_count": ...,
   "novel_transcript_count": ...}`, and `record_outputs` on the run.

**Verification:** unit tests on `assemble_command` and `parse_gtf`; a real
run against a HISAT2 BAM produces a GTF object.

## Stage 2 — launch path and card

1. **`pipeline_service.launch_transcript_assembly()`** — resolve the
   annotation via the existing `_is_annotation`/`resolve_annotation` path,
   create the `PipelineRun`, enqueue.
2. **`pipeline_service._is_annotation`**: add the D5 exclusion.
3. **`node_types.py`**: `"transcript_assembly"` spec + `_launch_*` adapter.
   Inputs: `alignment` (BAM), `annotation` (GFF/GTF, optional — resolved
   server-side like `quantify`). Output: `transcripts`
   (GTF, `role=ASSEMBLED_TRANSCRIPTS`).
4. **`api/v1/pipelines.py`**: `POST /pipelines/transcript-assembly`.
5. **`suggestion_service.build_transcript_assembly_card()`**, registered in
   `CARD_BUILDERS`, gated per D7/D8:
   ```python
   if obj.format.kind is not FormatKind.BAM:
       return None
   if str((obj.facts or {}).get("aligned_by") or "") not in _SPLICE_AWARE:
       return None
   ```
   where `_SPLICE_AWARE = frozenset({Aligner.HISAT2.value,
   Aligner.STAR.value})` — spelled from the registry so a rename cannot
   silently unhook the gate.
6. **Provenance + activity**: register the new kind in
   `provenance_walker.py`, `provenance_report.py`, `provenance_prompt.py`,
   and `running_now.py`, following each file's existing Salmon entry.

**Verification:** card appears on a HISAT2 BAM, absent on a bwa-mem2 BAM;
end-to-end launch produces a GTF.

## Stage 3 — close the `dta` loop

1. `align_params.py:464`: `dta: bool = True`.
2. `align_params.py:499`: `dta=bool(data.get("dta", True))` — **required**,
   or the default applies to fresh dialogs but not to any param dict
   round-tripped through the queue, a split-brain default that reads as
   "the flag I set didn't take."
3. `aligner_registry.py:911`: `default=True`; drop "only useful if that is
   the next step" from the help text, which is no longer accurate.
4. `align_params.py:463`: update the comment to name the real consumer.

**Verification:** an alignment launched from the RNA-seq card passes
`--dta`; existing `Hisat2Params` round-trip tests updated.

## Cross-cutting obligations (each has bitten this repo before)

- **Run the whole `TestExhaustiveness` class** in
  `tests/pipelines/test_node_types.py`, not just the named test. #355 landed
  two independent commits that each satisfied one half of the
  classified/not-double-classified partition and collided; it stayed red
  until someone ran the full file (#366).
- **`test_metadata_schemas.py:321`** will go red on the new `ObjectRole`
  member. That is the intended forcing function, not an obstacle.
- **Assert the card flips to unavailable when the probe is patched off.**
  The image ships most tools installed, so a test asserting a card is
  *available* passes whether or not its patch worked. Unavailable is the
  direction that fails when the seam breaks.
- **Check the rule against the real database**, not only fixtures:
  `docker compose exec api python -c "..."` over live BAMs to confirm which
  carry `aligned_by` and with what values. The Actions tab's rules passed a
  green suite while being wrong about real objects once already.
- **Verify StringTie's GTF attribute names against real output**, not
  recall, before relying on `reference_id` to count novel transcripts.

## Error handling

| Failure | Surfaced as |
|---|---|
| StringTie not installed | `UNAVAILABLE` card via `tools.require()` |
| No annotation in project | `UNAVAILABLE` card, wording mirroring Salmon's "no transcriptome" |
| Non-zero exit | `_failure(code, log_path, "stringtie assemble")`, as `salmon_quantify` does |

StringTie's common real-world failure is a chromosome-naming mismatch
between BAM and GTF (`chr1` vs `1`), reported on stderr; the log path is
what makes that legible rather than an opaque exit code.

## Testing

- Pure-function unit tests on `stringtie_runner` (argv construction, GTF
  parsing) — no queue, no filesystem.
- Card tests in `test_suggestion_service.py` covering the gate **both
  ways**: HISAT2 and STAR produce a card; bwa-mem2, minimap2, and
  missing-`aligned_by` produce `None`; probe patched off produces
  `UNAVAILABLE`.
- `test_every_tool_is_documented`.
- Full `TestExhaustiveness` in `test_node_types.py`.
- `Hisat2Params` round-trip tests updated for the new `dta` default.

## Verify before implementing (not asserted above)

1. **Is `stringtie` in Debian trixie?** The Dockerfile installs from trixie;
   if the package is absent or its arm64 build is broken (as Salmon's was —
   `Dockerfile:298`), Stage 0 needs an install script instead.
2. **StringTie's exact GTF attributes** for distinguishing novel from
   reference transcripts.
3. **Whether `-p` is the correct threads flag** for the installed version.

## Out of scope

- `stringtie --merge` (multi-sample non-redundant annotation) — blocked on
  the scalar-`PortSpec` N-input problem; separate issue.
- `-e -B` quantify-only mode and Ballgown output — overlaps featureCounts
  and Salmon; separate issue.
- De novo (no `-G`) assembly.
- Feeding assembled transcripts back into DE testing.
