# Delly Short-Read Structural Variant Calling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Delly as the short-read structural variant caller, so a short-read-only project can detect SVs, using the pipeline #619 built for Sniffles2.

**Architecture:** One `call_structural_variants` node type keeps handling both callers; a new `sv_caller.py` maps read chemistry to a caller and every consumer asks it rather than assuming Sniffles. Three places where #619 hardcoded "Sniffles2 is the only SV caller" — the dedup key, provenance, and the VCF support-column parser — become caller-aware first, since each is a silent wrong answer the moment a second caller exists.

**Tech Stack:** Python 3.12, FastAPI, pytest, SQLite, Docker (Debian trixie base), Delly v2.6.0 (C++ static binary), bcftools.

**Spec:** `docs/superpowers/specs/2026-08-20-delly-short-read-structural-variants-design.md`

## Global Constraints

- **Delly version: 2.6.0**, pinned as `ARG DELLY_VERSION=2.6.0`.
- **Both architectures supported.** Delly publishes `linux-amd64` and `linux-arm64` release binaries. No arm64 skip.
- **Subcommand is `delly sr`**, not `delly call`. Delly 2.x renamed it.
- **Delly has no minimum-SV-length flag.** Its `-m` is `minrefsep` (breakpoint clustering), *not* a call-size floor. Do not map Sniffles' 50 bp `--minsvlen` onto it, and do not add a post-filter.
- **Sniffles2 stays the long-read caller.** Delly's `lr` mode exists and is deliberately unused.
- **Delly merge is out of scope.** Do not add a short-read merge card or sidecar role.
- **Run `pytest` directly, never `python -m pytest`** — the image's `python` is the medaka venv.
- **Tests run from this worktree via `./backend/run-worktree-tests.sh`**, never `docker compose exec api pytest` (which would test main's code).
- License/citation values are fixed and verified; copy them verbatim from Task 6.

---

### Task 1: The caller-selection seam

**Files:**
- Create: `backend/app/pipelines/sv_caller.py`
- Test: `backend/tests/pipelines/test_sv_caller.py`

**Interfaces:**
- Consumes: `app.pipelines.align_runner.ReadChemistry` (existing enum with members `HIFI`, `CLR`, `ONT_SIMPLEX`, `ONT_DUPLEX`, `SHORT`, `UNKNOWN`).
- Produces: `SvCaller` (StrEnum, members `SNIFFLES2 = "sniffles2"`, `DELLY = "delly"`) and `caller_for_chemistry(chemistry: ReadChemistry) -> SvCaller | None`. Every later task imports both from here.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_sv_caller.py`:

```python
"""The one place chemistry chooses an SV caller."""

import pytest

from app.pipelines.align_runner import ReadChemistry
from app.pipelines.sv_caller import SvCaller, caller_for_chemistry

_LONG_READ = (
    ReadChemistry.HIFI,
    ReadChemistry.CLR,
    ReadChemistry.ONT_SIMPLEX,
    ReadChemistry.ONT_DUPLEX,
)


@pytest.mark.parametrize("chemistry", _LONG_READ)
def test_long_read_chemistries_get_sniffles(chemistry):
    assert caller_for_chemistry(chemistry) is SvCaller.SNIFFLES2


def test_short_reads_get_delly():
    assert caller_for_chemistry(ReadChemistry.SHORT) is SvCaller.DELLY


def test_unknown_gets_no_caller():
    """UNKNOWN means QC has not run. Guessing wrong in either direction
    produces a junk callset with nothing saying so."""
    assert caller_for_chemistry(ReadChemistry.UNKNOWN) is None


@pytest.mark.parametrize("chemistry", _LONG_READ)
def test_delly_is_never_chosen_for_long_reads(chemistry):
    """Delly 2.6.0 ships a `delly lr` long-read mode that this pipeline
    deliberately does not use -- Sniffles2 produces the .snf sidecar the
    merge card depends on. Requirement SV-620-4."""
    assert caller_for_chemistry(chemistry) is not SvCaller.DELLY


def test_every_chemistry_is_classified():
    """Exhaustiveness: a new ReadChemistry member must be given a caller or
    an explicit None, not silently fall through."""
    for chemistry in ReadChemistry:
        result = caller_for_chemistry(chemistry)
        assert result is None or isinstance(result, SvCaller)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_sv_caller.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.sv_caller'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/pipelines/sv_caller.py`:

```python
"""Which structural variant caller covers which read chemistry.

Its own module rather than a function inside either runner: a Delly run
importing `sniffles_runner` to discover that it should use Delly is the
tangle that puts the next caller's branch in the wrong file. Both runners
and every consumer depend on this; it depends on neither runner.
"""

from enum import StrEnum

from app.pipelines.align_runner import ReadChemistry


class SvCaller(StrEnum):
    SNIFFLES2 = "sniffles2"
    DELLY = "delly"


# Chemistries whose reads are long enough for breakpoint resolution.
_LONG_READ = frozenset(
    {
        ReadChemistry.HIFI,
        ReadChemistry.CLR,
        ReadChemistry.ONT_SIMPLEX,
        ReadChemistry.ONT_DUPLEX,
    }
)


def caller_for_chemistry(chemistry: ReadChemistry) -> SvCaller | None:
    """Which SV caller covers this chemistry. None means none does.

    CLR maps to a caller here while `variant_runner.caller_for_chemistry`
    refuses it outright. That asymmetry is deliberate and must not be
    harmonised away: small-variant calling reads per-base accuracy, which
    CLR does not have, while SV calling resolves breakpoints from alignment
    structure -- split reads and within-read gaps -- which tolerates a high
    per-base error rate. CLR reads are long, and length is the property SV
    detection needs.

    Delly ships a `delly lr` long-read mode that this function deliberately
    never selects. Sniffles2 is the long-read standard here and produces the
    .snf sidecar the merge card depends on; swapping it would invalidate
    #619's testing for no capability gain.

    UNKNOWN returns None because it means QC has not run. An unrecognised
    BAM that turns out to be Illumina would produce junk quietly under a
    long-read caller, and vice versa.
    """
    if chemistry in _LONG_READ:
        return SvCaller.SNIFFLES2
    if chemistry is ReadChemistry.SHORT:
        return SvCaller.DELLY
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_sv_caller.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/sv_caller.py backend/tests/pipelines/test_sv_caller.py
git commit -m "feat(pipelines): map read chemistry to an SV caller in one place"
```

---

### Task 2: Route `sv_calling_allowed_for` through the seam

**Files:**
- Modify: `backend/app/pipelines/sniffles_runner.py` (the `_LONG_READ` frozenset and `sv_calling_allowed_for`)
- Test: `backend/tests/pipelines/test_sniffles_runner.py` (existing tests must keep passing)

**Interfaces:**
- Consumes: `sv_caller.caller_for_chemistry` from Task 1.
- Produces: `sniffles_runner.sv_calling_allowed_for` keeps its existing signature and behaviour. Callers in `sv_handlers.py`, `suggestion_service.py`, and `pipeline_service.py` are untouched by this task.

This task exists so there is exactly one chemistry→caller mapping. Two that can disagree is the failure mode.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/pipelines/test_sniffles_runner.py`:

```python
def test_allowed_for_delegates_to_the_caller_seam():
    """One mapping, not two that can drift. Patching the seam must change
    this function's answer -- if it does not, a second copy of the
    chemistry table survives somewhere."""
    from unittest.mock import patch

    from app.pipelines import sniffles_runner
    from app.pipelines.align_runner import ReadChemistry

    with patch(
        "app.pipelines.sniffles_runner.sv_caller.caller_for_chemistry",
        return_value=None,
    ):
        assert not sniffles_runner.sv_calling_allowed_for(ReadChemistry.HIFI)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_sniffles_runner.py::test_allowed_for_delegates_to_the_caller_seam -v`
Expected: FAIL — `AttributeError: module 'app.pipelines.sniffles_runner' has no attribute 'sv_caller'`

- [ ] **Step 3: Write minimal implementation**

In `backend/app/pipelines/sniffles_runner.py`, add the import beside the existing ones:

```python
from app.pipelines import sv_caller
```

Delete the module-level `_LONG_READ` frozenset (it moved to `sv_caller.py` in Task 1), and replace the body of `sv_calling_allowed_for` — keeping the function and its full docstring, which callers rely on:

```python
def sv_calling_allowed_for(chemistry: ReadChemistry) -> bool:
    """Whether this chemistry's reads can support SV calling.

    Delegates to `sv_caller.caller_for_chemistry` so there is one chemistry
    table rather than two that can disagree. That function's docstring
    carries the CLR asymmetry note this function used to own: CLR is allowed
    for SV calling and refused by `variant_runner.caller_for_chemistry`, and
    harmonising the two would silently delete a real capability.

    Now true for SHORT as well as long reads, since #620 added Delly. The
    *which* caller question belongs to `caller_for_chemistry`; this function
    answers only whether any caller applies.
    """
    return sv_caller.caller_for_chemistry(chemistry) is not None
```

- [ ] **Step 4: Run the whole SV test file**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_sniffles_runner.py -v`
Expected: PASS for the new test. **Some existing tests asserting `sv_calling_allowed_for(SHORT) is False` will now FAIL** — that is the intended behaviour change. Update each to assert `is True`, and rename any test whose name claims short reads are refused (e.g. `test_short_reads_are_refused` → `test_short_reads_are_allowed_now_that_delly_exists`), with a one-line docstring pointing at #620.

- [ ] **Step 5: Re-run and commit**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_sniffles_runner.py -v`
Expected: PASS

```bash
git add backend/app/pipelines/sniffles_runner.py backend/tests/pipelines/test_sniffles_runner.py
git commit -m "refactor(pipelines): derive sv_calling_allowed_for from the caller seam"
```

---

### Task 3: Make the SV dedup key caller-aware

**Files:**
- Modify: `backend/app/services/pipeline_service.py:3626-3634` (`_sv_dedup_key`) and its call site at `:3781`
- Test: `backend/tests/services/test_sv_merge_launch.py`

**Interfaces:**
- Consumes: `SvCaller` from Task 1.
- Produces: `_sv_dedup_key(*, bam_id, caller: SvCaller, params: dict) -> str`. The `caller` argument is keyword-only and required.

This is the dangerous one: without it, a Delly request and a Sniffles request on the same BAM with equal param fingerprints **collide**, and the second silently returns the first's result. Requirement SV-620-5.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/services/test_sv_merge_launch.py`:

```python
def test_sv_dedup_key_distinguishes_callers():
    """Without the caller in the key, a Delly run and a Sniffles run on one
    BAM with equal params collapse into one result and the second silently
    returns the first caller's VCF. Nothing raises. Requirement SV-620-5.
    """
    from bson import ObjectId

    from app.pipelines.sv_caller import SvCaller
    from app.services.pipeline_service import _sv_dedup_key

    bam_id = ObjectId()
    params = {"threads": 4}

    sniffles = _sv_dedup_key(
        bam_id=bam_id, caller=SvCaller.SNIFFLES2, params=params
    )
    delly = _sv_dedup_key(bam_id=bam_id, caller=SvCaller.DELLY, params=params)

    assert sniffles != delly


def test_sv_dedup_key_is_stable_for_one_caller():
    """The same request twice is still a double-submit to collapse."""
    from bson import ObjectId

    from app.pipelines.sv_caller import SvCaller
    from app.services.pipeline_service import _sv_dedup_key

    bam_id = ObjectId()
    params = {"threads": 4}

    assert _sv_dedup_key(
        bam_id=bam_id, caller=SvCaller.DELLY, params=params
    ) == _sv_dedup_key(bam_id=bam_id, caller=SvCaller.DELLY, params=params)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_sv_merge_launch.py -k dedup -v`
Expected: FAIL — `TypeError: _sv_dedup_key() got an unexpected keyword argument 'caller'`

- [ ] **Step 3: Write minimal implementation**

Replace `_sv_dedup_key` in `backend/app/services/pipeline_service.py`:

```python
def _sv_dedup_key(*, bam_id, caller: SvCaller, params: dict) -> str:
    """Identity of a structural variant calling request.

    The caller is part of the key. Calling one BAM with Sniffles2 and with
    Delly is two results worth comparing, not a double-submit to collapse --
    and without the caller here the second request silently returns the
    first's VCF, with the job reporting success.

    Note that `_variant_dedup_key` is not the example to copy: its docstring
    claims to include the caller and its returned string does not. See
    https://github.com/syntheticgio/bioflow/issues/699.
    """
    return (
        f"call_structural_variants:{bam_id}:{caller.value}:"
        f"{_params_fingerprint(params)}"
    )
```

Add the import at the top of the file, beside the other `app.pipelines` imports:

```python
from app.pipelines.sv_caller import SvCaller
```

Update the call site at `:3781`. It currently reads `dedup_key=_sv_dedup_key(bam_id=bam.id, params=merged.as_dict())`; it becomes:

```python
        dedup_key=_sv_dedup_key(
            bam_id=bam.id, caller=caller, params=merged.as_dict()
        ),
```

The `caller` local it references is introduced in Task 7. **Until Task 7 lands, define it immediately above the `tools.require(...)` line** so this task is independently green:

```python
    caller = sv_caller.caller_for_chemistry(
        chemistry or align_runner.ReadChemistry.UNKNOWN
    )
```

and add `from app.pipelines import sv_caller` to the imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_sv_merge_launch.py tests/pipelines/test_sv_launch.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/services/test_sv_merge_launch.py
git commit -m "fix(services): put the caller in the SV dedup key so two callers cannot collide"
```

---

### Task 4: Make provenance record the caller that actually ran

**Files:**
- Modify: `backend/app/queue/results.py:2836-2845` (`sv_provenance`)
- Test: `backend/tests/queue/test_sv_results.py`

**Interfaces:**
- Consumes: nothing from earlier tasks; reads `result["caller"]`, which Task 8's handler writes.
- Produces: `sv_provenance(result: dict) -> dict` — unchanged signature, now caller-aware.

Requirement SV-620-6. Left alone, every Delly VCF is stamped as Sniffles output, permanently, on disk, in the one record whose purpose is saying what produced the file.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/queue/test_sv_results.py`:

```python
def test_sv_provenance_records_delly():
    """The VCF's own record of what made it. A literal caller here means
    every Delly callset claims to be Sniffles output. Requirement SV-620-6."""
    from app.queue.results import sv_provenance

    prov = sv_provenance({"caller": "delly", "tool_version": "2.6.0"})

    assert prov["variants_called_by"] == "delly"
    assert prov["variant_caller_version"] == "2.6.0"


def test_sv_provenance_records_sniffles():
    from app.queue.results import sv_provenance

    prov = sv_provenance({"caller": "sniffles2", "tool_version": "2.8.0"})

    assert prov["variants_called_by"] == "sniffles2"


def test_sv_provenance_falls_back_for_a_pre_620_result():
    """Jobs queued before #620 carry no caller field. They were all
    Sniffles, so that is the honest default -- but only for results with no
    caller at all, never as an override."""
    from app.queue.results import sv_provenance

    assert sv_provenance({})["variants_called_by"] == "sniffles2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/queue/test_sv_results.py -k provenance -v`
Expected: FAIL — `assert 'sniffles2' == 'delly'`

- [ ] **Step 3: Write minimal implementation**

Replace `sv_provenance` in `backend/app/queue/results.py`:

```python
def sv_provenance(result: dict) -> dict:
    """The facts a structural variant calling run stamps onto the VCF it
    produced. Mirrors `variant_provenance`.

    The caller comes from the result rather than a literal: since #620 this
    pipeline runs Sniffles2 for long reads and Delly for short ones, and a
    hardcoded name would mislabel one of them permanently on disk.

    A result with no caller predates #620, when Sniffles2 was the only SV
    caller -- so that is the fallback, and it applies only when the field is
    absent.
    """
    return {
        "variants_called_by": result.get("caller") or "sniffles2",
        "variant_caller_version": result.get("tool_version"),
        "variant_params": result.get("params") or {},
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/queue/test_sv_results.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/results.py backend/tests/queue/test_sv_results.py
git commit -m "fix(queue): stamp SV provenance with the caller that actually ran"
```

---

### Task 5: Per-caller support extraction in the SV table

**Files:**
- Modify: `backend/app/pipelines/sv_db.py` (`parse_sv_record` at `:92`, `build_sv_db` at `:152`)
- Test: `backend/tests/pipelines/test_sv_db.py`

**Interfaces:**
- Consumes: `SvCaller` from Task 1.
- Produces: `parse_sv_record(line: str, caller: SvCaller = SvCaller.SNIFFLES2) -> SvRecord | None` and `build_sv_db(*, rows, db_path: Path, caller: SvCaller = SvCaller.SNIFFLES2) -> int`. Both default to Sniffles so existing call sites keep working; Task 8 passes the real caller.

Requirement SV-620-7 and SV-620-8. Sniffles emits `SUPPORT=17`; Delly emits `PE` and `SR` as separate counts and for some call types only one. Left alone the support column is blank for every Delly row, which reads as "no support data" rather than as a mapping gap.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/pipelines/test_sv_db.py`:

```python
class TestSupportExtraction:
    """Sniffles reports SUPPORT; Delly reports PE and SR separately."""

    def _line(self, info: str) -> str:
        return (
            "chr1\t1000\tid1\tN\t<DEL>\t60\tPASS\t"
            f"{info}\tGT\t0/1"
        )

    def test_sniffles_reads_its_support_key(self):
        from app.pipelines.sv_caller import SvCaller
        from app.pipelines.sv_db import parse_sv_record

        rec = parse_sv_record(
            self._line("SVTYPE=DEL;SVLEN=-4823;END=5823;SUPPORT=17"),
            SvCaller.SNIFFLES2,
        )
        assert rec.support == 17

    def test_delly_sums_paired_end_and_split_read(self):
        """Both kinds are reads supporting the call, and the column means
        'how many reads support this'. Requirement SV-620-7."""
        from app.pipelines.sv_caller import SvCaller
        from app.pipelines.sv_db import parse_sv_record

        rec = parse_sv_record(
            self._line("SVTYPE=DEL;SVLEN=-4823;END=5823;PE=9;SR=4"),
            SvCaller.DELLY,
        )
        assert rec.support == 13

    def test_delly_with_only_paired_end_support(self):
        """Delly omits SR for calls it found by read-pair signal alone."""
        from app.pipelines.sv_caller import SvCaller
        from app.pipelines.sv_db import parse_sv_record

        rec = parse_sv_record(
            self._line("SVTYPE=DEL;SVLEN=-4823;END=5823;PE=9"),
            SvCaller.DELLY,
        )
        assert rec.support == 9

    def test_delly_with_neither_yields_none(self):
        from app.pipelines.sv_caller import SvCaller
        from app.pipelines.sv_db import parse_sv_record

        rec = parse_sv_record(
            self._line("SVTYPE=DEL;SVLEN=-4823;END=5823"), SvCaller.DELLY
        )
        assert rec.support is None

    def test_delly_ignores_the_sniffles_key(self):
        """A SUPPORT key in a Delly record is not Delly's own field; reading
        it would be the mapping bug in reverse."""
        from app.pipelines.sv_caller import SvCaller
        from app.pipelines.sv_db import parse_sv_record

        rec = parse_sv_record(
            self._line("SVTYPE=DEL;SVLEN=-4823;END=5823;SUPPORT=99"),
            SvCaller.DELLY,
        )
        assert rec.support is None

    def test_every_caller_has_an_extractor(self):
        """A hand-maintained registry keyed by an enum, which CLAUDE.md names
        as the shape that skips silently rather than raising. A new SvCaller
        member with no extractor must fail here, not blank a column in
        production. Requirement SV-620-8."""
        from app.pipelines.sv_caller import SvCaller
        from app.pipelines.sv_db import _SUPPORT_EXTRACTORS

        assert set(SvCaller) == set(_SUPPORT_EXTRACTORS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_sv_db.py -k Support -v`
Expected: FAIL — `TypeError: parse_sv_record() takes 1 positional argument but 2 were given`

- [ ] **Step 3: Write minimal implementation**

In `backend/app/pipelines/sv_db.py`, add the import:

```python
from app.pipelines.sv_caller import SvCaller
```

Add the extractors above `parse_sv_record`:

```python
def _sniffles_support(info: dict[str, str]) -> int | None:
    raw = info.get("SUPPORT")
    return int(raw) if raw and raw.isdigit() else None


def _delly_support(info: dict[str, str]) -> int | None:
    """Delly reports paired-end and split-read support as separate counts,
    and omits whichever kind did not contribute to a given call.

    Summed rather than surfaced as two columns: the SV table's `support`
    column means "how many reads support this call", and both kinds do.
    Splitting it would change the schema and SvResults.tsx for one caller.
    """
    total: int | None = None
    for key in ("PE", "SR"):
        raw = info.get(key)
        if raw and raw.isdigit():
            total = (total or 0) + int(raw)
    return total


# Keyed by SvCaller. Exhaustiveness is enforced by
# test_every_caller_has_an_extractor -- a member with no entry here would
# silently blank the support column rather than raise.
_SUPPORT_EXTRACTORS: dict[SvCaller, "Callable[[dict[str, str]], int | None]"] = {
    SvCaller.SNIFFLES2: _sniffles_support,
    SvCaller.DELLY: _delly_support,
}
```

Add `from collections.abc import Callable` to the imports.

Change `parse_sv_record`'s signature and its support line. The signature becomes:

```python
def parse_sv_record(
    line: str, caller: SvCaller = SvCaller.SNIFFLES2
) -> SvRecord | None:
```

Replace the two existing support lines — `support = info.get("SUPPORT")` and the `support=int(support) if support and support.isdigit() else None,` argument — with:

```python
    support = _SUPPORT_EXTRACTORS[caller](info)
```

and in the `SvRecord(...)` construction:

```python
        support=support,
```

Then thread the caller through `build_sv_db`. Its signature becomes:

```python
def build_sv_db(
    *, rows, db_path: Path, caller: SvCaller = SvCaller.SNIFFLES2
) -> int:
```

and its parse call at `:207` becomes:

```python
            rec = parse_sv_record(line, caller)
```

Both defaults are `SNIFFLES2` so the existing call sites keep working unchanged until Task 8 passes the real caller.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_sv_db.py -v`
Expected: PASS, including the pre-existing tests (the default keeps them on the Sniffles extractor)

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/sv_db.py backend/tests/pipelines/test_sv_db.py
git commit -m "fix(pipelines): read SV support per caller, not from Sniffles' key alone"
```

---

### Task 6: Install Delly and register the tool

**Files:**
- Modify: `backend/Dockerfile` (new layer beside the NCBI datasets layer at `:325`)
- Modify: `backend/app/config.py` (add `delly_path`)
- Modify: `backend/app/pipelines/tools.py` (probe, `_ALL_TOOLS` list, `TOOL_META`, cache clear)
- Test: `backend/tests/pipelines/test_tools.py`

**Interfaces:**
- Produces: `tools.delly() -> Tool` and `TOOL_META["delly"]`. Tasks 7 and 8 call `tools.delly()`.

Requirements SV-620-1 and SV-620-2.

- [ ] **Step 1: Add the Dockerfile layer**

Delly is a single static binary like the NCBI datasets CLI, so this goes inline rather than into a script. Add after the datasets layer (`backend/Dockerfile:340`):

```dockerfile
# --- Delly ------------------------------------------------------------------
#
# Short-read structural variant calling (#620), the counterpart to Sniffles2
# for long reads. A single static binary published for both architectures, so
# this is the NCBI datasets shape above -- only the URL differs. No builder
# stage and no arm64 skip: upstream ships linux-arm64, unlike Manta, which is
# archived and x86-64 only.
#
# TARGETARCH is already in scope from the bwa-mem2 ARG above.
ARG DELLY_VERSION=2.6.0
RUN set -e \
    && case "$TARGETARCH" in \
         arm64) DELLY_ARCH=linux-arm64 ;; \
         *)     DELLY_ARCH=linux-amd64 ;; \
       esac \
    && curl -fsSL \
         "https://github.com/dellytools/delly/releases/download/v${DELLY_VERSION}/delly-v${DELLY_VERSION}-${DELLY_ARCH}" \
         -o /usr/local/bin/delly \
    && chmod +x /usr/local/bin/delly \
    && delly --version
```

The trailing `delly --version` is the build-time check that the binary actually runs on this base image — it fails the build loudly if the release binary's glibc requirement is incompatible with Debian trixie.

- [ ] **Step 2: Build the image and confirm the binary runs**

Run:

```bash
docker compose -p biopipe-delly-probe build api
```

Expected: the Delly layer completes and prints a version line.

**If the build fails at `delly --version` with a glibc error**, stop and apply the spec's contingency in order: (1) source build in a discarded builder stage following `winnowmap-build` at `Dockerfile:20`; (2) bioconda following `install-clair3.sh`. Only if both fail does the amd64-only Polypolish route apply — and then `tools.delly()` must also return an explicit architecture note on arm64, as `tools.polypolish()` does at `tools.py:690`, because the generic "not found on PATH" reads as a broken install.

- [ ] **Step 3: Record the exact version output**

Run:

```bash
docker compose -p biopipe-delly-probe run --rm --no-deps api delly --version
```

Note the exact text and exit code. Several `tools.py` probes carry comments recording that a `--version` guess was wrong for their tool, so the probe below must match observed reality, not this plan's assumption. If `--version` exits non-zero or prints a usage block, adjust the probe's argument list and say so in its comment.

- [ ] **Step 4: Write the failing test**

Add to `backend/tests/pipelines/test_tools.py`:

```python
def test_delly_is_probed():
    """The image ships Delly installed, so this asserts the probe exists and
    names itself correctly rather than asserting availability -- per
    CLAUDE.md, the direction that fails when the seam breaks is the flip to
    unavailable, which the card test in Task 9 covers."""
    from app.pipelines import tools

    tools.delly.cache_clear()
    tool = tools.delly()
    assert tool.name == "delly"


def test_delly_is_in_the_tool_list():
    """A tool absent from _ALL_TOOLS never appears on /help/software."""
    from app.pipelines import tools

    names = {t.name for t in tools.all_tools()}
    assert "delly" in names
```

- [ ] **Step 5: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -k delly -v`
Expected: FAIL — `AttributeError: module 'app.pipelines.tools' has no attribute 'delly'`

- [ ] **Step 6: Add the setting**

In `backend/app/config.py`, beside `sniffles_path` at `:150`:

```python
    delly_path: str = "delly"
```

- [ ] **Step 7: Add the probe**

In `backend/app/pipelines/tools.py`, beside `sniffles()` at `:447`:

```python
@lru_cache(maxsize=1)
def delly() -> Tool:
    # Verify the argument and expected output against a real installed
    # binary before trusting this comment -- see Task 6 Step 3.
    return _probe("delly", settings.delly_path, ["--version"])
```

Add `delly(),` to the tool list at `:920`, beside `sniffles(),`. Add `delly.cache_clear()` beside `sniffles.cache_clear()` at `:2789`.

- [ ] **Step 8: Add the TOOL_META entry**

Beside the `"sniffles"` entry at `:1631`. **Every value below is verified against upstream on 2026-08-20 — copy verbatim, do not substitute recalled values:**

```python
    "delly": ToolMeta(
        pipelines=(PipelineType.STRUCTURAL_VARIANT,),
        one_liner="Structural variant caller for short reads",
        summary=(
            "Structural variant caller for short reads. Detects deletions, "
            "duplications, inversions, and translocations from paired-end "
            "and split-read signal -- the short-read counterpart to "
            "Sniffles2, which needs long reads."
        ),
        strengths=(
            "Detects SVs from Illumina paired-end data",
            "Integrates paired-end and split-read evidence for each call",
            "Single static binary on both amd64 and arm64",
            "Actively maintained, unlike Manta",
        ),
        homepage="https://github.com/dellytools/delly",
        repository="https://github.com/dellytools/delly",
        citation="Rausch et al., Bioinformatics 2012",
        citation_url="https://doi.org/10.1093/bioinformatics/bts378",
        # BSD-3-Clause, per the GitHub API's license field for
        # dellytools/delly and the repository's own LICENSE file.
        license="BSD-3-Clause",
        usage=(
            "The short-read structural variant caller: an SV job on "
            "short-read input runs `delly sr` against the BAM and its "
            "reference, then converts Delly's BCF output to VCF with "
            "bcftools. Long-read input goes to Sniffles2 instead. Unlike "
            "Sniffles2, Delly has no minimum call-size setting, so its "
            "output is reported as Delly produced it -- filter by length in "
            "the structural variants table instead."
        ),
    ),
```

- [ ] **Step 9: Run the documentation completeness test**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -v`
Expected: PASS, including `test_every_tool_is_documented` (requirement SV-620-2)

- [ ] **Step 10: Commit**

```bash
git add backend/Dockerfile backend/app/config.py backend/app/pipelines/tools.py backend/tests/pipelines/test_tools.py
git commit -m "feat(pipelines): install Delly 2.6.0 and document it"
```

---

### Task 7: The Delly runner

**Files:**
- Create: `backend/app/pipelines/delly_runner.py`
- Test: `backend/tests/pipelines/test_delly_runner.py`

**Interfaces:**
- Consumes: `app.errors.ValidationError`.
- Produces: `DellyParams` (dataclass, fields `threads: int = 4`, `min_map_quality: int = 1`, with `as_dict()` and `from_dict()`), `build_delly_command(*, delly_path, bam, reference, output, params) -> list[str]`, and `build_bcf_to_vcf_command(*, bcftools_path, bcf, output) -> list[str]`.

Requirement SV-620-11's command half. Pure functions over strings and paths, mirroring `sniffles_runner.py`, so the parts worth testing need no queue or filesystem.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_delly_runner.py`:

```python
"""Delly command construction. Pure functions, no queue, no filesystem."""

from pathlib import Path

import pytest

from app.errors import ValidationError
from app.pipelines.delly_runner import (
    DellyParams,
    build_bcf_to_vcf_command,
    build_delly_command,
)


class TestDellyParams:
    def test_defaults(self):
        params = DellyParams()
        assert params.threads == 4
        # Delly's own default for min. paired-end mapping quality, from
        # src/delly.h at v2.6.0.
        assert params.min_map_quality == 1

    def test_from_dict_rejects_zero_threads(self):
        with pytest.raises(ValidationError):
            DellyParams.from_dict({"threads": 0})

    def test_from_dict_rejects_negative_map_quality(self):
        with pytest.raises(ValidationError):
            DellyParams.from_dict({"min_map_quality": -1})

    def test_from_dict_accepts_zero_map_quality(self):
        """0 is meaningful to Delly: accept every mapping quality."""
        assert DellyParams.from_dict({"min_map_quality": 0}).min_map_quality == 0

    def test_round_trips_through_as_dict(self):
        params = DellyParams(threads=8, min_map_quality=20)
        assert DellyParams.from_dict(params.as_dict()) == params

    def test_has_no_minimum_sv_length(self):
        """Delly has no call-size floor flag -- its -m is minrefsep, which
        governs breakpoint clustering, not reported call size. Offering a
        min_sv_length here would be a wrong mapping that looks right."""
        assert not hasattr(DellyParams(), "min_sv_length")


class TestBuildDellyCommand:
    def _cmd(self, params=None):
        return build_delly_command(
            delly_path="delly",
            bam=Path("/w/in.bam"),
            reference=Path("/w/ref.fa"),
            output=Path("/w/out.bcf"),
            params=params or DellyParams(),
        )

    def test_uses_the_sr_subcommand(self):
        """Delly 2.x replaced `delly call` with `sr` (short-read) and `lr`.
        A `call` invocation targets a CLI that no longer exists."""
        cmd = self._cmd()
        assert cmd[0] == "delly"
        assert cmd[1] == "sr"

    def test_passes_reference_output_and_input(self):
        cmd = self._cmd()
        assert cmd[cmd.index("-g") + 1] == "/w/ref.fa"
        assert cmd[cmd.index("-o") + 1] == "/w/out.bcf"
        # The BAM is positional and last.
        assert cmd[-1] == "/w/in.bam"

    def test_passes_threads_and_map_quality(self):
        cmd = self._cmd(DellyParams(threads=8, min_map_quality=20))
        assert cmd[cmd.index("-h") + 1] == "8"
        assert cmd[cmd.index("-q") + 1] == "20"

    def test_never_uses_the_long_read_subcommand(self):
        """Sniffles2 is this pipeline's long-read caller. Requirement
        SV-620-4."""
        assert "lr" not in self._cmd()


class TestBcfConversion:
    def test_converts_to_bgzipped_vcf(self):
        cmd = build_bcf_to_vcf_command(
            bcftools_path="bcftools",
            bcf=Path("/w/out.bcf"),
            output=Path("/w/out.vcf.gz"),
        )
        assert cmd[:2] == ["bcftools", "view"]
        assert "/w/out.bcf" in cmd
        assert cmd[cmd.index("-o") + 1] == "/w/out.vcf.gz"
        # -O z is bgzipped VCF, which is what tabix can index.
        assert cmd[cmd.index("-O") + 1] == "z"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_delly_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.delly_runner'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/pipelines/delly_runner.py`:

```python
"""Building a short-read structural variant calling run with Delly.

Kept separate from the job handler so the parts worth testing -- command
construction and parameter validation -- are pure functions over strings and
paths, with no queue or filesystem involved. Mirrors `sniffles_runner.py`,
which splits the same way for the same reason.

Which caller runs for a given chemistry is `sv_caller.py`'s question, not
this module's.
"""

from dataclasses import dataclass
from pathlib import Path

from app.errors import ValidationError
from app.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class DellyParams:
    """User-facing knobs for a Delly run.

    Deliberately not a mirror of `SnifflesParams`. Delly has no minimum
    call-size flag: its `-m` is `minrefsep` (minimum reference separation,
    default 25), which governs breakpoint clustering rather than the size of
    calls it reports, so mapping Sniffles' 50 bp `--minsvlen` onto it would
    be a wrong mapping that looks right. Length filtering happens in the SV
    table instead, where the user can see it. Verified against src/delly.h at
    v2.6.0 on 2026-08-20.
    """

    threads: int = 4
    # Delly's own default for min. paired-end mapping quality (-q).
    min_map_quality: int = 1

    def as_dict(self) -> dict:
        return {
            "threads": self.threads,
            "min_map_quality": self.min_map_quality,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "DellyParams":
        raw = dict(raw or {})

        threads = int(raw.get("threads", 4))
        if threads < 1:
            raise ValidationError("threads must be at least 1")

        min_map_quality = int(raw.get("min_map_quality", 1))
        if min_map_quality < 0:
            raise ValidationError("min_map_quality cannot be negative")

        return cls(threads=threads, min_map_quality=min_map_quality)


def build_delly_command(
    *,
    delly_path: str,
    bam: Path,
    reference: Path,
    output: Path,
    params: DellyParams,
) -> list[str]:
    """Assemble the Delly invocation.

    `sr` is the short-read subcommand. Delly 2.x replaced the single `call`
    subcommand with `sr` and `lr`; a `delly call` invocation targets a CLI
    that no longer exists.

    `lr` is never used here. Sniffles2 is this pipeline's long-read caller --
    see `sv_caller.caller_for_chemistry`.

    Output is BCF (`-o`) rather than a VCF redirected from stdout. Delly
    supports both, and stdout is the worse choice: a crash mid-write leaves a
    truncated file that exists and is non-empty, which defeats the handler's
    "exited 0 but produced no VCF" check. `build_bcf_to_vcf_command` does the
    conversion.
    """
    return [
        delly_path,
        "sr",
        "-g",
        str(reference),
        "-o",
        str(output),
        "-q",
        str(params.min_map_quality),
        "-h",
        str(params.threads),
        str(bam),
    ]


def build_bcf_to_vcf_command(
    *,
    bcftools_path: str,
    bcf: Path,
    output: Path,
) -> list[str]:
    """Convert Delly's BCF output to the bgzipped VCF the rest of the SV
    pipeline expects.

    `-O z` is bgzipped VCF, which is what tabix indexes and what `sv_db`
    ingests. Teaching `sv_db` to read BCF directly was rejected: it would add
    a second ingest path into one table.
    """
    return [
        bcftools_path,
        "view",
        "-O",
        "z",
        "-o",
        str(output),
        str(bcf),
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_delly_runner.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/delly_runner.py backend/tests/pipelines/test_delly_runner.py
git commit -m "feat(pipelines): build Delly short-read SV commands"
```

---

### Task 8: Dispatch in the handler and the launch path

**Files:**
- Modify: `backend/app/queue/sv_handlers.py` (`_check_chemistry` at `:55`, `call_structural_variants` at `:86`)
- Modify: `backend/app/services/pipeline_service.py` (`launch_structural_variant_calling` around `:3740`)
- Test: `backend/tests/pipelines/test_sv_launch.py`

**Interfaces:**
- Consumes: `sv_caller.caller_for_chemistry` (Task 1), `delly_runner` (Task 7), `tools.delly()` (Task 6), `build_sv_db(..., caller=...)` (Task 5), `_sv_dedup_key(..., caller=...)` (Task 3).
- Produces: the handler's result dict now carries `"caller": <SvCaller value>`, which Task 4's `sv_provenance` reads.

- [ ] **Step 1: Write the failing test**

First, **replace** the existing `test_short_read_bam_is_refused_before_a_job_is_queued` at `backend/tests/pipelines/test_sv_launch.py:13`. Its assertion (`sv_calling_allowed_for(SHORT) is False`) is exactly what #620 inverts, and its docstring's point — that the gate belongs at launch, not only on the card — is still correct and must survive:

```python
def test_short_read_bam_reaches_delly_at_launch():
    """The gate belongs at launch, not only on the card.

    A card is a suggestion; the endpoint is reachable directly. Before #620
    this asserted short reads were refused outright. They are now routed to
    Delly instead -- but the launch path must still refuse a chemistry no
    caller covers, which `test_unknown_chemistry_is_refused_at_launch`
    below pins.
    """
    from app.pipelines import sv_caller

    assert sniffles_runner.sv_calling_allowed_for(ReadChemistry.SHORT) is True
    assert (
        sv_caller.caller_for_chemistry(ReadChemistry.SHORT)
        is sv_caller.SvCaller.DELLY
    )


def test_unknown_chemistry_is_refused_at_launch():
    """UNKNOWN means QC has not run. This is the gate the test above used
    to provide for short reads, and it must not be lost in the swap."""
    from app.pipelines import sv_caller

    assert sniffles_runner.sv_calling_allowed_for(ReadChemistry.UNKNOWN) is False
    assert sv_caller.caller_for_chemistry(ReadChemistry.UNKNOWN) is None
```

Then add the handler-result test. It asserts on the result dict `sv_provenance` consumes, following this file's existing `patch`/`AsyncMock` style:

```python
class TestCallerDispatch:
    """The handler must report which caller actually ran."""

    def test_delly_params_are_used_for_a_short_read_payload(self):
        """The handler picks the params class matching the caller. Parsing a
        Delly payload with SnifflesParams would silently accept
        `min_sv_length`, a knob Delly has no flag for."""
        from app.pipelines import delly_runner, sv_caller

        caller = sv_caller.caller_for_chemistry(ReadChemistry.SHORT)
        assert caller is sv_caller.SvCaller.DELLY

        params = delly_runner.DellyParams.from_dict({"threads": 8})
        assert params.threads == 8
        assert not hasattr(params, "min_sv_length")

    def test_provenance_round_trips_the_handler_result(self):
        """The contract between the handler's result dict and
        sv_provenance. Without the caller key, every Delly VCF is stamped as
        Sniffles output -- see Task 4."""
        from app.pipelines.sv_caller import SvCaller
        from app.queue.results import sv_provenance

        result = {"caller": SvCaller.DELLY.value, "tool_version": "2.6.0"}
        assert sv_provenance(result)["variants_called_by"] == "delly"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_sv_launch.py -k Dispatch -v`
Expected: FAIL on the first test if Task 1 is not yet merged; otherwise proceed.

- [ ] **Step 3: Rewrite the handler's caller-specific section**

In `backend/app/queue/sv_handlers.py`, add imports:

```python
from app.pipelines import delly_runner, sv_caller
```

Replace `_check_chemistry` so its error names what is wrong rather than asserting long reads:

```python
def _check_chemistry(chemistry) -> None:
    """Re-check the chemistry the launch path already validated.

    Not redundant, for the same reason `variant_handlers._check_chemistry`
    isn't: a payload outlives the check that built it. A job queued before a
    file was reclassified, or replayed by hand, arrives here without having
    passed through `launch_structural_variant_calling` in its current state.
    """
    if chemistry is None:
        return
    if sv_caller.caller_for_chemistry(chemistry) is None:
        raise PermanentError(
            f"Chemistry {chemistry.value!r} has no structural variant "
            f"caller. Run QC first if the platform is unknown."
        )
```

In `call_structural_variants`, replace the block from `tool = tools.require(tools.sniffles())` through the params line with a caller branch:

```python
    caller = sv_caller.caller_for_chemistry(
        chemistry or align_runner.ReadChemistry.UNKNOWN
    )
    if caller is None:
        raise PermanentError(
            "No structural variant caller covers this BAM's chemistry."
        )

    if caller is sv_caller.SvCaller.DELLY:
        tool = tools.require(tools.delly())
        params = delly_runner.DellyParams.from_dict(ctx.payload.get("params"))
    else:
        tool = tools.require(tools.sniffles())
        params = sniffles_runner.SnifflesParams.from_dict(
            ctx.payload.get("params")
        )
```

Add `align_runner` to the `app.pipelines` import line if it is not already there.

Replace the invocation block. The Sniffles branch keeps its existing body; the Delly branch writes BCF and converts:

```python
    if caller is sv_caller.SvCaller.DELLY:
        output_name = (
            ctx.payload.get("output_name")
            or f"{Path(bam_name).stem}.delly.vcf.gz"
        )
        vcf = out_dir / output_name
        bcf = out_dir / f"{Path(bam_name).stem}.delly.bcf"

        ctx.progress(phase="starting", pct=None, message="starting Delly")
        cmd = delly_runner.build_delly_command(
            delly_path=tool.path,
            bam=bam,
            reference=materialized.reference,
            output=bcf,
            params=params,
        )
        log.info("delly_started", job_id=ctx.job_id)

        code = run_subprocess(ctx, cmd, log_path=str(log_path))
        if code != 0:
            raise _failure(code, log_path, "delly")

        if not bcf.exists() or bcf.stat().st_size == 0:
            raise RetryableError("Delly exited 0 but produced no BCF")

        ctx.progress(phase="convert", pct=0.8, message="converting BCF to VCF")
        convert = delly_runner.build_bcf_to_vcf_command(
            bcftools_path=tools.require(tools.bcftools()).path,
            bcf=bcf,
            output=vcf,
        )
        code = run_subprocess(ctx, convert, log_path=str(log_path))
        if code != 0:
            raise _failure(code, log_path, "bcftools view")

        if not vcf.exists() or vcf.stat().st_size == 0:
            raise RetryableError("bcftools exited 0 but produced no VCF")
        snf = None
    else:
        # ... the existing Sniffles branch, unchanged, including its snf
        # sidecar and its own output_name default.
```

Pass the caller to the SV database build (Task 5) at the `build_sv_db` call, and add the caller to the result dict the handler returns:

```python
        "caller": caller.value,
```

Confirm the exact name of the bcftools probe before using it: run `grep -n "def bcftools" backend/app/pipelines/tools.py` and use what is there.

- [ ] **Step 4: Run the dispatch tests**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_sv_launch.py -v`
Expected: PASS

If `test_provenance_round_trips_the_handler_result` passes but the end-to-end check in Task 10 shows a Delly VCF stamped `sniffles2`, the handler is not writing the `caller` key into its result dict — that is the one link these unit tests cannot prove, since they construct the dict themselves.

- [ ] **Step 5: Update the launch path**

In `backend/app/services/pipeline_service.py`, in `launch_structural_variant_calling`:

Replace the chemistry guard's error message, which currently claims long reads are required:

```python
    caller = sv_caller.caller_for_chemistry(
        chemistry or align_runner.ReadChemistry.UNKNOWN
    )
    if caller is None:
        raise ValidationError(
            f"{bam.name!r} has no recognised sequencing platform "
            f"(chemistry={chemistry.value if chemistry else 'unknown'}). "
            f"Run QC on its reads first.",
            details={
                "bam_id": str(bam.id),
                "chemistry": chemistry.value if chemistry else None,
            },
        )
```

Replace `tools.require(tools.sniffles())` and the params line with the caller branch:

```python
    if caller is sv_caller.SvCaller.DELLY:
        tools.require(tools.delly())
        merged = delly_runner.DellyParams.from_dict(params)
    else:
        tools.require(tools.sniffles())
        merged = sniffles_runner.SnifflesParams.from_dict(params)
```

Update the run label and tool fields, which currently hardcode sniffles2:

```python
        label=f"{bam.name} → structural variants ({caller.value})",
        ...
        tool=caller.value,
```

The `dedup_key=` line already passes `caller=caller` from Task 3; remove the temporary `caller` definition Task 3 added, since this task defines it earlier and properly.

Add `delly_runner` to the `app.pipelines` import list.

- [ ] **Step 6: Run the SV suites**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_sv_launch.py tests/services/test_sv_merge_launch.py tests/queue/test_sv_results.py -v`
Expected: PASS. Existing tests asserting a short-read BAM is *refused* at launch will fail — that is the intended change; update them to assert a Delly launch.

- [ ] **Step 7: Commit**

```bash
git add backend/app/queue/sv_handlers.py backend/app/services/pipeline_service.py backend/tests/pipelines/test_sv_launch.py
git commit -m "feat(pipelines): dispatch SV calling to Delly for short-read BAMs"
```

---

### Task 9: The suggestion card

**Files:**
- Modify: `backend/app/services/suggestion_service.py:720-780` (`build_structural_variants_card`)
- Test: `backend/tests/services/test_suggestion_service.py:884-892` (replacing `test_short_read_reason_names_the_missing_capability`)

**Interfaces:**
- Consumes: `sv_caller.caller_for_chemistry` (Task 1), `tools.delly()` (Task 6).
- Produces: no new interface; the card's shape is unchanged.

Requirements SV-620-9 and SV-620-10. `build_merge_structural_variants_card` is **not** touched — Delly merge is out of scope.

- [ ] **Step 1: Write the failing test**

In `backend/tests/services/test_suggestion_service.py`, **replace** `test_short_read_reason_names_the_missing_capability` — whose docstring already says it is the seam #620 replaces — with:

```python
    def test_short_reads_are_offered_delly(self):
        """#619 left this card's SHORT branch saying a different tool was
        needed. #620 is that tool: the reason is replaced by an offer on the
        same card, not supplemented by a second SV card. Requirement
        SV-620-9."""
        card = build_structural_variants_card(
            _bam(), align_runner.ReadChemistry.SHORT
        )
        assert card.status is CardStatus.AVAILABLE
        assert card.launch is not None

    def test_short_read_card_is_unavailable_when_delly_is_missing(self):
        """The load-bearing direction. The image ships Delly installed, so
        asserting the card is *available* passes whether or not a patch
        worked -- only the flip to unavailable fails when the seam breaks.
        Requirement SV-620-10."""
        with patch(
            "app.services.suggestion_service.tools.delly",
            return_value=_FakeTool(False, name="delly"),
        ):
            card = build_structural_variants_card(
                _bam(), align_runner.ReadChemistry.SHORT
            )
        assert card.status is CardStatus.UNAVAILABLE
        assert "not installed" in card.reason
        assert card.launch is None

    def test_short_read_why_does_not_claim_parity_with_long_reads(self):
        """Paired-end and split-read signal detects SVs but resolves fewer
        of them than long reads do. A card that reads as equivalent
        misrepresents what the user gets."""
        card = build_structural_variants_card(
            _bam(), align_runner.ReadChemistry.SHORT
        )
        assert "long reads span breakpoints" not in (card.why or "").lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -k short_read -v`
Expected: FAIL — the card is UNAVAILABLE for SHORT

- [ ] **Step 3: Write minimal implementation**

In `backend/app/services/suggestion_service.py`, add `sv_caller` to the `app.pipelines` import list. Replace the block from the `sv_calling_allowed_for` check through the end of `build_structural_variants_card`:

```python
    caller = sv_caller.caller_for_chemistry(chemistry)
    if caller is None:
        return SuggestionCard(
            kind="structural_variants",
            category="VARIANTS",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason="Unknown sequencing platform for this BAM.",
        )

    if caller is sv_caller.SvCaller.DELLY:
        tool = tools.delly()
        why = (
            "Paired-end and split-read signal locates structural variants "
            "in short-read data. Fewer events resolve than with long reads, "
            "particularly insertions and repeats."
        )
    else:
        tool = tools.sniffles()
        why = (
            "Long reads span breakpoints, which is what makes structural "
            "variants resolvable."
        )

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
        why=why,
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/structural_variants",
            "body": {"bam_id": str(obj.id), "params": {}},
        },
    )
```

Note the `UNKNOWN`/`None` branch above this block already returns early; `caller_for_chemistry` returning `None` now covers the same ground, so the earlier explicit `UNKNOWN` check may be redundant — leave it, since it also handles `chemistry is None`, which is not a `ReadChemistry` member.

- [ ] **Step 4: Run the card suite**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -v`
Expected: PASS, including the untouched merge-card tests

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat(services): offer Delly on the SV card for short-read BAMs"
```

---

### Task 10: Full suite, lint, and end-to-end verification

**Files:**
- Modify: whatever the checks below turn up

Requirement SV-620-11 — the only check that exercises the install, the BCF conversion, and the ingest together. Per `CLAUDE.md`, unit tests over hand-built objects have been green here before while wrong about real files.

- [ ] **Step 1: Run the full suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS. Read the count, not the exit code.

- [ ] **Step 2: Lint the whole tree**

Run from the repo root:

```bash
ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e
```

Per `CLAUDE.md`, fix **every** error this reports, including pre-existing ones that predate this branch — not only the ones this work caused. Ruff is not a CI check here, so nothing downstream will catch what is skipped, and the same run that reports line length reports `F821` undefined names.

- [ ] **Step 3: Bring up the worktree stack**

Run: `./ops/worktree-up.sh`
Expected: UI on 5273, API on 8100.

- [ ] **Step 4: Run Delly end to end on a real short-read paired BAM**

Find a short-read project in the UI at localhost:5273, confirm the structural variants card is offered on an aligned BAM, and launch it. Then verify:

- The job completes.
- The VCF object exists and its provenance reads `variants_called_by: delly` — not `sniffles2` (Task 4).
- The SV table renders rows, and **the support column is populated** rather than blank (Task 5). A blank column here means the `PE`/`SR` extraction is not reaching real Delly output, which no unit test would catch.

- [ ] **Step 5: Confirm the long-read path still works**

Launch SV calling on a long-read BAM and confirm it still runs Sniffles2, produces its `.snf` sidecar, and that the merge card still appears for that sidecar. This is the regression direction for Tasks 2, 8, and 9.

- [ ] **Step 6: Bring the stack down**

Run: `./ops/worktree-up.sh --down`

Per `CLAUDE.md`, a stack left up wipes other test runs' databases and reads as flakiness in unrelated code.

- [ ] **Step 7: Close out the issue and commit**

Update issue #620 with what shipped. If any part of this plan was not implemented, say so explicitly there rather than leaving it implied.

```bash
git add -A
git commit -m "test(pipelines): verify Delly end to end on a short-read BAM"
```

---

## Follow-ups to file

- **Delly cross-sample merge.** After this lands, long-read SV callsets merge across samples and short-read ones do not. File as a `type:feature` issue with `area:pipelines`, noting it needs a second sidecar role, a merge handler over `delly merge`, and a per-sample genotyping round-trip.
- Already filed: [#699](https://github.com/syntheticgio/bioflow/issues/699), `_variant_dedup_key` dropping the caller its docstring promises.
