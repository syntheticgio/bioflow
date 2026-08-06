# A SamPlatform enum, and the two invalid values it found

Written 2026-08-06 for GitHub issue
[#61](https://github.com/syntheticgio/bioflow/issues/61). Supersedes the scope
in [`2026-08-05-sequencing-platform-enum-design.md`](2026-08-05-sequencing-platform-enum-design.md),
which remains correct on its findings but proposed more than the evidence
supports.

## What changed from the previous spec

That spec proposed five steps. This one keeps two of them, drops two, and
defers one, after checking each against the code rather than the plan:

- **Dropped: `SequencingPlatform` over SRA tags** (its steps 1 and 3). #11's
  commit 4 already built `TestPlatformVocabulary` in
  `tests/pipelines/test_qc_stats.py`, which pins the dict's own content, all
  three consumers, the derived inverse, and long/short disjointness.
  Cross-file drift in that vocabulary already fails a test, so an enum there
  catches nothing new.

  It would also cost something. SRA's vocabulary has a third category beyond
  long and short: `SHORT_READ_PLATFORMS`' comment records that `CAPILLARY` and
  similar tags are *deliberately* in neither set, falling through to
  `is_short_read`'s chemistry tie-break. The registry audit's exhaustiveness
  pattern (`set(Enum) == set(main) | companion`) cannot express that without a
  third hand-maintained set existing only to satisfy the test — a registry
  keyed by an enum, added to satisfy a test about registries keyed by an enum.

  Per that audit's own three-way split this vocabulary is case three: the keys
  belong to NCBI, outside this repo. An unclassified tag NCBI emits reaches a
  documented fallback rather than being silently skipped, so the STAR failure
  shape is not present.

- **Deferred: splitting `platform` from `instrument_model`** (its step 5). The
  only part with a user-visible decision in it, and the only genuine migration.
  Its own issue.

- **Kept: `SamPlatform`** (step 2) and **the frontend pin** (step 4).

## What the SAM spec actually says

Verified against `SAMv1.tex` in
[samtools/hts-specs](https://github.com/samtools/hts-specs), not against the
existing table — the previous spec required this, and it was load-bearing.
`@RG` `PL`, line 331:

> Platform/technology used to produce the reads. *Valid values*: `CAPILLARY`,
> `DNBSEQ` (MGI/BGI), `ELEMENT`, `HELICOS`, `ILLUMINA`, `IONTORRENT`, `LS454`,
> `ONT` (Oxford Nanopore), `PACBIO` (Pacific Biosciences), `SINGULAR`, `SOLID`,
> and `ULTIMA`.

and, decisively for the unrecognized case, line 335:

> This field should be omitted when the technology is not in this list (though
> the `PM` field may still be present in this case) or is unknown.

**Two of the twelve values this codebase emits are not in that list.**

| Emitted by `sam_platform()` | In the spec? |
|---|---|
| `ILLUMINA`, `ONT`, `PACBIO`, `IONTORRENT`, `LS454`, `SOLID`, `HELICOS`, `ELEMENT`, `ULTIMA`, `SINGULAR` | yes |
| **`BGI`** | **no** — the spec's name for this platform is `DNBSEQ` |
| **`OTHER`** | **no** — not a member at all |
| `CAPILLARY` | in the spec; nothing detects it, which is fine |

Two spec values are never emitted: `CAPILLARY`, which nothing detects and which
needs no detection, and `DNBSEQ` — emitted as `BGI` instead, which is the bug
below. So the enum will carry two members no current pattern produces, for
different reasons, and a reachability test in the style of
`test_every_option_is_reachable_by_some_token` would be **wrong** here: this
enum's membership is set by an external standard, not by what we happen to
detect.

### `BGI` is a live defect

The pattern row `(("dnbseq", "mgiseq", "bgiseq"), "BGI")` matches exactly the
instrument models the spec calls `DNBSEQ`, then writes `BGI`. The spec's
changelog confirms `DNBSEQ` was added in April 2020 as *the* name for this
platform; `BGI` was never valid. Every BGI/MGI file aligned through this
codebase carries a malformed `@RG PL` that downstream tools do not recognize.

This is not hygiene. It is the failure the previous spec anticipated when it
said to verify against the standard because "a wrong value here is written into
BAM headers that outlive this repo."

### `OTHER` is invalid, and the docstring says otherwise

`sam_platform()`'s docstring currently claims `OTHER` "is in the SAM
vocabulary; passing the raw label through would not be." The first half is
false. The spec's remedy for an unrecognized technology is to omit `PL`, not to
substitute a placeholder.

### The empty-metadata default stays

`sam_platform()` returns `ILLUMINA` when nothing is recorded at all. The spec
would have this omitted too, but that default is a deliberate, documented
product decision about this tool's users, and it is not the thing that is
wrong. It stays.

The resulting contract is deliberately asymmetric, and the docstring must say
why: **no metadata → `ILLUMINA` (an acknowledged guess); unrecognized metadata
→ omit (spec-correct).** The difference is that an empty field means "nobody
said," while an unrecognized non-empty field means "somebody said something
this vocabulary cannot express" — and only the second is a case where the spec
tells us what to do.

## Design

### `SamPlatform`

A `StrEnum` of exactly the spec's twelve values, in
`app/services/pipeline_service.py` beside the table it types — not in
`models/object.py`, because nothing persists it. It is a wire-format detail of
read groups, not a property of a stored object.

**No `OTHER` member.** The enum's job is to make the invalid value
unrepresentable; adding it back as a member would preserve precisely the bug
being fixed.

### `sam_platform() -> SamPlatform | None`

Two changes:

- `"BGI"` becomes `SamPlatform.DNBSEQ` in `_SAM_PLATFORM_PATTERNS`. The match
  needles (`dnbseq`, `mgiseq`, `bgiseq`) are unchanged and already correct.
- Unrecognized non-empty input returns `None` rather than `"OTHER"`. Empty
  input still returns `SamPlatform.ILLUMINA`.

### `PL` omission is a three-site change

`ReadGroup` builds the `PL` field in three separate places, one per aligner
family:

- `align_runner.py:148` — `as_sam_header()`, the `-R` line for bwa/minimap2
- `align_runner.py:165` — `as_rg_args()`, bowtie2/HISAT2's separate flags
- `align_runner.py:184` — `as_star_rg_fields()`, STAR's `--outSAMattrRGline`

All three must drop the field when there is no platform, and this is the
hand-maintained parallel structure `CLAUDE.md` warns about: miss one and STAR
silently emits `PL:None` into a header while bwa is correct. The enum does not
protect against that. A test asserting all three omit it does, and that test is
the reason this is called out as its own design point rather than left to the
implementer to notice.

`suggested_preset` takes `SamPlatform | None`; keying `_PLATFORM_PRESETS` by
the enum makes `.get(None, SHORT_READ)` fall through correctly with no extra
branch.

### The frontend pin needs something to pin to

The previous spec proposed "a backend test asserting the API's advertised
platform values." **Checked: no such advertisement exists.** `api/v1/ncbi.py`
carries the three SRA tags only as a prose comment on a free-string query
parameter, at `:33` and `:193`. The parameter accepts any string. There is
nothing for a test to assert against.

Proposed: promote the comment to a module-level `SRA_PLATFORM_FILTERS`
constant, validate the query parameter against it, and have the backend test
assert its contents. `NcbiDownloadDialog.tsx` gets a comment naming that
constant as the source of truth for its `PLATFORM_FILTERS`.

This pins the backend side properly and leaves the TypeScript copy findable
rather than defended — honest about what is achievable given that this repo has
no frontend test infrastructure and expects none (`CLAUDE.md`, "Verifying
changes"). The weaker alternative, comment-only cross-references in both
directions, pins nothing and is not worth the diff.

Note this pins the **SRA** vocabulary, which is a different standard from the
SAM one `SamPlatform` encodes — `OXFORD_NANOPORE` vs `ONT`, `PACBIO_SMRT` vs
`PACBIO`. One enum cannot serve both without arbitrarily betraying one
standard's spelling. `SamPlatform` must not be used here.

Separately: `frontend/src/api/types.ts:1159`'s `SraPlatform` union is dead.
`grep -rn SraPlatform frontend/src` returns one hit, its own declaration.
Delete it.

## Tests

- **Enum content pinned verbatim** against the spec's twelve, with the spec URL
  and the quoted line in the test's docstring. Follows
  `test_long_read_platforms_names_the_two_sra_tags`: a coverage test that only
  checked consumer agreement would pass while the enum silently lost a member.
- **Every value in `_SAM_PLATFORM_PATTERNS`' right column is a `SamPlatform`
  member.** This is the test that would have caught `BGI`.
- **Every `_PLATFORM_PRESETS` key is a `SamPlatform` member.**
- **Regression:** `"DNBSEQ-T7"` maps to `DNBSEQ`, not `BGI`.
- **Regression, all three sites:** an unrecognized platform produces a read
  group with no `PL` in `as_sam_header()`, `as_rg_args()`, *and*
  `as_star_rg_fields()`.
- **Regression:** empty metadata still yields `ILLUMINA`.
- **`SRA_PLATFORM_FILTERS` contents pinned** to the three tags the dialog
  offers.

## What must not be lost

Unchanged from the previous spec, and worth repeating because this work touches
the platform path: `reference_assembly.is_short_read`'s **platform-before-
chemistry precedence**. `ERR16145610.fastq` is a MinION run whose
`qc_read_chemistry` is `short`; trusting chemistry there feeds ONT reads to a
short-read polisher, which does not error and quietly degrades the assembly.
The file is in this database, as both `MinION` rows:

```
ERR16145610.fastq          chemistry='short'  is_short_read=False
ERR16145610.trimmed.fastq  chemistry='short'  is_short_read=False
```

Check against the real objects, not a fixture — a fixture built from the
refactored code's own assumptions is what would hide a break.

Nothing in this design touches `is_short_read`, and `SamPlatform` is
deliberately kept out of the SRA-tag path it reads. The check is a guard
against unintended reach, not an expected impact.

## Commits

Three, separable:

1. `SamPlatform` enum plus tests over the table as it stands. No behaviour
   change; `BGI` fails the new membership test, so this commit and the next
   land together or the first is written to expect the fix.
2. `BGI` → `DNBSEQ`, `OTHER` → omit across all three `ReadGroup` sites, with
   regressions. **This is a bug fix, not hygiene**, and its message should say
   so.
3. `SRA_PLATFORM_FILTERS` constant, its test, the TS cross-reference comment,
   and deletion of the dead `SraPlatform` union.

## Consequences for issue #61

The issue is titled after `SequencingPlatform`, which this spec drops, and its
scope lists five steps of which two survive. Retitle and rewrite it, and open a
separate issue for the `platform`/`instrument_model` split.

The issue's framing — "unify the platform vocabulary" as maintenance — also
understates what was found. Two of the twelve `PL` values this codebase writes
are invalid, one of them on the live path for every BGI/MGI file. That is
`priority:medium` maintenance only until someone aligns BGI reads.
