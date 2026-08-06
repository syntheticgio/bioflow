# Fact-grounded pipeline provenance narratives

Design for [#16](https://github.com/syntheticgio/bioflow/issues/16).
Backlog source: `More LLM usage: pipeline provenance narratives` in
`docs/TODO.md`.

## The problem, and the constraint that shapes every decision

Given a VCF, BioFlow cannot today answer "what produced this?" in a form
anyone can read. The facts exist -- `derived_from` records ancestry, the
`*_provenance` builders in `queue/results.py` stamp tool names, versions and
parameters onto every output, and `job_timings` records when each run
happened and whether it succeeded. What is missing is something that walks
the chain and renders it.

**This output is destined for a methods section.** That single constraint
drives the rest of this document. A file summary that is slightly wrong is a
mild annoyance; a methods paragraph that invents a version number is a
correction notice on a published paper. So the design is arranged so that
the cheapest, most reliable component is the one users depend on, and the
model is the part that can fail without taking the feature down.

## Shape: structured report first, prose as a bonus

The deliverable is a **deterministic structured report** -- ordered steps,
tools, versions, parameters, explicit gaps -- rendered as markdown with no
model involved. It works on an install with no AI provider configured at
all.

When a provider *is* configured, a second action renders that same fact set
as flowing prose. The structured report stays visible alongside it.

Three reasons this ordering rather than prose-only:

1. **It degrades correctly.** A user with no provider still gets the thing
   they need.
2. **It makes anti-fabrication testable.** "The model did not invent a step"
   is hard to assert. "Every version token in the prose appears in the
   structured fact set" is a string-containment check over deterministic
   output.
3. **Gaps are a rendering decision.** "Missing facts are represented
   explicitly rather than inferred" (the issue's own acceptance criterion) is
   far easier to enforce in a renderer than in a prompt instruction.

## Architecture

Three modules, each with one responsibility. Only the first touches the
database, which is what keeps the other two testable as pure functions.

| Module | Responsibility | Depends on |
|---|---|---|
| `services/provenance_walker.py` | Walk the DAG; produce a `ProvenanceChain`. No prose, no formatting. | `DataObject`, `Job`, `timing_service` |
| `services/provenance_report.py` | Render a `ProvenanceChain` as markdown. Deterministic. | the walker's types only |
| `services/provenance_prompt.py` | Turn a `ProvenanceChain` into system + user prompt strings. | the walker's types only |

### Routes

- `GET /objects/{object_id}/provenance-narrative` -- returns the structured
  chain, its rendered markdown, and the gap count. No model involvement, so
  it never waits on a provider.
- `POST /objects/{object_id}/provenance-narrative/prose` -- performs the
  model call and returns prose, or a reason it was unavailable or rejected.

Splitting prose onto its own route is not cosmetic. `ai.complete()` returns
`Completion | ToolCall | Failure` and **never raises**
(`services/ai/complete.py`); CLAUDE.md records that checking `if result is
None` type-checks, reads as correct, and treats every failure as a success.
Confining the model call to one route means that `isinstance(result,
Completion)` check has exactly one site to get right, and the report route
has none.

### Live, not stored

The walk is a handful of `_id` lookups up a chain realistically 3-6 deep,
with the `by_derived_from` index already in place. That is milliseconds. It
is deliberately **not** a queue job, unlike `summarize_object` -- there the
expense is the model call, which is why that one is a job.

Storing the narrative would buy nothing and cost a staleness problem: the
chain's facts can change underneath it (a re-probe fills in a version, a
parent is deleted), and a stored methods paragraph that no longer matches
its own provenance is precisely the failure this feature exists to prevent.

## The walker

```python
@dataclass(frozen=True)
class Step:
    job_type: str                  # registry handler name, e.g. "align_reads"
    verb: str                      # "aligned with" -- verb table, or generic
    tool: str | None
    tool_version: str | None
    params: dict
    ran_at: datetime | None
    outcome: str | None            # from timing records; failures included
    gaps: list[Gap]

@dataclass(frozen=True)
class Node:
    object_id: PydanticObjectId
    name: str
    role: ObjectRole | None
    kind: Literal["spine", "supporting"]
    produced_by: Step | None       # None = root (uploaded or downloaded)
    parents: list[PydanticObjectId]

@dataclass(frozen=True)
class ProvenanceChain:
    target: Node
    nodes: dict[PydanticObjectId, Node]
    order: list[PydanticObjectId]           # topological, oldest first
    gaps: list[Gap]                         # flattened, for the count
    branches: list[list[PydanticObjectId]]  # multiple spine parents
```

### Traversal

Breadth-first up `derived_from` from the target, memoized by object id with
a visited set. The DAG reconverges -- a reference used by both the alignment
and the variant call is reachable by two paths -- and must render once, not
twice.

Depth is capped at 64, emitting a `depth_exceeded` gap if hit. That guards
against a cycle from hand-edited data; it is not an expected case.

### Spine versus supporting inputs

`derived_from` is a list, so ancestry is a DAG, not a line. A VCF's parents
include the BAM *and* the reference; an annotated VCF's include the GFF3.
Walking everything to the roots is correct but would put the reference
genome's NCBI accession at the same level as the trim parameters.

So each ancestor is classified as it is walked:

- **Spine** -- the specimen lineage (reads -> trimmed -> BAM -> VCF).
  Rendered as ordered steps.
- **Supporting** -- reference, annotation. Rendered as a materials section,
  and still walked to full depth so an NCBI-downloaded reference reports its
  accession rather than its filename.

The rule is decided per-edge by the producing step's own facts, which
already name their parents by role: `reference_object_id` and
`annotation_object_id` are supporting, everything else is spine. Where a
step's facts do not say, the fallback is `ObjectRole`.

Walking supporting inputs to full depth is what makes "aligned to GRCh38.p14
(GCF_000001405.40)" possible. Naming them without recursing would yield
"aligned to reference.fasta", which is how a methods section becomes
useless.

**Ambiguity branches rather than resolving.** An assembly with two read
sets, or a polishing step taking both long and short reads, has multiple
genuine spine parents. The walker records all of them and the report renders
a visible fork. Picking one would be inventing a tidy story.

**Sidecars are excluded at traversal.** `sidecar_of` objects (`.bai`,
`.tbi`, `.fai`) are biologically inert, and a methods paragraph mentioning
BAM index construction is noise. Excluded during the walk rather than
filtered at render, so they never reach the prompt.

### Where facts come from

In priority order: the object's own `facts` (stamped by the `*_provenance`
builders), then the `Job` document, then
`timing_service.records_for_object()` for `ran_at` and `outcome`.

That last accessor **includes failed runs on purpose**. CLAUDE.md is
explicit that this is the deliberate counterpart to the outcome-filtered
`_modelled()` used by the predictive models: provenance is the one reader
that wants failures, because a step that failed and was retried is exactly
what a reader needs to know. This design must not switch to the filtered
accessor for tidiness.

## Gaps

Not every step has complete facts, and the situations are genuinely
different. Collapsing them into one "unknown" would be its own dishonesty,
so the walker distinguishes them explicitly, each with a different remedy.
Five are states a real chain reaches; `depth_exceeded` is a sixth that only a
cycle from hand-edited data can produce:

| `Gap.kind` | Situation | Rendered as |
|---|---|---|
| *(not a gap)* | Root: user-uploaded or downloaded input | "Input: `reads_R1.fastq.gz` (uploaded 2026-07-14)" |
| `version_unrecorded` | Step ran, version fact absent | "aligned with bwa-mem2 (**version not recorded**)" |
| `params_unrecorded` | Step ran, no provenance builder existed | "**step recorded without parameters** (job: align_reads, 2026-06-02)" |
| `share_boundary` | Chain terminates at a shared file | "**lineage continues in another profile and is not available here**" |
| `dangling_parent` | Parent id resolves to nothing | "**parent object no longer exists** (id: ...)" |
| `depth_exceeded` | Traversal cap hit | "**ancestry truncated at 64 steps**" |

The rule: **the walker never omits a step it knows ran, and never asserts a
fact it does not have.** A gap renders in the position the fact would have
occupied, so a reader scanning for "which bwa-mem2 version" sees the
question was asked and unanswered, rather than seeing nothing and assuming
it was irrelevant.

`share_boundary` is expected, not a bug: `shared_from` objects deliberately
clear `derived_from` and `produced_by_job` because those name objects and
jobs in the sender's partition, which the recipient's owner-scoped lookups
can never resolve.

**Roots are not gaps.** An uploaded FASTQ legitimately has no producing job.
Counting that would inflate the number with something no user can act on.

**The report carries a gap count**, not a percentage or a grade. "3 facts
not recorded" is actionable; "86% complete" is not.

## Steps come from jobs, not from facts

This is the one place the design departs from the issue's own framing, which
lists "inputs, steps, parameters, tools, versions, and outputs" as though
steps are discovered from facts.

**The spine is `produced_by_job`; facts only enrich it.**

The object graph knows a step happened even when the fact vocabulary cannot
describe it. Every applier sets `produced_by_job` unconditionally, including
several with no named `*_provenance` builder at all (`results.py` lines 722,
810, 1556, 1626, 1696 pass `facts=` without one). A facts-first walker would
miss those steps entirely. A jobs-first walker renders them as steps with a
`params_unrecorded` gap -- which is the honest answer.

### The verb table and its exhaustiveness test

`Job.type` is a plain `str` naming a registered handler
(`models/job.py:163`), **not an enum** -- so an enum-based exhaustiveness
test is not available. The registry provides an equivalent seam:
`registry.all_handlers()` enumerates every registered handler name.

```python
# Handler name -> how a methods section says it happened.
# Names verified against `registry.all_handlers()` (37 registered as of
# 2026-08-06), not recalled.
_STEP_VERBS: dict[str, str] = {
    "trim_reads": "trimmed with",
    "align_reads": "aligned with",
    "call_variants": "variant-called with",
    "annotate_variants": "annotated with",
    "assemble_reads": "assembled with",
    "assemble_upload": "assembled with",
    "polish_assembly": "polished with",
    "scaffold_assembly": "scaffolded with",
    "quantify": "quantified with",
    "differential_expression": "tested for differential expression with",
    "consensus_from_alignment": "called a consensus with",
    "run_qc": "quality-checked with",
    "assess_completeness": "assessed for completeness with",
    "assess_misassemblies": "assessed for misassemblies with",
    "download_sra_run": "downloaded from the SRA",
    "download_assembly": "downloaded from NCBI",
    "download_uniprot": "downloaded from UniProt",
    "download_lineage": "downloaded",
}

# Registered handlers that legitimately produce no narrative step, each with
# a reason. Together with _STEP_VERBS this must cover every registered handler.
_NO_NARRATIVE_STEP: frozenset[str] = frozenset({
    # Sidecar-only outputs, excluded at traversal anyway.
    "build_index", "index_bam",
    # Statistics written back onto an existing object; no new object, and the
    # numbers are already shown in the file panel.
    "run_bam_stats", "run_vcf_stats", "ingest_headers",
    # Bookkeeping on bytes already ingested.
    "hash_blob", "register_hash", "verify_blob", "verify_files",
    # Infrastructure; touches no project data.
    "install_tool", "uninstall_tool",
    # Housekeeping reapers and GC.
    "gc_blobs", "reap_pipeline_scratch", "reap_report_dirs", "reap_uploads",
    # AI features that write a field rather than producing a data object.
    "summarize_object", "answer_project_question",
    # Test-only.
    "noop", "sleep_test",
})
```

The test mirrors the `set(TheEnum) == set(main_dict) | companion_set`
pattern CLAUDE.md names as the one to copy:

```python
def test_every_registered_handler_is_classified():
    names = set(registry.all_handlers())
    assert names, "registry empty -- handler modules not imported"
    assert names == set(_STEP_VERBS) | _NO_NARRATIVE_STEP
```

The non-empty assertion is load-bearing: without it an import-order mistake
passes vacuously against an empty registry, which is the same silent-skip
failure the test exists to prevent.

This is CLAUDE.md's **category one** (genuinely derivable, companion
frozenset for the members the main dict should not cover). The `*_by` fact
keys are **category two** (intentionally partial, open vocabulary): a future
`polished_by` renders generically as "processed with wtdbg2" with no table
entry, because forcing every fact key to have a hand-written verb would turn
"we do not have a phrase for this" into a wrong guess.

**Verb strings carry a staleness risk nothing can catch mechanically** --
the same shape as `ToolMeta.usage`, where a comment made a wrong claim for
years because no test could check prose. What the test *does* guarantee is
that a wrong-but-present verb is the worst case, never a silently absent
step.

## The prompt, and what actually stops fabrication

Four layers, ordered by how much they can be trusted. The first three are
structural; only the fourth depends on the model behaving.

**1. The model gets no database access and no free text.** The prompt is
built solely from the `ProvenanceChain` -- the same object that rendered the
structured report. There is no path by which the model learns a fact the
report does not already display.

**2. Gaps are supplied as content to state, not blanks to fill.** The prompt
renders `version_unrecorded` as the literal string `version not recorded`
inside the fact list, and the system prompt instructs: state it exactly as
given, do not substitute a plausible version. The resulting prose --
"aligned with bwa-mem2 (version not recorded)" -- is awkward and correct.
The awkwardness is a feature: it prompts the user to go record the version
rather than ship the paragraph.

**3. A post-generation containment check.** Every version-shaped token
(`\d+\.\d+[\w.\-]*`) and every tool name in the model's output must appear
in the chain's fact set. On a miss the prose is **discarded and the
structured report shown alone**, with a note that generation was rejected.
This is what makes the guarantee real rather than aspirational: a
deterministic string assertion that fails closed.

**Its limit, stated plainly:** it catches invented versions and tool names,
the high-cost fabrication class. It does **not** catch an invented causal
claim -- "reads were trimmed to remove adapter contamination" when nothing
measured contamination. That is mitigated by layer 4 and by the structured
report being the citable artifact, not eliminated. Anyone extending this
should not read layer 3 as total coverage.

**4. The system prompt constrains the task to rephrasing** -- no
interpretation, no significance claims, no filling gaps.

### Routing

A new `TaskSlot.PROVENANCE_NARRATIVE` in `models/ai.py`, label "Methods
narratives", plus its `_SLOT_LABELS` entry. Per CLAUDE.md, reusing an
existing slot would silently tie two unrelated features to one provider with
no way for the user to separate them.

The route is `async`, so `ai.complete()` is awaited directly. No
`run_from_thread` -- that is required only for `HandlerMode.THREAD`
handlers, which this is not.

## UI

A "Provenance" section on the object detail panel, beside the existing
computation-provenance rows already served via `records_for_object`
(`api/v1/objects.py:62`).

- Structured report renders on open.
- Gap count shown as a plain badge.
- "Generate prose" button appears only when the slot has a provider.
- **Copy-to-clipboard on both halves** -- the entire point is pasting into a
  manuscript. The structured half copies as markdown.

## Testing

Because the walker is the only component touching the database, the other
two are tested as pure functions over hand-built `ProvenanceChain` values --
no Mongo, no model.

- **Renderer:** each of the six gap kinds renders its explicit marker; a
  branch renders as a visible fork; sidecars never appear.
- **Prompt:** a chain with a missing version produces a prompt containing
  "not recorded"; a chain with a share boundary produces a prompt that does
  not claim to know what preceded it.
- **Containment check:** a model output with an unsupported version token is
  rejected; one whose every token is supported passes.
- **Verb table:** the exhaustiveness test above.
- **Walker:** reconvergent DAG renders each node once; dangling parent
  yields `dangling_parent` rather than raising; failed runs appear.

**Beyond unit tests, per CLAUDE.md:** the rules must be checked against the
real database, not only hand-built fixtures. The Actions-tab suggestion
rules passed a full green suite while getting two things wrong that one look
at a real project exposed, precisely because the fixtures already looked the
way the rules expected. A `docker compose exec api python -c "..."` walk of
a real VCF's ancestry is required before this is considered done, and the
issue's own last acceptance criterion ("representative narrative output is
manually reviewed against its underlying facts") says the same thing.

## Explicitly out of scope

**Project-level methods generation** -- "generate methods for this entire
project" rather than for one object. It is a real want, but a different
feature: it must decide what a project's terminal outputs are, deduplicate
lineage shared across samples, and collapse "we did this to all 24 samples"
into one sentence. Deferred to its own spec, which will be able to build on
this walker.

**Backfilling provenance for historical objects.** Objects created before a
given `*_provenance` builder existed will render `params_unrecorded`
forever. That is the honest state, and inventing facts retroactively is the
one thing this feature must never do.

**The TODO entry's other candidates** -- explaining why a QC run failed a
threshold, and prose next-step suggestions alongside the Actions cards --
are separate features sharing only the heading.
