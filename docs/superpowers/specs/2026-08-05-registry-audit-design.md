# Auditing the hand-maintained registries

Written 2026-08-05 for GitHub issue
[#11](https://github.com/syntheticgio/bioflow/issues/11). Backlog source:
`Audit the hand-maintained registries a new tool must reach` in
`docs/TODO.md`, raised 2026-08-01.

## Problem

The failure the backlog entry names, precisely: **a module-level dict keyed by
something an enum already enumerates, where a missing key is skipped rather
than raised.** Adding STAR hit it in `results._SIDECAR_ROLES` and cost a
`build_index` job that reported success while storing none of its eight index
files. The suite was green throughout, because every fixture fed the appliers
roles that were already in the allowlist.

`_SIDECAR_ROLES` is now derived and is the template this spec measures
everything else against:

```python
# backend/app/queue/results.py:1982
_SIDECAR_ROLES = {role.value: role for role in SidecarRole}
```

The issue lists seven more registries. Walking them found that they are not one
problem but **four distinct ones**, and that only one of the seven is both
derivable and currently wrong. Treating them uniformly -- deriving everything,
or bolting an exhaustiveness test onto everything -- would be worse than the
status quo in at least two cases, because some of these registries hold
information the enum genuinely does not have, and one of them is keyed by a
vocabulary this repository does not own.

## Inventory

| Registry | Keyed by | Derivable? | State today |
|---|---|---|---|
| `ROLE_FIELDS` | `ObjectRole` | No (holds field groups) | **Already guarded** -- the template |
| `FORMAT_FIELDS` | `FormatKind` | No (holds field groups) | **Silently partial**, no test |
| `EXTENSION_MAP` | extension string | No (many-to-one) | Complete by luck, no test |
| `_QC_STATS_PLATFORM` | SRA platform tag | No (no enum exists) | **Fall-through bug + 3 duplicated sets** |
| `_TOKEN_SEQUENCE_TYPES` | filename token | No (open vocabulary) | Correctly partial, rule undocumented |
| `_EXTENSION_SEQUENCE_TYPES` | filename token | No (open vocabulary) | Correctly partial, rule undocumented |
| `assembly_components.COMPONENTS` | NCBI `--include` name | No (NCBI's vocabulary) | Three untested internal invariants |

The four problems those columns collapse into:

1. **A registry keyed by an enum with no exhaustiveness guard** --
   `FORMAT_FIELDS`. The one clean instance of the STAR shape left.
2. **A vocabulary that should be an enum and is not** -- the platform tables,
   and the four sequence-type strings.
3. **A correctly partial registry with no written inclusion rule** -- the two
   sequence-type token tables.
4. **Internal invariants nothing checks** -- `COMPONENTS` and its parallel
   `COMPONENT_ORDER` tuple.

## 1. `ROLE_FIELDS` is already correct, and is the pattern

`backend/app/metadata/schemas.py:344`. Every `ObjectRole` either has a field
group here or is listed in `FORMAT_DERIVED_ROLES`, an explicit frozenset whose
own comment says why it exists:

> Listed explicitly rather than left implicit so that a role added without
> thought still fails the "every role is accounted for" test.

And the test, at `backend/tests/storage/test_metadata_schemas.py:250`:

```python
assert set(ObjectRole) == set(schemas.ROLE_FIELDS) | schemas.FORMAT_DERIVED_ROLES
```

plus a second test that the two sets do not overlap, since `fields_for` prefers
`ROLE_FIELDS` and membership in both would make its precedence ambiguous.

**No work here.** It is in the issue's scope list, the audit is the deliverable,
and the finding is that it was done right. The value of writing that down is
that the next reader does not re-derive it.

## 2. `FORMAT_FIELDS` -- the remaining instance of the STAR shape

`backend/app/metadata/schemas.py:326`, ten lines above `ROLE_FIELDS` and
lacking its guard. Keyed by `FormatKind`, and **four members have no key at
all**: `GFA`, `FAI`, `TEXT`, `UNKNOWN`. A missing key means `fields_for` falls
through to `COMMON_FIELDS` with no warning and no log line.

There is also a third state. `FormatKind.FASTA` maps to `()` -- present, empty,
with a comment explaining that a FASTA is no longer assumed to be a reference.
So "deliberately has no format-specific fields" is today expressed two
different ways, one of which is indistinguishable from an oversight.

The consequence is milder than `_SIDECAR_ROLES`' was: falling back to
`COMMON_FIELDS` shows the user a narrower form rather than dropping files on
the floor. But it is the same silence, and `all_known_fields` has the same hole
-- a field group added for a format not in this dict would not be reachable for
coercion.

**Fix.** Mirror `ROLE_FIELDS` exactly:

```python
# Formats whose questions are entirely the common ones. Listed explicitly for
# the reason FORMAT_DERIVED_ROLES is: a format added without thought should
# fail a test rather than quietly show a narrower form.
FORMAT_COMMON_ONLY: frozenset[FormatKind] = frozenset({...})
```

`FASTA`'s `()` value folds into it, carrying its comment. `GFA`, `FAI`, `TEXT`
and `UNKNOWN` join it -- each with a sentence saying why, which is the part
that makes the set a record rather than a rubber stamp. Then:

```python
assert set(FormatKind) == set(schemas.FORMAT_FIELDS) | schemas.FORMAT_COMMON_ONLY
assert not set(schemas.FORMAT_FIELDS) & schemas.FORMAT_COMMON_ONLY
```

**Open question for the `UNKNOWN` member.** `FormatKind.UNKNOWN` is not a
format so much as the absence of an answer, and putting it in a set named
"common only" reads oddly. The alternative is a third set, or excluding it from
the assertion. Recommendation: include it in `FORMAT_COMMON_ONLY` with a
comment, because an exception carved out of the assertion is a hole in exactly
the place the assertion exists to cover.

## 3. `EXTENSION_MAP` -- not derivable, but the coverage test is free

`backend/app/storage/detect.py:29`. Many-to-one by nature: `fq` and `fastq`
both mean `FormatKind.FASTQ`, `fa`/`fna`/`faa`/`fas` all mean FASTA. The keys
are an open vocabulary of conventions and no enum can generate them.

The **values**, though, should cover `FormatKind` minus `UNKNOWN` -- a format
kind no filename can ever suggest is a format kind that only magic-byte
detection can ever produce, which is a real claim worth being deliberate about.
Read out of the file: every member except `UNKNOWN` is currently present.

So this test locks in a property that already holds:

```python
assert set(EXTENSION_MAP.values()) == set(FormatKind) - {FormatKind.UNKNOWN}
```

That is the cheapest acceptance-criterion satisfied in the whole issue: no
production change, and a new `FormatKind` can no longer be added without
someone deciding what it looks like on disk.

**Open question, and the reason this is not simply mechanical.** `TEXT` is
reachable today via `txt`/`tsv`. If a future kind is genuinely
extension-invisible, the equality above becomes wrong and wants the same
explicit-exception set `FORMAT_FIELDS` gets. Recommendation: use the strict
equality now, and let the first genuine exception introduce the set -- an
escape hatch built before it has a member tends to get used as a dumping
ground.

## 4. The platform vocabularies -- the one with a live bug

This is where the issue understates the problem. `_QC_STATS_PLATFORM`
(`backend/app/queue/pipeline_handlers.py:522`) translates SRA PLATFORM tags to
the short vocabulary `qc_stats.infer_chemistry` speaks:

```python
_QC_STATS_PLATFORM: dict[str, str] = {
    "OXFORD_NANOPORE": "ONT",
    "PACBIO_SMRT": "PACBIO",
}
```

consumed at line 587 as `_QC_STATS_PLATFORM.get(platform, platform)`.

**The fall-through is the bug.** An unmapped platform passes its *raw* value
into `infer_chemistry`, which compares against the literals `"PACBIO"` and
`"ONT"` and matches neither, returning `ReadChemistry.UNKNOWN` with a reason
string that reads as a considered judgement about the reads rather than a
missing dictionary entry. The dispatch that got there
(`pipeline_handlers.py:429`) had already decided this file was long-read, so
the file reaching this line is one we know needs chemistry inference.

Five uncoordinated vocabularies describe "platform" in this codebase:

| Vocabulary | Where | Examples |
|---|---|---|
| SRA PLATFORM tags | `LONG_READ_PLATFORMS`, `SHORT_READ_PLATFORMS` | `OXFORD_NANOPORE` |
| SAM `PL` values | `_SAM_PLATFORM_PATTERNS` (`pipeline_service.py:604`) | `ONT`, `PACBIO`, `BGI` |
| `qc_stats` short names | string literals in `qc_stats.py` | `ONT`, `PACBIO` |
| UI dropdown labels | `platform` FieldDef (`schemas.py:184`) | `Oxford Nanopore` |
| instrument models | user-entered, matched by substring | `PromethION` |

And the long-read SRA set is written out **three times, under three names, in
three files**, all identical:

- `pipeline_handlers.py:30` -- `LONG_READ_PLATFORMS`
- `reference_assembly.py:247` -- `LONG_READ_PLATFORMS`
- `pipeline_service.py:382` -- `_LONG_READ_QC_PLATFORMS`

`reference_assembly.is_short_read` even has a comment about exactly this
hazard, for a different table:

> `_qc_platform` is the one place that knows how to turn "PromethION" or
> "Illumina NovaSeq X Plus" into a platform name, and reimplementing that
> table here is how the two copies drift.

The advice is right and the frozenset three lines below it is a third copy.

Worse, `_SAM_TO_SRA_PLATFORM` (`pipeline_service.py:360`) is the **exact
inverse** of `_QC_STATS_PLATFORM`, maintained independently in a different
file:

```python
_SAM_TO_SRA_PLATFORM = {"ONT": "OXFORD_NANOPORE", "PACBIO": "PACBIO_SMRT"}
_QC_STATS_PLATFORM   = {"OXFORD_NANOPORE": "ONT", "PACBIO_SMRT": "PACBIO"}
```

Adding a long-read platform means editing five places, and getting four of them
right produces no error anywhere.

`SHORT_READ_PLATFORMS` is separately a partial registry with no documented
inclusion rule: SRA's PLATFORM vocabulary has more members than the seven it
names, and a file whose `sra_platform` is one of the omitted ones falls to the
chemistry tie-break rather than being answered directly. That may well be
correct -- but nothing says so, which is precisely the condition this issue
exists to end.

**Fix, minimum version:** one `LONG_READ_PLATFORMS` in one module, imported by
the other two; derive `_QC_STATS_PLATFORM` and `_SAM_TO_SRA_PLATFORM` from a
single bidirectional table; and

```python
assert set(LONG_READ_PLATFORMS) == set(_QC_STATS_PLATFORM)
```

so a platform that takes the long-read path always has a chemistry translation.
Plus a written inclusion rule on `SHORT_READ_PLATFORMS`.

**Fix, full version:** a `SequencingPlatform` StrEnum over the SRA tags, with
the SAM PL values, the QC short names, and the UI dropdown options all derived
from or checked against it.

**This is the one place where the recommendation is to stop short.** The full
version reaches the SAM read-group path (which writes BAM headers), the align
preset chooser, the polish eligibility check, and a user-facing dropdown whose
option strings are stored in existing objects' metadata. That is a larger blast
radius than the other six registries combined, and it is a data-migration
question rather than a registry-hygiene one. Recommendation: ship the minimum
version under this issue, and raise the enum separately with the migration
scoped properly. The minimum version alone kills the fall-through bug and the
triple duplication, which is what the STAR incident was about.

## 5. The sequence-type token tables -- partial on purpose

`backend/app/metadata/enrich.py:263` and `:292`. Keyed by filename token
(`genomic`, `cds`, `protein`, `mrna`, ...) and by extension convention
(`faa` → Protein, `ffn` → CDS, `frn` → RNA).

**Deriving these would be actively wrong.** The keys are an open vocabulary of
naming conventions, and `detect_sequence_type`'s contract is that it returns
`None` rather than guessing:

> Returns None rather than a guess whenever the name does not say. An absent
> tag is a question the user can answer at leisure; a wrong one is a claim they
> have to notice before they can correct it.

A passthrough or a permissive default would convert "we do not know" into a
wrong claim on the object -- the opposite of the STAR failure in consequence
but the same in kind. The existing comment already records one such near-miss:
substring matching would read `alternative_contigs.fna` as RNA.

What is checkable is the **value** side. `backend/tests/metadata/
test_sequence_type.py:190` asserts values ⊆ the dropdown's options, so a
detector cannot produce something the UI cannot display. Two gaps:

- The reverse is untested: every option should be **reachable** by some token.
  An option nobody can detect is a dropdown entry that only ever gets set by
  hand, which may be intended but should be stated.
- `("Genomic", "CDS", "Protein", "RNA")` is a bare tuple inside a `FieldDef` in
  `schemas.py`, repeated as string literals in `enrich.py` and again in
  `ncbi_assembly_components.py`'s `ComponentSpec.sequence_type`. Three files
  agree by hand.

**Fix.** Promote the four to a `SequenceType` StrEnum in `app/models/`; have
the `FieldDef`'s `options` derive from it; type `ComponentSpec.sequence_type`
as `SequenceType | None`. Both directions then become one-line assertions.

Unlike the platform enum, this one is safe: four values, already stable, and
the stored metadata strings are exactly the enum values, so no migration.

**And write the inclusion rule down**, since that is an explicit acceptance
criterion. Proposed wording for `_TOKEN_SEQUENCE_TYPES`: *a token belongs here
if and only if, appearing as the last meaningful `[._-]`-separated token of a
filename, it names the sequence type unambiguously. Ambiguity resolves to
omission, not to a best guess.*

## 6. `COMPONENTS` -- NCBI's vocabulary, our invariants

`backend/app/metadata/ncbi_assembly_components.py:56`. Keyed by NCBI's
`--include` names (`genome`, `gff3`, `protein`, `cds`). **Not derivable**: the
keys belong to the `datasets` CLI, and a member appearing here that NCBI does
not accept is a job that fails at the command line, not a silent skip. The
module's own docstring is already the right defence.

Three internal invariants nothing checks:

**(a) `role` is an unvalidated string.** Line 40: `role: str  # the ObjectRole
value its file lands as`. A comment where a type belongs -- a typo here
produces a role no `ObjectRole` matches, and nothing on the write path
notices.

What a wrong role costs is not speculative.
`backend/scripts/fix_legacy_component_roles.py` exists to repair rows ingested
as `reference` before this table set the role (its docstring is careful that
the table itself was right and the rows predate it), and it records the
consequence: `protein.faa` and `cds_from_genomic.fna` are FASTA, so a row
roled `reference` reaches the aligner's reference picker as though it were a
genome, and nearly broke genome-size inference -- a protein FASTA's
`total_bases` is 2.9 Mb against a 12.1 Mb yeast genome. The repair was needed
for a role that was *absent*; a role that is present and misspelled lands in
the same place. Typing it `ObjectRole` ends that at import time.

**(b) `COMPONENT_ORDER` is a hand-written parallel tuple with zero tests.**
Line 95, driving three loops in this module plus the ordering in
`ncbi_assembly_service.py:47`. A component added to `COMPONENTS` and not to
`COMPONENT_ORDER` is invisible in the download dialog: `parse_preview` and
`from_report` both iterate `COMPONENT_ORDER`, so it is never offered, and
nothing raises. **This is the closest structural match to the STAR failure in
the whole issue** -- an allowlist consulted by iteration, where absence means
silent omission -- and it is not in the issue's list.

```python
assert set(COMPONENT_ORDER) == set(COMPONENTS)
```

**(c) `file_type` and `preview_key` must be unique.**
`ncbi_assembly_handlers.py:292` builds `{spec.file_type: spec for spec in
COMPONENTS.values()}` -- a dict comprehension in which a duplicate `file_type`
silently drops a component from labelling. `preview_key` has the same exposure
in `parse_preview`. Both are one-line set-length assertions.

## Acceptance criteria mapping

| Criterion | Satisfied by |
|---|---|
| All named registries inventoried with consumers and completeness expectations | The inventory table and sections 1--6 |
| Safe mappings derived from authoritative enums | `SequenceType` (§5); `_QC_STATS_PLATFORM`/`_SAM_TO_SRA_PLATFORM` from one table (§4) |
| Non-derivable registries have exhaustive tests against the enums | `FORMAT_FIELDS` (§2), `EXTENSION_MAP` (§3), `COMPONENT_ORDER` (§6) |
| Adding an enum member cannot be silently skipped | §2, §3, §4, §6 assertions |
| Every intentionally partial registry documented with its inclusion rule | `FORMAT_COMMON_ONLY` (§2), `SHORT_READ_PLATFORMS` (§4), token tables (§5) |

## Work plan

Ordered so each commit is independently green and separately revertable.

1. **Tests only, no production change.** `EXTENSION_MAP` value coverage;
   `COMPONENT_ORDER` ↔ `COMPONENTS`; `file_type`/`preview_key` uniqueness.
   These pass on today's code and are pure ratchet.
2. **`FORMAT_FIELDS` + `FORMAT_COMMON_ONLY`**, mirroring `ROLE_FIELDS`, with
   its two tests.
3. **`SequenceType` StrEnum**; `FieldDef.options` derived from it;
   `ComponentSpec.sequence_type` and `ComponentSpec.role` typed as enums;
   reachability test.
4. **Platform de-duplication**: one `LONG_READ_PLATFORMS`, one bidirectional
   SAM↔SRA table, the coverage assertion, and the `SHORT_READ_PLATFORMS`
   inclusion rule. *Not* the `SequencingPlatform` enum.
5. **Docs**: a "hand-maintained registries" section in `CLAUDE.md` beside the
   existing "Adding a pipeline tool" one, naming the pattern and the
   `ROLE_FIELDS`/`FORMAT_COMMON_ONLY` shape as the house solution. `CLAUDE.md`
   is what an agent actually reads before touching this code, which is the
   whole reason the STAR failure recurred three times in one change.

Commit 1 is worth landing on its own even if the rest slips: it is the only
part that catches the `COMPONENT_ORDER` hazard, and it costs nothing.

## Deliberately out of scope

- **`SequencingPlatform` StrEnum** -- §4. Wants its own issue with a migration
  plan for stored `metadata.platform` strings.
- **`_SAM_PLATFORM_PATTERNS`** -- substring matching over instrument models, an
  open vocabulary by construction. Not a registry keyed by an enum.
- **`TOOL_META` and `suggestion_service`'s rules** -- already covered by
  `CLAUDE.md` and by `test_every_tool_is_documented`. They are the registries
  that were already known; this issue is about the ones that were not.
