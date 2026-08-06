# SamPlatform Enum and Invalid PL Value Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Encode the SAM `@RG PL` vocabulary as an enum, fix the two invalid values this codebase writes into BAM headers (`BGI`, `OTHER`), and pin the frontend's SRA platform copies.

**Architecture:** A `SamPlatform` StrEnum in `pipeline_service.py` types the existing substring table. `sam_platform()` returns `SamPlatform | None`; `None` means "omit the field," which the SAM spec prescribes for unrecognized technologies. `ReadGroup.platform` widens to `str | None` and its three emission methods skip `PL` when absent, rather than emitting `PL:None`.

**Tech Stack:** Python 3.12 (`StrEnum`), pytest, FastAPI/Pydantic, TypeScript/React frontend.

**Spec:** [`docs/superpowers/specs/2026-08-06-sam-platform-enum-design.md`](../specs/2026-08-06-sam-platform-enum-design.md)

---

## Critical context for the implementer

**Run tests from the worktree with `backend/run-worktree-tests.sh`, never `docker compose exec api`.** The `api` container bind-mounts the *main* checkout, so `docker compose exec` silently tests main's code and reports results describing the wrong tree. From this worktree:

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_align_runner.py -q
```

The script starts a throwaway container mounting this worktree's source, plus its own private Mongo replica set. The private Mongo matters: `conftest.py` drops every collection in `biopipe_test` at session start, so sharing Mongo with the running stack makes two test runs wipe each other mid-run — which reads as flaky tests when it is actually two runs fighting over one database.

**Why `None` and not a sentinel.** `SAMv1.tex` line 335: the `PL` field "should be omitted when the technology is not in this list ... or is unknown." There is no `OTHER` member in the SAM vocabulary. The enum's job is to make the invalid value unrepresentable, so no `OTHER` member gets added back.

**The asymmetry is deliberate.** Empty metadata → `ILLUMINA` (an acknowledged guess, a documented product decision for this tool's users). Unrecognized non-empty metadata → `None` → omit. The difference: an empty field means "nobody said," an unrecognized one means "somebody said something this vocabulary cannot express," and only the second is a case the spec rules on.

**Three emission sites, not one.** `ReadGroup` builds `PL` separately in `as_sam_header()`, `as_rg_args()`, and `as_star_rg_fields()` — one per aligner family, because each aligner takes read groups in a different shape. Missing one means STAR silently emits `PL:None` into a header while bwa is correct. Task 4 tests all three.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `backend/app/services/pipeline_service.py` | `SamPlatform` enum, `_SAM_PLATFORM_PATTERNS`, `_PLATFORM_PRESETS`, `sam_platform()`, `default_read_group()` | Modify |
| `backend/app/pipelines/align_runner.py` | `ReadGroup` dataclass and its three `PL` emission methods, `from_dict` | Modify |
| `backend/app/api/v1/ncbi.py` | `SRA_PLATFORM_FILTERS` constant replacing prose comments | Modify |
| `backend/tests/services/test_sam_platform.py` | All `SamPlatform` and `sam_platform()` tests | Create |
| `backend/tests/pipelines/test_read_group_pl.py` | `PL` omission across all three emission shapes | Create |
| `backend/tests/api/test_ncbi_platform_filters.py` | Pins `SRA_PLATFORM_FILTERS` to the dialog's three tags | Create |
| `frontend/src/api/types.ts` | Delete dead `SraPlatform` union | Modify |
| `frontend/src/components/NcbiDownloadDialog.tsx` | Cross-reference comment on `PLATFORM_FILTERS` | Modify |
| `frontend/src/components/AlignDialog.tsx` | Submit gate stops requiring platform | Modify |

---

## Task 1: The `SamPlatform` enum

**Files:**
- Modify: `backend/app/services/pipeline_service.py` (insert above `_SAM_PLATFORM_PATTERNS`, currently line 609)
- Test: `backend/tests/services/test_sam_platform.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_sam_platform.py`:

```python
"""Tests for the SAM @RG PL vocabulary.

The values here are owned by the SAM specification, not by this repo:
https://github.com/samtools/hts-specs -- SAMv1.tex, the @RG PL row.
"""

from app.services.pipeline_service import SamPlatform


class TestSamPlatformVocabulary:
    def test_enum_is_exactly_the_sam_specs_twelve_values(self):
        """Pinned verbatim against SAMv1.tex's @RG PL row, which reads:

            Valid values: CAPILLARY, DNBSEQ (MGI/BGI), ELEMENT, HELICOS,
            ILLUMINA, IONTORRENT, LS454, ONT (Oxford Nanopore), PACBIO
            (Pacific Biosciences), SINGULAR, SOLID, and ULTIMA.

        Pins the enum's own content rather than only its consumers' agreement
        with it: a test that just checked the pattern table against the enum
        would pass while both silently lost the same member.
        """
        assert {p.value for p in SamPlatform} == {
            "CAPILLARY",
            "DNBSEQ",
            "ELEMENT",
            "HELICOS",
            "ILLUMINA",
            "IONTORRENT",
            "LS454",
            "ONT",
            "PACBIO",
            "SINGULAR",
            "SOLID",
            "ULTIMA",
        }

    def test_other_is_not_a_member(self):
        """OTHER is not in the SAM vocabulary. sam_platform() used to return
        it for unrecognized input, and the docstring claimed it was valid.
        The spec's remedy for an unrecognized technology is to omit PL
        entirely, so there is no member to fall back to -- making the invalid
        value unrepresentable is the point of this enum.
        """
        assert not hasattr(SamPlatform, "OTHER")
        assert "OTHER" not in {p.value for p in SamPlatform}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_sam_platform.py -v
```

Expected: FAIL — `ImportError: cannot import name 'SamPlatform' from 'app.services.pipeline_service'`

- [ ] **Step 3: Write minimal implementation**

In `backend/app/services/pipeline_service.py`, add `StrEnum` to the stdlib imports at the top of the file (near `from dataclasses import dataclass` on line 9):

```python
from enum import StrEnum
```

Then insert this immediately **above** the `_SAM_PLATFORM_PATTERNS` comment block (currently starting line 595, `# The metadata vocabulary is human-facing...`):

```python
class SamPlatform(StrEnum):
    """The SAM `@RG PL` vocabulary, verbatim from the specification.

    Source: https://github.com/samtools/hts-specs, `SAMv1.tex`, the `@RG` `PL`
    row -- "Valid values: CAPILLARY, DNBSEQ (MGI/BGI), ELEMENT, HELICOS,
    ILLUMINA, IONTORRENT, LS454, ONT (Oxford Nanopore), PACBIO (Pacific
    Biosciences), SINGULAR, SOLID, and ULTIMA."

    Membership is set by that standard, not by what this codebase happens to
    detect. Two members are currently produced by no pattern -- CAPILLARY,
    which nothing sequences here, and DNBSEQ, which is new in this commit --
    and that is correct: a reachability test of the kind
    `test_every_option_is_reachable_by_some_token` applies would be wrong for
    an externally-owned vocabulary.

    There is deliberately no OTHER member. It is not in the spec, and the
    spec's remedy for an unrecognized technology is to omit the field rather
    than substitute a placeholder -- see `sam_platform`.
    """

    CAPILLARY = "CAPILLARY"
    DNBSEQ = "DNBSEQ"
    ELEMENT = "ELEMENT"
    HELICOS = "HELICOS"
    ILLUMINA = "ILLUMINA"
    IONTORRENT = "IONTORRENT"
    LS454 = "LS454"
    ONT = "ONT"
    PACBIO = "PACBIO"
    SINGULAR = "SINGULAR"
    SOLID = "SOLID"
    ULTIMA = "ULTIMA"
```

- [ ] **Step 4: Run test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/test_sam_platform.py -v
```

Expected: PASS, 2 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/services/test_sam_platform.py
git commit -m "feat(align): add SamPlatform enum from the SAM specification (#61)"
```

---

## Task 2: Type the tables against the enum, catching `BGI`

This task's membership test is the one that catches the live `BGI` defect. Write the test, watch it fail on real broken data, then fix the value.

**Files:**
- Modify: `backend/app/services/pipeline_service.py:609-631` (`_SAM_PLATFORM_PATTERNS`), `:632-636` (`_PLATFORM_PRESETS`)
- Test: `backend/tests/services/test_sam_platform.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_sam_platform.py`:

```python
class TestTablesAgreeWithTheEnum:
    def test_every_pattern_maps_to_a_spec_value(self):
        """The table's right-hand column is what lands in a BAM header, so a
        value outside the spec is not a style problem -- GATK warns on it and
        some tools read the platform as unknown.

        This test is why the enum exists. It caught `BGI`, which the table
        emitted for DNBSEQ/MGISEQ/BGISEQ instrument models; the spec's name
        for that platform has been DNBSEQ since April 2020 and BGI was never
        valid.
        """
        from app.services.pipeline_service import _SAM_PLATFORM_PATTERNS

        valid = {p.value for p in SamPlatform}
        offenders = [
            value for _needles, value in _SAM_PLATFORM_PATTERNS if value not in valid
        ]
        assert offenders == []

    def test_every_preset_key_is_a_spec_value(self):
        """_PLATFORM_PRESETS is keyed by SAM PL value. A key outside the
        vocabulary can never be looked up, so the preset would silently never
        apply and the platform would quietly take the short-read default.
        """
        from app.services.pipeline_service import _PLATFORM_PRESETS

        valid = {p.value for p in SamPlatform}
        assert [key for key in _PLATFORM_PRESETS if key not in valid] == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_sam_platform.py -v
```

Expected: `test_every_pattern_maps_to_a_spec_value` FAILS with `assert ['BGI'] == []`. This is the real defect, not a test artifact — confirm you see `BGI` in the output before continuing. `test_every_preset_key_is_a_spec_value` passes (its keys are `ONT` and `PACBIO`, both valid).

- [ ] **Step 3: Fix the invalid value**

In `backend/app/services/pipeline_service.py`, change the BGI row of `_SAM_PLATFORM_PATTERNS` (currently line 613). The match needles are already correct and unchanged; only the emitted value is wrong:

```python
    (("dnbseq", "mgiseq", "bgiseq"), SamPlatform.DNBSEQ),
```

While here, replace the remaining bare strings in the same table with enum members so the table is typed throughout. The full table becomes:

```python
_SAM_PLATFORM_PATTERNS: tuple[tuple[tuple[str, ...], SamPlatform], ...] = (
    (("nanopore", "minion", "gridion", "promethion", "flongle"), SamPlatform.ONT),
    (("pacbio", "sequel", "revio", "rs ii"), SamPlatform.PACBIO),
    (("dnbseq", "mgiseq", "bgiseq"), SamPlatform.DNBSEQ),
    (("ion torrent", "ion proton", "ion s5", "ion gene"), SamPlatform.IONTORRENT),
    (("454 gs", "gs flx", "gs junior"), SamPlatform.LS454),
    (("solid",), SamPlatform.SOLID),
    (("helicos",), SamPlatform.HELICOS),
    (("element", "aviti"), SamPlatform.ELEMENT),
    (("ultima",), SamPlatform.ULTIMA),
    (("singular", "g4"), SamPlatform.SINGULAR),
    (
        (
            "illumina", "novaseq", "nextseq", "miseq", "hiseq", "miniseq",
            "iseq", "genome analyzer", "nova x",
        ),
        SamPlatform.ILLUMINA,
    ),
)
```

And key `_PLATFORM_PRESETS` by the enum (currently line 632):

```python
_PLATFORM_PRESETS: dict[SamPlatform, str] = {
    SamPlatform.ONT: align_runner.Preset.MAP_ONT,
    SamPlatform.PACBIO: align_runner.Preset.MAP_PB,
}
```

`SamPlatform` is a `StrEnum`, so these compare and hash equal to their string values — existing callers passing plain strings keep working.

- [ ] **Step 4: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/services/test_sam_platform.py -v
```

Expected: PASS, 4 passed

- [ ] **Step 5: Add the regression test for the fixed value**

Append to `backend/tests/services/test_sam_platform.py`:

```python
class TestDnbseqRegression:
    def test_dnbseq_instrument_model_maps_to_the_spec_value(self):
        """Was BGI, which is not in the SAM vocabulary. A real MGI file's
        metadata.platform holds an instrument model like "DNBSEQ-T7", so this
        is the value that actually reaches a BAM header.
        """
        from app.services.pipeline_service import sam_platform

        assert sam_platform("DNBSEQ-T7") == SamPlatform.DNBSEQ
        assert sam_platform("MGISEQ-2000") == SamPlatform.DNBSEQ
        assert sam_platform("BGISEQ-500") == SamPlatform.DNBSEQ
```

- [ ] **Step 6: Run it**

```bash
./backend/run-worktree-tests.sh tests/services/test_sam_platform.py -v
```

Expected: PASS, 5 passed

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/services/test_sam_platform.py
git commit -m "fix(align): emit DNBSEQ, not the invalid BGI, in @RG PL (#61)

The SAM spec's name for this platform has been DNBSEQ since April 2020;
BGI was never a valid PL value. Every BGI/MGI file aligned through this
codebase carried an @RG PL that downstream tools do not recognize.

Found by typing the pattern table against SamPlatform rather than by
reading it -- the membership test fails on the real table before the fix."
```

---

## Task 3: `sam_platform()` returns `None` for unrecognized input

**Files:**
- Modify: `backend/app/services/pipeline_service.py:638-656` (`sam_platform`), `:658-671` (`suggested_preset`)
- Test: `backend/tests/services/test_sam_platform.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_sam_platform.py`:

```python
class TestUnrecognizedPlatformIsOmitted:
    def test_unrecognized_non_empty_value_returns_none(self):
        """SAMv1.tex: the PL field "should be omitted when the technology is
        not in this list ... or is unknown." This used to return "OTHER",
        which is not a spec value -- the docstring claimed it was.

        None means "omit the field"; the ReadGroup emission methods act on it.
        """
        from app.services.pipeline_service import sam_platform

        assert sam_platform("Sanger 3730xl") is None
        assert sam_platform("some bespoke sequencer") is None

    def test_empty_metadata_still_defaults_to_illumina(self):
        """The asymmetry is deliberate and is NOT the bug being fixed here.

        An empty field means "nobody said," and defaulting to the
        overwhelmingly common platform is a documented product decision for
        this tool's users -- a wrong guess is visible in the BAM header rather
        than silent. An unrecognized non-empty field means "somebody said
        something this vocabulary cannot express," which is the only case the
        spec rules on.
        """
        from app.services.pipeline_service import sam_platform

        assert sam_platform(None) == SamPlatform.ILLUMINA
        assert sam_platform("") == SamPlatform.ILLUMINA
        assert sam_platform("   ") == SamPlatform.ILLUMINA

    def test_suggested_preset_handles_none(self):
        """A None platform must fall through to the short-read default rather
        than raising -- an unrecognized platform should not break the align
        dialog's preset suggestion.
        """
        from app.pipelines import align_runner
        from app.services.pipeline_service import suggested_preset

        assert suggested_preset(None) == align_runner.Preset.SHORT_READ
```

Note on the whitespace case: today `sam_platform("   ")` returns `"OTHER"`. The emptiness check (`if not metadata_platform`) runs before the strip, so `"   "` passes it as truthy, then strips to `""`, matches no needle, and falls through to the `OTHER` return. A naive change would turn that into `None`, treating "somebody typed spaces" as "somebody named a platform this vocabulary cannot express." Step 3's implementation strips *first* and checks emptiness after, so whitespace-only input is correctly treated as empty and gets the `ILLUMINA` default.

- [ ] **Step 2: Run test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_sam_platform.py -v
```

Expected: 3 failures — the first two assert `None`/`ILLUMINA` against the current `"OTHER"` return, and `suggested_preset(None)` currently requires a positional `sam_pl` that it passes to `.get()`.

- [ ] **Step 3: Write the implementation**

Replace `sam_platform()` in `backend/app/services/pipeline_service.py` (currently lines 638-656) with:

```python
def sam_platform(metadata_platform: str | None) -> SamPlatform | None:
    """A SAM `PL` value from a platform label or instrument model.

    Returns None when the recorded value is not in the SAM vocabulary, which
    means *omit the field*: SAMv1.tex says the PL field "should be omitted
    when the technology is not in this list ... or is unknown." This used to
    return "OTHER", and the docstring used to claim OTHER was a spec value.
    It is not one.

    Falls back to ILLUMINA when nothing is recorded at all -- the
    overwhelmingly common case here, and a wrong guess is visible in the BAM
    header rather than silent. That asymmetry is deliberate: an empty field
    means "nobody said," while an unrecognized non-empty field means "somebody
    said something this vocabulary cannot express," and only the second is a
    case the spec rules on.
    """
    text = (metadata_platform or "").strip().lower()
    if not text:
        return SamPlatform.ILLUMINA

    for needles, sam_value in _SAM_PLATFORM_PATTERNS:
        if any(needle in text for needle in needles):
            return sam_value
    return None
```

Then update `suggested_preset()`'s signature (currently line 658) so it accepts the new `None`. Only the signature line and the final `.get()` change; the docstring and chemistry branch stay as they are:

```python
def suggested_preset(
    sam_pl: SamPlatform | None, *, chemistry: align_runner.ReadChemistry | None = None
) -> str:
```

The existing body's final line already works unchanged, since `_PLATFORM_PRESETS.get(None, ...)` returns the default:

```python
    return _PLATFORM_PRESETS.get(sam_pl, align_runner.Preset.SHORT_READ)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/services/test_sam_platform.py -v
```

Expected: PASS, 8 passed

- [ ] **Step 5: Update the two existing tests that assert the old behaviour**

These exist and **will fail** — they assert the bug. Both are in `backend/tests/pipelines/test_align_launch.py`.

At line 142, replace:

```python
    def test_an_unrecognized_platform_becomes_other(self):
        """OTHER is in the SAM vocabulary; passing the raw label through would
        not be."""
        assert pipeline_service.sam_platform("Some New Sequencer") == "OTHER"
```

with:

```python
    def test_an_unrecognized_platform_is_omitted(self):
        """This test used to assert OTHER, on the docstring's claim that OTHER
        "is in the SAM vocabulary." It is not -- SAMv1.tex lists twelve values
        and OTHER is not among them. The spec says to omit PL when the
        technology is not in the list, so None (meaning "omit") is the correct
        answer and there is no placeholder to substitute.
        """
        assert pipeline_service.sam_platform("Some New Sequencer") is None
```

At line 162, replace:

```python
    def test_unknown_platforms_get_short_read(self):
        assert pipeline_service.suggested_preset("OTHER") == Preset.SHORT_READ
```

with:

```python
    def test_unknown_platforms_get_short_read(self):
        """None is what sam_platform now returns for an unrecognized platform;
        it must still fall through to the short-read default rather than
        raising."""
        assert pipeline_service.suggested_preset(None) == Preset.SHORT_READ
```

Leave the comment at lines 98-103 alone — it describes the *substring-matching* bug that the pattern table already fixed, and it remains accurate.

- [ ] **Step 6: Run the wider suites that touch this function**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_align_launch.py tests/pipelines/test_launch_rules.py tests/services/test_suggestion_service.py -q
```

Expected: PASS. If any other test asserts `"OTHER"`, it is asserting the bug — update it to expect `None` and say why in the docstring. Do not change the implementation to satisfy it.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/services/test_sam_platform.py
git commit -m "fix(align): return None for platforms outside the SAM vocabulary (#61)

OTHER is not a SAM PL value, though sam_platform's docstring claimed it
was. The spec's remedy for an unrecognized technology is to omit the
field, so None now means omit. The ILLUMINA-when-empty default stays: an
empty field means nobody said, which the spec does not rule on."
```

---

## Task 4: Omit `PL` at all three emission sites

The trap this task exists for: `ReadGroup` builds `PL` in three separate methods, one per aligner family. Fixing two of three leaves STAR emitting `PL:None` into real BAM headers with nothing failing.

**Files:**
- Modify: `backend/app/pipelines/align_runner.py:118-209` (`ReadGroup`)
- Test: `backend/tests/pipelines/test_read_group_pl.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_read_group_pl.py`:

```python
"""PL omission across every aligner's read-group shape.

ReadGroup builds the PL field in three separate methods because each aligner
family accepts read groups differently. That is hand-maintained parallel
structure: fixing two of the three leaves STAR writing `PL:None` into a real
BAM header while bwa is correct, and nothing fails. These tests cover all
three deliberately.
"""

import pytest

from app.pipelines.align_runner import ReadGroup


def _rg(platform):
    return ReadGroup(sample="S1", library="L1", platform=platform)


class TestPlIsOmittedWhenAbsent:
    def test_sam_header_omits_pl(self):
        header = _rg(None).as_sam_header()
        assert "PL:" not in header
        assert "PL:None" not in header
        assert header == "@RG\\tID:S1\\tSM:S1\\tLB:L1"

    def test_rg_args_omits_pl(self):
        args = _rg(None).as_rg_args()
        assert not any(a.startswith("PL:") for a in args)
        assert args == ["--rg-id", "S1", "--rg", "SM:S1", "--rg", "LB:L1"]

    def test_star_rg_fields_omits_pl(self):
        fields = _rg(None).as_star_rg_fields()
        assert not any(f.startswith("PL:") for f in fields)
        assert fields == ["ID:S1", "SM:S1", "LB:L1"]

    @pytest.mark.parametrize(
        "method", ["as_sam_header", "as_rg_args", "as_star_rg_fields"]
    )
    def test_no_shape_ever_stringifies_none(self, method):
        """The failure this whole task guards against: `PL:None` is a
        syntactically valid header field carrying a garbage value, so it
        corrupts silently rather than erroring.
        """
        rendered = str(getattr(_rg(None), method)())
        assert "None" not in rendered


class TestPlIsPresentWhenKnown:
    def test_sam_header_includes_pl(self):
        assert _rg("ILLUMINA").as_sam_header() == "@RG\\tID:S1\\tSM:S1\\tLB:L1\\tPL:ILLUMINA"

    def test_rg_args_include_pl(self):
        assert _rg("ILLUMINA").as_rg_args() == [
            "--rg-id", "S1", "--rg", "SM:S1", "--rg", "LB:L1", "--rg", "PL:ILLUMINA",
        ]

    def test_star_rg_fields_include_pl(self):
        assert _rg("ILLUMINA").as_star_rg_fields() == [
            "ID:S1", "SM:S1", "LB:L1", "PL:ILLUMINA",
        ]


class TestFromDictAcceptsAMissingPlatform:
    def test_platform_is_no_longer_required(self):
        """from_dict used to reject a falsy platform, so an unrecognized
        instrument model would have failed the whole alignment launch with
        "Read group requires platform" once sam_platform started returning
        None. Sample and library stay required.
        """
        rg = ReadGroup.from_dict({"sample": "S1", "library": "L1"})
        assert rg.platform is None

    def test_sample_and_library_are_still_required(self):
        from app.errors import ValidationError

        with pytest.raises(ValidationError):
            ReadGroup.from_dict({"library": "L1", "platform": "ILLUMINA"})
        with pytest.raises(ValidationError):
            ReadGroup.from_dict({"sample": "S1", "platform": "ILLUMINA"})
```

- [ ] **Step 2: Run test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_read_group_pl.py -v
```

Expected: the omission tests FAIL (current code renders `PL:None`), and `test_platform_is_no_longer_required` FAILS with `ValidationError: Read group requires platform`.

- [ ] **Step 3: Write the implementation**

In `backend/app/pipelines/align_runner.py`, widen the field (line 130):

```python
    platform: str | None = None
```

Note this needs a default because `identifier` below it already has one — a non-default field cannot follow a defaulted one. Placing `platform` after `identifier` would change positional-argument order for existing callers, so give it a default instead and keep the order.

Replace the three emission methods' bodies so each builds its fields conditionally:

```python
    def as_sam_header(self) -> str:
        """The `@RG` line, tab-separated as the SAM spec requires.

        Emitted with literal backslash-t rather than real tabs: this string is
        passed as a single argv element to `-R`, and the aligners parse the
        two-character escape themselves.

        PL is omitted when the platform is unknown, which the SAM spec
        prescribes -- see `pipeline_service.sam_platform`.
        """
        rg_id = self.identifier or self.sample
        fields = [
            "@RG",
            f"ID:{rg_id}",
            f"SM:{self.sample}",
            f"LB:{self.library}",
        ]
        if self.platform:
            fields.append(f"PL:{self.platform}")
        return "\\t".join(fields)

    def as_rg_args(self) -> list[str]:
        """`--rg-id` plus one `--rg` per remaining field.

        bowtie2 and HISAT2 have no single -R taking a whole @RG line. Handing
        them `as_sam_header()` would embed a literal backslash-t in the BAM
        header, which reads as a corrupt read group to every downstream tool
        rather than failing at alignment time.

        PL is omitted when the platform is unknown, as in `as_sam_header`.
        """
        rg_id = self.identifier or self.sample
        args = ["--rg-id", rg_id]
        field_values = [f"SM:{self.sample}", f"LB:{self.library}"]
        if self.platform:
            field_values.append(f"PL:{self.platform}")
        for field_value in field_values:
            args += ["--rg", field_value]
        return args

    def as_star_rg_fields(self) -> list[str]:
        """The fields for STAR's `--outSAMattrRGline`, one argument each.

        A third shape, because STAR accepts neither of the first two: it takes
        every field as a separate argv element after a single flag, and reads
        `as_sam_header()`'s tab-escaped string as one malformed ID. Verified
        against STAR 2.7.11b, whose output carries the resulting `@RG` line
        intact.

        PL is omitted when the platform is unknown, as in `as_sam_header`.
        """
        rg_id = self.identifier or self.sample
        fields = [
            f"ID:{rg_id}",
            f"SM:{self.sample}",
            f"LB:{self.library}",
        ]
        if self.platform:
            fields.append(f"PL:{self.platform}")
        return fields
```

Then stop requiring `platform` in `from_dict` (line 198). Only the `missing` list and the `platform=` argument change:

```python
        missing = [k for k in ("sample", "library") if not data.get(k)]
```

```python
            platform=str(data["platform"]) if data.get("platform") else None,
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_read_group_pl.py -v
```

Expected: PASS, 11 passed

- [ ] **Step 5: Run the aligner suites**

```bash
./backend/run-worktree-tests.sh tests/pipelines/ -q
```

Expected: PASS, unchanged. Two existing tests assert full strings containing `PL:ILLUMINA` — `test_align_runner.py:211` (`as_sam_header`) and `:566` (STAR's `--outSAMattrRGline`) — but both build a read group **with** a platform, so `PL` is still emitted and both keep passing. They are worth reading as confirmation that the present-platform path is unchanged; do not edit them.

If either does fail, the conditional in Step 3 is wrong in a way that drops `PL` when it should be kept, which is the opposite of the intended change.

- [ ] **Step 6: Commit**

```bash
git add backend/app/pipelines/align_runner.py backend/tests/pipelines/test_read_group_pl.py
git commit -m "fix(align): omit @RG PL when the platform is unknown (#61)

All three emission shapes (as_sam_header, as_rg_args, as_star_rg_fields)
drop the field rather than rendering PL:None, and from_dict no longer
requires a platform -- without that, an unrecognized instrument model
would have failed the alignment launch outright once sam_platform began
returning None."
```

---

## Task 5: Stop the align dialog gating submit on platform

`AlignDialog.tsx:188` requires a truthy platform to enable the submit button. With `PL` now optional, an unrecognized platform would leave the user staring at a disabled Align button with no explanation.

**Files:**
- Modify: `frontend/src/components/AlignDialog.tsx:188`

- [ ] **Step 1: Make the change**

At `frontend/src/components/AlignDialog.tsx:188`, the current line reads:

```tsx
    !!readGroup?.sample && !!readGroup?.library && !!readGroup?.platform;
```

Replace it with:

```tsx
    // Platform is deliberately not required: the SAM spec says to omit @RG PL
    // when the technology is unknown, so a file whose instrument model is not
    // in the SAM vocabulary has no platform to offer and must still be
    // alignable. Sample and library remain required -- ReadGroup.from_dict
    // rejects those server-side.
    !!readGroup?.sample && !!readGroup?.library;
```

- [ ] **Step 2: Verify in the running app**

This repo has no frontend test infrastructure and expects none, so the browser is the verification step. From this worktree:

```bash
./ops/worktree-up.sh
```

Open http://localhost:5273, pick a FASTQ, open the Align dialog, clear the Platform field, and confirm the Align button stays enabled. Then stop the stack:

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/AlignDialog.tsx
git commit -m "fix(ui): do not require a platform to launch an alignment (#61)

PL is optional now that unknown platforms are omitted per the SAM spec;
gating submit on it left the Align button dead with no explanation."
```

---

## Task 6: Pin the SRA platform filters

**This pins the SRA vocabulary, which is a different standard from SAM's** — `OXFORD_NANOPORE` vs `ONT`, `PACBIO_SMRT` vs `PACBIO`. `SamPlatform` must not be used here; one enum cannot serve both without arbitrarily betraying one standard's spelling.

**Note on scope:** the spec flagged that adding validation to `platform_filter` is a small behaviour change riding along in a pin, and that a fourth NCBI tag would turn a working query into a rejected one. This task therefore adds the constant and the test **without** adding validation — the pin is what was asked for, and rejecting input is not.

**Files:**
- Modify: `backend/app/api/v1/ncbi.py:33` and `:192-194` (prose comments → constant reference)
- Test: `backend/tests/api/test_ncbi_platform_filters.py` (create)
- Modify: `frontend/src/components/NcbiDownloadDialog.tsx:27` (cross-reference comment)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_ncbi_platform_filters.py`:

```python
"""Pins the SRA platform filter vocabulary the download dialog offers.

This is NCBI's SRA PLATFORM vocabulary, not the SAM PL one -- OXFORD_NANOPORE
rather than ONT, PACBIO_SMRT rather than PACBIO. The two are different
standards and `SamPlatform` must not be used here.

The frontend keeps its own copy in NcbiDownloadDialog.tsx's PLATFORM_FILTERS,
because this repo has no frontend test infrastructure and expects none. This
test pins the backend side so the copy has a source of truth to be checked
against by a reader.
"""

from app.api.v1.ncbi import SRA_PLATFORM_FILTERS


def test_sra_platform_filters_are_the_three_ncbi_tags():
    """Matches NcbiDownloadDialog.tsx's PLATFORM_FILTERS values (minus its
    empty "Any platform" entry, which is a UI affordance rather than a tag).
    """
    assert SRA_PLATFORM_FILTERS == frozenset(
        {"ILLUMINA", "PACBIO_SMRT", "OXFORD_NANOPORE"}
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/api/test_ncbi_platform_filters.py -v
```

Expected: FAIL — `ImportError: cannot import name 'SRA_PLATFORM_FILTERS' from 'app.api.v1.ncbi'`

- [ ] **Step 3: Add the constant**

In `backend/app/api/v1/ncbi.py`, add immediately below `router = APIRouter(...)` (line 28):

```python
# NCBI's SRA PLATFORM vocabulary, as offered by the download dialog's filter.
# Deliberately NOT the SAM PL vocabulary in `pipeline_service.SamPlatform` --
# these are different standards (OXFORD_NANOPORE vs ONT, PACBIO_SMRT vs
# PACBIO), and one enum cannot serve both without betraying one spelling.
#
# `frontend/src/components/NcbiDownloadDialog.tsx`'s PLATFORM_FILTERS is a
# hand-maintained copy of this set; there is no frontend test infrastructure
# in this repo, so this constant is the source of truth a reader checks it
# against. Not validated against on the way in: NCBI owns this vocabulary and
# may add to it, and rejecting a tag they accept would break a working query.
SRA_PLATFORM_FILTERS: frozenset[str] = frozenset(
    {"ILLUMINA", "PACBIO_SMRT", "OXFORD_NANOPORE"}
)
```

Then replace the two prose comments that duplicated this list. At line 33, inside `SraResolveRequest`:

```python
    # One of SRA_PLATFORM_FILTERS, or None for everything.
    platform_filter: str | None = None
```

And at line 192, inside the search request model:

```python
    # One of SRA_PLATFORM_FILTERS, or None for everything. Applies only to
    # sequencing runs -- a genome assembly has no sequencing platform of its
    # own, it is downstream of whatever reads built it.
    platform_filter: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/api/test_ncbi_platform_filters.py -v
```

Expected: PASS, 1 passed

- [ ] **Step 5: Cross-reference from the frontend**

In `frontend/src/components/NcbiDownloadDialog.tsx`, above `PLATFORM_FILTERS` (line 27):

```tsx
// Mirrors SRA_PLATFORM_FILTERS in backend/app/api/v1/ncbi.py, which is the
// source of truth and is pinned by a test there. These are NCBI's SRA
// PLATFORM tags, not SAM PL values -- OXFORD_NANOPORE, not ONT.
const PLATFORM_FILTERS = [
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/ncbi.py backend/tests/api/test_ncbi_platform_filters.py frontend/src/components/NcbiDownloadDialog.tsx
git commit -m "refactor(ncbi): pin the SRA platform filter vocabulary (#61)

Promotes two duplicated prose comments to a tested constant and points
the frontend's hand-maintained copy at it. No validation added: NCBI owns
this vocabulary and rejecting a tag they accept would break a working
query."
```

---

## Task 7: Delete the dead `SraPlatform` union

**Files:**
- Modify: `frontend/src/api/types.ts:1159`

- [ ] **Step 1: Confirm it is still dead**

```bash
grep -rn "SraPlatform" frontend/src
```

Expected: exactly one hit, its own declaration at `frontend/src/api/types.ts:1159`. If anything else appears, stop — it is no longer dead and this task needs rethinking.

- [ ] **Step 2: Delete the line**

Remove from `frontend/src/api/types.ts`:

```typescript
export type SraPlatform = "ILLUMINA" | "PACBIO_SMRT" | "OXFORD_NANOPORE";
```

- [ ] **Step 3: Verify the frontend still builds**

```bash
docker compose -p biopipe-wt exec web npx tsc --noEmit
```

If that project is not running, start it with `./ops/worktree-up.sh` first. Expected: no errors referencing `SraPlatform`.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts
git commit -m "chore(frontend): delete the dead SraPlatform union (#61)

Declared and never read -- grep returned only its own declaration. A
fourth copy of the SRA vocabulary drifting unobserved because nothing
consumes it."
```

---

## Task 8: Full suite, regression check, and close out

- [ ] **Step 1: Run the whole backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: all pass. **Read the count, not just the exit code** — CLAUDE.md is explicit that "green" means reading the number. Note it for the commit message.

- [ ] **Step 2: Verify the platform-before-chemistry guard against real objects**

The spec names this as the thing that must not be lost. `ERR16145610.fastq` is a MinION run whose inferred chemistry is `short`; if platform ever stops winning over chemistry, ONT reads get fed to a short-read polisher, which does not error and quietly degrades the assembly.

Nothing in this plan touches `is_short_read`, so this is a guard against unintended reach, not an expected impact. Run it against the real database rather than a fixture — a fixture built from the changed code's own assumptions is exactly what would hide a break. From the **main checkout root**, not this worktree:

```bash
docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models import DataObject
from app.services import pipeline_service, reference_assembly

async def main():
    await connect_to_mongo()
    for name in ('ERR16145610.fastq', 'ERR16145610.trimmed.fastq'):
        obj = await DataObject.find_one(DataObject.name == name)
        if obj is None:
            print(f'{name}: NOT FOUND')
            continue
        chem = pipeline_service.read_chemistry(obj)
        print(f'{name:28} chemistry={chem}  is_short_read={reference_assembly.is_short_read(obj)}')

asyncio.run(main())
"
```

Expected, matching the spec exactly:

```
ERR16145610.fastq            chemistry=short  is_short_read=False
ERR16145610.trimmed.fastq    chemistry=short  is_short_read=False
```

If `is_short_read` is `True` for either, stop — the guard has broken and the cause must be found before merging.

- [ ] **Step 3: Sanity-check the fix against a real object's read group**

Confirm the `BGI` fix and the omission behave on real data, from the main checkout root:

```bash
docker compose exec api python -c "
from app.services.pipeline_service import sam_platform
for value in ('DNBSEQ-T7', 'MinION', 'Illumina NovaSeq X Plus', 'PacBio RS', 'Sanger 3730xl', None):
    print(f'{str(value):28} -> {sam_platform(value)}')
"
```

Expected:

```
DNBSEQ-T7                    -> DNBSEQ
MinION                       -> ONT
Illumina NovaSeq X Plus      -> ILLUMINA
PacBio RS                    -> PACBIO
Sanger 3730xl                -> None
None                         -> ILLUMINA
```

- [ ] **Step 4: Merge to main and push**

`main` is this project's dev branch and there is no review gate. Once the suite is green and `main` is clean, merge and push without asking. From the main checkout root:

```bash
git checkout main && git pull && git merge claude/issue-61-brainstorm-a1f417 && ./backend/run-worktree-tests.sh tests/ -q && git push origin main
```

If `main` moved under you, re-run the suite after merging rather than assuming the earlier green still holds.

- [ ] **Step 5: Update the issue**

```bash
gh issue comment 61 --body "Implemented and merged. Both invalid PL values are fixed: BGI -> DNBSEQ (live defect -- every BGI/MGI file aligned here had a malformed @RG PL), and OTHER -> field omitted per SAMv1.tex line 335. PL omission covers all three ReadGroup emission shapes. ReadGroup.platform and from_dict became optional, and AlignDialog stopped gating submit on platform -- without those an unrecognized instrument model would have failed the launch outright. SRA filters pinned via SRA_PLATFORM_FILTERS (no input validation added, per the spec's note); dead SraPlatform union deleted. Platform-before-chemistry guard verified against the real ERR16145610 objects."
gh issue close 61
```

- [ ] **Step 6: Check whether a TODO entry needs closing**

```bash
grep -rn "platform\|SamPlatform\|PL" docs/TODO.md | head
```

If an entry covers this work, append ` — FIXED` to its heading, write a short note saying what shipped and where the code lives, record what the implementation did differently from the plan (the `ReadGroup.platform` optionality and the `AlignDialog` gate were both discovered during planning, not in the spec), and move the whole entry to `docs/TODO-done.md`. If no entry covers it, nothing to do.

---

## Notes on what this plan deliberately does not do

- **No `SequencingPlatform` enum over SRA tags.** #11's `TestPlatformVocabulary` already pins that vocabulary's content, its three consumers, the derived inverse, and disjointness. An enum adds no drift protection, and SRA's deliberately-unclassified tags (`CAPILLARY`) cannot fit the registry audit's exhaustiveness pattern without a third hand-maintained set existing only to satisfy its own test.
- **No `platform`/`instrument_model` split.** Deferred to [#66](https://github.com/syntheticgio/bioflow/issues/66) — it is the only part with a user-visible decision in it.
- **No validation on `platform_filter`.** See Task 6.
- **No reachability test on `SamPlatform`.** Membership is set by an external standard; `CAPILLARY` and `DNBSEQ` are both unreachable by any current pattern, correctly.
