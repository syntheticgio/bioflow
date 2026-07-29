# Additional Aligners (bowtie2, HISAT2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bowtie2 and HISAT2 as fully selectable aligners, behind a registry that makes a fifth aligner a data change rather than a five-file edit, with a resource estimator that warns on tight configurations and blocks impossible ones.

**Architecture:** A new `aligner_registry.py` holds one `AlignerSpec` per aligner (probe, index layout, params class, UI field metadata, memory model). `aligners.py` gains an `IndexLayout` abstraction — `SuffixLayout` (existing bwa-mem2/minimap2 behavior) and `PrefixLayout` (bowtie2/HISAT2, whose tools take a basename via `-x` and build through a separate `*-build` binary). `AlignParams` splits into a shared base plus per-aligner subclasses. The dialog renders its parameter form from serialized registry metadata, and evaluates memory estimates client-side against coefficients fetched once per dialog open, with the authoritative check re-run in Python at launch.

**Tech Stack:** Python 3.12 / FastAPI / Beanie (MongoDB) / pytest on the backend; React + TypeScript + TanStack Query + Vite on the frontend; Docker Compose for the single running instance.

**Spec:** [docs/superpowers/specs/2026-07-29-additional-aligners-design.md](../specs/2026-07-29-additional-aligners-design.md)

---

## Critical environment notes

Read these before starting — they cause silent failures otherwise.

- **Run everything from the main repo root**, never from a worktree. Compose bind mounts are relative paths and the project name is pinned to `biopipe`, so `docker compose up` from a worktree silently repoints *the* stack at that branch.
- **`worker` does not hot-reload.** After any change to `backend/app/queue/*` or anything it imports, run `docker compose restart worker` before re-testing a job. Otherwise the job runs the old in-memory code and the fix appears not to work.
- **Run pytest inside the container**, not the host venv: `docker compose exec api python -m pytest tests/ -q`. The host venv hits Mongo replica-set connection errors.
- `api` (uvicorn --reload) and `web` (vite dev) do hot-reload; no restart needed for those.

## File structure

**Created:**
- `backend/app/pipelines/aligner_registry.py` — `AlignerSpec`, `ParamField`, `MemoryModel`, the registry dict, and lookup helpers. The single place an aligner is declared.
- `backend/app/pipelines/align_params.py` — `BaseAlignParams` and the per-aligner subclasses. Split from `align_runner.py` because that file is already 412 lines and command construction is a separate responsibility from parameter validation.
- `backend/app/pipelines/resource_estimator.py` — the memory formula, the three-band classification, and the envelope builder. Pure functions over numbers.
- `backend/tests/pipelines/test_aligner_registry.py`
- `backend/tests/pipelines/test_align_params.py`
- `backend/tests/pipelines/test_resource_estimator.py`
- `frontend/src/components/ToolDetailPane.tsx` — the right-hand pane of the redesigned selector.
- `frontend/src/components/AlignerParamFields.tsx` — renders inputs from registry field metadata.
- `frontend/src/lib/estimate.ts` — the client-side mirror of the band arithmetic.

**Modified:**
- `backend/app/pipelines/aligners.py` — add `IndexLayout`, `SuffixLayout`, `PrefixLayout`; two new `Aligner` members.
- `backend/app/pipelines/align_runner.py` — `_aligner_argv` dispatches through the registry; `AlignParams` re-exported from `align_params.py` for compatibility.
- `backend/app/pipelines/tools.py` — two probes, two `TOOL_META` entries, `one_liner` field.
- `backend/app/models/object.py` — two `SidecarRole` members.
- `backend/app/queue/align_handlers.py` — `_aligner_tool` via registry; builder-binary dispatch in `build_index`.
- `backend/app/queue/results.py` — two `_SIDECAR_ROLES` entries.
- `backend/app/services/pipeline_service.py` — the launch-time block check.
- `backend/app/api/v1/pipelines.py` — schema and envelope endpoints.
- `backend/app/config.py` — four tool paths.
- `backend/Dockerfile` — install bowtie2 and hisat2.
- `frontend/src/components/PipelineToolSelector.tsx` — list-and-detail layout.
- `frontend/src/components/AlignDialog.tsx` — generated fields, warning banner.
- `frontend/src/api/types.ts`, `frontend/src/api/client.ts` — new types and calls.
- `frontend/src/styles.css` (or the file holding `.tool-card-list`) — selector layout.

---

## Task 1: Add the two Aligner enum members and their sidecar roles

**Files:**
- Modify: `backend/app/pipelines/aligners.py:33-36`
- Modify: `backend/app/models/object.py:79-92`
- Test: `backend/tests/pipelines/test_aligners.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_aligners.py`:

```python
class TestNewAligners:
    def test_bowtie2_and_hisat2_are_aligners(self):
        assert Aligner.BOWTIE2.value == "bowtie2"
        assert Aligner.HISAT2.value == "hisat2"

    def test_every_aligner_has_an_index_role(self):
        """INDEX_ROLE is indexed by every member in reference_index_status,
        so a missing entry is a KeyError on an unrelated code path."""
        for aligner in Aligner:
            assert aligner in aligners.INDEX_ROLE

    def test_index_roles_are_distinct(self):
        """Two aligners sharing a role would make one reference's index
        satisfy the other's check, and the alignment would fail on a
        malformed index rather than a missing one."""
        roles = [aligners.INDEX_ROLE[a] for a in Aligner]
        assert len(set(roles)) == len(roles)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_aligners.py::TestNewAligners -v`
Expected: FAIL with `AttributeError: BOWTIE2`

- [ ] **Step 3: Add the enum members**

In `backend/app/pipelines/aligners.py`, extend the `Aligner` class:

```python
class Aligner(StrEnum):
    BWA_MEM2 = "bwa-mem2"
    MINIMAP2 = "minimap2"
    BOWTIE2 = "bowtie2"
    HISAT2 = "hisat2"
```

Add the suffix tuples below `MINIMAP2_SUFFIX`:

```python
# bowtie2 and HISAT2 both name their index files by appending a numbered
# suffix to a basename, and both are handed that basename via -x rather than
# a path to the reference. The counts differ: bowtie2 writes six files,
# HISAT2 eight.
#
# These lists are the tool's contract, and `build_index` fails loudly when a
# builder exits 0 without producing one of them -- see the verification step
# in Task 5, which builds a real index and compares.
BOWTIE2_SUFFIXES: tuple[str, ...] = (
    ".1.bt2", ".2.bt2", ".3.bt2", ".4.bt2", ".rev.1.bt2", ".rev.2.bt2",
)
HISAT2_SUFFIXES: tuple[str, ...] = (
    ".1.ht2", ".2.ht2", ".3.ht2", ".4.ht2",
    ".5.ht2", ".6.ht2", ".7.ht2", ".8.ht2",
)
```

Extend `INDEX_ROLE`:

```python
INDEX_ROLE: dict[Aligner, SidecarRole] = {
    Aligner.BWA_MEM2: SidecarRole.BWA_MEM2_INDEX,
    Aligner.MINIMAP2: SidecarRole.MINIMAP2_INDEX,
    Aligner.BOWTIE2: SidecarRole.BOWTIE2_INDEX,
    Aligner.HISAT2: SidecarRole.HISAT2_INDEX,
}
```

Extend `index_suffixes`:

```python
def index_suffixes(aligner: Aligner) -> tuple[str, ...]:
    """Every suffix an aligner's index is made of, relative to the reference."""
    if aligner is Aligner.BWA_MEM2:
        return BWA_MEM2_SUFFIXES
    if aligner is Aligner.BOWTIE2:
        return BOWTIE2_SUFFIXES
    if aligner is Aligner.HISAT2:
        return HISAT2_SUFFIXES
    return (MINIMAP2_SUFFIX,)
```

- [ ] **Step 4: Add the sidecar roles**

In `backend/app/models/object.py`, extend `SidecarRole`:

```python
class SidecarRole(StrEnum):
    BWA_MEM2_INDEX = "bwa-mem2-index"
    MINIMAP2_INDEX = "minimap2-index"
    BOWTIE2_INDEX = "bowtie2-index"
    HISAT2_INDEX = "hisat2-index"
    FAI = "fai"
    BAI = "bai"
    # The tabix index beside a bgzipped VCF -- to a VCF what BAI is to a BAM.
    TBI = "tbi"
```

- [ ] **Step 5: Register the roles for result application**

In `backend/app/queue/results.py`, find the `_SIDECAR_ROLES` dict and add the two new entries so `_apply_build_index` does not discard the produced files with an `unknown_sidecar_role` warning:

```python
    "bowtie2-index": SidecarRole.BOWTIE2_INDEX,
    "hisat2-index": SidecarRole.HISAT2_INDEX,
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/pipelines/test_aligners.py -v`
Expected: PASS, including the pre-existing tests

- [ ] **Step 7: Commit**

```bash
git add backend/app/pipelines/aligners.py backend/app/models/object.py backend/app/queue/results.py backend/tests/pipelines/test_aligners.py
git commit -m "feat: add bowtie2 and hisat2 aligner enum members and sidecar roles"
```

---

## Task 2: Add the IndexLayout abstraction

**Files:**
- Modify: `backend/app/pipelines/aligners.py`
- Test: `backend/tests/pipelines/test_aligners.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_aligners.py`:

```python
class TestIndexLayout:
    def test_suffix_layout_reference_argument_is_the_reference_path(self):
        """bwa-mem2 and minimap2 take the reference itself and find the index
        by appending. The path is what the tool is handed."""
        layout = aligners.layout_for(Aligner.BWA_MEM2)
        arg = layout.reference_argument(Path("/w/ref/genome.fna"))
        assert arg == "/w/ref/genome.fna"

    def test_prefix_layout_reference_argument_drops_nothing_from_the_name(self):
        """bowtie2 is handed a basename, and its index files are that basename
        plus a suffix. Since we name the index files after the *full*
        reference filename (genome.fna.1.bt2), the basename is the full path
        -- not the path with .fna stripped. Stripping it would make bowtie2
        look for genome.1.bt2, which does not exist."""
        layout = aligners.layout_for(Aligner.BOWTIE2)
        arg = layout.reference_argument(Path("/w/ref/genome.fna"))
        assert arg == "/w/ref/genome.fna"

    def test_prefix_layout_knows_its_builder_binary(self):
        assert aligners.layout_for(Aligner.BOWTIE2).builder == "bowtie2-build"
        assert aligners.layout_for(Aligner.HISAT2).builder == "hisat2-build"

    def test_suffix_layout_has_no_separate_builder(self):
        """bwa-mem2 indexes through a subcommand and minimap2 through a flag;
        neither has a separate builder binary."""
        assert aligners.layout_for(Aligner.MINIMAP2).builder is None

    def test_every_aligner_has_a_layout(self):
        for aligner in Aligner:
            assert aligners.layout_for(aligner) is not None

    def test_layout_accepts_its_own_sidecars(self):
        layout = aligners.layout_for(Aligner.BOWTIE2)
        assert layout.owns_sidecar("genome.fna", "genome.fna.1.bt2")

    def test_layout_rejects_a_foreign_sidecar(self):
        """The safety check that survives the refactor: an index attached to
        the wrong reference produces a plausible-looking wrong result rather
        than an error, so it must be dropped rather than renamed."""
        layout = aligners.layout_for(Aligner.BOWTIE2)
        assert not layout.owns_sidecar("genome.fna", "other.fna.1.bt2")
```

Add `from pathlib import Path` to the test file's imports if not already present.

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_aligners.py::TestIndexLayout -v`
Expected: FAIL with `AttributeError: module 'app.pipelines.aligners' has no attribute 'layout_for'`

- [ ] **Step 3: Implement the layouts**

Add to `backend/app/pipelines/aligners.py`, after `index_filenames`:

```python
@dataclass(frozen=True)
class IndexLayout:
    """How one aligner's index is shaped on disk, and how it is named.

    Three shapes exist in the wild and this application will eventually need
    all three:

    - suffix: files named by appending to the reference path, discovered by
      the tool (bwa-mem2, minimap2)
    - prefix: files named by appending to a basename that is passed to the
      tool explicitly via -x (bowtie2, HISAT2)
    - directory: a fixed set of names inside a directory passed via a flag
      (STAR) -- specified in the design, not implemented yet

    The distinction that matters for correctness is `owns_sidecar`. Dropping a
    sidecar that does not belong to a reference is what stops an index built
    for one genome from being silently materialized beside another; the
    resulting run would produce a plausible-looking wrong answer rather than
    an error.
    """

    suffixes: tuple[str, ...]
    # The separate binary that builds this index, when there is one. bwa-mem2
    # uses `bwa-mem2 index` and minimap2 uses `minimap2 -d`, so both are None.
    builder: str | None = None

    def filenames(self, reference_name: str) -> tuple[str, ...]:
        return tuple(f"{reference_name}{s}" for s in self.suffixes)

    def reference_argument(self, reference: Path) -> str:
        """What to hand the aligner to locate the index.

        The same string for both shapes today, and deliberately so: index
        files are named after the *full* reference filename
        (`genome.fna.1.bt2`), so bowtie2's basename is the full reference
        path. Stripping the extension to form a basename would make the tool
        look for `genome.1.bt2` and find nothing. This method exists so that
        assumption is stated in one place rather than assumed at each call
        site, and so a future layout can differ.
        """
        return str(reference)

    def owns_sidecar(self, reference_name: str, sidecar_name: str) -> bool:
        return Path(sidecar_name).name.startswith(reference_name)


_LAYOUTS: dict[Aligner, IndexLayout] = {
    Aligner.BWA_MEM2: IndexLayout(suffixes=BWA_MEM2_SUFFIXES),
    Aligner.MINIMAP2: IndexLayout(suffixes=(MINIMAP2_SUFFIX,)),
    Aligner.BOWTIE2: IndexLayout(suffixes=BOWTIE2_SUFFIXES, builder="bowtie2-build"),
    Aligner.HISAT2: IndexLayout(suffixes=HISAT2_SUFFIXES, builder="hisat2-build"),
}


def layout_for(aligner: Aligner) -> IndexLayout:
    return _LAYOUTS[aligner]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/pipelines/test_aligners.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/aligners.py backend/tests/pipelines/test_aligners.py
git commit -m "feat: add IndexLayout abstraction for suffix and prefix index shapes"
```

---

## Task 3: Split AlignParams into a base plus per-aligner subclasses

**Files:**
- Create: `backend/app/pipelines/align_params.py`
- Modify: `backend/app/pipelines/align_runner.py:141-193`
- Test: `backend/tests/pipelines/test_align_params.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_align_params.py`:

```python
"""Per-aligner parameter validation.

The property worth testing is that a knob belonging to one tool cannot be set
on another. A silently ignored parameter is the failure mode that matters
here: the run completes, the recorded provenance says one thing, and the
command that actually ran said another.
"""

import pytest

from app.errors import ValidationError
from app.pipelines import align_params
from app.pipelines.aligners import Aligner


class TestDispatch:
    def test_from_dict_returns_the_class_for_the_named_aligner(self):
        p = align_params.from_dict({"aligner": "bowtie2"})
        assert isinstance(p, align_params.Bowtie2Params)

    def test_hisat2_gets_its_own_class(self):
        p = align_params.from_dict({"aligner": "hisat2"})
        assert isinstance(p, align_params.Hisat2Params)

    def test_minimap2_still_carries_its_preset(self):
        p = align_params.from_dict({"aligner": "minimap2", "preset": "map-ont"})
        assert p.preset == "map-ont"

    def test_an_unknown_aligner_is_rejected(self):
        with pytest.raises(ValueError):
            align_params.from_dict({"aligner": "not-a-real-aligner"})


class TestSharedValidation:
    def test_threads_must_be_at_least_one(self):
        with pytest.raises(ValidationError):
            align_params.from_dict({"aligner": "bowtie2", "threads": 0})

    def test_sort_memory_has_a_floor(self):
        """Below this samtools spills to disk, which is slower than the
        memory saved is worth."""
        with pytest.raises(ValidationError):
            align_params.from_dict({"aligner": "bowtie2", "sort_memory_mb": 32})


class TestBowtie2:
    def test_sensitivity_defaults_to_sensitive(self):
        p = align_params.from_dict({"aligner": "bowtie2"})
        assert p.sensitivity == "--sensitive"

    def test_an_unknown_sensitivity_is_rejected(self):
        with pytest.raises(ValidationError):
            align_params.from_dict(
                {"aligner": "bowtie2", "sensitivity": "--extremely-sensitive"}
            )

    def test_maxins_is_carried(self):
        p = align_params.from_dict({"aligner": "bowtie2", "maxins": 800})
        assert p.maxins == 800

    def test_maxins_must_be_positive(self):
        with pytest.raises(ValidationError):
            align_params.from_dict({"aligner": "bowtie2", "maxins": 0})


class TestHisat2:
    def test_rna_strandness_defaults_to_unstranded(self):
        p = align_params.from_dict({"aligner": "hisat2"})
        assert p.rna_strandness == ""

    def test_rna_strandness_accepts_the_documented_values(self):
        for value in ("FR", "RF", "F", "R", ""):
            p = align_params.from_dict(
                {"aligner": "hisat2", "rna_strandness": value}
            )
            assert p.rna_strandness == value

    def test_an_unknown_strandness_is_rejected(self):
        """A wrong value here silently breaks downstream strand-specific
        counting rather than failing, so it must not reach the command."""
        with pytest.raises(ValidationError):
            align_params.from_dict({"aligner": "hisat2", "rna_strandness": "XY"})


class TestRoundTrip:
    def test_as_dict_round_trips_through_from_dict(self):
        """Params are persisted on the run record and read back when a run is
        inspected, so the two directions have to agree."""
        original = align_params.from_dict(
            {"aligner": "bowtie2", "threads": 8, "maxins": 700, "local": True}
        )
        restored = align_params.from_dict(original.as_dict())
        assert restored == original
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_align_params.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.pipelines.align_params'`

- [ ] **Step 3: Implement the parameter classes**

Create `backend/app/pipelines/align_params.py`:

```python
"""Per-aligner parameter sets.

Split from `align_runner` because parameter validation and command
construction are separate responsibilities, and because a single flat class
covering every aligner would be a union of about thirty fields, most of them
inapplicable to whichever tool is actually running. A field that exists but
does nothing is worse than one that is absent: it reaches the run's recorded
parameters and implies the tool honored it.

`from_dict` dispatches on the `aligner` key. Every subclass validates only
its own knobs, so an unknown key for the wrong tool is rejected at launch
rather than silently dropped.
"""

from dataclasses import dataclass

from app.errors import ValidationError
from app.pipelines.aligners import Aligner

# samtools spills to disk below this, which is slower than the memory saved.
MIN_SORT_MEMORY_MB = 64


@dataclass
class BaseAlignParams:
    """The knobs every aligner in this application shares.

    samtools does the sorting for all of them, which is why sort memory is
    here rather than per-tool. Threads is shared because every tool takes a
    thread count, even though the flag differs (-t, -p).
    """

    aligner: Aligner
    threads: int = 4
    sort_memory_mb: int = 1024
    mark_duplicates: bool = False

    def as_dict(self) -> dict:
        return {
            "aligner": self.aligner.value,
            "threads": self.threads,
            "sort_memory_mb": self.sort_memory_mb,
            "mark_duplicates": self.mark_duplicates,
        }

    @staticmethod
    def _shared(data: dict) -> dict:
        threads = int(data.get("threads", 4))
        if threads < 1:
            raise ValidationError("threads must be at least 1")

        sort_memory_mb = int(data.get("sort_memory_mb", 1024))
        if sort_memory_mb < MIN_SORT_MEMORY_MB:
            raise ValidationError(
                f"sort_memory_mb must be at least {MIN_SORT_MEMORY_MB}"
            )

        return {
            "threads": threads,
            "sort_memory_mb": sort_memory_mb,
            "mark_duplicates": bool(data.get("mark_duplicates", False)),
        }


@dataclass
class Bwa2Params(BaseAlignParams):
    aligner: Aligner = Aligner.BWA_MEM2

    @classmethod
    def from_dict(cls, data: dict) -> "Bwa2Params":
        return cls(aligner=Aligner.BWA_MEM2, **cls._shared(data))


# minimap2 presets. Not cosmetic: the wrong preset for long reads produces
# silently poor alignments rather than an error.
MINIMAP2_PRESETS: tuple[str, ...] = ("map-ont", "map-pb", "map-hifi", "lr:hq", "sr")


@dataclass
class Minimap2Params(BaseAlignParams):
    aligner: Aligner = Aligner.MINIMAP2
    preset: str = "sr"

    def as_dict(self) -> dict:
        return {**super().as_dict(), "preset": self.preset}

    @classmethod
    def from_dict(cls, data: dict) -> "Minimap2Params":
        preset = data.get("preset") or "sr"
        if preset not in MINIMAP2_PRESETS:
            raise ValidationError(
                f"Unknown minimap2 preset {preset!r}",
                details={"valid": list(MINIMAP2_PRESETS)},
            )
        return cls(aligner=Aligner.MINIMAP2, preset=preset, **cls._shared(data))


BOWTIE2_SENSITIVITIES: tuple[str, ...] = (
    "--very-fast", "--fast", "--sensitive", "--very-sensitive",
)


@dataclass
class Bowtie2Params(BaseAlignParams):
    aligner: Aligner = Aligner.BOWTIE2
    sensitivity: str = "--sensitive"
    # End-to-end requires the whole read to align; --local soft-clips the
    # ends. Local is the right choice when reads carry adapter remnants or
    # when the reference is a partial assembly.
    local: bool = False
    # The insert-size ceiling. A pair whose implied fragment exceeds this is
    # not called properly-paired, which is why it matters for ChIP-seq, where
    # fragment length is the experimental variable.
    maxins: int = 500
    no_mixed: bool = False
    no_discordant: bool = False
    # Report up to N alignments per read rather than the single best. 0 means
    # "leave the flag off", which is bowtie2's default behavior.
    report_k: int = 0

    def as_dict(self) -> dict:
        return {
            **super().as_dict(),
            "sensitivity": self.sensitivity,
            "local": self.local,
            "maxins": self.maxins,
            "no_mixed": self.no_mixed,
            "no_discordant": self.no_discordant,
            "report_k": self.report_k,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Bowtie2Params":
        sensitivity = data.get("sensitivity") or "--sensitive"
        if sensitivity not in BOWTIE2_SENSITIVITIES:
            raise ValidationError(
                f"Unknown bowtie2 sensitivity {sensitivity!r}",
                details={"valid": list(BOWTIE2_SENSITIVITIES)},
            )

        maxins = int(data.get("maxins", 500))
        if maxins < 1:
            raise ValidationError("maxins must be at least 1")

        report_k = int(data.get("report_k", 0))
        if report_k < 0:
            raise ValidationError("report_k cannot be negative")

        return cls(
            aligner=Aligner.BOWTIE2,
            sensitivity=sensitivity,
            local=bool(data.get("local", False)),
            maxins=maxins,
            no_mixed=bool(data.get("no_mixed", False)),
            no_discordant=bool(data.get("no_discordant", False)),
            report_k=report_k,
            **cls._shared(data),
        )


# "" means unstranded, which is HISAT2's default (the flag is omitted).
HISAT2_STRANDNESS: tuple[str, ...] = ("", "FR", "RF", "F", "R")


@dataclass
class Hisat2Params(BaseAlignParams):
    aligner: Aligner = Aligner.HISAT2
    # FR/RF for paired libraries, F/R for single. The wrong value does not
    # fail -- it silently reverses which strand a read is attributed to, and
    # the error only surfaces as nonsense in downstream counting.
    rna_strandness: str = ""
    max_intronlen: int = 500000
    # For DNA input: HISAT2 is splice-aware by default, and spliced alignment
    # over genomic DNA invents junctions that are not there.
    no_spliced_alignment: bool = False
    # Formats output for downstream transcript assembly (StringTie et al).
    dta: bool = False
    report_k: int = 0

    def as_dict(self) -> dict:
        return {
            **super().as_dict(),
            "rna_strandness": self.rna_strandness,
            "max_intronlen": self.max_intronlen,
            "no_spliced_alignment": self.no_spliced_alignment,
            "dta": self.dta,
            "report_k": self.report_k,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Hisat2Params":
        strandness = data.get("rna_strandness") or ""
        if strandness not in HISAT2_STRANDNESS:
            raise ValidationError(
                f"Unknown rna_strandness {strandness!r}",
                details={"valid": list(HISAT2_STRANDNESS)},
            )

        max_intronlen = int(data.get("max_intronlen", 500000))
        if max_intronlen < 1:
            raise ValidationError("max_intronlen must be at least 1")

        report_k = int(data.get("report_k", 0))
        if report_k < 0:
            raise ValidationError("report_k cannot be negative")

        return cls(
            aligner=Aligner.HISAT2,
            rna_strandness=strandness,
            max_intronlen=max_intronlen,
            no_spliced_alignment=bool(data.get("no_spliced_alignment", False)),
            dta=bool(data.get("dta", False)),
            report_k=report_k,
            **cls._shared(data),
        )


PARAMS_CLASSES: dict[Aligner, type[BaseAlignParams]] = {
    Aligner.BWA_MEM2: Bwa2Params,
    Aligner.MINIMAP2: Minimap2Params,
    Aligner.BOWTIE2: Bowtie2Params,
    Aligner.HISAT2: Hisat2Params,
}


def from_dict(data: dict | None) -> BaseAlignParams:
    """Build the parameter set for whichever aligner the payload names.

    `Aligner(...)` raises ValueError on an unknown name, which is the right
    failure: an aligner this application has no spec for has no command
    builder either, and defaulting would run a tool the user did not choose.
    """
    data = dict(data or {})
    aligner = Aligner(data.get("aligner", Aligner.MINIMAP2))
    return PARAMS_CLASSES[aligner].from_dict(data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/pipelines/test_align_params.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Keep the old import path working**

`align_runner.AlignParams` is imported by `align_handlers.py`, `pipeline_service.py`, and the existing tests. Rather than editing every call site now, re-export from `align_runner.py`. Replace the `AlignParams` dataclass in `backend/app/pipelines/align_runner.py` (lines 141-188, the whole `@dataclass class AlignParams` block) with:

```python
# Parameter classes moved to align_params.py when the second and third
# aligners arrived: one flat class covering four tools would be a union of
# mostly-inapplicable fields. Re-exported here because this is where every
# existing call site imports them from.
from app.pipelines.align_params import (  # noqa: E402
    BaseAlignParams,
    Bowtie2Params,
    Bwa2Params,
    Hisat2Params,
    Minimap2Params,
)
from app.pipelines.align_params import from_dict as _params_from_dict

AlignParams = BaseAlignParams
```

Move that import block to the top of the file with the other imports, and delete the now-unused `default_preset` body if nothing references it — check first:

```bash
grep -rn "default_preset" backend/app backend/tests
```

If it is still referenced, leave it in place; it is superseded in Task 4 but not yet dead.

- [ ] **Step 6: Run the full backend suite**

Run: `docker compose exec api python -m pytest tests/ -q`
Expected: PASS. If `test_align_runner.py` fails on `AlignParams()` being constructed with no arguments, that is expected — `BaseAlignParams` requires `aligner`. Fix those call sites by passing `aligner=Aligner.MINIMAP2` explicitly, which is what they were relying on as a default.

- [ ] **Step 7: Commit**

```bash
git add backend/app/pipelines/align_params.py backend/app/pipelines/align_runner.py backend/tests/
git commit -m "refactor: split AlignParams into per-aligner subclasses"
```

---

## Task 4: Build the aligner registry

**Files:**
- Create: `backend/app/pipelines/aligner_registry.py`
- Test: `backend/tests/pipelines/test_aligner_registry.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_aligner_registry.py`:

```python
"""The registry is the contract between the backend and the dialog.

The tests that matter are the completeness ones: every aligner must have a
spec, and every spec's fields must match the parameter class it names. A
field the form renders but the params class rejects is a dialog the user
cannot submit, and it would not be caught by any per-tool test.
"""

import pytest

from app.pipelines import align_params, aligner_registry
from app.pipelines.aligners import Aligner


class TestCompleteness:
    def test_every_aligner_has_a_spec(self):
        for aligner in Aligner:
            assert aligner_registry.spec_for(aligner) is not None

    def test_every_spec_names_its_own_aligner(self):
        for aligner in Aligner:
            assert aligner_registry.spec_for(aligner).aligner is aligner

    def test_every_spec_has_a_memory_model(self):
        for aligner in Aligner:
            model = aligner_registry.spec_for(aligner).memory_model
            assert model.fixed_overhead_mb > 0
            assert model.index_bytes_per_ref_base > 0


class TestFieldMetadataMatchesParams:
    def test_every_field_key_is_accepted_by_the_params_class(self):
        """A field the form renders that the params class does not accept is
        a form the user cannot submit."""
        for aligner in Aligner:
            spec = aligner_registry.spec_for(aligner)
            payload = {"aligner": aligner.value}
            for f in spec.fields:
                payload[f.key] = f.default
            params = align_params.from_dict(payload)
            for f in spec.fields:
                assert hasattr(params, f.key), (
                    f"{aligner.value} field {f.key!r} has no params attribute"
                )

    def test_select_fields_declare_choices(self):
        for aligner in Aligner:
            for f in aligner_registry.spec_for(aligner).fields:
                if f.kind == "select":
                    assert f.choices, f"{f.key} is a select with no choices"

    def test_select_defaults_are_among_their_choices(self):
        for aligner in Aligner:
            for f in aligner_registry.spec_for(aligner).fields:
                if f.kind == "select":
                    values = [c.value for c in f.choices]
                    assert f.default in values

    def test_every_field_has_help_text(self):
        """The help line is the only explanation a generated form carries,
        so an empty one is a knob with no stated meaning."""
        for aligner in Aligner:
            for f in aligner_registry.spec_for(aligner).fields:
                assert f.help.strip(), f"{aligner.value}.{f.key} has no help"


class TestSerialization:
    def test_schema_is_json_serializable(self):
        """It is served straight to the dialog, so anything not JSON-native
        breaks the endpoint rather than the test that built it."""
        import json

        schema = aligner_registry.schema_for(Aligner.BOWTIE2)
        json.dumps(schema)

    def test_schema_carries_the_field_groups(self):
        schema = aligner_registry.schema_for(Aligner.BOWTIE2)
        groups = {f["group"] for f in schema["fields"]}
        assert "performance" in groups
        assert "biology" in groups
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_aligner_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.pipelines.aligner_registry'`

- [ ] **Step 3: Implement the registry**

Create `backend/app/pipelines/aligner_registry.py`:

```python
"""One spec per aligner: the single place an aligner is declared.

Before this existed, adding an aligner meant five coordinated edits --
index shape in `aligners`, command construction and defaults in
`align_runner`, tool resolution in `align_handlers`, probe and description in
`tools`, and a hand-written block in the dialog. Nothing said what an aligner
*was*, so the answer was "whatever those five files agree on", and they only
agree until someone edits four of them.

The field metadata here is also what the dialog renders its parameter form
from, so a knob is added in one place rather than two. That is the same
reasoning TOOL_META already follows: a second copy of tool descriptions in
the frontend is the copy nobody updates.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.pipelines import align_params, aligners, tools
from app.pipelines.aligners import Aligner, IndexLayout


@dataclass(frozen=True)
class Choice:
    value: str
    label: str


@dataclass(frozen=True)
class ParamField:
    """One input in the generated parameter form.

    `group` is what keeps a generated form from becoming an undifferentiated
    pile of inputs: biology fields render in the dialog body, performance
    fields under the advanced disclosure -- which is roughly how AlignDialog
    was already organized by hand.
    """

    key: str
    label: str
    kind: Literal["int", "bool", "select", "text"]
    default: Any
    help: str
    group: Literal["biology", "performance"] = "biology"
    min: int | None = None
    max: int | None = None
    choices: tuple[Choice, ...] = ()


@dataclass(frozen=True)
class MemoryModel:
    """Coefficients for estimating a run's peak memory.

    Heuristics from published tool documentation, not measurements on this
    hardware. They will be roughly right and occasionally wrong, which is why
    the estimator's block band is set at genuinely-impossible rather than
    merely-tight: a bad coefficient should cost a spurious warning, never a
    blocked run that would have worked.
    """

    # The dominant term for every aligner: index size scales with the
    # reference, and the whole index is resident during alignment.
    index_bytes_per_ref_base: float
    fixed_overhead_mb: int
    bytes_per_thread_mb: int
    # Building an index costs more than loading one. STAR's is the extreme
    # case (roughly 10x), which is the reason this field exists now.
    index_build_multiplier: float = 1.0


@dataclass(frozen=True)
class AlignerSpec:
    aligner: Aligner
    tool: Callable[[], tools.Tool]
    index: IndexLayout
    params_class: type[align_params.BaseAlignParams]
    memory_model: MemoryModel
    fields: tuple[ParamField, ...] = ()


# Threads and sort memory are on every aligner, so they are declared once and
# spliced into each spec rather than repeated four times.
_SHARED_FIELDS: tuple[ParamField, ...] = (
    ParamField(
        key="threads",
        label="Threads",
        kind="int",
        default=4,
        min=1,
        max=64,
        group="performance",
        help="More threads finish sooner but compete with other work.",
    ),
    ParamField(
        key="sort_memory_mb",
        label="Sort memory (MB per thread)",
        kind="int",
        default=1024,
        min=64,
        group="performance",
        help=(
            "Per thread, not total -- 8 threads at 1024 MB is 8 GB. samtools "
            "spills to disk when it runs out, which is slower."
        ),
    ),
    ParamField(
        key="mark_duplicates",
        label="Mark duplicates",
        kind="bool",
        default=False,
        group="biology",
        help=(
            "Standard for DNA-seq variant calling. Wrong for RNA-seq and "
            "amplicon data, where duplicates are expected."
        ),
    ),
)


REGISTRY: dict[Aligner, AlignerSpec] = {
    Aligner.BWA_MEM2: AlignerSpec(
        aligner=Aligner.BWA_MEM2,
        tool=tools.bwa_mem2,
        index=aligners.layout_for(Aligner.BWA_MEM2),
        params_class=align_params.Bwa2Params,
        # ~2 bytes/base: about 6 GB for a 3.1 Gb human genome, which matches
        # the figure bwa-mem2's own README gives for its index.
        memory_model=MemoryModel(
            index_bytes_per_ref_base=2.0,
            fixed_overhead_mb=512,
            bytes_per_thread_mb=256,
            index_build_multiplier=2.0,
        ),
        fields=_SHARED_FIELDS,
    ),
    Aligner.MINIMAP2: AlignerSpec(
        aligner=Aligner.MINIMAP2,
        tool=tools.minimap2,
        index=aligners.layout_for(Aligner.MINIMAP2),
        params_class=align_params.Minimap2Params,
        memory_model=MemoryModel(
            index_bytes_per_ref_base=1.5,
            fixed_overhead_mb=512,
            bytes_per_thread_mb=512,
            index_build_multiplier=1.5,
        ),
        fields=(
            ParamField(
                key="preset",
                label="Read type",
                kind="select",
                default="sr",
                group="biology",
                help="The wrong choice aligns long reads poorly rather than failing.",
                choices=(
                    Choice("sr", "Short read (Illumina)"),
                    Choice("map-ont", "Oxford Nanopore"),
                    Choice("map-pb", "PacBio (CLR)"),
                    Choice("map-hifi", "PacBio (HiFi/CCS)"),
                    Choice("lr:hq", "Oxford Nanopore (duplex / Q20+)"),
                ),
            ),
            *_SHARED_FIELDS,
        ),
    ),
    Aligner.BOWTIE2: AlignerSpec(
        aligner=Aligner.BOWTIE2,
        tool=tools.bowtie2,
        index=aligners.layout_for(Aligner.BOWTIE2),
        params_class=align_params.Bowtie2Params,
        # ~1 byte/base: about 3.5 GB for human, matching the published size of
        # the prebuilt GRCh38 bowtie2 index.
        memory_model=MemoryModel(
            index_bytes_per_ref_base=1.0,
            fixed_overhead_mb=256,
            bytes_per_thread_mb=200,
            index_build_multiplier=3.0,
        ),
        fields=(
            ParamField(
                key="sensitivity",
                label="Sensitivity",
                kind="select",
                default="--sensitive",
                group="biology",
                help=(
                    "More sensitive settings find more alignments in divergent "
                    "or repetitive regions, and take proportionally longer."
                ),
                choices=(
                    Choice("--very-fast", "Very fast"),
                    Choice("--fast", "Fast"),
                    Choice("--sensitive", "Sensitive (default)"),
                    Choice("--very-sensitive", "Very sensitive"),
                ),
            ),
            ParamField(
                key="local",
                label="Local alignment (soft-clip read ends)",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "End-to-end requires the whole read to align. Local "
                    "soft-clips the ends, which suits reads with adapter "
                    "remnants or a partial reference."
                ),
            ),
            ParamField(
                key="maxins",
                label="Maximum insert size",
                kind="int",
                default=500,
                min=1,
                group="biology",
                help=(
                    "Pairs implying a longer fragment are not counted as "
                    "properly paired. Raise it for ChIP-seq or any library "
                    "with long fragments."
                ),
            ),
            ParamField(
                key="no_mixed",
                label="Suppress unpaired alignments for pairs",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "By default bowtie2 falls back to aligning each mate "
                    "alone when the pair does not align. This forbids that."
                ),
            ),
            ParamField(
                key="no_discordant",
                label="Suppress discordant alignments",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "Discordant pairs align uniquely but not with the "
                    "expected orientation or spacing. Structural variant work "
                    "wants them; most other analyses do not."
                ),
            ),
            ParamField(
                key="report_k",
                label="Report up to N alignments (0 = best only)",
                kind="int",
                default=0,
                min=0,
                group="biology",
                help=(
                    "Reporting multiple alignments per read grows the BAM and "
                    "changes what downstream counting sees. Leave at 0 unless "
                    "a specific analysis needs multi-mapping reads."
                ),
            ),
            *_SHARED_FIELDS,
        ),
    ),
    Aligner.HISAT2: AlignerSpec(
        aligner=Aligner.HISAT2,
        tool=tools.hisat2,
        index=aligners.layout_for(Aligner.HISAT2),
        params_class=align_params.Hisat2Params,
        # HISAT2's graph FM index is notably compact -- about 4 GB for human
        # including the transcript index.
        memory_model=MemoryModel(
            index_bytes_per_ref_base=1.3,
            fixed_overhead_mb=256,
            bytes_per_thread_mb=200,
            index_build_multiplier=4.0,
        ),
        fields=(
            ParamField(
                key="rna_strandness",
                label="RNA strandness",
                kind="select",
                default="",
                group="biology",
                help=(
                    "A wrong value does not fail -- it reverses which strand "
                    "a read is attributed to, and only shows up as nonsense "
                    "in downstream counting. FR is the usual dUTP protocol."
                ),
                choices=(
                    Choice("", "Unstranded"),
                    Choice("FR", "FR (paired, forward)"),
                    Choice("RF", "RF (paired, reverse / dUTP)"),
                    Choice("F", "F (single, forward)"),
                    Choice("R", "R (single, reverse)"),
                ),
            ),
            ParamField(
                key="no_spliced_alignment",
                label="Disable spliced alignment (DNA input)",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "HISAT2 is splice-aware by default. Over genomic DNA that "
                    "invents junctions that are not there, so turn it off for "
                    "non-RNA input."
                ),
            ),
            ParamField(
                key="max_intronlen",
                label="Maximum intron length",
                kind="int",
                default=500000,
                min=1,
                group="biology",
                help=(
                    "Caps how far a spliced alignment may span. The default "
                    "suits mammalian genomes; compact genomes want far less."
                ),
            ),
            ParamField(
                key="dta",
                label="Format for transcript assembly",
                kind="bool",
                default=False,
                group="biology",
                help=(
                    "Tailors the output for downstream transcript assemblers "
                    "such as StringTie. Harmless otherwise, but only useful "
                    "if that is the next step."
                ),
            ),
            ParamField(
                key="report_k",
                label="Report up to N alignments (0 = default)",
                kind="int",
                default=0,
                min=0,
                group="biology",
                help=(
                    "Reporting multiple alignments per read grows the BAM and "
                    "changes what downstream counting sees."
                ),
            ),
            *_SHARED_FIELDS,
        ),
    ),
}


def spec_for(aligner: Aligner) -> AlignerSpec:
    return REGISTRY[aligner]


def schema_for(aligner: Aligner) -> dict:
    """The field list, as JSON for the dialog.

    `asdict` on each field rather than a hand-written projection: a field
    added to ParamField should reach the form without a second edit here.
    """
    spec = spec_for(aligner)
    return {
        "aligner": aligner.value,
        "fields": [asdict(f) for f in spec.fields],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/pipelines/test_aligner_registry.py -v`
Expected: PASS. `tools.bowtie2` and `tools.hisat2` do not exist yet — this will fail with `AttributeError` until Task 5. If so, complete Task 5 first and return here; the two tasks are order-independent otherwise.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/aligner_registry.py backend/tests/pipelines/test_aligner_registry.py
git commit -m "feat: add aligner registry with per-tool params, fields, and memory models"
```

---

## Task 5: Install bowtie2 and HISAT2 and probe them

**Files:**
- Modify: `backend/Dockerfile:46-61`
- Modify: `backend/app/config.py:60-63`
- Modify: `backend/app/pipelines/tools.py`
- Test: `backend/tests/pipelines/test_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_tools.py`:

```python
class TestNewAlignerProbes:
    def test_bowtie2_probes(self):
        """Runs against the real binary in the image. An installed-but-broken
        tool is exactly what `available` exists to report, so this asserts the
        probe returns a Tool rather than asserting availability."""
        t = tools.bowtie2()
        assert t.name == "bowtie2"

    def test_hisat2_probes(self):
        t = tools.hisat2()
        assert t.name == "hisat2"

    def test_both_are_in_all_tools(self):
        names = {t.name for t in tools.all_tools()}
        assert "bowtie2" in names
        assert "hisat2" in names

    def test_both_have_metadata(self):
        """A tool with no TOOL_META entry defaults to runnable=False and would
        render as a permanently greyed-out card."""
        assert tools.TOOL_META["bowtie2"].runnable is True
        assert tools.TOOL_META["hisat2"].runnable is True

    def test_both_are_align_pipeline_tools(self):
        from app.pipelines.tools import PipelineType

        assert PipelineType.ALIGN in tools.TOOL_META["bowtie2"].pipelines
        assert PipelineType.ALIGN in tools.TOOL_META["hisat2"].pipelines

    def test_every_tool_meta_has_a_one_liner(self):
        """The selector rail shows this instead of the full summary."""
        for name, meta in tools.TOOL_META.items():
            assert meta.one_liner.strip(), f"{name} has no one_liner"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_tools.py::TestNewAlignerProbes -v`
Expected: FAIL with `AttributeError: module 'app.pipelines.tools' has no attribute 'bowtie2'`

- [ ] **Step 3: Install the binaries**

In `backend/Dockerfile`, add to the apt install list (after `minimap2 \`):

```
        bowtie2 \
        hisat2 \
```

- [ ] **Step 4: Add the config paths**

In `backend/app/config.py`, after `minimap2_path`:

```python
    bowtie2_path: str = "bowtie2"
    # The index builders are separate binaries rather than subcommands, so
    # they need their own paths -- `bowtie2 index` is not a thing.
    bowtie2_build_path: str = "bowtie2-build"
    hisat2_path: str = "hisat2"
    hisat2_build_path: str = "hisat2-build"
```

- [ ] **Step 5: Add the probes and metadata**

In `backend/app/pipelines/tools.py`, add after `minimap2()`:

```python
@lru_cache(maxsize=1)
def bowtie2() -> Tool:
    return _probe("bowtie2", settings.bowtie2_path, ["--version"])


@lru_cache(maxsize=1)
def hisat2() -> Tool:
    return _probe("hisat2", settings.hisat2_path, ["--version"])
```

Add both to `all_tools()` after `minimap2()`, and to `reset_cache()`:

```python
    bowtie2.cache_clear()
    hisat2.cache_clear()
```

Add a `one_liner` field to `ToolMeta`, before `runnable`:

```python
    # The rail in the tool selector shows this; `summary` is a paragraph and
    # too long for a row. Kept beside it rather than derived by truncation,
    # since a sentence cut at 60 characters reads as a bug.
    one_liner: str = ""
```

Add `one_liner=` to every existing `TOOL_META` entry. Suggested values:

- fastp: `"All-in-one Illumina QC and adapter trimming"`
- cutadapt: `"Flexible adapter, primer, and barcode trimming"`
- trimmomatic: `"Classic sliding-window quality trimmer"`
- fastqc: `"The canonical per-file HTML QC report"`
- nanoplot: `"QC plots for Nanopore and PacBio long reads"`
- fasterq-dump: `"Converts an SRA run into FASTQ"`
- prefetch: `"Fetches an SRA run into the local cache"`
- bwa-mem2: `"Standard short-read aligner for DNA-seq"`
- minimap2: `"Long-read and splice-aware aligner"`
- samtools: `"Universal BAM/CRAM/SAM toolkit"`
- bcftools: `"Pileup variant caller and VCF toolkit"`
- clair3: `"Deep-learning variant caller for long reads"`

Add the two new entries to `TOOL_META`:

```python
    "bowtie2": ToolMeta(
        pipelines=(PipelineType.ALIGN,),
        one_liner="Short-read aligner for ChIP-seq, ATAC-seq, and resequencing",
        summary=(
            "Fast, memory-efficient short-read aligner. The standard choice "
            "for ChIP-seq and ATAC-seq, where its local alignment mode and "
            "explicit insert-size control matter more than the indel "
            "sensitivity a variant-calling pipeline wants."
        ),
        strengths=(
            "The conventional aligner for ChIP-seq and ATAC-seq",
            "Compact index: about 3.5 GB for a human genome",
            "Local mode soft-clips read ends rather than discarding the read",
            "Explicit insert-size ceiling for fragment-length-sensitive work",
            "Four sensitivity presets trading speed against divergent regions",
        ),
    ),
    "hisat2": ToolMeta(
        pipelines=(PipelineType.ALIGN,),
        one_liner="Splice-aware RNA-seq aligner with a compact graph index",
        summary=(
            "Splice-aware aligner built for RNA-seq. Its graph FM index is "
            "far smaller than STAR's for the same genome, which makes it the "
            "practical choice for transcriptome alignment on a machine that "
            "cannot spare 32 GB."
        ),
        strengths=(
            "Splice-aware: designed for RNA-seq junction discovery",
            "Compact index -- roughly 4 GB for human, against STAR's ~30 GB",
            "Strandness handling for dUTP and other stranded protocols",
            "Can be told to skip spliced alignment for DNA input",
            "Output mode tailored for downstream transcript assembly",
        ),
    ),
```

- [ ] **Step 6: Rebuild and run the tests**

```bash
docker compose up -d --build api web worker
```

Run: `docker compose exec api python -m pytest tests/pipelines/test_tools.py -v`
Expected: PASS

- [ ] **Step 7: Verify the index suffix lists against the real builders**

The suffix tuples in Task 1 were written from documentation. Confirm them against the installed versions — a missing member makes the tool refuse to load the index, and this is the cheapest place to catch it:

```bash
docker compose exec api sh -c 'cd /tmp && printf ">c1\nACGTACGTACGTACGTACGTACGTACGTACGT\n" > t.fa && bowtie2-build t.fa t.fa >/dev/null 2>&1 && ls t.fa.*'
```

Expected output: `t.fa.1.bt2  t.fa.2.bt2  t.fa.3.bt2  t.fa.4.bt2  t.fa.rev.1.bt2  t.fa.rev.2.bt2`

```bash
docker compose exec api sh -c 'cd /tmp && hisat2-build t.fa t.fa >/dev/null 2>&1 && ls t.fa.*.ht2'
```

Expected output: `t.fa.1.ht2` through `t.fa.8.ht2`.

If either listing disagrees with `BOWTIE2_SUFFIXES` or `HISAT2_SUFFIXES` in `aligners.py`, correct the tuples now and re-run `pytest tests/pipelines/test_aligners.py`. Note that large indexes get `.bt2l`/`.ht2l` (long) suffixes instead — if the tuples need to vary by reference size, record that as a follow-up rather than solving it here, and note it in the commit.

- [ ] **Step 8: Commit**

```bash
git add backend/Dockerfile backend/app/config.py backend/app/pipelines/tools.py backend/tests/pipelines/test_tools.py
git commit -m "feat: install and probe bowtie2 and hisat2"
```

---

## Task 6: Build commands for bowtie2 and HISAT2

**Files:**
- Modify: `backend/app/pipelines/align_runner.py:196-297`
- Test: `backend/tests/pipelines/test_align_runner.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_align_runner.py`:

```python
class TestBowtie2Command:
    def cmd(self, **kw):
        params = align_params.from_dict({"aligner": "bowtie2", **kw})
        return align_cmd(
            aligner=Aligner.BOWTIE2, aligner_path="bowtie2", params=params
        )

    def test_reads_are_passed_with_the_paired_flags(self):
        """bowtie2 does not take positional read files the way bwa does:
        R1 goes to -1 and R2 to -2, and a bare positional would be read as
        the index basename."""
        params = align_params.from_dict({"aligner": "bowtie2"})
        cmd = align_cmd(
            aligner=Aligner.BOWTIE2,
            aligner_path="bowtie2",
            params=params,
            r2=Path("/w/r2.fq.gz"),
        )
        joined = " ".join(cmd)
        assert "-1 /w/r1.fq.gz" in joined
        assert "-2 /w/r2.fq.gz" in joined

    def test_single_end_reads_use_the_unpaired_flag(self):
        joined = " ".join(self.cmd())
        assert "-U /w/r1.fq.gz" in joined

    def test_the_index_is_passed_with_dash_x(self):
        joined = " ".join(self.cmd())
        assert "-x /w/genome.fna" in joined

    def test_threads_use_dash_p(self):
        joined = " ".join(self.cmd(threads=8))
        assert "-p 8" in joined

    def test_sensitivity_reaches_the_command(self):
        joined = " ".join(self.cmd(sensitivity="--very-sensitive"))
        assert "--very-sensitive" in joined

    def test_local_mode_is_a_flag(self):
        assert "--local" in " ".join(self.cmd(local=True))
        assert "--local" not in " ".join(self.cmd(local=False))

    def test_maxins_reaches_the_command(self):
        assert "-X 800" in " ".join(self.cmd(maxins=800))

    def test_report_k_is_omitted_when_zero(self):
        """0 means 'leave the flag off'. Passing -k 0 tells bowtie2 to report
        zero alignments, which silently produces an empty BAM."""
        assert " -k " not in " ".join(self.cmd(report_k=0))
        assert "-k 4" in " ".join(self.cmd(report_k=4))

    def test_the_read_group_is_split_into_id_and_fields(self):
        """bowtie2 has no single -R: it takes --rg-id for the ID and one --rg
        per remaining field. Passing bwa's tab-joined @RG line would put a
        literal backslash-t into the BAM header."""
        joined = " ".join(self.cmd())
        assert "--rg-id" in joined
        assert "SM:SAMP1" in joined
        assert "@RG" not in joined


class TestHisat2Command:
    def cmd(self, **kw):
        params = align_params.from_dict({"aligner": "hisat2", **kw})
        return align_cmd(
            aligner=Aligner.HISAT2, aligner_path="hisat2", params=params
        )

    def test_the_index_is_passed_with_dash_x(self):
        assert "-x /w/genome.fna" in " ".join(self.cmd())

    def test_strandness_is_omitted_when_unstranded(self):
        """The flag has no 'unstranded' value -- omitting it is how you say
        that. Passing an empty string would make HISAT2 reject the argument."""
        assert "--rna-strandness" not in " ".join(self.cmd(rna_strandness=""))
        assert "--rna-strandness RF" in " ".join(self.cmd(rna_strandness="RF"))

    def test_max_intronlen_reaches_the_command(self):
        assert "--max-intronlen 20000" in " ".join(self.cmd(max_intronlen=20000))

    def test_no_spliced_alignment_is_a_flag(self):
        assert "--no-spliced-alignment" in " ".join(
            self.cmd(no_spliced_alignment=True)
        )

    def test_dta_is_a_flag(self):
        assert "--dta" in " ".join(self.cmd(dta=True))


class TestNewAlignersKeepPipefail:
    """The truncated-BAM failure applies to every aligner, not just the two
    that existed when the pipe was written."""

    @pytest.mark.parametrize("aligner", [Aligner.BOWTIE2, Aligner.HISAT2])
    def test_pipefail_is_set(self, aligner):
        params = align_params.from_dict({"aligner": aligner.value})
        cmd = align_cmd(aligner=aligner, aligner_path=aligner.value, params=params)
        assert cmd[:3] == ["/bin/sh", "-o", "pipefail"]
```

Add `from app.pipelines import align_params` to the test file imports.

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_align_runner.py::TestBowtie2Command -v`
Expected: FAIL — the current `_aligner_argv` treats anything that is not bwa-mem2 as minimap2, so `-x` will be absent.

- [ ] **Step 3: Add read-group formatting for the prefix aligners**

In `backend/app/pipelines/align_runner.py`, add to the `ReadGroup` dataclass:

```python
    def as_rg_args(self) -> list[str]:
        """`--rg-id` plus one `--rg` per remaining field.

        bowtie2 and HISAT2 have no single -R taking a whole @RG line. Handing
        them `as_sam_header()` would embed a literal backslash-t in the BAM
        header, which reads as a corrupt read group to every downstream tool
        rather than failing at alignment time.
        """
        rg_id = self.identifier or self.sample
        args = ["--rg-id", rg_id]
        for field_value in (
            f"SM:{self.sample}",
            f"LB:{self.library}",
            f"PL:{self.platform}",
        ):
            args += ["--rg", field_value]
        return args
```

- [ ] **Step 4: Rewrite `_aligner_argv` to dispatch per aligner**

Replace the body of `_aligner_argv` in `backend/app/pipelines/align_runner.py`:

```python
def _aligner_argv(
    *,
    aligner: Aligner,
    aligner_path: str,
    reference: Path,
    r1: Path,
    r2: Path | None,
    read_group: ReadGroup,
    params,
) -> list[str]:
    """The aligner half of the pipeline, before samtools.

    Four tools, three calling conventions. bwa-mem2 and minimap2 take reads
    positionally and the reference as a path; bowtie2 and HISAT2 take the
    index basename via -x and the reads via -U or -1/-2. Getting that wrong
    does not fail cleanly -- bowtie2 reads a stray positional argument as its
    index basename and reports a missing index.
    """
    if aligner is Aligner.BWA_MEM2:
        argv = [aligner_path, "mem", "-t", str(params.threads)]
        argv += ["-R", read_group.as_sam_header()]
        argv += [str(reference), str(r1)]
        if r2 is not None:
            argv.append(str(r2))
        return argv

    if aligner is Aligner.MINIMAP2:
        # -a emits SAM rather than PAF, which samtools sort requires.
        argv = [aligner_path, "-a", "-x", params.preset, "-t", str(params.threads)]
        argv += ["-R", read_group.as_sam_header(), str(reference), str(r1)]
        if r2 is not None:
            argv.append(str(r2))
        return argv

    return _prefix_aligner_argv(
        aligner=aligner,
        aligner_path=aligner_path,
        reference=reference,
        r1=r1,
        r2=r2,
        read_group=read_group,
        params=params,
    )


def _prefix_aligner_argv(
    *,
    aligner: Aligner,
    aligner_path: str,
    reference: Path,
    r1: Path,
    r2: Path | None,
    read_group: ReadGroup,
    params,
) -> list[str]:
    """bowtie2 and HISAT2, which share a calling convention."""
    layout = aligners.layout_for(aligner)
    argv = [aligner_path, "-x", layout.reference_argument(reference)]

    if r2 is not None:
        argv += ["-1", str(r1), "-2", str(r2)]
    else:
        argv += ["-U", str(r1)]

    argv += ["-p", str(params.threads)]
    argv += read_group.as_rg_args()

    if params.report_k > 0:
        # 0 means "leave the flag off". `-k 0` tells the tool to report zero
        # alignments, which produces an empty BAM rather than an error.
        argv += ["-k", str(params.report_k)]

    if aligner is Aligner.BOWTIE2:
        argv.append(params.sensitivity)
        if params.local:
            argv.append("--local")
        argv += ["-X", str(params.maxins)]
        if params.no_mixed:
            argv.append("--no-mixed")
        if params.no_discordant:
            argv.append("--no-discordant")
    else:
        if params.rna_strandness:
            # The flag has no "unstranded" value -- omitting it is how that is
            # expressed, and an empty string would be rejected as an argument.
            argv += ["--rna-strandness", params.rna_strandness]
        argv += ["--max-intronlen", str(params.max_intronlen)]
        if params.no_spliced_alignment:
            argv.append("--no-spliced-alignment")
        if params.dta:
            argv.append("--dta")

    return argv
```

Add `from app.pipelines import aligners` to the imports at the top of `align_runner.py` if only `Aligner` is currently imported.

- [ ] **Step 5: Extend the index-build command**

Replace `build_index_command` in `backend/app/pipelines/align_runner.py`:

```python
def build_index_command(
    *, aligner: Aligner, tool_path: str, reference: Path, output: Path | None = None
) -> list[str]:
    """The command that builds an aligner's index for a reference.

    Three shapes: bwa-mem2 writes its five files beside the reference and
    takes no output path, minimap2 writes one file wherever it is told, and
    bowtie2/HISAT2 take a reference and a basename as two positional
    arguments. `tool_path` for the latter two is the *builder* binary
    (bowtie2-build, hisat2-build), not the aligner -- see
    `aligners.layout_for(...).builder`.
    """
    if aligner is Aligner.BWA_MEM2:
        return [tool_path, "index", str(reference)]

    if aligner is Aligner.MINIMAP2:
        if output is None:
            raise ValidationError("minimap2 index requires an output path")
        return [tool_path, "-d", str(output), str(reference)]

    # bowtie2-build / hisat2-build: <reference> <basename>. The basename is
    # the reference path itself, so the index files land beside it as
    # `genome.fna.1.bt2` and materialize back under names the layout knows.
    return [tool_path, str(reference), str(reference)]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/pipelines/test_align_runner.py -v`
Expected: PASS (all tests, including the pre-existing pipefail and flagstat ones)

- [ ] **Step 7: Commit**

```bash
git add backend/app/pipelines/align_runner.py backend/tests/pipelines/test_align_runner.py
git commit -m "feat: build bowtie2 and hisat2 alignment and index commands"
```

---

## Task 7: Wire the handlers to the registry

**Files:**
- Modify: `backend/app/queue/align_handlers.py:28-29,86-135,179-183`
- Modify: `backend/app/services/pipeline_service.py`
- Test: `backend/tests/pipelines/test_align_launch.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_align_launch.py`:

```python
class TestAlignerToolResolution:
    def test_every_aligner_resolves_to_its_own_tool(self):
        """_aligner_tool used to be an if/else over two tools, and returned
        minimap2 for anything that was not bwa-mem2 -- so a bowtie2 job would
        have silently run minimap2 against a bowtie2 index."""
        from app.pipelines.aligners import Aligner
        from app.queue.align_handlers import _aligner_tool

        for aligner in Aligner:
            assert _aligner_tool(aligner).name == aligner.value
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_align_launch.py::TestAlignerToolResolution -v`
Expected: FAIL — `_aligner_tool(Aligner.BOWTIE2)` currently returns the minimap2 tool

- [ ] **Step 3: Replace `_aligner_tool` with a registry lookup**

In `backend/app/queue/align_handlers.py`:

```python
def _aligner_tool(aligner: Aligner):
    """The probe for one aligner.

    A registry lookup rather than an if/else: the old form returned minimap2
    for anything that was not bwa-mem2, so a new aligner would silently run
    the wrong binary against the right index.
    """
    return aligner_registry.spec_for(aligner).tool()
```

Add `aligner_registry` to the `from app.pipelines import ...` line.

Apply the identical change to `_aligner_tool` in `backend/app/services/pipeline_service.py` — it has its own copy with the same two-branch bug.

- [ ] **Step 4: Dispatch the index builder in `build_index`**

In `backend/app/queue/align_handlers.py`, replace the index-command block in `build_index` (the `if aligner is Aligner.BWA_MEM2: ... else: ...` around lines 117-128):

```python
    layout = aligners.layout_for(aligner)
    if layout.builder is not None:
        # bowtie2 and HISAT2 index through a separate binary, so the tool
        # whose version was probed is not the one that runs here.
        builder_path = shutil.which(layout.builder)
        if builder_path is None:
            raise PermanentError(
                f"{layout.builder} is not on PATH; {aligner.value} cannot "
                f"build an index without it"
            )
        cmd = align_runner.build_index_command(
            aligner=aligner, tool_path=builder_path, reference=ref.reference
        )
    elif aligner is Aligner.BWA_MEM2:
        cmd = align_runner.build_index_command(
            aligner=aligner, tool_path=tool.path, reference=ref.reference
        )
    else:
        cmd = align_runner.build_index_command(
            aligner=aligner,
            tool_path=tool.path,
            reference=ref.reference,
            output=ref.reference.parent
            / f"{ref.reference.name}{aligners.MINIMAP2_SUFFIX}",
        )
```

Add `import shutil` at the top of the file.

- [ ] **Step 5: Route params through the dispatcher**

In `align_handlers.align_reads`, replace:

```python
    params = align_runner.AlignParams.from_dict(ctx.payload.get("params"))
```

with:

```python
    params = align_params.from_dict(ctx.payload.get("params"))
```

Add `align_params` to the `from app.pipelines import ...` line. Apply the same substitution in `pipeline_service.launch_alignment` (currently `align_runner.AlignParams.from_dict({...})`).

- [ ] **Step 6: Run the full suite**

```bash
docker compose restart worker
docker compose exec api python -m pytest tests/ -q
```

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/app/queue/align_handlers.py backend/app/services/pipeline_service.py backend/tests/pipelines/test_align_launch.py
git commit -m "feat: dispatch aligner tools and index builders through the registry"
```

---

## Task 8: The resource estimator

**Files:**
- Create: `backend/app/pipelines/resource_estimator.py`
- Test: `backend/tests/pipelines/test_resource_estimator.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_resource_estimator.py`:

```python
"""Memory estimation and the three bands.

Band edges carry the weight here. The logic is pure arithmetic over
coefficients, and an off-by-one comparison at a boundary is invisible in
review but decides whether a run is blocked -- so every boundary is tested
from both sides.
"""

import pytest

from app.pipelines import resource_estimator as est
from app.pipelines.aligners import Aligner


class TestEstimate:
    def test_sort_memory_is_multiplied_by_threads(self):
        """The term users actually trip over: sort memory is per thread, so
        8 threads at 1024 MB is 8 GB, not 1 GB."""
        one = est.estimate_mb(
            aligner=Aligner.BOWTIE2, reference_bases=0, threads=1,
            sort_memory_mb=1024, building_index=False,
        )
        eight = est.estimate_mb(
            aligner=Aligner.BOWTIE2, reference_bases=0, threads=8,
            sort_memory_mb=1024, building_index=False,
        )
        assert eight - one >= 7 * 1024

    def test_a_larger_reference_costs_more(self):
        small = est.estimate_mb(
            aligner=Aligner.BOWTIE2, reference_bases=1_000_000, threads=4,
            sort_memory_mb=512, building_index=False,
        )
        human = est.estimate_mb(
            aligner=Aligner.BOWTIE2, reference_bases=3_100_000_000, threads=4,
            sort_memory_mb=512, building_index=False,
        )
        assert human > small

    def test_bowtie2_human_index_is_in_the_expected_range(self):
        """A sanity check on the coefficient, not a precise claim: bowtie2's
        published GRCh38 index is about 3.5 GB, so an estimate that came out
        at 300 MB or 30 GB would mean the units are wrong."""
        mb = est.estimate_mb(
            aligner=Aligner.BOWTIE2, reference_bases=3_100_000_000, threads=1,
            sort_memory_mb=64, building_index=False,
        )
        assert 2_000 < mb < 8_000

    def test_building_an_index_costs_more_than_loading_one(self):
        loading = est.estimate_mb(
            aligner=Aligner.BOWTIE2, reference_bases=3_100_000_000, threads=4,
            sort_memory_mb=512, building_index=False,
        )
        building = est.estimate_mb(
            aligner=Aligner.BOWTIE2, reference_bases=3_100_000_000, threads=4,
            sort_memory_mb=512, building_index=True,
        )
        assert building > loading


class TestBands:
    def test_well_under_budget_is_ok(self):
        assert est.classify(estimated_mb=1000, mem_budget_mb=16000,
                            threads=4, cpu_budget=8) is est.Band.OK

    def test_just_under_the_warn_edge_is_ok(self):
        assert est.classify(estimated_mb=6999, mem_budget_mb=10000,
                            threads=4, cpu_budget=8) is est.Band.OK

    def test_at_the_warn_edge_is_warn(self):
        assert est.classify(estimated_mb=7000, mem_budget_mb=10000,
                            threads=4, cpu_budget=8) is est.Band.WARN

    def test_just_under_budget_is_warn_not_block(self):
        assert est.classify(estimated_mb=9999, mem_budget_mb=10000,
                            threads=4, cpu_budget=8) is est.Band.WARN

    def test_over_budget_is_block(self):
        assert est.classify(estimated_mb=10001, mem_budget_mb=10000,
                            threads=4, cpu_budget=8) is est.Band.BLOCK

    def test_exactly_at_budget_is_warn(self):
        """The conservative edge the design calls for: block is reserved for
        genuinely impossible, and exactly-at-budget is merely doomed."""
        assert est.classify(estimated_mb=10000, mem_budget_mb=10000,
                            threads=4, cpu_budget=8) is est.Band.WARN

    def test_too_many_threads_warns_but_does_not_block(self):
        """Oversubscribing CPUs is slow and recoverable; it is not the
        twenty-minutes-then-OOM failure that blocking exists for."""
        assert est.classify(estimated_mb=100, mem_budget_mb=16000,
                            threads=32, cpu_budget=8) is est.Band.WARN

    def test_an_unknown_budget_never_blocks(self):
        """A host whose budget could not be read is not evidence that a run
        will fail, and blocking on missing information would be wrong."""
        assert est.classify(estimated_mb=99_999, mem_budget_mb=None,
                            threads=4, cpu_budget=None) is est.Band.OK


class TestExplain:
    def test_the_message_names_the_dominant_term(self):
        """A warning that does not say what to change is not actionable."""
        msg = est.explain(
            aligner=Aligner.BOWTIE2, reference_bases=100_000, threads=16,
            sort_memory_mb=2048, building_index=False, mem_budget_mb=8000,
        )
        assert "sort" in msg.lower()

    def test_the_message_reports_both_numbers(self):
        msg = est.explain(
            aligner=Aligner.BOWTIE2, reference_bases=3_100_000_000, threads=4,
            sort_memory_mb=1024, building_index=False, mem_budget_mb=8000,
        )
        assert "8000" in msg or "8,000" in msg or "7.8" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_resource_estimator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.pipelines.resource_estimator'`

- [ ] **Step 3: Implement the estimator**

Create `backend/app/pipelines/resource_estimator.py`:

```python
"""Whether a proposed alignment fits on this machine.

Two failure modes, and they differ in kind. Too many threads is slow and
recoverable. An index that does not fit in RAM is an OOM kill twenty minutes
in, with a log that says nothing useful -- the job simply stops. Only the
second is worth blocking, which is why the bands below are asymmetric: thread
oversubscription warns, memory overrun blocks.

The coefficients are heuristics from published tool documentation, not
measurements on this hardware. That is the reason BLOCK is set at
strictly-over-budget rather than at some safety margin below it: a wrong
coefficient should cost a spurious warning, never a blocked run that would
have worked.
"""

from enum import StrEnum

from app.pipelines.aligner_registry import spec_for
from app.pipelines.aligners import Aligner

# Below this fraction of the budget, say nothing.
WARN_FRACTION = 0.70


class Band(StrEnum):
    OK = "ok"
    WARN = "warn"
    BLOCK = "block"


def estimate_mb(
    *,
    aligner: Aligner,
    reference_bases: int,
    threads: int,
    sort_memory_mb: int,
    building_index: bool,
) -> int:
    """Peak resident memory for a run, in MB.

    The samtools sort term is the one that surprises people: `-m` is per
    thread, so it multiplies. Everything else is the aligner's own index plus
    per-worker buffers.
    """
    model = spec_for(aligner).memory_model

    index_mb = (reference_bases * model.index_bytes_per_ref_base) / (1024 * 1024)
    if building_index:
        index_mb *= model.index_build_multiplier

    worker_mb = threads * model.bytes_per_thread_mb
    sort_mb = threads * sort_memory_mb

    return int(model.fixed_overhead_mb + index_mb + worker_mb + sort_mb)


def classify(
    *,
    # Named `estimated_mb` rather than `estimate_mb` so it does not shadow the
    # function above within this scope -- the two would be indistinguishable
    # at a glance in a file where both are in play.
    estimated_mb: int,
    mem_budget_mb: int | None,
    threads: int,
    cpu_budget: float | None,
) -> Band:
    """Which band a configuration falls in.

    A missing budget yields OK rather than a guess: not being able to read the
    host's limits is not evidence that a run will fail, and blocking on absent
    information would stop work for no reason.
    """
    if mem_budget_mb is None:
        return Band.OK

    if estimated_mb > mem_budget_mb:
        return Band.BLOCK

    if estimated_mb >= mem_budget_mb * WARN_FRACTION:
        return Band.WARN

    if cpu_budget is not None and threads > cpu_budget:
        return Band.WARN

    return Band.OK


def explain(
    *,
    aligner: Aligner,
    reference_bases: int,
    threads: int,
    sort_memory_mb: int,
    building_index: bool,
    mem_budget_mb: int | None,
) -> str:
    """A sentence naming the dominant term and both numbers.

    "Estimated 14 GB of 16 GB" is a fact; "Sort buffer is 8 GB of that (8
    threads x 1024 MB)" is what tells someone which slider to move. A warning
    without the second half is not actionable.
    """
    total = estimate_mb(
        aligner=aligner,
        reference_bases=reference_bases,
        threads=threads,
        sort_memory_mb=sort_memory_mb,
        building_index=building_index,
    )
    model = spec_for(aligner).memory_model

    sort_mb = threads * sort_memory_mb
    index_mb = int((reference_bases * model.index_bytes_per_ref_base) / (1024 * 1024))
    if building_index:
        index_mb = int(index_mb * model.index_build_multiplier)

    budget_text = f" of {mem_budget_mb:,} MB available" if mem_budget_mb else ""
    parts = [f"Estimated {total:,} MB{budget_text}."]

    if sort_mb >= index_mb:
        parts.append(
            f"The sort buffer is {sort_mb:,} MB of that "
            f"({threads} threads x {sort_memory_mb} MB each)."
        )
    else:
        what = "building the index" if building_index else "the index"
        parts.append(f"Most of it is {what}: about {index_mb:,} MB.")

    return " ".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/pipelines/test_resource_estimator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/resource_estimator.py backend/tests/pipelines/test_resource_estimator.py
git commit -m "feat: add alignment resource estimator with warn and block bands"
```

---

## Task 9: The schema and envelope endpoints, plus the launch guard

**Files:**
- Modify: `backend/app/api/v1/pipelines.py`
- Modify: `backend/app/services/pipeline_service.py`
- Test: `backend/tests/api/test_pipelines_align_schema.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_pipelines_align_schema.py`, mirroring the synchronous `TestClient` fixture in `backend/tests/api/test_pipelines_trim_tool.py`:

```python
"""The registry-driven schema endpoint.

Only the schema route is covered here. The envelope route loads two objects
from Mongo, and this suite mounts the router against a bare FastAPI app with
no database -- so an envelope test would be testing the fixture, not the
endpoint. The envelope's real logic (the arithmetic and the bands) is covered
directly in test_resource_estimator.py, and the wiring is verified by hand in
Task 14.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.pipelines import router
from app.errors import register_exception_handlers


@pytest.fixture
def client():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    return TestClient(app)


class TestSchemaEndpoint:
    @pytest.mark.parametrize(
        "aligner", ["bwa-mem2", "minimap2", "bowtie2", "hisat2"]
    )
    def test_every_aligner_has_a_schema(self, client, aligner):
        resp = client.get(f"/pipelines/aligners/{aligner}/schema")
        assert resp.status_code == 200
        assert resp.json()["aligner"] == aligner

    def test_fields_carry_what_the_form_needs(self, client):
        resp = client.get("/pipelines/aligners/bowtie2/schema")
        fields = {f["key"]: f for f in resp.json()["fields"]}
        assert fields["maxins"]["kind"] == "int"
        assert fields["sensitivity"]["kind"] == "select"
        assert fields["sensitivity"]["choices"]
        assert fields["threads"]["group"] == "performance"

    def test_help_text_survives_serialization(self, client):
        """The generated form has no other explanation for a knob, so an
        empty help string is a field with no stated meaning."""
        resp = client.get("/pipelines/aligners/hisat2/schema")
        for f in resp.json()["fields"]:
            assert f["help"].strip()

    def test_an_unknown_aligner_is_a_client_error(self, client):
        resp = client.get("/pipelines/aligners/not-real/schema")
        assert resp.status_code == 404
```

Note the route prefix: this suite mounts the router directly, so paths start at `/pipelines`, not `/api/v1/pipelines`.

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/api/test_pipelines_align_schema.py -v`
Expected: FAIL with 404 on the schema route

- [ ] **Step 3: Add the endpoints**

In `backend/app/api/v1/pipelines.py`, add after the `align_defaults` route:

```python
@router.get("/aligners/{aligner}/schema")
async def aligner_schema(aligner: str) -> dict:
    """The parameter fields for one aligner, for the dialog to render.

    Served from the registry rather than duplicated in the frontend: two
    copies of a tool's knobs drift, and the frontend copy is the one nobody
    updates when a flag is added.
    """
    try:
        parsed = Aligner(aligner)
    except ValueError:
        raise NotFoundError(f"Unknown aligner: {aligner}") from None
    return aligner_registry.schema_for(parsed)


@router.get("/align-envelope")
async def align_envelope(object_id: PydanticObjectId, reference_id: PydanticObjectId) -> dict:
    """Everything the dialog needs to estimate memory without a round trip.

    Sent once when the dialog opens; the client then evaluates the same
    arithmetic locally as sliders move. The formula stays in Python -- only
    the coefficients ship -- so there is no second implementation to drift,
    and `launch_alignment` re-runs the authoritative check regardless.
    """
    return await pipeline_service.align_envelope(
        object_id=object_id, reference_id=reference_id
    )
```

Import `aligner_registry` and `NotFoundError` at the top of the file if not already present.

- [ ] **Step 4: Implement the envelope service function**

In `backend/app/services/pipeline_service.py`, add:

```python
async def align_envelope(
    *, object_id: PydanticObjectId, reference_id: PydanticObjectId
) -> dict:
    """Host budgets, input sizes, and the per-aligner memory coefficients.

    Budgets come from the governor, which reads cgroup limits -- so inside
    Docker this reports the container's real allocation rather than the
    host's. That distinction is the whole reason the warning is trustworthy:
    a machine with 64 GB and an 8 GB Docker allocation will OOM at 8.
    """
    from dataclasses import asdict

    from app.pipelines import aligner_registry
    from app.queue.governor import LoadGovernor

    obj = await DataObject.get(object_id)
    if obj is None:
        raise NotFoundError(f"Object not found: {object_id}")

    reference = await DataObject.get(reference_id)
    if reference is None:
        raise NotFoundError(f"Reference not found: {reference_id}")

    governor = LoadGovernor()

    # Reference size in bases, approximated by file size. A FASTA carries
    # about one byte per base plus headers and newlines, so this overestimates
    # by a few percent -- the right direction for a memory warning.
    reference_bases = reference.size or 0

    status = await reference_index_status(reference)

    return {
        "cpu_budget": governor.cpu_budget(),
        "mem_budget_mb": int(governor.mem_budget_bytes() / (1024 * 1024)),
        "reference_bases": reference_bases,
        "input_bytes": obj.size or 0,
        "index_status": status,
        "models": {
            aligner.value: asdict(
                aligner_registry.spec_for(aligner).memory_model
            )
            for aligner in Aligner
        },
    }
```

If `LoadGovernor()` cannot be instantiated standalone (check how `system.py` obtains it — it may use a module-level singleton), use the same accessor `system.py` does rather than constructing a new one.

- [ ] **Step 5: Add the launch guard**

In `pipeline_service.launch_alignment`, after `align_params` is built and `reference` is loaded (after the `_check_reference(reference)` call), add:

```python
    # The authoritative check. The dialog runs the same arithmetic for
    # immediacy, but it can be bypassed -- the API is directly callable -- and
    # its envelope goes stale if the host's load changes between opening the
    # dialog and pressing Launch.
    from app.pipelines import resource_estimator
    from app.queue.governor import LoadGovernor

    governor = LoadGovernor()
    mem_budget_mb = int(governor.mem_budget_bytes() / (1024 * 1024))
    status = await reference_index_status(reference)
    building = not status.get(aligner.value) or not status.get("fai")

    estimate = resource_estimator.estimate_mb(
        aligner=aligner,
        reference_bases=reference.size or 0,
        threads=align_params.threads,
        sort_memory_mb=align_params.sort_memory_mb,
        building_index=building,
    )
    band = resource_estimator.classify(
        estimated_mb=estimate,
        mem_budget_mb=mem_budget_mb,
        threads=align_params.threads,
        cpu_budget=governor.cpu_budget(),
    )
    if band is resource_estimator.Band.BLOCK:
        raise ValidationError(
            resource_estimator.explain(
                aligner=aligner,
                reference_bases=reference.size or 0,
                threads=align_params.threads,
                sort_memory_mb=align_params.sort_memory_mb,
                building_index=building,
                mem_budget_mb=mem_budget_mb,
            ),
            details={"estimate_mb": estimate, "budget_mb": mem_budget_mb},
        )
```

Note the variable name: the existing function calls its params object `align_params`, which now shadows the module name. Import the module as `from app.pipelines import align_params as align_params_module` at the top of the file, or rename the local — check what the file already does and stay consistent.

- [ ] **Step 6: Run the suite**

Run: `docker compose exec api python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/v1/pipelines.py backend/app/services/pipeline_service.py backend/tests/api/test_pipelines_align_schema.py
git commit -m "feat: serve aligner schemas and resource envelope, guard launch on block band"
```

---

## Task 10: Frontend types and API client

**Files:**
- Modify: `frontend/src/api/types.ts:475-521`
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Extend the types**

In `frontend/src/api/types.ts`, replace the `AlignerName` and `AlignParams` block:

```typescript
export type AlignerName = "bwa-mem2" | "minimap2" | "bowtie2" | "hisat2";

/** minimap2 presets. The wrong one for long reads aligns poorly rather than failing. */
export type AlignPreset = "map-ont" | "map-pb" | "map-hifi" | "lr:hq" | "sr";

/**
 * Mirrors align_params.BaseAlignParams plus whichever subclass is in play.
 * Tool-specific keys are optional because they only exist for one aligner --
 * the backend rejects a key belonging to a different tool rather than
 * ignoring it, so sending the wrong one fails loudly at launch.
 */
export interface AlignParams {
  aligner: AlignerName;
  threads: number;
  sort_memory_mb: number;
  mark_duplicates: boolean;
  preset?: AlignPreset | "";
  sensitivity?: string;
  local?: boolean;
  maxins?: number;
  no_mixed?: boolean;
  no_discordant?: boolean;
  report_k?: number;
  rna_strandness?: string;
  max_intronlen?: number;
  no_spliced_alignment?: boolean;
  dta?: boolean;
}

/** One input in the generated parameter form. Mirrors registry ParamField. */
export interface ParamFieldMeta {
  key: string;
  label: string;
  kind: "int" | "bool" | "select" | "text";
  default: unknown;
  help: string;
  group: "biology" | "performance";
  min: number | null;
  max: number | null;
  choices: { value: string; label: string }[];
}

export interface AlignerSchema {
  aligner: AlignerName;
  fields: ParamFieldMeta[];
}

/** Mirrors resource_estimator.MemoryModel. */
export interface MemoryModel {
  index_bytes_per_ref_base: number;
  fixed_overhead_mb: number;
  bytes_per_thread_mb: number;
  index_build_multiplier: number;
}

/**
 * Fetched once per dialog open. The client evaluates the same arithmetic the
 * backend does against these coefficients, so sliders give instant feedback
 * without a request per keystroke -- and the backend re-checks at launch.
 */
export interface AlignEnvelope {
  cpu_budget: number | null;
  mem_budget_mb: number | null;
  reference_bases: number;
  input_bytes: number;
  index_status: Record<string, boolean>;
  models: Record<string, MemoryModel>;
}
```

Add `one_liner: string;` to the `PipelineTool` interface.

- [ ] **Step 2: Add the client calls**

In `frontend/src/api/client.ts`, after `alignDefaults`:

```typescript
  alignerSchema: (aligner: string) =>
    request<AlignerSchema>(
      `/pipelines/aligners/${encodeURIComponent(aligner)}/schema`,
    ),

  alignEnvelope: (objectId: string, referenceId: string) =>
    request<AlignEnvelope>(
      `/pipelines/align-envelope?object_id=${objectId}&reference_id=${referenceId}`,
    ),
```

Add `AlignerSchema` and `AlignEnvelope` to the type import list at the top of the file.

- [ ] **Step 3: Verify the frontend compiles**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no errors. If `tsc` is not available that way, check `frontend/package.json` for the typecheck script and use it.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat: add aligner schema and envelope types and client calls"
```

---

## Task 11: The client-side estimator

**Files:**
- Create: `frontend/src/lib/estimate.ts`

- [ ] **Step 1: Implement it**

Create `frontend/src/lib/estimate.ts`:

```typescript
import type { AlignEnvelope, MemoryModel } from "../api/types";

export type Band = "ok" | "warn" | "block";

/** Mirrors resource_estimator.WARN_FRACTION. */
const WARN_FRACTION = 0.7;

/**
 * The client half of the resource check.
 *
 * This duplicates arithmetic that also lives in Python, which is a real cost
 * -- but the alternative was a request per keystroke, and the coefficients
 * (not the formula) are what ship from the server, so the two stay in step as
 * long as this file matches `resource_estimator.py`. The backend re-runs the
 * authoritative check at launch, so a drift here degrades the preview rather
 * than letting a bad run through.
 */
export function estimateMb(
  model: MemoryModel,
  opts: {
    referenceBases: number;
    threads: number;
    sortMemoryMb: number;
    buildingIndex: boolean;
  },
): number {
  let indexMb =
    (opts.referenceBases * model.index_bytes_per_ref_base) / (1024 * 1024);
  if (opts.buildingIndex) indexMb *= model.index_build_multiplier;

  const workerMb = opts.threads * model.bytes_per_thread_mb;
  const sortMb = opts.threads * opts.sortMemoryMb;

  return Math.round(model.fixed_overhead_mb + indexMb + workerMb + sortMb);
}

export function classify(opts: {
  estimateMb: number;
  memBudgetMb: number | null;
  threads: number;
  cpuBudget: number | null;
}): Band {
  if (opts.memBudgetMb == null) return "ok";
  if (opts.estimateMb > opts.memBudgetMb) return "block";
  if (opts.estimateMb >= opts.memBudgetMb * WARN_FRACTION) return "warn";
  if (opts.cpuBudget != null && opts.threads > opts.cpuBudget) return "warn";
  return "ok";
}

/** The sentence shown in the banner. Names the dominant term, as the backend does. */
export function explain(
  model: MemoryModel,
  envelope: AlignEnvelope,
  opts: {
    threads: number;
    sortMemoryMb: number;
    buildingIndex: boolean;
  },
): string {
  const total = estimateMb(model, {
    referenceBases: envelope.reference_bases,
    threads: opts.threads,
    sortMemoryMb: opts.sortMemoryMb,
    buildingIndex: opts.buildingIndex,
  });

  const sortMb = opts.threads * opts.sortMemoryMb;
  let indexMb = Math.round(
    (envelope.reference_bases * model.index_bytes_per_ref_base) / (1024 * 1024),
  );
  if (opts.buildingIndex) indexMb = Math.round(indexMb * model.index_build_multiplier);

  const budget = envelope.mem_budget_mb
    ? ` of ${envelope.mem_budget_mb.toLocaleString()} MB available`
    : "";

  const dominant =
    sortMb >= indexMb
      ? `The sort buffer is ${sortMb.toLocaleString()} MB of that (${opts.threads} threads × ${opts.sortMemoryMb} MB each).`
      : `Most of it is ${opts.buildingIndex ? "building the index" : "the index"}: about ${indexMb.toLocaleString()} MB.`;

  return `Estimated ${total.toLocaleString()} MB${budget}. ${dominant}`;
}
```

- [ ] **Step 2: Verify it compiles**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/estimate.ts
git commit -m "feat: add client-side alignment resource estimate"
```

---

## Task 12: Redesign the tool selector as list-and-detail

**Files:**
- Modify: `frontend/src/components/PipelineToolSelector.tsx`
- Create: `frontend/src/components/ToolDetailPane.tsx`
- Modify: the stylesheet holding `.tool-card-list` (find with `grep -rn "tool-card-list" frontend/src`)

- [ ] **Step 1: Create the detail pane**

Create `frontend/src/components/ToolDetailPane.tsx`:

```typescript
import type { PipelineTool } from "../api/types";

/**
 * The right-hand half of the selector: everything about the focused tool.
 *
 * Driven by *focus* rather than selection, which is the whole reason the
 * redesign works. A disabled tool cannot be selected, but its explanation --
 * "installed, but this application does not run it yet" -- is exactly what a
 * user needs to see, and that was the point of the original always-render
 * card list. Following focus keeps that reachable.
 */
export function ToolDetailPane({ tool }: { tool: PipelineTool | null }) {
  if (!tool) {
    return (
      <div className="tool-detail empty">
        Select a tool to see what it does.
      </div>
    );
  }

  const reason = !tool.available
    ? tool.error || `${tool.name} is not installed`
    : !tool.runnable
      ? `${tool.name} is installed, but this application does not run it yet.`
      : null;

  return (
    <div className="tool-detail">
      <div className="tool-detail-header">
        <h3>{tool.name}</h3>
        {tool.version && <span className="tool-version">v{tool.version}</span>}
      </div>

      {reason && <div className="tool-card-error">{reason}</div>}

      {tool.summary && <p className="tool-detail-summary">{tool.summary}</p>}

      {tool.strengths.length > 0 && (
        <ul className="tool-detail-strengths">
          {tool.strengths.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Rewrite the selector**

Replace the body of `frontend/src/components/PipelineToolSelector.tsx` from the `onKeyDown` handler through the end of `ToolCard`. Key changes: track `focused` separately from `selected`, let arrow keys traverse *every* row including disabled ones, and render the pane from `focused`.

```typescript
export function PipelineToolSelector({
  pipeline,
  selected,
  onSelect,
  onContinue,
  onClose,
}: Props) {
  const listRef = useRef<HTMLDivElement>(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["pipelines", "tools"],
    queryFn: api.pipelineTools,
    staleTime: 60_000,
  });

  const tools = (data?.tools ?? []).filter((t) => t.pipelines.includes(pipeline));

  // A row is choosable only when the binary works *and* something in this
  // application actually calls it. `available` alone would offer cutadapt as
  // a choice that silently does nothing once Continue is pressed; `runnable`
  // alone would offer bwa-mem2 on a host where it cannot execute.
  const choosable = (t: PipelineTool) => t.available && t.runnable;

  // Focus is tracked separately from selection because a disabled row can be
  // focused but not selected -- that is what keeps its "not installed"
  // explanation reachable in the detail pane. The old card list skipped
  // disabled entries entirely, which was right for a plain radio group and
  // wrong once the pane carries the explanation.
  const [focused, setFocused] = useState<string | null>(null);
  const focusedTool =
    tools.find((t) => t.name === (focused ?? selected)) ?? tools[0] ?? null;

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (tools.length === 0) return;
    const current = tools.findIndex(
      (t) => t.name === (focused ?? selected),
    );

    let next: number;
    switch (e.key) {
      case "ArrowDown":
      case "ArrowRight":
        next = (current + 1 + tools.length) % tools.length;
        break;
      case "ArrowUp":
      case "ArrowLeft":
        next = (current - 1 + tools.length) % tools.length;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = tools.length - 1;
        break;
      default:
        return;
    }

    e.preventDefault();
    const tool = tools[next];
    setFocused(tool.name);
    // Selection follows focus only for choosable rows; a disabled row can be
    // read but not chosen.
    if (choosable(tool)) onSelect(tool.name);
    listRef.current
      ?.querySelector<HTMLDivElement>(`[data-tool="${tool.name}"]`)
      ?.focus();
  };

  const label = PIPELINE_LABEL[pipeline] ?? "a tool";

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal tool-selector" onClick={(e) => e.stopPropagation()}>
        <h2>Select {label}</h2>

        {isLoading && (
          <div className="empty">
            <span className="spinner" /> Checking installed tools…
          </div>
        )}

        {isError && (
          <div className="error-box">
            Could not reach the tools API. Close this and try again.
          </div>
        )}

        {!isLoading && !isError && tools.length === 0 && (
          <div className="warn-box">
            No {label} tools are known to this pipeline. That is a
            configuration problem, not a missing binary — report it.
          </div>
        )}

        {tools.length > 0 && (
          <div className="tool-picker">
            <div
              className="tool-rail"
              role="listbox"
              aria-label={`Select ${label}`}
              ref={listRef}
              onKeyDown={onKeyDown}
            >
              {tools.map((tool, i) => {
                const disabled = !choosable(tool);
                const isSelected = tool.name === selected;
                const isFocused = tool.name === (focused ?? selected);
                return (
                  <div
                    key={tool.name}
                    className={`tool-row${isSelected ? " selected" : ""}${
                      disabled ? " disabled" : ""
                    }${isFocused ? " focused" : ""}`}
                    role="option"
                    aria-selected={isSelected}
                    aria-disabled={disabled}
                    data-tool={tool.name}
                    tabIndex={isFocused || (!focused && !selected && i === 0) ? 0 : -1}
                    onFocus={() => setFocused(tool.name)}
                    onClick={() => {
                      setFocused(tool.name);
                      if (!disabled) onSelect(tool.name);
                    }}
                    onKeyDown={(e) => {
                      if ((e.key === "Enter" || e.key === " ") && !disabled) {
                        e.preventDefault();
                        onSelect(tool.name);
                      }
                    }}
                  >
                    <div className="tool-row-main">
                      <span className="tool-name">{tool.name}</span>
                      {tool.version && (
                        <span className="tool-version">v{tool.version}</span>
                      )}
                    </div>
                    <div className="tool-row-line">{tool.one_liner}</div>
                    {disabled && (
                      <span className="tool-row-badge">
                        {!tool.available ? "not installed" : "not supported yet"}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>

            <ToolDetailPane tool={focusedTool} />
          </div>
        )}

        <div className="modal-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={!selected}
            onClick={onContinue}
          >
            Continue
          </button>
        </div>
      </div>
    </div>
  );
}
```

Update the imports: add `useState` to the React import, and `import { ToolDetailPane } from "./ToolDetailPane";`. Delete the now-unused `ToolCard` function. Keep the file's existing top-of-file doc comment but update its second paragraph to describe the list-and-detail layout and the focus-vs-selection distinction.

- [ ] **Step 3: Add the styles**

Find the stylesheet: `grep -rn "tool-card-list" frontend/src`. Add beside the existing `.tool-card-*` rules:

```css
.tool-picker {
  display: grid;
  grid-template-columns: minmax(180px, 260px) 1fr;
  gap: 16px;
  align-items: start;
  max-height: 60vh;
}

.tool-rail {
  display: flex;
  flex-direction: column;
  gap: 4px;
  overflow-y: auto;
  max-height: 60vh;
}

.tool-row {
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: 6px;
  cursor: pointer;
}

.tool-row.focused {
  border-color: var(--border, #ccc);
}

.tool-row.selected {
  background: var(--selected-bg, #eef4ff);
  border-color: var(--accent, #3b82f6);
}

.tool-row.disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.tool-row-main {
  display: flex;
  align-items: baseline;
  gap: 6px;
}

.tool-row-line {
  font-size: 12px;
  opacity: 0.8;
  margin-top: 2px;
}

.tool-row-badge {
  display: inline-block;
  margin-top: 4px;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--warn-bg, #fee);
}

.tool-detail {
  overflow-y: auto;
  max-height: 60vh;
}

.tool-detail-header {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.tool-detail-summary {
  font-size: 13px;
  line-height: 1.5;
}

@media (max-width: 640px) {
  .tool-picker {
    grid-template-columns: 1fr;
  }
}
```

Match the existing CSS variable names in that file rather than inventing new ones — check what `.tool-card.selected` currently uses and reuse it.

- [ ] **Step 4: Verify manually**

```bash
docker compose up -d --build api web worker
```

Open http://localhost:5173, pick a FASTQ, and open the align tool selector. Confirm:
- Five aligner rows appear in the rail, each with a one-liner.
- Clicking a row shows its full summary and strengths on the right.
- Arrow keys move through every row *including* any greyed-out one, and a greyed-out row's reason appears in the pane.
- `Continue` stays disabled while a disabled row is focused and no valid tool is selected.
- The trim selector still works (it shares this component).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/PipelineToolSelector.tsx frontend/src/components/ToolDetailPane.tsx frontend/src/
git commit -m "feat: redesign tool selector as list and detail pane"
```

---

## Task 13: Generated parameter fields and the warning banner

**Files:**
- Create: `frontend/src/components/AlignerParamFields.tsx`
- Modify: `frontend/src/components/AlignDialog.tsx`

- [ ] **Step 1: Create the field renderer**

Create `frontend/src/components/AlignerParamFields.tsx`:

```typescript
import type { AlignParams, ParamFieldMeta } from "../api/types";

/**
 * Renders parameter inputs from registry metadata.
 *
 * Generated rather than hand-written per aligner: four tools with six-odd
 * knobs each is twenty-plus inputs, and the copy explaining them belongs
 * beside the validation that enforces them. The cost is that `help` text
 * lives in a Python table -- worth it while the fields stay this regular,
 * and reversible per-field if one tool ever needs bespoke layout.
 */
export function AlignerParamFields({
  fields,
  params,
  onChange,
}: {
  fields: ParamFieldMeta[];
  params: Partial<AlignParams>;
  onChange: (key: string, value: unknown) => void;
}) {
  return (
    <>
      {fields.map((f) => {
        const value = (params as Record<string, unknown>)[f.key] ?? f.default;

        if (f.kind === "bool") {
          return (
            <label key={f.key} className="trim-check trim-wide">
              <input
                type="checkbox"
                checked={Boolean(value)}
                onChange={(e) => onChange(f.key, e.target.checked)}
              />
              <span>
                {f.label}
                <small style={{ display: "block" }}>{f.help}</small>
              </span>
            </label>
          );
        }

        if (f.kind === "select") {
          return (
            <label key={f.key}>
              <span>{f.label}</span>
              <select
                value={String(value ?? "")}
                onChange={(e) => onChange(f.key, e.target.value)}
              >
                {f.choices.map((c) => (
                  <option key={c.value} value={c.value}>
                    {c.label}
                  </option>
                ))}
              </select>
              <small>{f.help}</small>
            </label>
          );
        }

        return (
          <label key={f.key}>
            <span>{f.label}</span>
            <input
              type={f.kind === "int" ? "number" : "text"}
              {...(f.min != null ? { min: f.min } : {})}
              {...(f.max != null ? { max: f.max } : {})}
              value={String(value ?? "")}
              onChange={(e) =>
                onChange(
                  f.key,
                  f.kind === "int" ? Number(e.target.value) : e.target.value,
                )
              }
            />
            <small>{f.help}</small>
          </label>
        );
      })}
    </>
  );
}
```

- [ ] **Step 2: Wire the dialog**

In `frontend/src/components/AlignDialog.tsx`:

Add the two queries after the existing ones:

```typescript
  const { data: schema } = useQuery({
    queryKey: ["pipelines", "aligner-schema", params?.aligner],
    queryFn: () => api.alignerSchema(params.aligner),
    enabled: !!params?.aligner,
  });

  const { data: envelope } = useQuery({
    queryKey: ["pipelines", "align-envelope", object.id, chosenId],
    queryFn: () => api.alignEnvelope(object.id, chosenId!),
    enabled: chosenId != null,
  });
```

Note: `chosenId` is declared below the current query block — move these two queries after `chosen` is computed, or hoist `chosenId`. Keep the existing ordering intact otherwise.

Compute the band, after `needsIndex`:

```typescript
  // The same arithmetic the backend runs at launch. Local so the numbers move
  // with the sliders; the backend re-checks authoritatively, so a drift here
  // costs a wrong preview rather than a bad run.
  const model = envelope?.models[params?.aligner ?? ""] ?? null;
  const estimate =
    model && envelope
      ? estimateMb(model, {
          referenceBases: envelope.reference_bases,
          threads: params?.threads ?? 4,
          sortMemoryMb: params?.sort_memory_mb ?? 1024,
          buildingIndex: needsIndex,
        })
      : null;
  const band =
    estimate != null && envelope
      ? classify({
          estimateMb: estimate,
          memBudgetMb: envelope.mem_budget_mb,
          threads: params?.threads ?? 4,
          cpuBudget: envelope.cpu_budget,
        })
      : "ok";
  const bandMessage =
    model && envelope && band !== "ok"
      ? explain(model, envelope, {
          threads: params?.threads ?? 4,
          sortMemoryMb: params?.sort_memory_mb ?? 1024,
          buildingIndex: needsIndex,
        })
      : null;
```

Import: `import { classify, estimateMb, explain } from "../lib/estimate";`

Gate the launch button by adding `&& band !== "block"` to the `ready` expression:

```typescript
  const ready =
    defaults != null &&
    chosenId != null &&
    rgComplete &&
    alignerInfo?.available === true &&
    band !== "block";
```

Render the banner just above `<div className="modal-actions">`:

```typescript
        {bandMessage && (
          <div className={band === "block" ? "error-box" : "warn-box"}>
            {bandMessage}
            {band === "block" && (
              <div style={{ marginTop: 4 }}>
                Reduce threads or sort memory, or choose an aligner with a
                smaller index.
              </div>
            )}
          </div>
        )}
```

Replace the hand-written advanced block (the `{advanced && (...)}` section containing the preset select, threads, sort memory, and mark-duplicates inputs) with the generated fields, split by group:

```typescript
        {schema && (
          <div className="trim-fields">
            <AlignerParamFields
              fields={schema.fields.filter((f) => f.group === "biology")}
              params={params}
              onChange={(k, v) => set(k as keyof AlignParams, v as never)}
            />
          </div>
        )}

        <button
          type="button"
          className="trim-advanced-toggle"
          onClick={() => setAdvanced((a) => !a)}
          aria-expanded={advanced}
        >
          <span className="trim-chevron">{advanced ? "▾" : "▸"}</span>
          Performance
        </button>

        {advanced && schema && (
          <div className="trim-fields">
            <AlignerParamFields
              fields={schema.fields.filter((f) => f.group === "performance")}
              params={params}
              onChange={(k, v) => set(k as keyof AlignParams, v as never)}
            />
          </div>
        )}
```

Delete the now-unused `PRESET_LABELS` constant — the choices come from the schema.

- [ ] **Step 3: Verify it compiles**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no errors

- [ ] **Step 4: Verify manually**

At http://localhost:5173, open the align dialog for a FASTQ:
- Selecting bowtie2 shows sensitivity, local, insert size, no-mixed, no-discordant, report-k in the body, and threads/sort memory under Performance.
- Selecting HISAT2 shows strandness, spliced-alignment, intron length, dta.
- Raising threads to 32 with sort memory at 4096 produces a warning, then a block, and the launch button disables at block.
- Lowering them clears it and re-enables launch.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/AlignerParamFields.tsx frontend/src/components/AlignDialog.tsx
git commit -m "feat: generate aligner parameter form and show resource warnings"
```

---

## Task 14: End-to-end verification

**Files:** none — this is verification, not code.

- [ ] **Step 1: Rebuild everything from the main repo root**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose up -d --build api web worker
```

- [ ] **Step 2: Confirm the stack is serving main, not a worktree**

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

Expected: no path contains `.claude/worktrees/`. If one does, re-run Step 1 from the main repo root.

- [ ] **Step 3: Run the full backend suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 4: Run a real bowtie2 alignment**

In the UI: pick a small paired FASTQ and a small reference, choose bowtie2, launch. Watch the Activity view.

Confirm:
- The index build job runs first and succeeds (this is where a wrong suffix tuple surfaces — the handler raises "bowtie2-build exited 0 but did not produce X").
- The alignment job succeeds and produces a BAM.
- `index_bam` follows and flagstat numbers appear.
- The BAM's `@RG` header is right, not a literal backslash-t:

```bash
docker compose exec api sh -c 'samtools view -H /srv/data/blobs/<digest> | grep "@RG"'
```

Find the digest from the object's detail panel. Expected: `@RG	ID:...	SM:...	LB:...	PL:...` with real tabs.

- [ ] **Step 5: Run a real HISAT2 alignment**

Same flow, choosing HISAT2. Set strandness to RF and confirm the run completes.

- [ ] **Step 6: Confirm the existing aligners still work**

Run one minimap2 alignment and, if the host supports it, one bwa-mem2. These share the refactored `_aligner_argv` and params dispatch, so a regression here is the most likely fallout of this whole change.

- [ ] **Step 7: Commit any fixes**

If Steps 4-6 surfaced problems, fix them, re-run, and commit. If everything passed, there is nothing to commit — say so rather than making an empty commit.

---

## Follow-on work (not this plan)

**STAR.** The spec's "STAR: what is already decided" section records the design: subdirectory-carrying sidecar names, `DirectoryLayout`, GTF digest folded into index identity, and the block band as its main safety net. Implement it as its own plan once these two are in use.

**Long-index suffixes.** bowtie2 and HISAT2 emit `.bt2l`/`.ht2l` for references above roughly 4 Gb. If Task 5 Step 7 showed this matters for the references in use here, the layout needs to pick a suffix tuple by reference size. Left out deliberately — it is speculative until a reference that large is actually indexed.
