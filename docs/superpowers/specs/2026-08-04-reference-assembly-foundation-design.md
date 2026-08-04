# Reference-based assembly foundation

Written 2026-08-04 for GitHub issue #21. This design intentionally builds the
shared model for reference-based assembly workflows before adding Pilon, RagTag,
or iVar. The goal is to make those tools slot into the same vocabulary later
without confusing them with de novo assembly or post-assembly QC.

## Problem

BioFlow can assemble reads de novo and it can score assemblies after they exist,
but it does not yet have a clean way to represent workflows that use a reference,
a draft assembly, or an alignment to improve or produce an assembly.

That gap matters before any single tool lands. Pilon, RagTag, and iVar all sit
in the same broad family, but their inputs differ enough that a generic runner
would be premature:

- Pilon polishes a draft assembly from reads aligned back to that same draft.
- RagTag scaffolds a draft assembly against a trusted reference assembly.
- iVar trims primers and produces amplicon or viral consensus from an alignment
  to a reference.

The shared problem is not command construction yet. It is naming the workflow
family, recording user intent, checking provenance, and refusing category errors
before a long-running job starts.

## Scope

This is a foundation slice. It should add the shared vocabulary and validation
seams needed by future reference-based assembly tools, but it should not add a
placeholder launch endpoint, fake queue handler, or generic UI card.

In scope:

- A distinct pipeline family for reference-based assembly tooling.
- Run and input-role vocabulary that can describe Pilon, RagTag, and iVar.
- Reusable validation helpers for draft assemblies, reference assemblies, and
  BAM alignment targets.
- A clear provenance rule for workflows that consume BAM/CRAM.
- Documented suggestion behavior for future tool cards.

Out of scope:

- Installing or dispatching Pilon, RagTag, or iVar.
- Post-assembly QC, which remains under issue #13.
- Automatic prerequisite chaining from Actions cards.
- Replacing or mutating an input assembly in place.

## Architecture

Reference-based assembly should be a first-class pipeline family:
`reference_assembly`.

Add `PipelineType.REFERENCE_ASSEMBLY` for tool metadata, the Software help page,
and future picker grouping. It must stay separate from `PipelineType.ASSEMBLE`
because these tools do not assemble reads de novo, and separate from
`PipelineType.ASSEMBLY_QC` because they produce or improve assemblies rather
than merely scoring them.

Add `RunKind.REFERENCE_ASSEMBLY` for the activity view and provenance history.
`PipelineRun` is user intent, not an execution DAG, so this single kind is the
right shared label for "polished this assembly", "scaffolded this assembly", and
"produced this consensus". The individual tool belongs in `params` or `tool`,
the way existing runs separate broad intent from concrete implementation.

Add a run job role only when the first real handler needs it. A foundation-only
slice can document the likely role name, but adding unused queue vocabulary
would create a branch of code nothing dispatches to. The first tool can add
`RunJobRole.REFERENCE_ASSEMBLY` or a more specific role once its job shape is
real.

## Data Model

Add `RunInputRole` values that describe the objects a user selected, not the
internal command layout:

- `DRAFT_ASSEMBLY`: the assembly being polished or scaffolded.
- `REFERENCE`: the trusted reference assembly or viral/amplicon reference.
- `ALIGNMENT`: the BAM/CRAM consumed by a polishing or consensus workflow.
- `PRIMERS`: a future primer BED role for iVar, once primer file handling is
  designed explicitly.

Existing roles such as `READS` and `MATE` remain available for a future tool
that aligns reads internally, but the first foundation does not need new object
roles for raw reads.

Outputs are normal `DataObject` rows. They should not overwrite inputs:

- Pilon produces a polished FASTA with role `REFERENCE`, derived from the draft
  assembly and the alignment.
- RagTag produces a scaffolded FASTA with role `REFERENCE`, derived from the
  draft assembly and the trusted reference.
- iVar produces a consensus FASTA with role `REFERENCE`, derived from the
  alignment, reference, and primer file when one is supplied.

This keeps every generated assembly addressable, alignable, deletable, and
auditable through the same object model de novo assembly already uses.

## Provenance

Any workflow that consumes a BAM/CRAM must prove what sequence the alignment was
made against. It must not infer the answer from filenames.

The existing `reference_for_bam()` helper in `pipeline_service.py` already walks
`DataObject.derived_from` to find the reference used for variant calling. The
foundation should generalize that behavior into a reusable alignment-target
helper, for example `alignment_target_for_bam(bam)`.

Rules:

- If a BAM has one assembly/reference target in `derived_from`, return it.
- If no target can be found, validation fails with an actionable message.
- If multiple plausible targets exist, validation fails rather than guessing.
- A tool-specific validator can then require that target to equal the selected
  draft assembly or reference.

This is the key rule that makes Pilon honest: a BAM aligned to a published
reference is not valid input for polishing a separate de novo draft, even if the
organism and filenames look compatible.

## Validation

The foundation should add reusable validators in `pipeline_service.py` or a
small adjacent helper module.

`check_draft_assembly(obj)`:

- Requires a ready FASTA.
- Rejects protein and transcript FASTA roles.
- Accepts uploaded assemblies and pipeline-produced assemblies.
- Does not require an NCBI accession.

`check_reference_assembly(obj)`:

- Requires a ready FASTA.
- Requires the object to be usable as a reference assembly.
- Rejects protein and transcript FASTA roles.
- Enforces same project/profile at the caller boundary, matching existing
  alignment launch behavior.

`alignment_target_for_bam(bam)`:

- Requires a ready BAM/CRAM alignment object.
- Resolves its target from provenance.
- Raises if the target is absent or ambiguous.

`check_bam_aligned_to(bam, target)`:

- Calls `alignment_target_for_bam`.
- Requires the resolved target id to match the selected assembly/reference id.
- Raises a validation error that says which alignment target was found.

Future `check_primer_bed(obj)`:

- Should wait until iVar defines primer file ingestion and role/format handling.
- Should validate BED-like content rather than accepting any text file.

## Future Tool Fit

Pilon:

- Inputs: `DRAFT_ASSEMBLY` plus `ALIGNMENT`.
- Validation: BAM target must be the draft assembly.
- Output: polished FASTA derived from both inputs.
- Suggested from a draft assembly only when a matching alignment exists.

RagTag:

- Inputs: `DRAFT_ASSEMBLY` plus `REFERENCE`.
- Validation: both are ready assembly FASTA objects in the same project.
- Output: scaffolded FASTA derived from both inputs.
- Suggested from a draft assembly when a second reference assembly exists.

iVar:

- Inputs: `ALIGNMENT`, `REFERENCE`, and optionally `PRIMERS`.
- Validation: BAM target must be the selected viral/amplicon reference.
- Output: consensus FASTA derived from the alignment, reference, and primers.
- Suggested from the BAM or reference path once the UI can make that choice
  clearly.

## Suggestion Behavior

Do not add a generic Actions card in this slice. The current suggestion system
deliberately does not chain prerequisites, and this design should preserve that
rule.

Future cards should be tool-specific and actionable:

- Pilon unavailable reason: no BAM aligned back to this draft assembly.
- RagTag unavailable reason: no other reference assembly in this project.
- iVar unavailable reason: no compatible alignment, or primer support missing
  when primer trimming is requested.

Cards should not enqueue "align first" or "index first" behind the user's back.
That belongs to a future DAG system, not to this foundation.

## Error Handling

Most failure modes should happen before queueing:

- The selected draft assembly is not ready.
- The selected object is not FASTA.
- A protein or transcript FASTA was selected as an assembly.
- A BAM has no provenance target.
- A BAM target does not match the selected draft/reference.
- A selected reference belongs to another project/profile.
- Primer-file support is requested before primer inputs are modelled.

Tool-specific handlers will still have tool-specific errors later, but this
foundation should prevent the common category errors early.

## Testing

Foundation tests should cover the shared behavior, not future tools:

- Draft assembly validation accepts genome FASTA and rejects protein/transcript
  FASTA.
- Reference assembly validation accepts a ready reference FASTA and rejects bad
  shapes.
- `alignment_target_for_bam()` finds the target from `derived_from`.
- `alignment_target_for_bam()` rejects missing or ambiguous targets.
- `check_bam_aligned_to()` accepts a matching target and rejects a mismatched
  target.
- Enum/API mirror tests are updated wherever new `PipelineType`, `RunKind`, or
  `RunInputRole` values cross into TypeScript.

No browser verification is required for this foundation-only slice unless it
adds visible UI text. The first tool-specific card will require browser
verification against the running app.

## Implementation Note

This design deliberately stops short of a launcher. A generic
`/pipelines/reference-assembly` endpoint would force Pilon, RagTag, and iVar
through one request shape before their real differences are understood. The
shared foundation should make those workflows easy to add, not pretend they
already have one command surface.
