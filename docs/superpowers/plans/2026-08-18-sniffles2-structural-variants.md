# Sniffles2 Structural Variant Calling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Sniffles2 as a long-read structural variant caller on its own pipeline, with SV-native storage, a results view including a length histogram, a chemistry-gated Actions card, and a viewer-loadable export.

**Architecture:** Structural variants get a separate pipeline, node type, endpoint, SQLite table, and results view rather than becoming a fourth entry in the existing variant-caller dispatch. The existing small-variant machinery classifies variant type by comparing `len(REF)` to `len(ALT)`, which misclassifies every symbolic-ALT SV record as an indel — silently, with nothing raising. A separate substrate cannot be forgotten into.

**Tech Stack:** Python 3.12, FastAPI, Motor/MongoDB, SQLite (results tables), pytest; React + TypeScript with hand-written SVG charts (no charting library).

**Spec:** `docs/superpowers/specs/2026-08-18-sniffles2-structural-variants-design.md`

## Global Constraints

- **Sniffles version: 2.8.0.** Pinned exactly in the Dockerfile, matching how `NanoPlot==1.45.0` and `pydeseq2==0.5.4` are pinned.
- **`edlib` has no linux-aarch64 wheel.** It must be built from sdist in a builder stage and copied into the final image. The final image has no compiler — `build-essential` exists only in intermediate layers (`backend/Dockerfile:196`, `:225`). A bare `pip install sniffles` passes x86-64 CI and fails on arm64.
- **License is `MIT`.** GitHub's API reports `NOASSERTION`; the LICENSE file and PyPI metadata both say MIT. Do not copy the API's answer.
- **Citation:** `Smolka et al., Nature Biotechnology 2024`, `https://doi.org/10.1038/s41587-023-02024-y`.
- **Minimum SV length default: 50 bp.** The conventional SV floor.
- **`min_support` defaults to Sniffles' automatic mode**, never a fixed integer — the flag is omitted entirely when unset.
- **Run tests with `./backend/run-worktree-tests.sh`**, never the main-checkout exec form. From a worktree the latter tests `main`'s code with no error to say so.
- **Conventional Commits** subjects, imperative mood, lowercase after the colon, no trailing period.

---

### Task 1: `PipelineType.STRUCTURAL_VARIANT` and its frontend label

Spec Decision 7. This lands first because `TOOL_META` in Task 2 references the enum member.

The backend and frontend halves must be one commit: `PIPELINE_LABEL` is typed `Record<PipelineType, string>` and is deliberately exhaustive, so adding the enum member without the label is a TypeScript compile error. That exhaustiveness is load-bearing — its comment records it being the only thing that caught `expression` reaching the backend but not the frontend.

**Files:**
- Modify: `backend/app/pipelines/tools.py:875-905` (the `PipelineType` enum)
- Modify: `frontend/src/components/PipelineToolSelector.tsx:64-75` (`PIPELINE_LABEL`)
- Modify: `frontend/src/api/types.ts` (the `PipelineType` union, if it enumerates members)
- Test: `backend/tests/pipelines/test_tools.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PipelineType.STRUCTURAL_VARIANT` (value `"structural_variant"`), used by Task 2's `TOOL_META` entry.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/pipelines/test_tools.py`:

```python
def test_structural_variant_is_its_own_pipeline_type():
    """SVs are not a kind of small variant.

    PipelineType drives the tool picker, and VARIANT's label is "a variant
    caller". Declaring Sniffles under it would offer a user picking an SNV
    caller a tool that cannot produce SNVs -- the mistake ASSEMBLY_QC's own
    comment records avoiding.
    """
    assert tools.PipelineType.STRUCTURAL_VARIANT.value == "structural_variant"
    assert tools.PipelineType.STRUCTURAL_VARIANT is not tools.PipelineType.VARIANT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_tools.py::test_structural_variant_is_its_own_pipeline_type -v`

Expected: FAIL with `AttributeError: STRUCTURAL_VARIANT`

- [ ] **Step 3: Add the enum member**

In `backend/app/pipelines/tools.py`, after the `VARIANT` member:

```python
    VARIANT = "variant"
    # Structural variants, not a flavour of VARIANT. This enum drives the
    # tool picker (PipelineToolSelector.tsx), whose VARIANT screen is headed
    # "a variant caller" -- listing Sniffles there would offer it to someone
    # picking an SNV caller, as something to call small variants with. Same
    # reasoning as ASSEMBLY_QC's separation from ASSEMBLE below.
    STRUCTURAL_VARIANT = "structural_variant"
```

- [ ] **Step 4: Add the frontend label**

In `frontend/src/components/PipelineToolSelector.tsx`, inside `PIPELINE_LABEL`:

```ts
  variant: "a variant caller",
  structural_variant: "a structural variant caller",
```

If `frontend/src/api/types.ts` declares `PipelineType` as a string-literal union, add `"structural_variant"` to it there too.

- [ ] **Step 5: Run the test and the frontend typecheck**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -v`

Expected: PASS

Run: `cd frontend && npx tsc --noEmit`

Expected: no errors. An error naming `PIPELINE_LABEL` means Step 4 was skipped or the union in `types.ts` still lacks the member.

- [ ] **Step 6: Commit**

```bash
git add backend/app/pipelines/tools.py frontend/src/components/PipelineToolSelector.tsx frontend/src/api/types.ts backend/tests/pipelines/test_tools.py
git commit -m "feat(pipelines): give structural variants their own pipeline type"
```

---

### Task 2: Install Sniffles2 and probe it

Spec Decisions 4 and 5. The builder stage is the whole point of this task — a bare `pip install` would pass CI and fail on arm64.

**Files:**
- Modify: `backend/Dockerfile` (a new builder stage near `winnowmap-build` at `:20`; the install and `COPY --from` near the other pip installs around `:349`)
- Modify: `backend/app/config.py` (add `sniffles_path`)
- Modify: `backend/app/pipelines/tools.py` (probe, `all_tools()` list, `TOOL_META`, `cache_clear` block)
- Test: `backend/tests/pipelines/test_tools.py`

**Interfaces:**
- Consumes: `PipelineType.STRUCTURAL_VARIANT` from Task 1.
- Produces: `tools.sniffles() -> Tool` and `TOOL_META["sniffles"]`, used by Task 6's card.

- [ ] **Step 1: Write the failing test**

```python
def test_sniffles_is_documented_with_a_verified_license():
    """MIT, read from the LICENSE file.

    GitHub's API reports NOASSERTION for this repo -- its detector is
    defeated by the unconventional copyright lines -- so the automated
    answer is wrong here and a recalled one would be a guess.
    """
    meta = tools.TOOL_META["sniffles"]
    assert meta.license == "MIT"
    assert meta.homepage == "https://github.com/fritzsedlazeck/Sniffles"
    assert meta.citation_url == "https://doi.org/10.1038/s41587-023-02024-y"
    assert tools.PipelineType.STRUCTURAL_VARIANT in meta.pipelines
    assert tools.PipelineType.VARIANT not in meta.pipelines
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_tools.py::test_sniffles_is_documented_with_a_verified_license -v`

Expected: FAIL with `KeyError: 'sniffles'`

- [ ] **Step 3: Add the builder stage to the Dockerfile**

Near the `winnowmap-build` stage at `backend/Dockerfile:20`:

```dockerfile
# edlib is a required Sniffles dependency and publishes no linux-aarch64
# wheel for any Python version -- only x86-64 wheels and an sdist (verified
# 2026-08-18 against PyPI). The final image carries no compiler, so pip would
# fall back to that sdist and die with `command 'gcc' failed`, exactly as
# recorded at line 336 for another package and at line 59 for cutadapt.
#
# Building the wheel here keeps the toolchain in a stage that is discarded
# and leaves both architectures running identical code. This fails *only* on
# arm64, so a bare `pip install sniffles` passes x86-64 CI and breaks on an
# Apple Silicon machine.
FROM python:3.12-slim AS edlib-build
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN pip wheel --no-deps --wheel-dir /wheels edlib==1.3.9.post1
```

- [ ] **Step 4: Install Sniffles in the final image**

Near the other pip installs (around `backend/Dockerfile:349`):

```dockerfile
# Sniffles2, the long-read structural variant caller. Its edlib dependency
# comes from the wheel built in the edlib-build stage above -- see there for
# why. --no-index is deliberately absent: the other dependencies (pysam,
# pyspoa, numpy, psutil) all publish arm64 wheels and come from PyPI.
COPY --from=edlib-build /wheels /tmp/wheels
RUN pip install --no-cache-dir --find-links /tmp/wheels sniffles==2.8.0 \
    && rm -rf /tmp/wheels
```

- [ ] **Step 5: Add the config setting**

In `backend/app/config.py`, beside `clair3_path`:

```python
    sniffles_path: str = "sniffles"
```

- [ ] **Step 6: Add the probe**

In `backend/app/pipelines/tools.py`, near `clair3()`:

```python
@lru_cache(maxsize=1)
def sniffles() -> Tool:
    # `--version` prints "Sniffles2, version 2.8.0" and exits 0.
    return _probe("sniffles", settings.sniffles_path, ["--version"])
```

Add `sniffles(),` to the list in `all_tools()` (beside `clair3(),` at `:846`), and `sniffles.cache_clear()` to the block at `:2409`.

- [ ] **Step 7: Add the TOOL_META entry**

```python
    "sniffles": ToolMeta(
        pipelines=(PipelineType.STRUCTURAL_VARIANT,),
        one_liner="Structural variant caller for long reads",
        summary=(
            "Structural variant caller for long reads. Detects deletions, "
            "insertions, duplications, inversions, and translocations from "
            "split-read and within-read-gap signal, which is the variant "
            "class long reads resolve best."
        ),
        strengths=(
            "The standard long-read SV caller",
            "Resolves breakpoints from alignment structure, not per-base accuracy",
            "Works on ONT and PacBio, including high-error CLR reads",
            "Types and sizes each call (SVTYPE, SVLEN)",
        ),
        homepage="https://github.com/fritzsedlazeck/Sniffles",
        repository="https://github.com/fritzsedlazeck/Sniffles",
        citation="Smolka et al., Nature Biotechnology 2024",
        citation_url="https://doi.org/10.1038/s41587-023-02024-y",
        # MIT, read from the repo's LICENSE file. GitHub's API reports
        # NOASSERTION because unconventional copyright lines defeat its
        # detector; PyPI's metadata for 2.8.0 agrees it is MIT.
        license="MIT",
        usage=(
            "The structural variant caller: an SV job on long-read input "
            "runs Sniffles against the BAM and its reference, producing a "
            "typed VCF of deletions, insertions, duplications, inversions, "
            "and breakends. Small variants go to Clair3 or bcftools instead."
        ),
    ),
```

- [ ] **Step 8: Run the tests**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -v`

Expected: PASS, including `test_every_tool_is_documented` — success criterion 1.

- [ ] **Step 9: Verify the image builds on this machine's architecture**

Run: `docker build -f backend/Dockerfile -t bioflow-sniffles-check backend/`

Expected: build succeeds and the `edlib-build` stage runs. On arm64, confirm the `pip install` step did **not** attempt to compile edlib from sdist — the log should show it resolving from `/tmp/wheels`. A `command 'gcc' failed` here means Step 3 or 4 was skipped.

- [ ] **Step 10: Commit**

```bash
git add backend/Dockerfile backend/app/config.py backend/app/pipelines/tools.py backend/tests/pipelines/test_tools.py
git commit -m "feat(pipelines): install and probe Sniffles2 for SV calling"
```

---

### Task 3: The runner — params, command, chemistry gate

Spec Decision 2 and the runner component. Pure functions over strings and paths, following the `csq_runner`/`csq_parse` split: no queue, no filesystem, so every assertion here is cheap.

**Files:**
- Create: `backend/app/pipelines/sniffles_runner.py`
- Test: `backend/tests/pipelines/test_sniffles_runner.py`

**Interfaces:**
- Consumes: `ReadChemistry` from `app.pipelines.align_runner`.
- Produces:
  - `SnifflesParams(threads: int = 4, min_support: int | None = None, min_sv_length: int = 50, tandem_repeats: str | None = None)` with `as_dict() -> dict` and `from_dict(raw: dict | None) -> SnifflesParams`
  - `build_sniffles_command(*, sniffles_path: str, bam: Path, reference: Path, output: Path, params: SnifflesParams) -> list[str]`
  - `sv_calling_allowed_for(chemistry: ReadChemistry) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_sniffles_runner.py`:

```python
from pathlib import Path

import pytest

from app.pipelines import sniffles_runner
from app.pipelines.align_runner import ReadChemistry


def test_command_carries_bam_reference_and_output():
    argv = sniffles_runner.build_sniffles_command(
        sniffles_path="sniffles",
        bam=Path("/data/sample.bam"),
        reference=Path("/data/ref.fa"),
        output=Path("/out/sample.sv.vcf.gz"),
        params=sniffles_runner.SnifflesParams(),
    )
    assert argv[0] == "sniffles"
    assert "--input" in argv and "/data/sample.bam" in argv
    assert "--reference" in argv and "/data/ref.fa" in argv
    assert "--vcf" in argv and "/out/sample.sv.vcf.gz" in argv


def test_min_support_is_omitted_when_unset():
    """Unset must reach Sniffles as "decide for me", not as a number.

    Sniffles derives support from coverage. A hardcoded default would be
    wrong in both directions -- too high on a 10x callset, too low on a
    100x one -- so the flag is absent rather than defaulted.
    """
    argv = sniffles_runner.build_sniffles_command(
        sniffles_path="sniffles",
        bam=Path("/data/s.bam"),
        reference=Path("/data/r.fa"),
        output=Path("/out/s.vcf.gz"),
        params=sniffles_runner.SnifflesParams(),
    )
    assert "--minsupport" not in argv


def test_min_support_is_passed_when_set():
    argv = sniffles_runner.build_sniffles_command(
        sniffles_path="sniffles",
        bam=Path("/data/s.bam"),
        reference=Path("/data/r.fa"),
        output=Path("/out/s.vcf.gz"),
        params=sniffles_runner.SnifflesParams(min_support=7),
    )
    assert "--minsupport" in argv
    assert argv[argv.index("--minsupport") + 1] == "7"


def test_min_sv_length_defaults_to_fifty():
    params = sniffles_runner.SnifflesParams()
    assert params.min_sv_length == 50


@pytest.mark.parametrize(
    "chemistry",
    [
        ReadChemistry.HIFI,
        ReadChemistry.ONT_SIMPLEX,
        ReadChemistry.ONT_DUPLEX,
    ],
)
def test_long_read_chemistries_are_allowed(chemistry):
    assert sniffles_runner.sv_calling_allowed_for(chemistry) is True


def test_clr_is_allowed_even_though_small_variant_calling_refuses_it():
    """The asymmetry is deliberate -- do not "fix" it into consistency.

    variant_runner.caller_for_chemistry refuses CLR because its error rate
    ruins SNV calling. SV calling accepts it: Sniffles resolves breakpoints
    from alignment structure, which tolerates that error rate, and CLR reads
    are long -- which is the property SV detection actually needs.
    """
    assert sniffles_runner.sv_calling_allowed_for(ReadChemistry.CLR) is True


@pytest.mark.parametrize(
    "chemistry", [ReadChemistry.SHORT, ReadChemistry.UNKNOWN]
)
def test_short_and_unknown_are_refused(chemistry):
    assert sniffles_runner.sv_calling_allowed_for(chemistry) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_sniffles_runner.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.sniffles_runner'`

- [ ] **Step 3: Write the runner**

Create `backend/app/pipelines/sniffles_runner.py`:

```python
"""Building and observing a structural variant calling run.

Kept separate from the job handler so the parts worth testing -- command
construction and the chemistry gate -- are pure functions over strings and
paths, with no queue or filesystem involved. Mirrors `variant_runner.py` and
`align_runner.py`, which split the same way for the same reason.
"""

from dataclasses import dataclass
from pathlib import Path

from app.errors import ValidationError
from app.logging import get_logger
from app.pipelines.align_runner import ReadChemistry

log = get_logger(__name__)

# Chemistries whose reads are long enough for breakpoint resolution.
_LONG_READ = frozenset(
    {
        ReadChemistry.HIFI,
        ReadChemistry.CLR,
        ReadChemistry.ONT_SIMPLEX,
        ReadChemistry.ONT_DUPLEX,
    }
)


@dataclass
class SnifflesParams:
    """User-facing knobs for a structural variant run."""

    threads: int = 4
    # None means Sniffles' own automatic mode, which derives the threshold
    # from coverage. Deliberately not a fixed integer: a hardcoded default is
    # wrong in both directions -- too high on a 10x callset, too low on a
    # 100x one -- so this exists to *override* the automatic value, and unset
    # must reach Sniffles as "decide for me" rather than as a number this
    # application chose.
    min_support: int | None = None
    # 50 bp, the conventional floor for what counts as structural rather
    # than an indel.
    min_sv_length: int = 50
    tandem_repeats: str | None = None

    def as_dict(self) -> dict:
        return {
            "threads": self.threads,
            "min_support": self.min_support,
            "min_sv_length": self.min_sv_length,
            "tandem_repeats": self.tandem_repeats,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "SnifflesParams":
        raw = dict(raw or {})

        threads = int(raw.get("threads", 4))
        if threads < 1:
            raise ValidationError("threads must be at least 1")

        min_support = raw.get("min_support")
        if min_support is not None:
            min_support = int(min_support)
            if min_support < 1:
                raise ValidationError("min_support must be at least 1")

        min_sv_length = int(raw.get("min_sv_length", 50))
        if min_sv_length < 1:
            raise ValidationError("min_sv_length must be at least 1")

        tandem_repeats = raw.get("tandem_repeats")
        return cls(
            threads=threads,
            min_support=min_support,
            min_sv_length=min_sv_length,
            tandem_repeats=str(tandem_repeats) if tandem_repeats else None,
        )


def sv_calling_allowed_for(chemistry: ReadChemistry) -> bool:
    """Whether this chemistry's reads can support SV calling.

    CLR is allowed here and *refused* by
    `variant_runner.caller_for_chemistry`. That asymmetry is deliberate and
    should not be harmonised away: small-variant calling reads per-base
    accuracy, which CLR does not have, while Sniffles resolves breakpoints
    from alignment structure -- split reads and within-read gaps -- which
    tolerates a high per-base error rate. CLR reads are long, and length is
    the property SV detection needs.

    SHORT is refused because Sniffles is a long-read caller; UNKNOWN because
    it means QC has not run, and an unrecognised BAM that turns out to be
    Illumina would produce junk quietly.
    """
    return chemistry in _LONG_READ


def build_sniffles_command(
    *,
    sniffles_path: str,
    bam: Path,
    reference: Path,
    output: Path,
    params: SnifflesParams,
) -> list[str]:
    """Assemble the Sniffles invocation.

    `--reference` is passed so insertion sequences are reported rather than
    left symbolic; without it an INS record carries no inserted bases, which
    is most of what makes an insertion call useful.
    """
    argv = [
        sniffles_path,
        "--input",
        str(bam),
        "--reference",
        str(reference),
        "--vcf",
        str(output),
        "--threads",
        str(params.threads),
        "--minsvlen",
        str(params.min_sv_length),
    ]
    if params.min_support is not None:
        argv += ["--minsupport", str(params.min_support)]
    if params.tandem_repeats:
        argv += ["--tandem-repeats", params.tandem_repeats]
    return argv
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_sniffles_runner.py -v`

Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/sniffles_runner.py backend/tests/pipelines/test_sniffles_runner.py
git commit -m "feat(pipelines): add the Sniffles2 command builder and chemistry gate"
```

---

### Task 4: SV record parsing and the SQLite table

Spec Decision 1 and the `sv_db.py` component. These are the record shapes the existing SNV machinery mishandles, so they are the ones worth asserting.

**Files:**
- Create: `backend/app/pipelines/sv_db.py`
- Test: `backend/tests/pipelines/test_sv_db.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `parse_sv_record(line: str) -> SvRecord | None`
  - `SvRecord(chrom, pos, end, svtype, svlen, qual, filter_value, support, gt, mate)`
  - `build_sv_db(*, rows, db_path: Path) -> int`
  - `SvFilters(contig=None, pos_min=None, pos_max=None, svtype=None, min_length=None, max_length=None, filter_value=None, min_qual=None)`
  - `query_svs(db_path, filters, *, limit, offset) -> list[dict]`, `count_svs(db_path, filters) -> int`
  - `type_counts(db_path) -> dict[str, int]`
  - `LENGTH_BINS` and `length_histogram(db_path) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_sv_db.py`:

```python
from pathlib import Path

from app.pipelines import sv_db

DEL = (
    "chr1\t1000\tSniffles2.DEL.1\tN\t<DEL>\t60\tPASS\t"
    "SVTYPE=DEL;SVLEN=-4823;END=5823;SUPPORT=17\tGT:DR:DV\t0/1:12:17"
)
INS = (
    "chr1\t8000\tSniffles2.INS.1\tN\t<INS>\t50\tPASS\t"
    "SVTYPE=INS;SVLEN=312;END=8000;SUPPORT=9\tGT:DR:DV\t1/1:0:9"
)
BND = (
    "chr2\t5000\tSniffles2.BND.1\tN\tN[chr7:900[\t40\tPASS\t"
    "SVTYPE=BND;MATEID=Sniffles2.BND.2;SUPPORT=6\tGT:DR:DV\t0/1:8:6"
)


def test_deletion_length_is_stored_as_a_magnitude():
    """SVLEN is negative for deletions; a length is not negative.

    The sign is redundant with SVTYPE and would make every length filter and
    the histogram wrong -- a -4823 bp deletion sorts below a 50 bp insertion
    and lands in no positive bin.
    """
    rec = sv_db.parse_sv_record(DEL)
    assert rec.svtype == "DEL"
    assert rec.svlen == 4823
    assert rec.end == 5823


def test_deletion_span_is_not_a_point_event():
    """The failure this whole design exists to prevent.

    Run through the small-variant path, this record is a 1 bp event at
    POS with `<DEL>` as its ALT string. END is what makes it a span.
    """
    rec = sv_db.parse_sv_record(DEL)
    assert rec.end - rec.pos == 4823


def test_insertion_length_comes_from_svlen_not_from_end():
    """An insertion's END equals its POS -- the inserted bases are not in
    the reference. Deriving length from END would report every insertion as
    zero-length."""
    rec = sv_db.parse_sv_record(INS)
    assert rec.svtype == "INS"
    assert rec.svlen == 312
    assert rec.end == rec.pos


def test_breakend_carries_its_mate_and_has_no_length():
    """A translocation joins two loci; it has no span on either."""
    rec = sv_db.parse_sv_record(BND)
    assert rec.svtype == "BND"
    assert rec.mate == "Sniffles2.BND.2"
    assert rec.svlen is None


def test_support_is_parsed():
    assert sv_db.parse_sv_record(DEL).support == 17


def test_malformed_line_is_skipped_not_raised():
    assert sv_db.parse_sv_record("not\ta\tvcf\tline") is None


def test_build_and_query_round_trip(tmp_path: Path):
    db = tmp_path / "sv.sqlite"
    inserted = sv_db.build_sv_db(rows=iter([DEL, INS, BND]), db_path=db)
    assert inserted == 3
    assert sv_db.count_svs(db, sv_db.SvFilters()) == 3


def test_svtype_filter_selects_one_type(tmp_path: Path):
    db = tmp_path / "sv.sqlite"
    sv_db.build_sv_db(rows=iter([DEL, INS, BND]), db_path=db)
    rows = sv_db.query_svs(db, sv_db.SvFilters(svtype="DEL"), limit=10, offset=0)
    assert [r["svtype"] for r in rows] == ["DEL"]


def test_length_filter_uses_magnitude(tmp_path: Path):
    """A 4823 bp deletion is longer than a 312 bp insertion."""
    db = tmp_path / "sv.sqlite"
    sv_db.build_sv_db(rows=iter([DEL, INS, BND]), db_path=db)
    rows = sv_db.query_svs(
        db, sv_db.SvFilters(min_length=1000), limit=10, offset=0
    )
    assert [r["svtype"] for r in rows] == ["DEL"]


def test_type_counts(tmp_path: Path):
    db = tmp_path / "sv.sqlite"
    sv_db.build_sv_db(rows=iter([DEL, INS, BND]), db_path=db)
    assert sv_db.type_counts(db) == {"DEL": 1, "INS": 1, "BND": 1}


def test_length_histogram_bins_logarithmically(tmp_path: Path):
    """SV sizes span five orders of magnitude; linear bins would put
    nearly every call in the first bar."""
    db = tmp_path / "sv.sqlite"
    sv_db.build_sv_db(rows=iter([DEL, INS, BND]), db_path=db)
    hist = sv_db.length_histogram(db)
    by_label = {b["label"]: b["count"] for b in hist}
    # 312 bp -> the 100 bp bin; 4823 bp -> the 1 kb bin.
    assert by_label["100 bp"] == 1
    assert by_label["1 kb"] == 1
    # Every bin is present even when empty, so the chart has a stable axis.
    assert len(hist) == len(sv_db.LENGTH_BINS)


def test_breakends_are_absent_from_the_length_histogram(tmp_path: Path):
    """A BND has no length -- counting it as zero would invent a bar."""
    db = tmp_path / "sv.sqlite"
    sv_db.build_sv_db(rows=iter([BND]), db_path=db)
    assert sum(b["count"] for b in sv_db.length_histogram(db)) == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_sv_db.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.sv_db'`

- [ ] **Step 3: Write the module**

Create `backend/app/pipelines/sv_db.py`:

```python
"""The SQLite database backing the structural variant table.

Separate from `variant_db.py` rather than an extension of it, because the
small-variant table classifies type by comparing `len(REF)` to `len(ALT)`.
A Sniffles record's ALT is symbolic (`<DEL>`), so every SV matches that
table's indel filter and a 4.8 kb deletion renders as a 1 bp point event at
its start position -- silently, with nothing raising. See
docs/superpowers/specs/2026-08-18-sniffles2-structural-variants-design.md.

The streaming build mirrors `variant_db.py`'s structure. Note that its own
justification -- millions of rows, a 32M-row memory ceiling -- largely does
not apply here, since an SV callset is typically thousands of records. The
shape is copied for consistency and because it costs nothing.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)

_INSERT_BATCH = 10_000

# Log-scaled, because SV sizes span five orders of magnitude and linear bins
# would collapse nearly every call into the first bar. Each entry is the
# bin's inclusive lower bound and its label; the last bin has no upper bound.
LENGTH_BINS: tuple[tuple[int, str], ...] = (
    (50, "50 bp"),
    (100, "100 bp"),
    (1_000, "1 kb"),
    (10_000, "10 kb"),
    (100_000, "100 kb"),
    (1_000_000, "1 Mb+"),
)


@dataclass(frozen=True)
class SvRecord:
    chrom: str
    pos: int
    end: int | None
    svtype: str
    svlen: int | None
    qual: float | None
    filter_value: str
    support: int | None
    gt: str
    mate: str | None


@dataclass(frozen=True)
class SvFilters:
    """What the SV table is currently showing.

    One object rather than loose arguments so `query_svs` and `count_svs`
    cannot drift apart about what is being filtered -- the page and its total
    have to agree or pagination silently misreports.
    """

    contig: str | None = None
    pos_min: int | None = None
    pos_max: int | None = None
    svtype: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    filter_value: str | None = None
    min_qual: float | None = None


def _num(value: str) -> float | None:
    if value in (".", ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _info(field: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in field.split(";"):
        if not item:
            continue
        key, _, value = item.partition("=")
        out[key] = value
    return out


def parse_sv_record(line: str) -> SvRecord | None:
    """One VCF data line into an SV record, or None if it is not one.

    None rather than an exception: a malformed line in a large callset should
    cost that line, not the whole build, matching how `variant_db` skips and
    counts.
    """
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 8:
        return None
    try:
        pos = int(parts[1])
    except ValueError:
        return None

    info = _info(parts[7])
    svtype = info.get("SVTYPE")
    if not svtype:
        return None

    # SVLEN is negative for deletions. Stored as a magnitude: the sign is
    # redundant with SVTYPE, and keeping it would make every length filter
    # and the histogram wrong -- a -4823 bp deletion sorts below a 50 bp
    # insertion and falls into no positive bin.
    raw_len = info.get("SVLEN")
    svlen: int | None = None
    if raw_len not in (None, "", "."):
        try:
            svlen = abs(int(raw_len))
        except ValueError:
            svlen = None

    raw_end = info.get("END")
    end: int | None = None
    if raw_end not in (None, "", "."):
        try:
            end = int(raw_end)
        except ValueError:
            end = None

    support = info.get("SUPPORT")
    qual = _num(parts[5])

    return SvRecord(
        chrom=parts[0],
        pos=pos,
        end=end,
        svtype=svtype,
        svlen=svlen,
        qual=qual,
        filter_value=parts[6],
        support=int(support) if support and support.isdigit() else None,
        # Every column after FORMAT is one sample's genotype. Rejoined
        # rather than taking the first alone, which would silently drop
        # samples 2..n -- the trap `variant_db.py`'s own gt comment records.
        gt="\t".join(parts[9:]) if len(parts) > 9 else "",
        mate=info.get("MATEID"),
    )


def build_sv_db(*, rows, db_path: Path) -> int:
    """Stream VCF data lines into an indexed SQLite database.

    Indexes are built after the bulk insert, and journaling is off, for the
    reasons `variant_db.build_variant_db` documents: this file is a derived
    artifact rebuilt from the VCF on demand, so durability buys nothing.

    Returns the number of rows inserted.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)

    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute(
            """
            CREATE TABLE svs (
              chrom   TEXT,
              pos     INTEGER,
              end     INTEGER,
              svtype  TEXT,
              svlen   INTEGER,
              qual    REAL,
              filter  TEXT,
              support INTEGER,
              gt      TEXT,
              mate    TEXT
            )
            """
        )

        inserted = 0
        skipped = 0
        batch: list[tuple] = []
        for line in rows:
            if not line or line.startswith("#"):
                continue
            rec = parse_sv_record(line)
            if rec is None:
                skipped += 1
                continue
            batch.append(
                (
                    rec.chrom,
                    rec.pos,
                    rec.end,
                    rec.svtype,
                    rec.svlen,
                    rec.qual,
                    rec.filter_value,
                    rec.support,
                    rec.gt,
                    rec.mate,
                )
            )
            if len(batch) >= _INSERT_BATCH:
                con.executemany(
                    "INSERT INTO svs VALUES (?,?,?,?,?,?,?,?,?,?)", batch
                )
                inserted += len(batch)
                batch = []

        if batch:
            con.executemany("INSERT INTO svs VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
            inserted += len(batch)

        con.execute("CREATE INDEX ix_svs_locus ON svs(chrom, pos)")
        con.execute("CREATE INDEX ix_svs_svtype ON svs(svtype)")
        con.execute("CREATE INDEX ix_svs_filter ON svs(filter)")
        con.commit()
    finally:
        con.close()

    if skipped:
        log.warning("sv_db_skipped_lines", count=skipped, db=str(db_path))
    return inserted


def _where(filters: SvFilters) -> tuple[str, list]:
    """The WHERE clause and its bound parameters.

    Every value is bound, never interpolated: these come from query string
    arguments and reach a SQL statement directly.
    """
    clauses: list[str] = []
    args: list = []

    if filters.contig:
        clauses.append("chrom = ?")
        args.append(filters.contig)
    if filters.pos_min is not None:
        clauses.append("pos >= ?")
        args.append(filters.pos_min)
    if filters.pos_max is not None:
        clauses.append("pos <= ?")
        args.append(filters.pos_max)
    if filters.svtype:
        clauses.append("svtype = ?")
        args.append(filters.svtype)
    if filters.min_length is not None:
        clauses.append("svlen >= ?")
        args.append(filters.min_length)
    if filters.max_length is not None:
        clauses.append("svlen <= ?")
        args.append(filters.max_length)
    if filters.filter_value:
        clauses.append("filter = ?")
        args.append(filters.filter_value)
    if filters.min_qual is not None:
        clauses.append("qual >= ?")
        args.append(filters.min_qual)

    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), args


_COLUMNS = (
    "chrom",
    "pos",
    "end",
    "svtype",
    "svlen",
    "qual",
    "filter",
    "support",
    "gt",
    "mate",
)


def query_svs(
    db_path: Path, filters: SvFilters, *, limit: int, offset: int
) -> list[dict]:
    where, args = _where(filters)
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM svs{where} "
            "ORDER BY chrom, pos LIMIT ? OFFSET ?",
            [*args, limit, offset],
        )
        return [dict(zip(_COLUMNS, row)) for row in cur.fetchall()]
    finally:
        con.close()


def count_svs(db_path: Path, filters: SvFilters) -> int:
    where, args = _where(filters)
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(f"SELECT COUNT(*) FROM svs{where}", args)
        return int(cur.fetchone()[0])
    finally:
        con.close()


def type_counts(db_path: Path) -> dict[str, int]:
    """How many of each SVTYPE."""
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute("SELECT svtype, COUNT(*) FROM svs GROUP BY svtype")
        return {row[0]: int(row[1]) for row in cur.fetchall()}
    finally:
        con.close()


def length_histogram(db_path: Path) -> list[dict]:
    """SV counts per log-scaled length bin.

    Records with no length -- breakends, which join two loci and span
    neither -- are excluded rather than counted as zero, which would invent a
    bar for events that have no size.

    Every bin is returned even when empty, so the chart's axis is stable
    across callsets rather than reshaping itself per run.
    """
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute("SELECT svlen FROM svs WHERE svlen IS NOT NULL")
        lengths = [int(row[0]) for row in cur.fetchall()]
    finally:
        con.close()

    counts = [0] * len(LENGTH_BINS)
    for length in lengths:
        for i in range(len(LENGTH_BINS) - 1, -1, -1):
            if length >= LENGTH_BINS[i][0]:
                counts[i] += 1
                break

    return [
        {"label": label, "min_length": lower, "count": count}
        for (lower, label), count in zip(LENGTH_BINS, counts)
    ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_sv_db.py -v`

Expected: PASS, 12 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/sv_db.py backend/tests/pipelines/test_sv_db.py
git commit -m "feat(pipelines): store structural variants with SV-native columns"
```

---

### Task 5: The job handler, launcher, endpoint, and node type

Wires the runner into the queue and the API. The node type's partition invariant is the specific trap here.

**Files:**
- Create: `backend/app/queue/sv_handlers.py` (the SV handler — **not** `pipeline_handlers.py`; see the note below)
- Modify: `backend/app/queue/handlers.py` (register `sv_handlers` for its `@handler` side effects, in the `from app.queue import (...)` block, alongside `variant_handlers`)
- Modify: `backend/app/services/pipeline_service.py` (`launch_structural_variant_calling`)
- Modify: `backend/app/api/v1/pipelines.py` (`StructuralVariantRequest` and its route)
- Modify: `backend/app/models/run.py` (`RunKind.STRUCTURAL_VARIANT_CALLING`)
- Modify: `backend/app/pipelines/node_types.py` (`_launch_structural_variant_calling`, the `call_structural_variants` spec)
- Test: `backend/tests/pipelines/test_node_types.py`, `backend/tests/pipelines/test_sv_launch.py`

**Note on the handler's module** — corrected during pre-flight review, since
the plan's original text named the wrong file. `pipeline_handlers.py` is
*shared* infrastructure (`_failure`, `_prepare_workdir`, `_resolve_input`,
`_named_link`) that domain-specific handler modules import from — it is not
where a domain handler's own code lives. The small-variant handler that this
task mirrors is `backend/app/queue/variant_handlers.py`, whose own docstring
says it was "split from `align_handlers.py` for the same reason that file
was split from `pipeline_handlers.py`: these share a problem the others do
not." Follow that pattern: create `sv_handlers.py`, `from
app.queue.pipeline_handlers import _failure, _named_link, _prepare_workdir`
(or whichever subset is actually needed), and register the new module in
`handlers.py`'s import block — `registry.load_handlers()` imports only
`handlers.py`, so a handler module absent from that block never registers,
which fails silently (the job simply has no handler) rather than raising.

**Interfaces:**
- Consumes: `sniffles_runner.build_sniffles_command`, `SnifflesParams`, `sv_calling_allowed_for` (Task 3); `sv_db.build_sv_db` (Task 4); `tools.sniffles` (Task 2).
- Produces: `pipeline_service.launch_structural_variant_calling(*, bam_id, params, owner)`; endpoint `POST /pipelines/structural_variants` keyed on `bam_id`; node type key `"call_structural_variants"`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_sv_launch.py`:

```python
import pytest

from app.errors import ValidationError
from app.pipelines import sniffles_runner
from app.pipelines.align_runner import ReadChemistry


def test_short_read_bam_is_refused_before_a_job_is_queued():
    """The gate belongs at launch, not only on the card.

    A card is a suggestion; the endpoint is reachable directly. Refusing
    only in the UI would let a short-read BAM through the API and produce a
    junk callset with nothing saying so.
    """
    assert sniffles_runner.sv_calling_allowed_for(ReadChemistry.SHORT) is False


def test_params_reject_a_zero_support_threshold():
    with pytest.raises(ValidationError):
        sniffles_runner.SnifflesParams.from_dict({"min_support": 0})


def test_params_round_trip_through_as_dict():
    params = sniffles_runner.SnifflesParams(min_support=5, min_sv_length=100)
    assert sniffles_runner.SnifflesParams.from_dict(params.as_dict()) == params
```

Add to `backend/tests/pipelines/test_node_types.py`, inside the existing `TestExhaustiveness` class:

```python
    def test_structural_variant_launcher_is_classified_exactly_once(self):
        """NODE_TYPES/EXCLUDED_LAUNCHES is a partition, not a covering.

        #355 added a spec entry and an exclusion for the same launcher in two
        independent commits; both landed, satisfying the test its issue named
        while failing the double-classification test in this class. Run the
        whole class, not this test alone.
        """
        name = "pipeline_service.launch_structural_variant_calling"
        classified = {
            spec.launch_name for spec in node_types.NODE_TYPES.values()
        }
        assert name in classified
        assert name not in node_types.EXCLUDED_LAUNCHES
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_sv_launch.py tests/pipelines/test_node_types.py -v`

Expected: `test_sv_launch.py` passes on the first test (it only exercises Task 3) and fails on nothing; `test_node_types.py::TestExhaustiveness::test_structural_variant_launcher_is_classified_exactly_once` FAILS with the launcher absent from `classified`.

- [ ] **Step 3: Add the RunKind member**

In `backend/app/models/run.py`, after `VARIANT_CALLING`:

```python
    VARIANT_CALLING = "variant_calling"
    # Structural variants. Separate from VARIANT_CALLING because RunKind is a
    # display and grouping vocabulary, and "called structural variants" is
    # not the same line in an activity view as "called variants" -- the same
    # reasoning that separates ASSEMBLY_DOWNLOAD from SRA_DOWNLOAD.
    STRUCTURAL_VARIANT_CALLING = "structural_variant_calling"
```

- [ ] **Step 4: Add the launcher**

In `backend/app/services/pipeline_service.py`, following `launch_variant_calling`'s structure: resolve the BAM, infer its reference via the same `reference_for_bam` walk the variants launcher uses, read the chemistry, and refuse with `ValidationError` when `sniffles_runner.sv_calling_allowed_for(chemistry)` is False. Require the tool with the existing `require()` helper, then enqueue a job of `RunKind.STRUCTURAL_VARIANT_CALLING` carrying `SnifflesParams.from_dict(params).as_dict()`.

- [ ] **Step 5: Add the handler**

In the new `backend/app/queue/sv_handlers.py`, following `variant_handlers.py`'s handler: materialize the BAM and reference, build the command with `sniffles_runner.build_sniffles_command`, run it, bgzip and tabix the VCF with `variant_runner.build_index_command` (the same helper `variant_handlers.py:376` calls — no SV-specific indexing exists, and none is needed), store the VCF as an object with `ObjectRole.VARIANTS` and its `.tbi` as a `SidecarRole.TBI` sidecar, then build the SQLite table with `sv_db.build_sv_db` over the VCF's data lines. Register `@handler` on the job kind exactly as `variant_handlers.py` does for `call_variants`.

- [ ] **Step 6: Add the endpoint**

In `backend/app/api/v1/pipelines.py`, beside `VariantRequest`:

```python
class StructuralVariantRequest(BaseModel):
    # Keyed on bam_id, matching /pipelines/variants -- both take an
    # alignment rather than a generic object.
    bam_id: PydanticObjectId
    params: dict = {}
```

with a `POST /pipelines/structural_variants` route delegating to the launcher.

- [ ] **Step 7: Add the node type**

In `backend/app/pipelines/node_types.py`, an adapter beside `_launch_variant_calling`:

```python
async def _launch_structural_variant_calling(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_structural_variant_calling(
        bam_id=inputs["alignment"], params=params, owner=owner
    )
```

and the spec, beside `call_variants`:

```python
    "call_structural_variants": NodeTypeSpec(
        label="Call structural variants",
        launch_name="pipeline_service.launch_structural_variant_calling",
        launch=_launch_structural_variant_calling,
        run_kind=RunKind.STRUCTURAL_VARIANT_CALLING,
        inputs=(
            PortSpec(
                "alignment",
                PortType(format=FormatKind.BAM, role=ObjectRole.ALIGNMENT),
            ),
            # Optional, like call_variants: the launcher infers the reference
            # from the BAM's own provenance when this is not wired.
            PortSpec(
                "reference",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
                required=False,
            ),
        ),
        outputs=(
            PortSpec(
                "variants",
                PortType(format=FormatKind.VCF, role=ObjectRole.VARIANTS),
            ),
        ),
    ),
```

- [ ] **Step 8: Run the whole exhaustiveness class**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py -v`

Expected: PASS — **the entire file**, not only the new test. A fix that adds an entry can collide with one that excludes it, and only the partition-completeness test catches the collision.

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_sv_launch.py -v`

Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add backend/app/models/run.py backend/app/services/pipeline_service.py backend/app/queue/pipeline_handlers.py backend/app/api/v1/pipelines.py backend/app/pipelines/node_types.py backend/tests/pipelines/test_sv_launch.py backend/tests/pipelines/test_node_types.py
git commit -m "feat(pipelines): run structural variant calling as its own pipeline"
```

---

### Task 6: The suggestion card

Spec Decision 3 and success criterion 3. The availability test must assert the *unavailable* direction — the image ships Sniffles installed, so an "is available" assertion passes whether or not the patch worked.

**Files:**
- Modify: `backend/app/services/suggestion_service.py` (the builder, the card list around `:2049`, the noun map around `:2038`)
- Test: `backend/tests/services/test_suggestion_service.py`

**Interfaces:**
- Consumes: `tools.sniffles` (Task 2), `sniffles_runner.sv_calling_allowed_for` (Task 3), the endpoint from Task 5.
- Produces: `build_structural_variants_card(obj, chemistry) -> SuggestionCard | None`, `kind="structural_variants"`.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from app.pipelines.align_runner import ReadChemistry
from app.services import suggestion_service
from app.services.suggestion_service import CardStatus


@pytest.mark.parametrize(
    "chemistry",
    [
        ReadChemistry.HIFI,
        ReadChemistry.CLR,
        ReadChemistry.ONT_SIMPLEX,
        ReadChemistry.ONT_DUPLEX,
    ],
)
def test_card_is_offered_for_every_long_read_chemistry(bam_object, chemistry):
    card = suggestion_service.build_structural_variants_card(
        bam_object, chemistry
    )
    assert card.status is CardStatus.AVAILABLE
    assert card.launch["endpoint"] == "/pipelines/structural_variants"
    assert card.launch["body"]["bam_id"] == str(bam_object.id)


def test_card_is_unavailable_when_the_probe_fails(bam_object, monkeypatch):
    """The load-bearing direction.

    The image ships Sniffles installed, so asserting the card is *available*
    passes whether or not a patch worked. Only the flip to unavailable fails
    when the seam breaks. This is #619's third success criterion.
    """
    monkeypatch.setattr(
        suggestion_service.tools,
        "sniffles",
        lambda: suggestion_service.tools.Tool(
            name="sniffles", path=None, version=None
        ),
    )
    card = suggestion_service.build_structural_variants_card(
        bam_object, ReadChemistry.ONT_SIMPLEX
    )
    assert card.status is CardStatus.UNAVAILABLE
    assert "not installed" in card.reason
    assert card.launch is None


def test_short_read_reason_names_the_missing_capability(bam_object):
    """The wording is the seam #620's Delly card replaces -- it must say
    a different *tool* is needed, not that SV calling is impossible."""
    card = suggestion_service.build_structural_variants_card(
        bam_object, ReadChemistry.SHORT
    )
    assert card.status is CardStatus.UNAVAILABLE
    assert "long reads" in card.reason


def test_unknown_chemistry_is_refused(bam_object):
    """UNKNOWN means QC has not run. Running Sniffles on a BAM that turns
    out to be Illumina produces junk quietly."""
    card = suggestion_service.build_structural_variants_card(
        bam_object, ReadChemistry.UNKNOWN
    )
    assert card.status is CardStatus.UNAVAILABLE
```

Use whatever BAM fixture the neighbouring variants-card tests in this file already use; `bam_object` above is a placeholder for that existing fixture's name.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -k structural -v`

Expected: FAIL — `AttributeError: build_structural_variants_card`

- [ ] **Step 3: Write the builder**

```python
def build_structural_variants_card(obj, chemistry) -> SuggestionCard | None:
    """Call structural variants against the reference this BAM was aligned to.

    Separate from `build_variants_card` rather than a branch inside it: SVs
    and small variants answer different questions from the same BAM, and the
    two callsets are stored and displayed separately (see the SV design doc).
    """
    title = "Call structural variants"
    description = "Find deletions, insertions, duplications, and inversions."

    # `align_runner.ReadChemistry`, not a bare import: that is this module's
    # existing convention (see `_is_long_read` and `build_variants_card`), and
    # `sniffles_runner` must be added to the `from app.pipelines import (...)`
    # block at the top of the file.
    if chemistry is None or chemistry is align_runner.ReadChemistry.UNKNOWN:
        return SuggestionCard(
            kind="structural_variants",
            category="VARIANTS",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason="Unknown sequencing platform for this BAM.",
        )

    if not sniffles_runner.sv_calling_allowed_for(chemistry):
        # Worded so #620's short-read caller replaces this reason on the same
        # card rather than adding a second SV card beside it.
        return SuggestionCard(
            kind="structural_variants",
            category="VARIANTS",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=(
                "Sniffles2 needs long reads; short-read structural variant "
                "calling needs a different tool."
            ),
        )

    tool = tools.sniffles()
    if not tool.available:
        return SuggestionCard(
            kind="structural_variants",
            category="VARIANTS",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=f"{tool.name} is not installed.",
        )

    return SuggestionCard(
        kind="structural_variants",
        category="VARIANTS",
        title=title,
        description=description,
        why=(
            "Long reads span breakpoints, which is what makes structural "
            "variants resolvable."
        ),
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/structural_variants",
            "body": {"bam_id": str(obj.id), "params": {}},
        },
    )
```

Register it in `CARD_BUILDERS` — the module-level constant typed
`tuple[tuple[str, object], ...]` around `suggestion_service.py:2046`, whose
existing entries look like
`("variants", lambda obj, ctx: build_variants_card(obj, ctx.chemistry))`.
Add, beside that entry:

```python
    (
        "structural_variants",
        lambda obj, ctx: build_structural_variants_card(obj, ctx.chemistry),
    ),
```

and add `"structural_variants": "structural variant",` to the `kind`->noun
map just above it (the dict containing `"variants": "variant",`).

Also add `sniffles_runner` to the `from app.pipelines import (...)` block at
the top of the module, beside `variant_runner`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -v`

Expected: PASS — the whole file, since the card list is shared state and a new entry can affect neighbouring assertions about how many cards a BAM produces.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat(suggestions): offer structural variant calling on long-read BAMs"
```

---

### Task 7: The results view, length histogram, and export

Spec Decisions 6 and the frontend component. `SvLengthChart` follows `DepthHistogramChart.tsx`'s hand-written SVG approach — this repo has no charting library.

**Files:**
- Create: `frontend/src/components/SvResults.tsx`, `SvTable.tsx`, `SvLengthChart.tsx`
- Modify: `frontend/src/api/types.ts` (`SvRecord`, `SvLengthBucket`)
- Modify: `backend/app/api/v1/pipelines.py` (the SV results read endpoints)

**Interfaces:**
- Consumes: `sv_db.query_svs`, `count_svs`, `type_counts`, `length_histogram` (Task 4); the VCF and TBI objects from Task 5.
- Produces: the results view. Nothing later consumes it.

- [ ] **Step 1: Add the read endpoints**

In `backend/app/api/v1/pipelines.py`, following the variants results endpoints: a paginated `GET` returning `query_svs` rows plus the `count_svs` total, and a summary `GET` returning `type_counts` and `length_histogram`. Both take the SV VCF's object id and the filter query parameters mapping onto `SvFilters`.

- [ ] **Step 2: Add the frontend types**

In `frontend/src/api/types.ts`:

```ts
export type SvRecord = {
  chrom: string;
  pos: number;
  end: number | null;
  svtype: string;
  svlen: number | null;
  qual: number | null;
  filter: string;
  support: number | null;
  gt: string;
  mate: string | null;
};

export type SvLengthBucket = {
  label: string;
  min_length: number;
  count: number;
};
```

- [ ] **Step 3: Write the length chart**

Create `frontend/src/components/SvLengthChart.tsx`, modelled on `DepthHistogramChart.tsx` — same hand-written SVG bar approach, same `w`/`h`/`pad` layout constants:

```tsx
import type { SvLengthBucket } from "../api/types";
import { InfoMarker } from "./InfoMarker";

/**
 * How many structural variants fall in each length bin.
 *
 * The bins are log-scaled because SV sizes span five orders of magnitude --
 * linear bins would put nearly every call in the first bar. The shape is
 * what makes a callset readable at a glance: a nanopore callset is dominated
 * by sub-kb events, and a spike in the 1 Mb+ bin is usually a mapping
 * artifact rather than biology.
 *
 * Breakends are absent by construction -- they join two loci and span
 * neither, so they have no length to bin.
 */
export function SvLengthChart({ buckets }: { buckets: SvLengthBucket[] }) {
  if (!buckets?.length) return null;

  const w = 360;
  const h = 160;
  const pad = { top: 10, right: 10, bottom: 26, left: 34 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const maxCount = Math.max(...buckets.map((b) => b.count), 1);
  const barW = plotW / buckets.length;
  const x = (i: number) => pad.left + i * barW;
  const y = (count: number) => pad.top + plotH - (count / maxCount) * plotH;

  return (
    <svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label="Structural variant lengths">
      {buckets.map((b, i) => (
        <rect
          key={b.label}
          x={x(i) + 1}
          y={y(b.count)}
          width={Math.max(barW - 2, 1)}
          height={pad.top + plotH - y(b.count)}
        >
          <title>{`${b.label}: ${b.count}`}</title>
        </rect>
      ))}
    </svg>
  );
}
```

Match the surrounding components' class names and theme tokens rather than inventing styling — read `DepthHistogramChart.tsx` in full first.

- [ ] **Step 4: Write the table and the results shell**

`SvTable.tsx` renders the paginated rows with columns for position, type, length, quality, filter, and support, with filter controls for contig, type, and length range. `SvResults.tsx` composes the type counts, the chart, and the table, and carries the Decision 6 download: the VCF and its `.tbi` offered **together**, since a `.vcf.gz` without its index is not a track a viewer can load.

- [ ] **Step 5: Typecheck and view the result**

Run: `cd frontend && npx tsc --noEmit`

Expected: no errors.

Bring up the worktree stack and look at a real SV callset:

```bash
./ops/worktree-up.sh
```

Expected: at localhost:5273, an SV run's results show typed rows with real lengths, a histogram weighted toward short events, and both files downloadable. This is the manual verification step — there is no headless component-testing setup in this repo.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/SvResults.tsx frontend/src/components/SvTable.tsx frontend/src/components/SvLengthChart.tsx frontend/src/api/types.ts backend/app/api/v1/pipelines.py
git commit -m "feat(ui): show structural variants with a log-binned length histogram"
```

---

### Task 8: End-to-end verification on a real BAM

Success criterion 2, and the check `CLAUDE.md` asks for beyond the suite: hand-built fixtures already look the way the code expects, so they cannot catch a rule that is wrong about real data.

**Files:**
- Modify: `docs/TODO.md` / `docs/TODO-done.md` only if an entry covers this work.

- [ ] **Step 1: Run the full backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`

Expected: PASS. Read the **count**, not the exit code of whatever ran last.

- [ ] **Step 2: Run SV calling on a real long-read BAM**

Through the UI at localhost:5273, on a project with an ONT or HiFi alignment. Confirm:

- The Actions tab offers "Call structural variants" on the long-read BAM.
- The run completes and produces a VCF with a `.tbi` sidecar.
- The results table shows typed calls with non-zero lengths — specifically that a deletion's length is its span, not 1 bp. That is the failure Decision 1 exists to prevent, and it is invisible to every unit test.
- The histogram is weighted toward sub-kb events.

- [ ] **Step 3: Confirm the card is absent for short reads**

On a short-read BAM in the same project, confirm the card reads that a different tool is needed rather than being missing entirely or offering a run that would produce junk.

- [ ] **Step 4: Verify the export loads in a genome browser**

Download the VCF and its `.tbi` together and load them in IGV against the same reference. Expected: SVs appear at their breakpoints. This is Decision 6's criterion, and it is the check that catches an index that was written but never stored.

- [ ] **Step 5: Bring the stack down**

```bash
./ops/worktree-up.sh --down
```

A stack left up wipes other test runs' data mid-run — `conftest.py` drops every collection in `biopipe_test` at session start.

- [ ] **Step 6: Open the PR**

```bash
git fetch origin main && git rebase origin/main
```

```bash
git diff origin/main...HEAD --stat
```

Confirm the file list matches the tasks above, then push and open the PR with `Closes #619`, labelled `type:feature` and `area:pipelines`.

---

## Notes for the executor

**The arm64 gap is the highest-risk item in this plan.** Task 2 Step 9 is the only place it surfaces. On an x86-64 machine that step passes regardless of whether the builder stage exists, so if you are working on x86-64, treat the `edlib-build` stage as unverified and say so rather than reporting it green.

**Do not harmonise `sv_calling_allowed_for` with `caller_for_chemistry`.** They disagree about CLR on purpose; Task 3's test names this and the docstring explains it.

**Run whole test files, not single tests, for `test_node_types.py` and `test_suggestion_service.py`.** Both hold registries where a passing new test can coexist with a broken invariant elsewhere in the file — Task 5 Step 8 records the specific incident.
