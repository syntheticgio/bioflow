# A SequencingPlatform enum, and what it must not touch

Written 2026-08-05 for GitHub issue
[#61](https://github.com/syntheticgio/bioflow/issues/61). Split out of
[#11](https://github.com/syntheticgio/bioflow/issues/11)'s registry audit
([`2026-08-05-registry-audit-design.md`](2026-08-05-registry-audit-design.md),
§4), which de-duplicated the SRA-tag sets and deliberately stopped short of
this.

## The issue body is wrong on its central claim, and that changes the shape of the work

Issue #61 says the dropdown's option strings are

> **already stored verbatim in `metadata.platform` on real objects**

and builds its "this is a migration question, not a hygiene one" framing on
top of that. **Checked against the real database, it is false.** Every object
carrying `metadata.platform` holds an *instrument model*, and not one holds a
dropdown option:

```
total objects: 49

metadata.platform: 10 objects
       4  'Illumina NovaSeq X Plus'
       4  'PacBio RS'
       2  'MinION'

facts.sra_platform: 4 objects        facts.qc_platform: 6 objects
       2  'ILLUMINA'                        2  'ILLUMINA'
       1  'OXFORD_NANOPORE'                 2  'OXFORD_NANOPORE'
       1  'PACBIO_SMRT'                     2  'PACBIO_SMRT'
```

The dropdown offers `Illumina NovaSeq`, `Illumina NextSeq`, `Illumina MiSeq`,
`Illumina HiSeq`, `Oxford Nanopore`, `PacBio`, `Element`, `Other`. The stored
values are `Illumina NovaSeq X Plus`, `PacBio RS`, `MinION`. The two sets are
disjoint and, on this data, always will be — because `metadata/sra.py:99`
writes the instrument model, not the platform tag:

```python
if self.instrument:
    out["platform"] = self.instrument
```

The SRA `PLATFORM` tag goes somewhere else entirely (`facts.sra_platform`,
written at `queue/results.py:501`), which is why that field *is* clean
NCBI-spelling data.

`_SAM_PLATFORM_PATTERNS`' own comment already said this, and I did not read it
carefully enough before filing the issue:

> Matched on *substrings* rather than by exact label, because the values that
> actually land in `metadata.platform` are instrument models, not dropdown
> entries: SRA enrichment writes INSTRUMENT_MODEL, so a real file says
> "NextSeq 550" and never "Illumina NextSeq". An exact-match table read every
> such file as OTHER.

**The consequence: there is no migration.** The single largest reason #11 gave
for deferring this work does not exist. What remains is smaller and differently
shaped than the issue describes, and the issue should be corrected rather than
implemented as written.

## What `metadata.platform` actually is

An **open free-text field with suggestions**, and that is deliberate. The
`FieldDef` declares `type=ENUM` with `options`, but `schemas.py`'s module
docstring is explicit that this constrains nothing:

> Validation follows the same principle: a value that does not match its
> declared type produces a *warning* and is still stored. Refusing to record
> what someone typed loses information; telling them it looks wrong does not.

Confirmed against the running code:

```python
>>> schemas.coerce_and_validate({"platform": "NextSeq 550"}, FormatKind.FASTQ)
ValidationResult(
    values={'platform': 'NextSeq 550'},
    warnings=[{'key': 'platform',
               'message': "Sequencing platform: 'NextSeq 550' is not one of "
                          "the suggested options; stored anyway"}],
)
```

So the first and most important design conclusion:

> **`SequencingPlatform` must not be applied to `metadata.platform`.**

Typing that field would either reject instrument models — every real value in
the database today — or force a lossy coercion that discards which machine
produced the reads. `sam_platform()`'s substring funnel is not a wart working
around a missing enum; it is the correct handling of a field whose vocabulary
is open by design, and it demonstrably works on both vocabularies at once:

```
'Illumina NovaSeq X Plus' -> SAM 'ILLUMINA' -> SRA 'ILLUMINA'        -> preset 'sr'
'PacBio RS'               -> SAM 'PACBIO'   -> SRA 'PACBIO_SMRT'     -> preset 'map-pb'
'MinION'                  -> SAM 'ONT'      -> SRA 'OXFORD_NANOPORE' -> preset 'map-ont'

'Oxford Nanopore'         -> SAM 'ONT'          (dropdown option, same funnel)
'PacBio'                  -> SAM 'PACBIO'
'Element'                 -> SAM 'ELEMENT'
```

Every real stored value and every dropdown option resolves correctly today.
Nothing in the *correctness* of the platform path is broken.

## What is actually closed, and therefore enum-able

Two vocabularies are genuinely closed and owned by an external standard:

| Vocabulary | Owner | Where it lives now |
|---|---|---|
| SRA `PLATFORM` tags | NCBI | `qc_stats.LONG_READ_PLATFORMS` keys, `SHORT_READ_PLATFORMS` |
| SAM `PL` values | the SAM spec | `_SAM_PLATFORM_PATTERNS`' right-hand column |

These are where an enum earns its place. `metadata.platform` (open),
instrument models (open, unbounded — new machines ship constantly), and the
dropdown's marketing labels (a display concern) are not.

The SAM `PL` values this codebase currently emits are `ILLUMINA`, `ONT`,
`PACBIO`, `BGI`, `IONTORRENT`, `LS454`, `SOLID`, `HELICOS`, `ELEMENT`,
`ULTIMA`, `SINGULAR`, and `OTHER`. **Verify that list against the SAM
specification itself before encoding it** rather than trusting this document
or the existing table — the same rule `CLAUDE.md` sets for tool licences and
citations, and for the same reason: a wrong value here is written into BAM
headers that outlive this repo, and `OTHER` is a safe landing place precisely
so that guessing is never necessary.

## The copies the issue did not count

#11 found the SRA tag set written three times in Python and fixed that. Two
more copies live in TypeScript, where no backend test can see them:

- **`frontend/src/api/types.ts:1119`** —
  `export type SraPlatform = "ILLUMINA" | "PACBIO_SMRT" | "OXFORD_NANOPORE";`
  A hand-maintained TS mirror of the SRA tag vocabulary. `grep -rn SraPlatform
  frontend/src` returns **one hit: its own declaration.** It is dead code, and
  has been drifting unobserved because nothing reads it.
- **`frontend/src/components/NcbiDownloadDialog.tsx:29-31`** — `PLATFORM_FILTERS`,
  a live hardcoded list of the same three tags driving the SRA search filter,
  plus a second hardcoded comparison chain at `NcbiDownloadDialog.tsx:1160-1164`.

This matters for scoping: a backend-only enum leaves the drift the issue is
about still present, just in a language the test suite does not cover. There
is no `.test.tsx` infrastructure in this repo and none is expected
(`CLAUDE.md`, "Verifying changes"), so a TS copy cannot be defended by a test
the way a Python one can. The realistic options are generating the TS union
from the Python enum at build time, or accepting the copy and pinning it with
a backend test that asserts the API's advertised values match — the second is
cheaper and fits this repo's scale.

## The one real defect this audit found

Not a correctness bug — a data-quality one, and it is worth stating plainly
because it is the only user-visible thing here.

**The dropdown and SRA enrichment name the same machines differently, so they
never agree.** A user who picks `PacBio` from the dropdown and a file enriched
from SRA to `PacBio RS` have recorded the same fact in two strings. Grouping,
filtering, or searching on `metadata.platform` splits them. And because the
dropdown's options never match what enrichment writes, **every SRA-enriched
file's platform value carries a "not one of the suggested options; stored
anyway" warning** — a warning that is, in the case that matters most, wrong
about which value is more authoritative.

Three ways to resolve it, in increasing order of ambition:

1. **Leave it.** Nothing is incorrect; `sam_platform` normalizes both. Cheapest,
   and the fragmentation stays.
2. **Widen the dropdown's options to the instrument models SRA actually
   writes**, so the suggestions match reality. Kills the spurious warning.
   Loses the tidy family-name list.
3. **Split the field**: `platform` (closed, the enum, family-level) and
   `instrument_model` (open, free text). Cleanest data model, and the only
   option here that *is* a migration — existing `metadata.platform` values
   would move to `instrument_model` with `platform` derived via
   `sam_platform`. Ten objects on this database, so the migration is trivial in
   size, but it changes a field's meaning, which is the part worth thinking
   about rather than the row count.

**Recommendation: (3), and it is the only part of #61 worth doing eagerly.**
It is the change that makes the enum meaningful — a closed `platform` field the
enum can actually type — rather than an enum that types nothing the user
touches. (1) and (2) both leave `metadata.platform` open, in which case the
enum's only job is de-duplicating internal constants that #11 already
de-duplicated on the Python side.

## Proposed scope

1. **`SequencingPlatform` StrEnum over SRA tags**, in `app/models/object.py`
   beside `SequenceType`. SRA tags are the right canonical form: they are what
   `facts.sra_platform` already stores, what `_qc_platform` already returns, and
   what NCBI stamps directly, so no translation is invented.
2. **`SamPlatform` StrEnum over SAM `PL` values**, verified against the SAM
   spec. These are a *different* closed vocabulary, not the same one spelled
   differently — `OXFORD_NANOPORE` vs `ONT`, `PACBIO_SMRT` vs `PACBIO` — so one
   enum cannot serve both without an arbitrary choice of which standard to
   betray.
3. **Derive or exhaustively test** `qc_stats.LONG_READ_PLATFORMS`,
   `SHORT_READ_PLATFORMS`, `_SAM_PLATFORM_PATTERNS`' value column, and
   `_PLATFORM_PRESETS` against the two enums, following #11's
   `ROLE_FIELDS`/`FORMAT_COMMON_ONLY` pattern where full derivation is wrong.
4. **Pin the frontend copies** with a backend test asserting the API's
   advertised platform values, and delete the dead `SraPlatform` union.
5. **Split `platform` from `instrument_model`** per the recommendation above,
   with the ten-row migration.

Steps 1–4 are hygiene and carry no user-visible change. Step 5 is the one with
a decision in it and should not be bundled into the same commit as the others.

## What must not be lost

`reference_assembly.is_short_read`'s **platform-before-chemistry precedence** is
load-bearing and has a documented real-world regression behind it. Its docstring
records that `ERR16145610.fastq` is a MinION run whose `qc_read_chemistry` is
`short` — the chemistry inference reads read *lengths*, so a nanopore run
carrying short reads infers short, "true about the reads, false about the
data." Trusting chemistry there would feed ONT reads to a short-read polisher,
which does not error and quietly degrades the assembly.

That file is in this database — it is *both* `MinION` rows above, the original
and its trimmed derivative. Run against the real objects, the guard currently
holds:

```
ERR16145610.fastq          chemistry='short'  is_short_read=False
ERR16145610.trimmed.fastq  chemistry='short'  is_short_read=False
```

Any refactor of this path must keep platform disqualifying regardless of
chemistry, and the regression check is exactly the two lines above: chemistry
`short`, `is_short_read` still `False`. Worth running against the database
rather than a fixture, since a fixture built from the refactored code's own
assumptions is what would hide the break.

## Recommendation on the issue itself

Correct #61's body before implementing it. Its stated justification — a
migration of stored dropdown strings — describes something that is not in the
database, and an implementer working from the issue alone would go looking for
a data problem that does not exist while missing the two TypeScript copies and
the dropdown/instrument-model mismatch, which are the things actually worth
fixing.
