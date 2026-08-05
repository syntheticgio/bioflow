# RagTag reference-guided scaffolding

Written 2026-08-05 for GitHub issue #52. This is the third and last tool slice
on the reference-based assembly foundation shipped under #21
(`2026-08-04-reference-assembly-foundation-design.md`), after iVar (#47,
`2026-08-05-ivar-consensus-design.md`) and Polypolish (#23,
`2026-08-05-polypolish-design.md`). Shipping it closes epic #14.

Every upstream fact below was checked against a real `ragtag 2.1.0` install in
this image on 2026-08-05, not recalled. Several behaviours it found are not in
the README and one of them changes the handler's design outright.

## Problem

BioFlow can assemble reads into contigs (Flye) and now polish them
(Polypolish), but it cannot *order and orient* those contigs. A de novo
assembly of a few hundred contigs is a bag of sequence: it has the bases but
not the arrangement, so it cannot be browsed as chromosomes, compared to a
reference coordinate-wise, or used for anything positional.

RagTag scaffolds a draft against a related reference assembly, producing
chromosome-scale sequences without Hi-C data. It is the cheapest route from
contigs to something chromosome-shaped, and for a genome with a decent
reference already published it is often the only one anybody needs.

## Scope

In scope:

- Installing RagTag, which needs a decision because Debian has no package.
- `ragtag.py scaffold` only, as one user-visible pipeline.
- A draft assembly plus a reference assembly as inputs.
- A launch endpoint, a queue handler, a runner, and an Actions card.
- `TOOL_META` entry and the Software help page's required fields.

Out of scope:

- **`ragtag.py correct`** (homology-based misassembly correction). It *breaks*
  the user's contigs at suspected misassemblies, which is a destructive edit
  justified by the same cross-species inference that makes scaffolding risky
  (below). It deserves its own slice and its own argument, not a checkbox on
  this one.
- **`ragtag.py patch`** and **`merge`**. Patch fills gaps with reference
  sequence, which puts *another organism's bases* into the user's assembly --
  a much stronger claim than ordering their own. Merge combines scaffoldings
  from multiple sources, which presupposes more than one.
- **`updategff`.** Lifting annotations onto scaffolds is real work but belongs
  with the annotation features, not here.
- Hi-C scaffolding (YaHS, HapHiC). A different input shape entirely, and the
  genome-analysis review treats it as the rigorous route where this is the
  cheap one.

## The third provenance shape

Epic #14 requires that provenance for alignments against pipeline-produced
assembly output be explicit. The three slices answer that differently, and
#52 was written with this as an open question specifically so it would not be
assumed to inherit one of the other two:

| Slice | Alignment comes from | How provenance is discharged |
|---|---|---|
| iVar | user-supplied BAM | **validated** -- `check_bam_aligned_to` refuses a BAM aligned elsewhere |
| Polypolish | aligned internally with bwa-mem2 | **by construction** -- recorded as facts on the output |
| RagTag | aligned internally with minimap2 | **by construction**, same as Polypolish |

So RagTag follows Polypolish, not iVar: it invokes minimap2 itself (verified --
see the run log below), the query is the draft and the target is the reference,
and neither can be anything else. Record `scaffold_aligner`,
`scaffold_aligner_version` and `scaffold_reference_*` on the output for the
same reason Polypolish records its aligner: nothing else in the object graph
witnesses that step, and an unrecorded guarantee is not an explicit one.

**But there is a second provenance obligation here that neither sibling has**,
and it is the more important one. A scaffolded assembly is *partly a claim
about the reference*, not only about the sample -- see "Scaffolds are
inference" below. Which reference was used is not a footnote, it is a
precondition for interpreting the output at all, and it must be on the object.

## Inputs and validation

| Role | Object | Required |
|---|---|---|
| `DRAFT_ASSEMBLY` | the contigs to order, FASTA | yes |
| `REFERENCE` | a related reference assembly, FASTA | yes |

Both inputs are assembly FASTA, which is what makes this slice's validation
harder than its siblings'. The foundation already distinguishes them:

- `check_draft_assembly(draft)` -- ready FASTA, not protein/transcript, role
  unconstrained (an uploaded assembly may have none).
- `check_reference_assembly(reference)` -- the same, plus
  `ObjectRole.REFERENCE`, so a generic uploaded FASTA is not silently treated
  as authoritative.

**The swap problem.** Nothing above stops a user passing the reference as the
draft and vice versa, and RagTag will not complain: it will scaffold the
reference against the draft and produce a successful, meaningless result.
`check_reference_assembly`'s role requirement catches the common direction
(the reference must be marked `REFERENCE`, and a fresh Flye assembly is not),
which is most of the value. Two further guards are worth having and one is
not:

- **Refuse when draft and reference are the same object.** Cheap, and it is
  the degenerate case of the swap.
- **Warn -- not refuse -- when the draft is more contiguous than the
  reference.** A draft with 12 sequences and a reference with 400 is very
  likely reversed. Both objects carry `sequence_lengths` in facts (confirmed
  on the real yeast reference), so contig count and N50 are free. This is a
  heuristic about intent, not a correctness rule, so it belongs in the launch
  dialog's copy rather than in a `ValidationError`.
- **Do not require the two to be the same organism.** Tempting, and wrong for
  the same reason the `protein.faa` rule was wrong: cross-species scaffolding
  against a related genome is a legitimate and common workflow, which is
  precisely what RagTag is *for*.

## Scaffolds are inference, not observation

This is the part the card copy and the facts have to get right, and it is the
reason this slice needs more than a runner.

RagTag names its output scaffolds after the **reference's** sequences. Verified
on a real run: a draft of 7 shuffled, reverse-complemented contigs scaffolded
against a 2-sequence reference produced exactly `>chr1_RagTag` and
`>chr2_RagTag`. The output therefore *inherits the reference's karyotype* --
its chromosome count, its names, and implicitly its structure. If the sample
genuinely differs from the reference by a translocation, a fusion, or a
different chromosome number, the scaffolded assembly will not show that. It
will show the reference's arrangement with the sample's bases in it.

That is not a bug in RagTag; it is what reference-guided scaffolding *is*, and
this repo's own genome-analysis review already says the equivalent about the
scaffolding loop -- "every join added here is inference, not observation."

Consequences for this slice:

- **The card and dialog must say so**, in one plain sentence, not in a help
  page. Something close to: "Scaffolds are ordered to match the reference. Real
  structural differences between your sample and the reference will not appear."
- **The reference goes in the output's name and facts**, not just its
  `derived_from`. A `scaffolds.fasta` sitting in the explorer with no visible
  indication of what it was ordered against is the failure mode.
- **The confidence numbers are the honest counterweight** and must be surfaced,
  not just stored -- see facts below.

## Installing RagTag

**Not in Debian.** Checked against the running image with `apt-get update`
first, the same stale-cache trap that produced a wrong claim for iVar: no
candidate for `ragtag`, `ragtag-python`, or `python3-ragtag`.

**Use pip.** RagTag is pure Python and PyPI carries 2.1.0. Verified in this
image:

```
pip install ragtag   ->   ragtag.py --version  ->  v2.1.0
Requires: intervaltree, networkx, numpy, pysam
```

Of those four, `networkx` (3.6.1), `numpy` (2.5.1) and `pysam` (0.24.0) are
already in the image; only `intervaltree` is new. This is the lightest install
of the three slices -- lighter than iVar's apt package -- and unlike Polypolish
it has **no arm64 problem**: pure Python, and its only external binary
dependency is minimap2, which this image already ships for both architectures.

Add it to the pip install block in `backend/Dockerfile` alongside the other
Python tools, pinned to `ragtag==2.1.0`. No install script is needed.

Add `tools.ragtag()` with `_probe("ragtag", settings.ragtag_path,
["--version"])`. Note the binary is **`ragtag.py`**, not `ragtag` -- a probe
looking for `ragtag` on PATH finds nothing and reports a working install as
missing, the same shape as iVar's `version`-vs-`--version` trap.

One currency note to record rather than hide: RagTag's last upstream commit is
2024-02-14 and 2.1.0 is its latest release. It is not archived and it is widely
used, but it is not actively developed, and the Software help page's `usage`
should not imply otherwise.

## Architecture

Follows the two shipped slices:

- **`backend/app/pipelines/ragtag_runner.py`** -- command construction plus
  parsers for RagTag's two machine-readable outputs (`.stats` and
  `.confidence.txt`, both clean TSV).
- **`backend/app/queue/reference_assembly_handlers.py`** -- a
  `scaffold_assembly` handler alongside `consensus_from_alignment` and
  `polish_assembly`. `HandlerMode.SUBPROCESS`, `JobClass.COMPUTE`.
- **`launch_scaffold`** in `pipeline_service.py`, and a `/pipelines/scaffold`
  endpoint.
- `RunJobRole.SCAFFOLD` alongside `CONSENSUS` and `POLISH`.

**No registry**, for the third time and the same reason: one tool, no second
candidate close enough to name. (YaHS and HapHiC are not alternatives to this
-- they need Hi-C data RagTag does not.)

### The run: one invocation, and RagTag calls minimap2 itself

Unlike Polypolish's five stages, this is a single subprocess:

```
ragtag.py scaffold <reference.fa> <query.fa> -o <outdir> -t <threads> -u
```

Note the argument order -- **reference first, draft second** -- which is the
opposite of how the inputs read in the UI and an easy transposition to make.

RagTag runs minimap2 internally rather than taking an alignment. Verified from
its own log:

```
INFO: Running: minimap2 -x asm5 -t 4 ref.fasta draft.fasta > ragtag.scaffold.asm.paf
```

`-u` is not optional in practice. Without it RagTag itself warns that
"some component/object AGP pairs might share the same ID", which produces an
AGP that downstream tools reject.

**`-x asm5` is the default preset and it is a real parameter, not a detail.**
asm5 assumes roughly 5% divergence -- appropriate for the same or a very close
species. Scaffolding against a more distant relative needs `asm10` or `asm20`,
and with the wrong preset minimap2 simply finds fewer alignments, so RagTag
places fewer contigs and reports a worse result that looks like a poor assembly
rather than a wrong setting. Expose divergence as a three-way choice in the
dialog (same species / same genus / more distant → asm5 / asm10 / asm20),
default asm5, and record which was used as a fact. Do **not** try to infer it
from organism metadata: the two objects' organism fields are frequently absent
and a wrong inference here is invisible.

### The trap: RagTag exits 0 when it fails

**Verified twice on 2026-08-05.** Given an unrelated reference, RagTag raises

```
RuntimeError: There are no useful alignments. Check output alignment files.
```

writes no `ragtag.scaffold.fasta`, and **exits with status 0**.

This is the single most important thing in this document for the handler. A
handler that trusts the exit code will mark the job succeeded, find no output
to ingest, and either crash in the applier or -- worse -- record a successful
run that produced nothing. iVar has a milder version of the same problem and
its handler already checks the output file explicitly; here that check is not
belt-and-braces, it is the *only* signal.

So the handler must:

1. Check `ragtag.scaffold.fasta` exists and is non-empty, and fail the job
   explicitly when it is not, regardless of return code.
2. Surface RagTag's stderr in that failure message. "There are no useful
   alignments" is a genuinely useful diagnosis -- it means the reference is too
   distant or is the wrong organism -- and it is the sentence that tells the
   user what to change.
3. Treat this as `PermanentError`, not retryable. The same two assemblies will
   fail identically.

A `max_attempts=1` handler is right for the usual determinism reason, and here
it also avoids retrying something whose failure is a statement about the
inputs.

## Outputs

RagTag writes a directory; verified contents from a real run:

```
ragtag.scaffold.fasta            the scaffolded assembly  <- the deliverable
ragtag.scaffold.agp              the arrangement: which contig, where, which orientation
ragtag.scaffold.stats            TSV summary
ragtag.scaffold.confidence.txt   per-contig confidence, TSV
ragtag.scaffold.asm.paf          the minimap2 alignment
ragtag.scaffold.asm.paf.log, ragtag.scaffold.err
```

**Ingest the FASTA and the AGP; leave the rest in the workdir.** The FASTA is
the deliverable. The AGP is the exception to the "intermediates stay in the
workdir" rule the other two slices follow, for a specific reason: it is the
*only* record of which contig went where and in what orientation, it is small,
and it is the standard interchange format for exactly this. A user who wants to
check or undo a join needs it, and regenerating it means re-running the job.
The PAF is large and reproducible; the logs are logs.

Store the FASTA as `FormatKind.FASTA`, `ObjectRole.REFERENCE`, `derived_from`
both the draft and the reference -- consistent with the consensus and polish
appliers, and with the foundation's rule that every generated assembly is
addressable and alignable.

Facts, from the two TSVs (real values from the verification run shown):

- `scaffold_placed_sequences` / `scaffold_placed_bp` -- `7` / `100000`
- `scaffold_unplaced_sequences` / `scaffold_unplaced_bp` -- `0` / `0`
- `scaffold_gap_bp` / `scaffold_gap_sequences` -- `500` / `5`
- `scaffold_count` -- how many scaffolds resulted (2 in that run)
- `scaffold_reference_name` and `scaffold_reference_object_id`
- `scaffold_aligner`, `scaffold_aligner_version`, `scaffold_divergence_preset`
- `scaffold_min_grouping_confidence` -- the *minimum* across contigs from
  `confidence.txt`, not the mean. A mean of 0.98 hides the one contig placed at
  0.3, and that contig is the one worth looking at.

**`scaffold_unplaced_sequences` is the number to show first.** Verified
behaviour: scaffolding the same draft against a reference containing only one
of its two chromosomes placed 4 contigs and left 3 unplaced, and the output
FASTA contained the scaffold *plus the three unplaced contigs* -- so the
assembly stays complete and the unplaced count is the only thing that says how
much of it is actually ordered. A run that placed 4 of 7 contigs is a very
different result from one that placed 7, and both look like "scaffolding
succeeded".

## Suggestion behavior

One card, `kind="scaffold"`, category `REFERENCE_ASSEMBLY`, anchored on the
**draft assembly** -- the object being improved, consistent with the polish
card.

The rules, in order:

- Not assembly-like FASTA (`_is_assembly_like`), or not ready → `None`.
- RagTag not installed → `UNAVAILABLE` with the probe's error.
- No object with `ObjectRole.REFERENCE` in the project → `UNAVAILABLE`,
  "Scaffolding needs a reference assembly, and this project has none."
- Exactly one reference → `AVAILABLE`, body carries both ids.
- More than one reference → **`AVAILABLE`, picking none**, is *not* an option;
  cards launch straight into the queue with no dialog. Follow the polish card:
  `UNAVAILABLE`, naming the ambiguity.

That last rule will fire more often here than it does for polishing. The real
yeast project holds two reference-role FASTAs (`GCA_...genomic.fna` and
`GCF_...genomic.fna` -- the same assembly from two archives), so the common
case is genuinely ambiguous and the card will refuse. **That is the correct
behaviour and also an argument for the dialog**: unlike polishing, scaffolding
needs a real reference *chooser* in the UI, because "this project has 2
references" is the normal state rather than the exception. Build the dialog in
this slice rather than deferring it.

Two familiar traps:

- **Do not gate on the draft looking unscaffolded.** "Only offer scaffolding
  for assemblies with many contigs" is the `protein.faa` mistake again --
  rescaffolding an already-scaffolded assembly against a better reference is
  legitimate.
- **Test the unavailable direction.** The image will ship RagTag installed, so
  asserting availability passes whether or not the patch took. Assert the card
  flips when the probe is patched off, and remember the probe is `lru_cache`d.

## Resources

`JobResources(cpu=4, mem_mb=8192, io=IoClass.LIGHT)`. (Implementation note,
2026-08-05: `IoClass` has no `NORMAL` member -- the enum is
`NONE`/`LIGHT`/`HEAVY`. `LIGHT` is the correct match for the reasoning below.)

The cost is minimap2's whole-genome alignment of draft against reference, plus
RagTag's own graph work in `networkx`. Neither is heavy for bacterial or fungal
genomes; both scale with genome size, and a multi-Gb plant reference is a
different proposition -- minimap2's index dominates there, the same way
bwa-mem2's does for polishing. Size for the reference, not the draft.

I/O is `NORMAL`, not `HEAVY`: unlike the other two slices there is no
high-coverage read file being streamed, just two assemblies.

Lease: minutes for a bacterial genome, up to an hour for a large one. Size it
like the alignment jobs rather than like consensus.

`max_attempts=1` -- deterministic, and see the exit-code section.

## Tool metadata

`TOOL_META["ragtag"]`, `pipelines=(PipelineType.REFERENCE_ASSEMBLY,)`.
`test_every_tool_is_documented` requires `homepage`, `citation`, `license`,
`usage`; the entry fails the suite until they are filled.

Verified 2026-08-05 against the project's own repository, `LICENSE`, and README:

- `homepage` / `repository`: `https://github.com/malonge/RagTag`
- `license`: MIT (from `LICENSE`: "MIT License / Copyright (c) 2021 Michael
  Alonge")
- `citation`: Alonge M, Lebeigle L, Kirsche M, et al. "Automated assembly
  scaffolding using RagTag elevates a new tomato system for high-throughput
  genome editing." *Genome Biology*. 2022;23:258.
- `citation_url`: `https://doi.org/10.1186/s13059-022-02823-7`

RagTag's README also cites the earlier RaGOO paper (Alonge et al., *Genome
Biology* 2019, doi:10.1186/s13059-019-1829-6). As with Polypolish's two
citations, put the current paper in the field and mention the predecessor in
`usage` rather than concatenating both into a field the help page renders as
one.

`usage` describes behaviour: that BioFlow runs `ragtag.py scaffold` to order
and orient a draft assembly's contigs against a related reference, that RagTag
aligns the two internally with minimap2 at a divergence preset the user
chooses, that scaffolds are named after the reference's sequences and therefore
inherit its arrangement, and that unplaced contigs are carried through into the
output rather than dropped.

## Testing

Unit tests, `backend/tests/`:

- `build_scaffold_command` puts the reference before the draft, and includes
  `-u`. The transposition is silent and produces a plausible wrong answer, so
  it is asserted on the argv.
- The divergence choice maps to the right `--mm2-params` preset in all three
  cases.
- The `.stats` parser returns the six fields from a real fixture, and `{}` from
  garbage rather than raising.
- The `.confidence.txt` parser returns the **minimum** grouping confidence, not
  the mean -- with a fixture where those differ, since a test whose fixture has
  one contig cannot tell them apart.
- The launch path refuses draft and reference being the same object.
- The launch path refuses a draft that is `protein.faa` or a transcript FASTA.
- The launch path refuses a reference not marked `ObjectRole.REFERENCE`.
- The scaffold card is `UNAVAILABLE` with `tools.ragtag` patched off, and
  `UNAVAILABLE` with two references in the project.
- `None` for a FASTQ and for a BAM.

**The handler test that matters most:** a run that exits 0 but writes no
`ragtag.scaffold.fasta` must fail the job. Simulate it by pointing the handler
at an empty output directory with a zero return code. This is the one behaviour
that no amount of tool-level correctness protects against, and it is verified
real rather than hypothetical.

Against the real database, before believing any of it:

```bash
docker compose exec api python -c "..."
```

Build the scaffold card from real objects. Two specific things to check, both
of which fixtures would get wrong: that `protein.faa` and
`cds_from_genomic.fna` produce no card, and that the yeast project -- which
holds *two* reference-role FASTAs -- produces the ambiguity refusal rather than
silently picking one.

End-to-end, at localhost:5173 (or 5273 from a worktree). The verification data
used to write this spec is reusable: a synthetic reference of two chromosomes
(60kb + 40kb) and a draft of the same sequence cut into 7 contigs, shuffled and
partly reverse-complemented, scaffolds back to exactly `chr1_RagTag` and
`chr2_RagTag` with all 7 placed and 5 gaps. That is a real correctness check,
not just a smoke test, and it runs in under a second.

Note the same caveat the polish slice hit: as of 2026-08-05 every object in the
database reads `status=missing`, so an in-app run needs a project with intact
blobs first. A draft assembly is also needed, which means running Flye first --
or uploading contigs.

Restart the worker before testing (`docker compose restart worker`): the
handler is new and `worker` does not hot-reload.

## Risks

**The exit-code trap is the big one**, and it is retired by design rather than
mitigated: the handler checks the output file as its primary success signal.
The risk that remains is someone later "simplifying" that check on the grounds
that the return code already covers it. The comment at that check should say
what it costs.

**A plausible-but-wrong scaffolding is undetectable from the output.** A
reference from a related species produces scaffolds that look exactly like good
ones. Nothing in this slice can catch that; the mitigations are the confidence
facts, the unplaced count, and honest card copy, which is why those are
requirements rather than polish.

**Two references is the normal case, not the edge case.** If the dialog slips
out of scope, the card will refuse on most real projects and the feature will
appear broken. The dialog is load-bearing here in a way it was not for
polishing.

**Upstream is quiet.** Last commit 2024-02-14. Not a blocker -- it is MIT, pure
Python, and small -- but if it breaks against a future numpy or pysam, this
repo owns the fix. Pinning `ragtag==2.1.0` is what keeps that from arriving
unannounced.
