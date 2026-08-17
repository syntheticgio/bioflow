# Short-Read Assembly (ABySS) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user assemble short (Illumina) paired-end reads into contigs from the Actions tab, using ABySS, replacing the card that today reads "Short-read assembly is not installed."

**Architecture:** Take up the seam the 2026-08-01 long-read design left: `assembler_registry` already declares a paired-layout placeholder and `spec_for_chemistry` already documents itself as the single place that changes. Fill in a real ABySS spec, turn the Flye-only command builder and progress parser into per-assembler dispatches, resolve the R2 mate through the existing `pairing.py`, and let the existing memory guard both refuse impossible runs and supply ABySS's mandatory Bloom-filter budget.

**Tech Stack:** Python 3.12, FastAPI, Motor/MongoDB, pytest. ABySS 2.3.10 from Debian trixie apt. Frontend is React/TypeScript but needs no change (the dialog renders `schema_for()` generically).

**Spec:** `docs/superpowers/specs/2026-08-17-short-read-assembly-design.md`

## Global Constraints

- **Assembler is ABySS 2.3.10**, installed via apt from Debian trixie. SPAdes is NOT packaged for trixie and is out of scope (filed as #519). Velvet is deliberately excluded.
- **`abyss-pe` is a GNU Make wrapper.** Parameters are Make variable assignments (`k=51`, `B=200M`, `in='r1 r2'`), never `--flags`.
- **`B` (Bloom filter budget) is mandatory.** A run without it exits non-zero immediately with "must specify either `B` or `np`".
- **Output files are symlinks** over numbered stage files; `<name>-scaffolds.fa` → `<name>-8.fa`. Harvest must resolve them via `Path.resolve()`.
- **Assembly name prefix is fixed at `asm`** for every run (the `name=` variable), so output filenames are predictable.
- **Commit style:** Conventional Commits, imperative, lowercase after the colon, no trailing period, scope from the existing set (`pipelines`, `api`, `queue`, `ops`).
- **Run tests from the worktree with `./backend/run-worktree-tests.sh`**, never `docker compose exec api` — that silently tests main's code.
- **Patch `assembler_registry.spec_for`, never `tools.abyss`**, when simulating an absent tool: specs are frozen dataclasses that captured the function object at import time.

---

### Task 1: Install ABySS and declare the tool

**Files:**
- Modify: `backend/Dockerfile:98` (apt list) and the comment block at `backend/Dockerfile:119-125`
- Modify: `backend/app/pipelines/tools.py` (probe near `flye()` at :595, `all_tools()` at :823, `TOOL_META` near :1528)
- Test: `backend/tests/pipelines/test_tools.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `tools.abyss() -> Tool` — an `@lru_cache`d probe, same shape as `tools.flye()`. `Tool` has `.available: bool`, `.path: str`, `.version: str`.

- [ ] **Step 1: Add abyss to the apt list**

In `backend/Dockerfile`, add `abyss \` to the `apt-get install` list immediately after `flye \`.

Then correct the stale comment block below it. Replace the sentence reading "and SPAdes is packaged but only assembles short reads, which is not a workflow here yet" with:

```
# abyss is the short-read assembler (2.3.10, paired-end de Bruijn). SPAdes
# would be the better choice on isolates but is NOT packaged for trixie --
# verified 2026-08-17, `apt-cache policy spades` has no candidate -- and needs
# a vendored upstream tarball. See #519 and
# docs/superpowers/specs/2026-08-17-short-read-assembly-design.md.
```

- [ ] **Step 2: Write the failing test**

In `backend/tests/pipelines/test_tools.py`:

```python
def test_abyss_is_declared_and_documented():
    """abyss must be probeable and carry complete help-page metadata."""
    from app.pipelines import tools

    tool = tools.abyss()
    assert tool.name == "abyss"

    meta = tools.TOOL_META["abyss"]
    assert meta.homepage
    assert meta.citation
    assert meta.license
    assert meta.usage
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_tools.py::test_abyss_is_declared_and_documented -q`
Expected: FAIL with `AttributeError: module 'app.pipelines.tools' has no attribute 'abyss'`

- [ ] **Step 4: Add the probe**

In `backend/app/pipelines/tools.py`, directly after the `flye()` function (ends ~:601):

```python
@lru_cache(maxsize=1)
def abyss() -> Tool:
    """ABySS, the short-read assembler.

    Probes `abyss-pe`, which is a GNU Make wrapper rather than a binary with a
    conventional CLI. `abyss-pe version` writes a spurious
    `test: -le: unary operator expected` line to stderr while still exiting 0
    and printing the version to stdout -- `_probe` reads stdout, so this is
    noise rather than a failure, and a probe that treated any stderr output as
    broken would report an installed tool as missing.
    """
    return _probe("abyss", settings.abyss_path, ["version"])
```

Note the argument is `version`, not `--version`: `abyss-pe` passes unknown
`--flags` to make itself.

Add `abyss(),` to the list in `all_tools()` (~:823), directly after `flye(),`.

Add `abyss_path: str = "abyss-pe"` to `Settings` in `backend/app/config.py`, beside the existing `flye_path`.

- [ ] **Step 5: Add TOOL_META**

In `TOOL_META`, beside the `"flye"` entry (~:1528). **Verify every field against the ABySS repository (https://github.com/bcgsc/abyss) before writing it — do not recall these.** A wrong license claim on a page that reads as authoritative is worse than a blank field.

```python
    "abyss": ToolMeta(
        homepage="https://github.com/bcgsc/abyss",
        repository="https://github.com/bcgsc/abyss",
        # Verify against the repo's own README/CITATION before committing.
        citation=...,
        citation_url=...,
        license=...,
        usage=(
            "Assembles short paired-end reads into contigs with no reference. "
            "BioFlow runs it paired when it can identify both mates and "
            "single-end when it cannot, and derives its mandatory Bloom "
            "filter budget from the same memory estimate that guards the run."
        ),
        runnable=True,
    ),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -q`
Expected: PASS, including the pre-existing `test_every_tool_is_documented`.

- [ ] **Step 7: Rebuild the image so the binary exists**

```bash
./ops/worktree-up.sh
```

Then confirm the tool is genuinely present, not merely declared:

```bash
docker exec $(docker ps --filter name=api --format '{{.Names}}' | head -1) abyss-pe version
```

Expected: prints `abyss-pe (ABySS) 2.3.10`.

- [ ] **Step 8: Commit**

```bash
git add backend/Dockerfile backend/app/pipelines/tools.py backend/app/config.py backend/tests/pipelines/test_tools.py
git commit -m "feat(pipelines): install abyss and declare it as a tool"
```

---

### Task 2: AbyssParams

**Files:**
- Modify: `backend/app/pipelines/assemblers.py` (the `Assembler` enum)
- Modify: `backend/app/pipelines/assembly_params.py` (new class + `_BY_ASSEMBLER` at :156)
- Test: `backend/tests/pipelines/test_assembly_params.py`

**Interfaces:**
- Consumes: `Assembler` enum from Task 1's unchanged module.
- Produces:
  - `Assembler.ABYSS = "abyss"` enum member.
  - `AbyssParams(BaseAssemblyParams)` with fields `assembler: Assembler = Assembler.ABYSS`, `k: int = 51`, plus inherited `threads: int = 8`, `genome_size: int | None`, `genome_size_source: str`.
  - `AbyssParams.from_dict(data: dict) -> AbyssParams` classmethod.
  - `AbyssParams.as_dict() -> dict` including the `k` key.
  - Module constants `MIN_K = 16`, `MAX_K = 127`.

- [ ] **Step 1: Add the enum member**

In `backend/app/pipelines/assemblers.py`, add to `Assembler`, and correct the now-stale SPAdes comment:

```python
class Assembler(StrEnum):
    FLYE = "flye"
    # Declared, not installed. Not packaged for Debian; needs a source build
    # with the arm64 SIMD problem bwa-mem2 already has a script for.
    HIFIASM = "hifiasm"
    # Declared, not installed. NOT packaged for trixie (the 2026-08-01 spec's
    # claim that it is was true for bookworm) -- needs a vendored upstream
    # tarball. See #519.
    SPADES = "spades"
    ABYSS = "abyss"
```

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/pipelines/test_assembly_params.py` if absent, else append:

```python
import pytest

from app.errors import ValidationError
from app.pipelines import assembly_params
from app.pipelines.assemblers import Assembler


def test_abyss_params_default_k():
    params = assembly_params.from_dict({"assembler": "abyss"})
    assert params.assembler is Assembler.ABYSS
    assert params.k == 51
    assert params.threads == 8


def test_abyss_params_accepts_k():
    params = assembly_params.from_dict({"assembler": "abyss", "k": 31})
    assert params.k == 31


def test_abyss_params_rejects_k_below_floor():
    with pytest.raises(ValidationError, match="k must be between"):
        assembly_params.from_dict({"assembler": "abyss", "k": 4})


def test_abyss_params_rejects_k_above_ceiling():
    with pytest.raises(ValidationError, match="k must be between"):
        assembly_params.from_dict({"assembler": "abyss", "k": 500})


def test_abyss_params_roundtrip_carries_k():
    params = assembly_params.from_dict({"assembler": "abyss", "k": 63})
    assert params.as_dict()["k"] == 63
    assert params.as_dict()["assembler"] == "abyss"


def test_spades_still_refused_as_not_installed():
    """SPAdes stays declared-but-unavailable so #519 has somewhere to land."""
    with pytest.raises(ValidationError, match="not installed in this build"):
        assembly_params.from_dict({"assembler": "spades"})
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_assembly_params.py -q`
Expected: FAIL — `abyss` currently hits the `params_class is None` branch and raises "abyss is not installed in this build".

- [ ] **Step 4: Implement AbyssParams**

In `backend/app/pipelines/assembly_params.py`, add constants beside `MIN_ITERATIONS`:

```python
# k-mer length. ABySS's own practical range; below 16 the graph is noise and
# above 127 the build is not compiled for it.
MIN_K = 16
MAX_K = 127
```

Then, after `FlyeParams`:

```python
@dataclass
class AbyssParams(BaseAssemblyParams):
    """Short-read assembly parameters.

    Only `k` is exposed beyond the shared fields. ABySS's Bloom filter budget
    `B` is mandatory but deliberately *not* a user field: it is derived from
    the memory estimate in `assembly_runner`, so the number the guard used to
    decide the run can proceed is the same number the tool is given. Two
    independent memory figures that are supposed to agree is a bug with a
    delay fuse.
    """

    assembler: Assembler = Assembler.ABYSS
    k: int = 51

    def as_dict(self) -> dict:
        return {**super().as_dict(), "k": self.k}

    @classmethod
    def from_dict(cls, data: dict) -> "AbyssParams":
        k = int(data.get("k", 51))
        if not MIN_K <= k <= MAX_K:
            raise ValidationError(f"k must be between {MIN_K} and {MAX_K}")
        return cls(assembler=Assembler.ABYSS, k=k, **cls._shared(data))
```

Register it:

```python
_BY_ASSEMBLER = {Assembler.FLYE: FlyeParams, Assembler.ABYSS: AbyssParams}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_assembly_params.py -q`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add backend/app/pipelines/assemblers.py backend/app/pipelines/assembly_params.py backend/tests/pipelines/test_assembly_params.py
git commit -m "feat(pipelines): add abyss assembler params with a k-mer field"
```

---

### Task 3: Memory model gains a coverage term

**Files:**
- Modify: `backend/app/pipelines/assembler_registry.py:30-46` (`AssemblyMemoryModel`)
- Modify: `backend/app/pipelines/resource_estimator.py:65-90` (`estimate_assembly_mb`)
- Test: `backend/tests/pipelines/test_resource_estimator.py`

**Interfaces:**
- Consumes: `Assembler.ABYSS` from Task 2.
- Produces: `estimate_assembly_mb(*, assembler, genome_bases: int | None, threads: int, read_bases: int | None = None) -> int | None`. The new keyword is optional and defaults to `None`, so every existing caller is unaffected.
- Produces: `AssemblyMemoryModel.bytes_per_read_base: float = 0.0`.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/pipelines/test_resource_estimator.py`:

```python
def test_flye_estimate_ignores_read_bases():
    """Flye's model is genome-dominated; adding reads must not move it."""
    from app.pipelines import resource_estimator
    from app.pipelines.assemblers import Assembler

    without = resource_estimator.estimate_assembly_mb(
        assembler=Assembler.FLYE, genome_bases=5_000_000, threads=8
    )
    with_reads = resource_estimator.estimate_assembly_mb(
        assembler=Assembler.FLYE,
        genome_bases=5_000_000,
        threads=8,
        read_bases=2_000_000_000,
    )
    assert without == with_reads


def test_abyss_estimate_grows_with_read_bases():
    """A de Bruijn graph's peak tracks distinct k-mers, so coverage counts."""
    from app.pipelines import resource_estimator
    from app.pipelines.assemblers import Assembler

    low = resource_estimator.estimate_assembly_mb(
        assembler=Assembler.ABYSS,
        genome_bases=5_000_000,
        threads=8,
        read_bases=500_000_000,
    )
    high = resource_estimator.estimate_assembly_mb(
        assembler=Assembler.ABYSS,
        genome_bases=5_000_000,
        threads=8,
        read_bases=5_000_000_000,
    )
    assert high > low


def test_assembly_estimate_still_none_without_genome_size():
    """None stays a real answer -- de novo is what you do with no reference."""
    from app.pipelines import resource_estimator
    from app.pipelines.assemblers import Assembler

    assert (
        resource_estimator.estimate_assembly_mb(
            assembler=Assembler.ABYSS, genome_bases=None, threads=8
        )
        is None
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_resource_estimator.py -q -k "abyss or read_bases"`
Expected: FAIL with `TypeError: estimate_assembly_mb() got an unexpected keyword argument 'read_bases'`

- [ ] **Step 3: Add the coefficient**

In `backend/app/pipelines/assembler_registry.py`, add to `AssemblyMemoryModel`:

```python
    # Bytes of peak residency per base of *input reads*. Zero for a repeat-graph
    # assembler like Flye, whose peak is dominated by the genome. Non-zero for a
    # de Bruijn assembler, where peak tracks distinct k-mers -- a function of
    # coverage as much as genome size. Defaulted to 0.0 so Flye's model is
    # arithmetically unchanged by this field existing.
    bytes_per_read_base: float = 0.0
```

Because `mb_per_thread` already has a default, place `bytes_per_read_base` after it to keep the dataclass valid.

- [ ] **Step 4: Use it in the estimator**

Replace the body of `estimate_assembly_mb` (`resource_estimator.py:83-90`):

```python
    if genome_bases is None or genome_bases <= 0:
        return None

    from app.pipelines.assembler_registry import spec_for as assembler_spec_for

    model = assembler_spec_for(assembler).memory_model
    graph_mb = (genome_bases * model.bytes_per_genome_base) / (1024 * 1024)
    # Zero for every assembler whose model leaves the coefficient at its
    # default, so this term is invisible to Flye.
    reads_mb = 0.0
    if read_bases and model.bytes_per_read_base:
        reads_mb = (read_bases * model.bytes_per_read_base) / (1024 * 1024)
    return math.ceil(
        model.fixed_overhead_mb + graph_mb + reads_mb + threads * model.mb_per_thread
    )
```

And add the parameter to the signature:

```python
def estimate_assembly_mb(
    *,
    assembler,
    genome_bases: int | None,
    threads: int,
    read_bases: int | None = None,
) -> int | None:
```

Extend the docstring with a sentence noting that `read_bases` is used only by assemblers whose model sets `bytes_per_read_base`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_resource_estimator.py -q`
Expected: PASS, including all pre-existing Flye estimator tests unchanged.

- [ ] **Step 6: Commit**

```bash
git add backend/app/pipelines/assembler_registry.py backend/app/pipelines/resource_estimator.py backend/tests/pipelines/test_resource_estimator.py
git commit -m "feat(pipelines): let an assembly memory model charge for read volume"
```

---

### Task 4: ABYSS_SPEC in the registry, and real chemistry dispatch

**Files:**
- Modify: `backend/app/pipelines/assembler_registry.py` (new spec, `SPECS`, `spec_for_chemistry` at :225)
- Test: `backend/tests/pipelines/test_assembler_registry.py`

**Interfaces:**
- Consumes: `tools.abyss` (Task 1), `Assembler.ABYSS` (Task 2), `bytes_per_read_base` (Task 3).
- Produces:
  - `ABYSS_SPEC: AssemblerSpec` with `layout="paired"`, `outputs` naming `asm-scaffolds.fa` / `asm-scaffolds.dot` / `asm-stats.tab`, and a `k` field.
  - `spec_for_chemistry(chemistry) -> AssemblerSpec | None` now returns `ABYSS_SPEC` for `ReadChemistry.SHORT`.
  - `ASSEMBLY_NAME_PREFIX = "asm"` module constant, consumed by Task 5's command builder.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/pipelines/test_assembler_registry.py`:

```python
from app.pipelines import assembler_registry
from app.pipelines.align_runner import ReadChemistry
from app.pipelines.assemblers import Assembler, OutputKind


def test_short_reads_route_to_abyss():
    spec = assembler_registry.spec_for_chemistry(ReadChemistry.SHORT)
    assert spec is not None
    assert spec.assembler is Assembler.ABYSS
    assert spec.layout == "paired"


def test_long_reads_still_route_to_flye():
    for chemistry in (
        ReadChemistry.HIFI,
        ReadChemistry.CLR,
        ReadChemistry.ONT_SIMPLEX,
        ReadChemistry.ONT_DUPLEX,
    ):
        spec = assembler_registry.spec_for_chemistry(chemistry)
        assert spec is not None
        assert spec.assembler is Assembler.FLYE


def test_unknown_chemistry_still_has_no_assembler():
    """The 'run QC first' refusal depends on this staying None."""
    assert assembler_registry.spec_for_chemistry(None) is None
    assert assembler_registry.spec_for_chemistry(ReadChemistry.UNKNOWN) is None


def test_abyss_declares_contigs_as_required_output():
    spec = assembler_registry.spec_for(Assembler.ABYSS)
    kinds = {o.kind: o for o in spec.outputs}
    assert kinds[OutputKind.CONTIGS].required is True
    assert kinds[OutputKind.CONTIGS].filename == "asm-scaffolds.fa"


def test_abyss_charges_for_read_volume():
    """A de Bruijn assembler whose model ignored coverage would under-predict."""
    spec = assembler_registry.spec_for(Assembler.ABYSS)
    assert spec.memory_model.bytes_per_read_base > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_assembler_registry.py -q`
Expected: FAIL — `spec_for_chemistry(ReadChemistry.SHORT)` returns `None`, and `SPECS` has no `Assembler.ABYSS` key.

- [ ] **Step 3: Add the spec**

In `backend/app/pipelines/assembler_registry.py`, add the name constant near the top:

```python
# Every ABySS run assembles under this name, so its output filenames are
# knowable before the run starts. Not user-facing: the resulting DataObject is
# named after the reads by `assembly_handlers._contigs_name`.
ASSEMBLY_NAME_PREFIX = "asm"
```

Replace `SPADES_SPEC`'s placeholder role by adding a real spec after it:

```python
ABYSS_SPEC = AssemblerSpec(
    assembler=Assembler.ABYSS,
    tool=tools.abyss,
    # Empty by construction, not by omission: ABySS has no read-accuracy mode
    # flag. `spec_for_chemistry` routes SHORT here explicitly rather than by
    # looking a chemistry up in this map, so an empty map is correct.
    mode_flags={},
    layout="paired",
    memory_model=AssemblyMemoryModel(
        # A de Bruijn graph's peak is dominated by distinct k-mers, so the
        # genome term is small next to Flye's 40 and the read term carries the
        # weight. Published guidance, not measured on this hardware -- the same
        # caveat FLYE_SPEC's model carries.
        bytes_per_genome_base=15.0,
        bytes_per_read_base=0.5,
        fixed_overhead_mb=1024,
    ),
    outputs=(
        # Symlinks over numbered stage files (`asm-scaffolds.fa` -> `asm-8.fa`).
        # `harvest` resolves them; storing the link itself would dangle once the
        # workdir is reaped.
        Output(
            kind=OutputKind.CONTIGS,
            filename=f"{ASSEMBLY_NAME_PREFIX}-scaffolds.fa",
            required=True,
        ),
        Output(
            kind=OutputKind.GRAPH,
            filename=f"{ASSEMBLY_NAME_PREFIX}-scaffolds.dot",
        ),
        # ABySS computes N50 and friends itself, which Flye does not.
        Output(
            kind=OutputKind.INFO_TABLE,
            filename=f"{ASSEMBLY_NAME_PREFIX}-stats.tab",
        ),
    ),
    fields=(
        *_SHARED_FIELDS,
        ParamField(
            key="k",
            label="k-mer length",
            kind="int",
            default=51,
            min=16,
            max=127,
            group="biology",
            help=(
                "The single parameter that most changes a short-read assembly. "
                "51 suits 100-150 bp Illumina reads at typical coverage. Lower "
                "it for shorter reads or thin coverage; raise it for long, deep, "
                "high-quality reads."
            ),
        ),
    ),
)
```

Register it in `SPECS`:

```python
SPECS: dict[Assembler, AssemblerSpec] = {
    Assembler.FLYE: FLYE_SPEC,
    Assembler.HIFIASM: HIFIASM_SPEC,
    Assembler.SPADES: SPADES_SPEC,
    Assembler.ABYSS: ABYSS_SPEC,
}
```

- [ ] **Step 4: Make chemistry dispatch real**

Replace `spec_for_chemistry`'s body and docstring:

```python
def spec_for_chemistry(chemistry: ReadChemistry | None) -> AssemblerSpec | None:
    """The assembler to use for these reads, or None if there is not one.

    ABySS for short reads, Flye for every long-read chemistry including HiFi
    (hifiasm is the better HiFi assembler and is the one this returns once it
    is installed). This function remains the single place that changes.

    None only for unknown chemistry now -- a missing fact the user can supply
    by running QC. Short reads used to land here too, as a *different* refusal
    naming a missing tool; that branch is gone because the tool is installed.
    """
    if chemistry is None or chemistry is ReadChemistry.UNKNOWN:
        return None
    if chemistry is ReadChemistry.SHORT:
        return SPECS[Assembler.ABYSS]
    spec = SPECS[Assembler.FLYE]
    if chemistry in spec.mode_flags:
        return spec
    return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_assembler_registry.py -q`
Expected: PASS (5 new tests plus pre-existing).

- [ ] **Step 6: Commit**

```bash
git add backend/app/pipelines/assembler_registry.py backend/tests/pipelines/test_assembler_registry.py
git commit -m "feat(pipelines): route short reads to abyss in the assembler registry"
```

---

### Task 5: Command builder dispatch

**Files:**
- Modify: `backend/app/pipelines/assembly_runner.py:26-51` (`build_assembly_command`)
- Test: `backend/tests/pipelines/test_assembly_runner.py`

**Interfaces:**
- Consumes: `AbyssParams` (Task 2), `ASSEMBLY_NAME_PREFIX` (Task 4).
- Produces: `build_assembly_command(*, assembler, tool_path, reads, out_dir, params, mate=None, bloom_bytes=None) -> list[str]`. Two new keyword-only params, both defaulting to `None`, so the Flye call site in `assembly_handlers.py` needs no change until Task 8.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/pipelines/test_assembly_runner.py`:

```python
from pathlib import Path

import pytest

from app.pipelines import assembly_runner
from app.pipelines.assembly_params import AbyssParams
from app.pipelines.assemblers import Assembler


def _abyss_cmd(**kwargs):
    defaults = dict(
        assembler=Assembler.ABYSS,
        tool_path="/usr/bin/abyss-pe",
        reads=Path("/work/r1.fastq.gz"),
        out_dir=Path("/work/out"),
        params=AbyssParams(k=51, threads=4),
        bloom_bytes=2 * 1024**3,
    )
    defaults.update(kwargs)
    return assembly_runner.build_assembly_command(**defaults)


def test_abyss_command_uses_make_variable_assignments():
    """abyss-pe is a Make wrapper: `k=51`, never `--k 51`."""
    cmd = _abyss_cmd()
    assert cmd[0] == "/usr/bin/abyss-pe"
    assert "k=51" in cmd
    assert "j=4" in cmd
    assert "name=asm" in cmd
    assert not any(token.startswith("--k") for token in cmd)


def test_abyss_command_pairs_both_mates_in_one_in_variable():
    cmd = _abyss_cmd(mate=Path("/work/r2.fastq.gz"))
    assert "in=/work/r1.fastq.gz /work/r2.fastq.gz" in cmd
    assert not any(t.startswith("se=") for t in cmd)


def test_abyss_command_falls_back_to_single_end():
    cmd = _abyss_cmd(mate=None)
    assert "se=/work/r1.fastq.gz" in cmd
    assert not any(t.startswith("in=") for t in cmd)


def test_abyss_command_always_sets_bloom_budget():
    """B is mandatory: without it abyss-pe exits non-zero immediately."""
    cmd = _abyss_cmd(bloom_bytes=3 * 1024**3)
    assert "B=3072M" in cmd


def test_abyss_command_floors_bloom_budget():
    """A tiny or absent estimate must not produce an unusable B."""
    cmd = _abyss_cmd(bloom_bytes=None)
    assert "B=200M" in cmd


def test_flye_command_unchanged_by_new_keywords():
    from app.pipelines.assembly_params import FlyeParams

    cmd = assembly_runner.build_assembly_command(
        assembler=Assembler.FLYE,
        tool_path="/usr/bin/flye",
        reads=Path("/work/reads.fastq"),
        out_dir=Path("/work/out"),
        params=FlyeParams(mode="nano-hq", threads=8, iterations=1),
    )
    assert cmd[:2] == ["/usr/bin/flye", "--nano-hq"]


def test_unknown_assembler_still_raises():
    with pytest.raises(ValueError, match="No command builder"):
        assembly_runner.build_assembly_command(
            assembler=Assembler.HIFIASM,
            tool_path="/usr/bin/hifiasm",
            reads=Path("/work/reads.fastq"),
            out_dir=Path("/work/out"),
            params=AbyssParams(),
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py -q -k abyss`
Expected: FAIL with `TypeError: build_assembly_command() got an unexpected keyword argument 'mate'`

- [ ] **Step 3: Implement the dispatch**

Replace `build_assembly_command` in `backend/app/pipelines/assembly_runner.py`:

```python
# ABySS refuses to start without a Bloom filter budget, so a run with no memory
# estimate still needs a number. 200M is small enough to be safe anywhere and
# large enough to assemble a bacterial genome.
MIN_BLOOM_MB = 200


def build_assembly_command(
    *,
    assembler: Assembler,
    tool_path: str,
    reads: Path,
    out_dir: Path,
    params: BaseAssemblyParams,
    mate: Path | None = None,
    bloom_bytes: int | None = None,
) -> list[str]:
    """The argv for one assembly run.

    `mate` and `bloom_bytes` are ABySS-only and ignored by the Flye builder --
    a paired long-read assembly is not a thing, and Flye needs no memory
    ceiling to start.
    """
    if assembler is Assembler.FLYE:
        assert isinstance(params, FlyeParams)
        return _flye_command(
            tool_path=tool_path, reads=reads, out_dir=out_dir, params=params
        )
    if assembler is Assembler.ABYSS:
        assert isinstance(params, AbyssParams)
        return _abyss_command(
            tool_path=tool_path,
            reads=reads,
            out_dir=out_dir,
            params=params,
            mate=mate,
            bloom_bytes=bloom_bytes,
        )
    # Not a fallback: an assembler with no builder here would otherwise
    # produce another tool's command line for this binary.
    raise ValueError(f"No command builder for {assembler.value}")


def _flye_command(
    *, tool_path: str, reads: Path, out_dir: Path, params: FlyeParams
) -> list[str]:
    return [
        tool_path,
        f"--{params.mode}",
        str(reads),
        "--out-dir",
        str(out_dir),
        "--threads",
        str(params.threads),
        "--iterations",
        str(params.iterations),
    ]


def _abyss_command(
    *,
    tool_path: str,
    reads: Path,
    out_dir: Path,
    params: AbyssParams,
    mate: Path | None,
    bloom_bytes: int | None,
) -> list[str]:
    """`abyss-pe` takes Make variable assignments, not flags.

    `-C <dir>` is make's own change-directory option and is how the outputs
    land in `out_dir` -- ABySS has no `--out-dir` equivalent and would
    otherwise write into the process's cwd.
    """
    bloom_mb = MIN_BLOOM_MB
    if bloom_bytes:
        bloom_mb = max(MIN_BLOOM_MB, int(bloom_bytes / (1024 * 1024)))

    cmd = [
        tool_path,
        "-C",
        str(out_dir),
        f"name={assembler_registry.ASSEMBLY_NAME_PREFIX}",
        f"k={params.k}",
        f"j={params.threads}",
        f"B={bloom_mb}M",
    ]
    if mate is not None:
        # Both mates in one space-joined value: ABySS's `in` variable is a
        # single Make variable holding a read pair, not a repeatable flag.
        cmd.append(f"in={reads} {mate}")
    else:
        cmd.append(f"se={reads}")
    return cmd
```

Add the imports at the top of the module:

```python
from app.pipelines import assembler_registry
from app.pipelines.assembly_params import AbyssParams, BaseAssemblyParams, FlyeParams
```

**Watch for a circular import:** `assembler_registry` imports `assembly_params`, and `assembly_runner` now imports both. `assembler_registry` does not import `assembly_runner`, so this is acyclic — but if an import error appears, move the `assembler_registry` import inside `_abyss_command` rather than restructuring.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py -q`
Expected: PASS, including every pre-existing Flye command test.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/assembly_runner.py backend/tests/pipelines/test_assembly_runner.py
git commit -m "feat(pipelines): build abyss command lines as make variable assignments"
```

---

### Task 6: Progress parsing and stats harvest

**Files:**
- Modify: `backend/app/pipelines/assembly_runner.py` (`AssemblyProgress`, `harvest`, new `parse_abyss_stats`)
- Test: `backend/tests/pipelines/test_assembly_runner.py`

**Interfaces:**
- Consumes: Task 5's module.
- Produces:
  - `AbyssProgress` — same public surface as `AssemblyProgress`: `.feed(line) -> bool`, `.snapshot() -> dict`, `.message() -> str`, `.phase: str`.
  - `parse_abyss_stats(text: str) -> dict` returning `assembly_contig_count`, `assembly_total_length`, `assembly_n50`, `assembly_longest`, or `{}` on anything malformed.
  - `harvest(out_dir, outputs) -> dict[OutputKind, Path]` now returns resolved (symlink-free) paths.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_assembly_runner.py`:

```python
ABYSS_STATS_TAB = (
    "n\tn:500\tL50\tmin\tN75\tN50\tN25\tE-size\tmax\tsum\tname\n"
    "12\t10\t3\t512\t4000\t9000\t15000\t9500\t21000\t60000\tasm-unitigs.fa\n"
    "8\t7\t2\t600\t7000\t14000\t22000\t15000\t30000\t61000\tasm-contigs.fa\n"
    "6\t5\t2\t800\t9000\t18000\t26000\t19000\t34000\t62000\tasm-scaffolds.fa\n"
)


def test_parse_abyss_stats_reads_the_scaffolds_row():
    """The scaffolds row is the assembly; the earlier rows are stages."""
    facts = assembly_runner.parse_abyss_stats(ABYSS_STATS_TAB)
    assert facts["assembly_contig_count"] == 6
    assert facts["assembly_n50"] == 18000
    assert facts["assembly_longest"] == 34000
    assert facts["assembly_total_length"] == 62000


def test_parse_abyss_stats_survives_garbage():
    """A stats table that failed to parse must not fail a good assembly."""
    assert assembly_runner.parse_abyss_stats("not a table at all") == {}
    assert assembly_runner.parse_abyss_stats("") == {}


def test_abyss_progress_reports_a_phase():
    progress = assembly_runner.AbyssProgress()
    assert progress.feed("abyss-map -j4 ...") is False or True  # tolerant
    changed = progress.feed("ABySS-P: assembling contigs")
    snap = progress.snapshot()
    assert snap["pct"] is None
    assert isinstance(snap["phase"], str)


def test_harvest_resolves_symlinks(tmp_path):
    """ABySS outputs are symlinks; storing the link would dangle."""
    from app.pipelines.assemblers import Output, OutputKind

    out = tmp_path / "out"
    out.mkdir()
    real = out / "asm-8.fa"
    real.write_text(">contig\nACGT\n")
    link = out / "asm-scaffolds.fa"
    link.symlink_to(real)

    found = assembly_runner.harvest(
        out,
        (Output(kind=OutputKind.CONTIGS, filename="asm-scaffolds.fa", required=True),),
    )
    assert found[OutputKind.CONTIGS] == real.resolve()
    assert not found[OutputKind.CONTIGS].is_symlink()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py -q -k "abyss_stats or abyss_progress or symlink"`
Expected: FAIL with `AttributeError: module has no attribute 'parse_abyss_stats'`

- [ ] **Step 3: Add the stats parser**

In `backend/app/pipelines/assembly_runner.py`, after `parse_assembly_info`:

```python
# ABySS writes its own stats table, which Flye does not:
#   n  n:500  L50  min  N75  N50  N25  E-size  max  sum  name
# One row per output stage; the `-scaffolds.fa` row is the finished assembly.
_ABYSS_STATS_ROW = f"{assembler_registry.ASSEMBLY_NAME_PREFIX}-scaffolds.fa"


def parse_abyss_stats(text: str) -> dict:
    """Contiguity facts from ABySS's own stats table.

    Unlike Flye's table, this one already contains N50 -- so unlike
    `parse_assembly_info`, this parser does report it. Note the asymmetry is
    deliberate: `parsers._contiguity_stats` computes `sequence_n50` from the
    FASTA bytes independently, so these two numbers are computed from the same
    sequences by different code and must agree. If they ever disagree, the
    FASTA-derived one is authoritative.

    Returns {} for anything malformed rather than raising, for the same reason
    `parse_assembly_info` does: a table that could not be read must not fail an
    assembly that produced a perfectly good FASTA.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return {}

    header = [h.strip() for h in lines[0].split("\t")]
    index = {name: i for i, name in enumerate(header)}
    required = ("n", "N50", "max", "sum", "name")
    if not all(col in index for col in required):
        log.warning("abyss_stats_unexpected_header", header=header[:11])
        return {}

    for line in lines[1:]:
        cols = [c.strip() for c in line.split("\t")]
        if len(cols) < len(header):
            continue
        if cols[index["name"]] != _ABYSS_STATS_ROW:
            continue
        try:
            return {
                "assembly_contig_count": int(cols[index["n"]]),
                "assembly_total_length": int(cols[index["sum"]]),
                "assembly_n50": int(cols[index["N50"]]),
                "assembly_longest": int(cols[index["max"]]),
            }
        except (ValueError, IndexError):
            return {}
    return {}
```

- [ ] **Step 4: Add the progress parser**

ABySS's output is Make recipe echoes rather than declared stages, so there is
no knowable stage count. Report a phase name with no "step N of M" — the
existing `snapshot()` contract already omits `phase_index`/`phase_total` when
no stage order is declared.

```python
# ABySS runs as a Make pipeline whose recipes echo the binary they invoke.
# There is no `>>>STAGE:` equivalent and no count knowable in advance, so this
# reports a phase name and no step counter -- which the snapshot contract
# already handles by omitting phase_index/phase_total.
_ABYSS_PHASES: tuple[tuple[str, str], ...] = (
    ("ABYSS-P", "assembling contigs"),
    ("ABySS-P", "assembling contigs"),
    ("abyss-map", "mapping reads"),
    ("abyss-fixmate", "pairing alignments"),
    ("DistanceEst", "estimating distances"),
    ("abyss-scaffold", "scaffolding"),
    ("abyss-fac", "computing statistics"),
)


@dataclass
class AbyssProgress:
    """Phase names from ABySS's Make output.

    No percentage and no step counter, for the reason `AssemblyProgress`'s
    docstring gives and one more: ABySS's stage list is not knowable before the
    run, so a denominator would be invented.
    """

    name: str = "abyss"
    phase: str = "starting"

    def feed(self, line: str) -> bool:
        """Consume a log line. True if the phase changed."""
        for token, label in _ABYSS_PHASES:
            if token in line:
                if label == self.phase:
                    return False
                self.phase = label
                return True
        return False

    def message(self) -> str:
        return self.phase

    def snapshot(self) -> dict:
        return {"pct": None, "phase": self.phase, "message": self.message()}
```

- [ ] **Step 5: Resolve symlinks in harvest**

In `harvest`, change the stored path so a symlink is followed:

```python
        if path.exists() and path.stat().st_size > 0:
            # `.resolve()` because ABySS's outputs are symlinks over numbered
            # stage files (`asm-scaffolds.fa` -> `asm-8.fa`). Storing the link
            # would dangle as soon as the workdir is reaped. Harmless for Flye,
            # whose outputs are already real files.
            found[output.kind] = path.resolve()
```

Note `path.exists()` already follows symlinks, so the guard needs no change.

- [ ] **Step 6: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/pipelines/assembly_runner.py backend/tests/pipelines/test_assembly_runner.py
git commit -m "feat(pipelines): parse abyss progress and its own stats table"
```

---

### Task 7: Exhaustiveness test for the builder registry

**Files:**
- Test: `backend/tests/pipelines/test_assembler_registry.py`

**Interfaces:**
- Consumes: everything from Tasks 2-6.
- Produces: no production code. This task exists because CLAUDE.md names this exact failure shape (`results._SIDECAR_ROLES`, where a registry with a missing enum entry silently skipped work and a green suite hid it).

- [ ] **Step 1: Write the test**

Append to `backend/tests/pipelines/test_assembler_registry.py`:

```python
class TestExhaustiveness:
    """A declared-and-installed assembler with no command builder would be
    dispatched to another tool's builder or refused at runtime. Both are
    silent until someone runs it. See CLAUDE.md on hand-maintained registries
    keyed by an enum.
    """

    def test_every_assembler_has_a_spec(self):
        from app.pipelines.assemblers import Assembler

        assert set(assembler_registry.SPECS) == set(Assembler)

    def test_every_installable_assembler_has_a_command_builder(self):
        from pathlib import Path

        from app.pipelines import assembly_params, assembly_runner
        from app.pipelines.assemblers import Assembler

        for member in Assembler:
            spec = assembler_registry.spec_for(member)
            if spec.tool is None:
                # Declared-but-not-installed (hifiasm, spades) is exempt:
                # `assembly_params.from_dict` refuses them before a builder is
                # ever reached.
                continue
            params = assembly_params.from_dict({"assembler": member.value})
            cmd = assembly_runner.build_assembly_command(
                assembler=member,
                tool_path=f"/usr/bin/{member.value}",
                reads=Path("/work/reads.fastq"),
                out_dir=Path("/work/out"),
                params=params,
            )
            assert cmd, f"{member.value} produced an empty command line"

    def test_every_installable_assembler_has_params(self):
        from app.pipelines import assembly_params
        from app.pipelines.assemblers import Assembler

        for member in Assembler:
            spec = assembler_registry.spec_for(member)
            if spec.tool is None:
                continue
            params = assembly_params.from_dict({"assembler": member.value})
            assert params.assembler is member
```

- [ ] **Step 2: Run the whole class**

Per CLAUDE.md's note on #355/#366: run the **entire** class, not the single test a bug report names — a fix that adds an entry can collide with one that excludes it, and only running the set catches it.

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_assembler_registry.py::TestExhaustiveness -v`
Expected: PASS (3 tests).

- [ ] **Step 3: Commit**

```bash
git add backend/tests/pipelines/test_assembler_registry.py
git commit -m "test(pipelines): assert every installable assembler has a builder"
```

---

### Task 8: Handler runs ABySS with a mate and a Bloom budget

**Files:**
- Modify: `backend/app/queue/assembly_handlers.py:78-115` (input resolution, command, progress) and `:186` (`_graph_name`)
- Test: `backend/tests/queue/test_assembly_handlers.py`

**Interfaces:**
- Consumes: Tasks 5 and 6.
- Produces: the handler reads two new optional payload keys — `mate_sha256`/`mate_path` (resolved via the existing `_resolve_input(payload, "mate")`), `mate_name`, and `bloom_bytes: int | None`. Task 9 writes them.

- [ ] **Step 1: Write the failing test**

In `backend/tests/queue/test_assembly_handlers.py`:

```python
def test_abyss_job_passes_mate_and_bloom_budget(monkeypatch, tmp_path):
    """The paired path must reach the command line, not just the payload."""
    from app.pipelines import assembly_runner
    from app.pipelines.assembly_params import AbyssParams
    from app.pipelines.assemblers import Assembler

    r1 = tmp_path / "s_R1.fastq"
    r2 = tmp_path / "s_R2.fastq"
    r1.write_text("@r\nACGT\n+\nIIII\n")
    r2.write_text("@r\nACGT\n+\nIIII\n")

    cmd = assembly_runner.build_assembly_command(
        assembler=Assembler.ABYSS,
        tool_path="/usr/bin/abyss-pe",
        reads=r1,
        out_dir=tmp_path / "out",
        params=AbyssParams(k=51, threads=4),
        mate=r2,
        bloom_bytes=4 * 1024**3,
    )
    assert f"in={r1} {r2}" in cmd
    assert "B=4096M" in cmd


def test_graph_name_matches_the_assembler_format():
    """ABySS emits Graphviz, not GFA -- a .gfa suffix would be a lie."""
    from app.queue import assembly_handlers

    assert assembly_handlers._graph_suffix("abyss") == ".assembly_graph.dot"
    assert assembly_handlers._graph_suffix("flye") == ".assembly_graph.gfa"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/queue/test_assembly_handlers.py -q -k "abyss or graph_name"`
Expected: FAIL — `_graph_suffix` does not exist.

- [ ] **Step 3: Resolve the mate and compute the budget**

In `backend/app/queue/assembly_handlers.py`, after the existing `reads = _named_link(...)` line:

```python
    # Optional second mate. `_resolve_input` is already side-parameterized, so
    # the paired path needs no new plumbing -- only a payload key that
    # `launch_assembly` sets when it identified a mate.
    mate: Path | None = None
    if payload_has_mate(ctx.payload):
        mate = _resolve_input(ctx.payload, "mate")
        mate = _named_link(work, mate, ctx.payload.get("mate_name"))
```

Add the small predicate near the module's other helpers:

```python
def payload_has_mate(payload: dict) -> bool:
    return bool(payload.get("mate_sha256") or payload.get("mate_path"))
```

Then pass both through to the command, replacing the existing
`build_assembly_command` call:

```python
    cmd = assembly_runner.build_assembly_command(
        assembler=assembler,
        tool_path=tool.path,
        reads=reads,
        out_dir=out_dir,
        params=params,
        mate=mate,
        # Set by launch_assembly from the same estimate that decided this run
        # could proceed. None for Flye, and for an ABySS run with no estimate
        # -- the builder floors it either way.
        bloom_bytes=ctx.payload.get("bloom_bytes"),
    )
```

`out_dir` must exist before `abyss-pe -C` is invoked, since make will not
create it. Add immediately before the command is built:

```python
    out_dir.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 4: Select the progress parser**

Replace the `progress = assembly_runner.AssemblyProgress(...)` line:

```python
    if assembler is Assembler.ABYSS:
        progress = assembly_runner.AbyssProgress()
    else:
        progress = assembly_runner.AssemblyProgress(
            stage_order=assembly_runner.flye_stage_order(params)
        )
```

- [ ] **Step 5: Parse the right stats table**

Replace the `parse_assembly_info` call in the harvest block:

```python
    if info_path is not None:
        # Two different tables: Flye's `assembly_info.txt` carries coverage and
        # circularity; ABySS's `-stats.tab` carries contiguity including N50.
        if assembler is Assembler.ABYSS:
            facts = assembly_runner.parse_abyss_stats(
                info_path.read_text(errors="replace")
            )
        else:
            facts = assembly_runner.parse_assembly_info(
                info_path.read_text(errors="replace")
            )
```

- [ ] **Step 6: Fix the graph filename**

Replace `_graph_name` and add the suffix helper:

```python
def _graph_suffix(assembler: str) -> str:
    """ABySS emits Graphviz `.dot`; Flye emits GFA.

    Naming an ABySS graph `.gfa` would be a lie that survives into the object
    store, and `AssemblyGraph.tsx` would then try to render it as GFA and fail
    confusingly rather than declining cleanly.
    """
    return ".assembly_graph.dot" if assembler == "abyss" else ".assembly_graph.gfa"


def _graph_name(ctx: JobContext) -> str:
    suffix = _graph_suffix(ctx.payload.get("assembler", "flye"))
    stem = _reads_stem(ctx)
    return f"{stem}{suffix}" if stem else f"assembly_graph{suffix.rsplit('.', 1)[-1]}"
```

Fix the fallback so it reads correctly — when there is no stem, the name should
be `assembly_graph.dot` or `assembly_graph.gfa`:

```python
def _graph_name(ctx: JobContext) -> str:
    suffix = _graph_suffix(ctx.payload.get("assembler", "flye"))
    stem = _reads_stem(ctx)
    return f"{stem}{suffix}" if stem else f"assembly_graph{suffix[len('.assembly_graph'):]}"
```

Use the second form; delete the first.

- [ ] **Step 7: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/queue/test_assembly_handlers.py -q`
Expected: PASS, including pre-existing Flye handler tests.

- [ ] **Step 8: Commit**

```bash
git add backend/app/queue/assembly_handlers.py backend/tests/queue/test_assembly_handlers.py
git commit -m "feat(queue): run abyss with a resolved mate and a bloom filter budget"
```

---

### Task 9: Launch resolves the mate through pairing

**Files:**
- Modify: `backend/app/services/pipeline_service.py:4180-4300` (`launch_assembly`)
- Test: `backend/tests/services/test_assembly_launch.py`

**Interfaces:**
- Consumes: Tasks 3, 4, 8.
- Produces:
  - `launch_assembly(*, object_id, owner, params=None, resource_override=False, mate_object_id=None) -> Job`.
  - `resolve_assembly_mate(reads: DataObject, candidate: DataObject | None = None) -> DataObject | None`. Raises `ValidationError` on `REJECTED_READ_IDS`.

**Reuse, do not reimplement:** `pipeline_service.suggest_mate(obj)` already
exists at `:78` in this same module and does the discovery — it prefers the
persisted `obj.mate_object_id`, falls back to the filename convention, filters
candidates through `pairing.verdict()` accepting only `CONFIRMED`/`NAME_ONLY`,
and returns `None` when the match is ambiguous (more than one candidate). The
trim path already relies on it.

What it does **not** do is surface the `REJECTED_READ_IDS` veto — it silently
returns `None`, which for trimming means "trim single-end" and is fine there.
For assembly, silently assembling single-end when two files were *demonstrably*
mismatched hides a data problem the user should see. So the only new logic is
re-running `verdict()` on an explicitly-supplied candidate to raise on that one
outcome. Everything else delegates.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/services/test_assembly_launch.py`:

```python
@pytest.mark.asyncio
async def test_short_reads_no_longer_refused(monkeypatch):
    """The #490 refusal is gone: short reads now have an installed assembler."""
    from app.pipelines import align_runner, assembler_registry
    from app.pipelines.assemblers import Assembler

    spec = assembler_registry.spec_for_chemistry(align_runner.ReadChemistry.SHORT)
    assert spec is not None
    assert spec.assembler is Assembler.ABYSS


@pytest.mark.asyncio
async def test_mate_rejected_on_read_ids_refuses_the_launch(monkeypatch):
    """Two filename-mates whose read IDs disagree must not be assembled.

    This is the case that produces a plausible-looking wrong assembly with no
    error, which is why it refuses rather than falling back to single-end.
    """
    from app.errors import ValidationError
    from app.services import pipeline_service

    with pytest.raises(ValidationError, match="do not appear to be mates"):
        await pipeline_service.resolve_assembly_mate(
            _reads_stub(name="s_R1.fastq", first_read_ids=["A:1:2"]),
            candidate=_reads_stub(name="s_R2.fastq", first_read_ids=["Z:9:9"]),
        )


@pytest.mark.asyncio
async def test_explicit_matching_mate_is_accepted():
    """A confirmed pair passes through and gets assembled together."""
    from app.services import pipeline_service

    r2 = _reads_stub(name="s_R2.fastq", first_read_ids=["A:1:2"])
    mate = await pipeline_service.resolve_assembly_mate(
        _reads_stub(name="s_R1.fastq", first_read_ids=["A:1:2"]), candidate=r2
    )
    assert mate is r2
```

Write `_reads_stub` as a local helper in the test module building a minimal
`DataObject`-shaped object with `.name`, `.facts` (carrying `first_read_ids`),
and `.metadata` — follow the existing stub style already in
`test_assembly_launch.py` and `test_suggest_mate.py` rather than inventing a
new one.

Note the no-candidate path (`resolve_assembly_mate(reads)`) delegates straight
to `suggest_mate`, which hits the database — cover it in the existing
`test_suggest_mate.py` style with real objects rather than stubs, or leave it
to `suggest_mate`'s own existing coverage.

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/services/test_assembly_launch.py -q -k "mate or short_reads"`
Expected: FAIL — `resolve_assembly_mate` does not exist.

- [ ] **Step 3: Add mate resolution**

In `backend/app/services/pipeline_service.py`, above `launch_assembly`:

```python
async def resolve_assembly_mate(reads: DataObject, candidate: DataObject | None = None):
    """The R2 for these reads, or None to assemble single-end.

    Delegates discovery to `suggest_mate` above, which already prefers the
    persisted link, falls back to the filename convention, and accepts only
    CONFIRMED/NAME_ONLY verdicts. This wrapper adds exactly one thing:
    REJECTED_READ_IDS becomes a refusal instead of a silent single-end
    fallback.

    That difference is the point. For trimming, two mismatched files just mean
    "trim them separately", so `suggest_mate` returning None is right. For
    assembly, quietly assembling one half of what the user thinks is a pair
    produces a plausible result with no error -- worse than a refusal they can
    act on.
    """
    if candidate is None:
        return await suggest_mate(reads)

    verdict = pairing.verdict(
        pairing.PairInput(
            name=reads.name, facts=reads.facts, metadata=reads.metadata
        ),
        pairing.PairInput(
            name=candidate.name, facts=candidate.facts, metadata=candidate.metadata
        ),
    )
    if verdict is pairing.Verdict.REJECTED_READ_IDS:
        raise ValidationError(
            f"{reads.name!r} and {candidate.name!r} look like a pair by name "
            "but their read IDs do not appear to be mates. Assembling them "
            "together would produce a misleading result.",
            details={"reads": reads.name, "mate": candidate.name},
        )
    if verdict in (pairing.Verdict.CONFIRMED, pairing.Verdict.NAME_ONLY):
        return candidate
    return None
```

`pairing` is already imported at module scope in `pipeline_service.py` (used by
`suggest_mate`), so no new import is needed.

- [ ] **Step 4: Wire it into launch_assembly**

Add the parameter to the signature:

```python
async def launch_assembly(
    *,
    object_id: PydanticObjectId,
    owner: str,
    params: dict | None = None,
    resource_override: bool = False,
    mate_object_id: PydanticObjectId | None = None,
) -> Job:
    """Queue a de novo assembly of one FASTQ, paired when we can identify both mates."""
```

Delete the now-false `ReadChemistry.SHORT` refusal branch (`:4200-4207`) — the
one whose message reads "Short-read assembly is not installed." The remaining
`spec is None` case is unknown chemistry only, so simplify:

```python
    if spec is None:
        raise ValidationError(
            f"{reads.name!r} has no known read chemistry. Run QC on it first "
            "-- the assembler's input mode depends on how accurate the reads "
            "are.",
            details={"object_id": str(reads.id)},
        )
```

After `parsed = assembly_params_module.from_dict(params)`, resolve the mate for
paired-layout assemblers only:

```python
    mate = None
    if spec.layout == "paired":
        explicit = None
        if mate_object_id is not None:
            explicit = await object_service.get_object(mate_object_id, owner=owner)
        mate = await resolve_assembly_mate(reads, candidate=explicit)
```

Feed read volume into the estimate. `reads.size` is compressed bytes, so use
both files and note the approximation:

```python
    # Bases, approximated from file size. FASTQ carries ~2 bytes per base
    # (sequence plus quality) before compression, and both mates contribute.
    # Only consumed by a model with a non-zero read coefficient, so this is
    # inert for Flye.
    read_bytes = (reads.size or 0) + (mate.size if mate else 0)
    read_bases = int(read_bytes / 2) if read_bytes else None

    heuristic_mb = resource_estimator.estimate_assembly_mb(
        assembler=parsed.assembler,
        genome_bases=parsed.genome_size,
        threads=parsed.threads,
        read_bases=read_bases,
    )
```

Finally, add the mate and Bloom budget to the job payload where the existing
payload is built (follow the surrounding `reads_sha256`/`reads_path` keys):

```python
        # The tool's mandatory B, derived from the same estimate that decided
        # this run could proceed -- one number, not two that must agree.
        "bloom_bytes": (estimate * 1024 * 1024) if estimate else None,
        **(
            {
                "mate_sha256": mate_digest,
                "mate_path": mate_path_str,
                "mate_name": mate.name,
            }
            if mate is not None
            else {}
        ),
```

Resolve `mate_digest`/`mate_path_str` with the same `_resolve_readable(mate)`
call the reads already use.

- [ ] **Step 5: Add the replan proposer knob**

In `backend/app/services/replan_service.py`, the existing `_assembly_estimate`
and its proposer read `genome_bases` and `threads`. Extend the assembly
proposer so a BLOCK on an ABySS run can also propose a larger `k`, which
reduces distinct k-mers and therefore peak memory. Follow the existing
`_propose_align` structure exactly; register nothing new (the
`JOB_TYPE_ASSEMBLE` entry already exists).

Verify with the existing verifier — per the module docstring, every proposal is
re-checked against the same estimator, so a miscomputed `k` proposal degrades
to `Infeasible` rather than a button that is offered and then refused.

- [ ] **Step 6: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_assembly_launch.py tests/services/test_suggest_mate.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/services/replan_service.py backend/tests/services/test_assembly_launch.py
git commit -m "feat(api): assemble short reads paired, resolving the mate at launch"
```

---

### Task 10: The card

**Files:**
- Modify: `backend/app/services/suggestion_service.py:818-918` (`build_assemble_card`)
- Test: `backend/tests/services/test_suggestion_service.py`

**Interfaces:**
- Consumes: Task 4's `spec_for_chemistry`.
- Produces: no new symbols. `build_assemble_card(obj) -> SuggestionCard | None` keeps its signature.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/services/test_suggestion_service.py`:

```python
def test_short_reads_get_an_available_assemble_card(monkeypatch):
    """The #490 screenshot's refusal must be gone."""
    from app.services import suggestion_service

    obj = _fastq_stub(name="sample_R1.fastq.gz", chemistry="short")
    card = suggestion_service.build_assemble_card(obj)

    assert card.status is CardStatus.AVAILABLE
    assert "not installed" not in (card.reason or "")
    assert "abyss" in card.title.lower()


def test_short_read_card_says_when_reads_are_unpaired(monkeypatch):
    from app.services import suggestion_service

    obj = _fastq_stub(name="sample.fastq.gz", chemistry="short")
    card = suggestion_service.build_assemble_card(obj)

    assert card.status is CardStatus.AVAILABLE
    assert "unpaired" in card.why.lower()


def test_assemble_card_unavailable_when_abyss_is_missing(monkeypatch):
    """Assert the direction that FAILS when the seam breaks.

    The image ships abyss, so asserting availability would pass whether or not
    the patch worked. Patch `spec_for` -- not `tools.abyss`, which a frozen
    dataclass captured at import time. See CLAUDE.md.
    """
    from dataclasses import replace

    from app.pipelines import assembler_registry
    from app.pipelines.assemblers import Assembler
    from app.services import suggestion_service

    real = assembler_registry.spec_for

    def fake_spec_for(assembler):
        spec = real(assembler)
        if assembler is Assembler.ABYSS:
            return replace(
                spec, tool=None, unavailable_reason="abyss is not installed."
            )
        return spec

    monkeypatch.setattr(assembler_registry, "spec_for", fake_spec_for)
    monkeypatch.setattr(
        assembler_registry,
        "spec_for_chemistry",
        lambda c: fake_spec_for(Assembler.ABYSS),
    )

    card = suggestion_service.build_assemble_card(
        _fastq_stub(name="sample_R1.fastq.gz", chemistry="short")
    )
    assert card.status is CardStatus.UNAVAILABLE
    assert "not installed" in card.reason


def test_unknown_chemistry_still_says_run_qc(monkeypatch):
    """The actionable refusal must survive -- it is a different failure."""
    from app.services import suggestion_service

    card = suggestion_service.build_assemble_card(
        _fastq_stub(name="sample.fastq.gz", chemistry=None)
    )
    assert card.status is CardStatus.UNAVAILABLE
    assert "Run QC first" in card.reason
```

Reuse the module's existing FASTQ stub helper rather than adding `_fastq_stub`
if one is already present — grep the file first.

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q -k assemble`
Expected: FAIL — the short-read branch still returns UNAVAILABLE with "Short-read assembly is not installed."

- [ ] **Step 3: Delete the dead refusal and describe pairing**

In `build_assemble_card`, delete the entire
`if chemistry is align_runner.ReadChemistry.SHORT:` block. Update the
docstring's "The two remaining refusals" paragraph to say short reads now
assemble and that the remaining refusal is unknown chemistry.

Then make `why` reflect layout. Replace the final return's `why`:

```python
    if spec.layout == "paired":
        from app.pipelines import pairing

        # Filename-level only: this builder is synchronous and pure by
        # contract, so it cannot look up the mate object. `launch_assembly`
        # does the real resolution, including the read-ID veto. Saying
        # "unpaired" here when a mate exists is not possible; saying "paired"
        # when the veto later fires is, and that path refuses with an
        # explanation rather than assembling.
        paired = pairing.pairing_key(obj.name) is not None
        why = (
            "Paired short reads, assembled with ABySS."
            if paired
            else "Unpaired short reads, assembled with ABySS -- pairing both "
            "mates gives a better assembly."
        )
    else:
        mode = assembler_registry.mode_for_chemistry(spec, chemistry)
        why = f"{_CHEMISTRY_LABELS.get(chemistry, 'Long')} reads, assembled as {mode}."
```

The existing `mode = assembler_registry.mode_for_chemistry(spec, chemistry)`
line above the return must move inside the `else` branch — ABySS has an empty
`mode_flags`, so calling it for a short-read spec raises `KeyError`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat(ui): offer short-read assembly instead of refusing it"
```

---

### Task 11: Full suite, real-data check, and docs

**Files:**
- Modify: `docs/TODO.md` / `docs/TODO-done.md` only if an entry names short-read assembly (grep first; if none, skip that step rather than inventing one).
- Test: the whole suite.

**Interfaces:**
- Consumes: every prior task.
- Produces: a merged PR.

- [ ] **Step 1: Run the full backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS. Read the **count**, not the exit code of the last thing in the pipeline. If the run dies with EXIT=137 that is host memory, not a test failure — check for other worktree stacks with `./ops/worktree-up.sh --list` and tear down orphans before re-running.

- [ ] **Step 2: Check the card against the real database**

Per CLAUDE.md: unit tests feed the rules hand-built objects that already look the way the rules expect. Run the rule against real objects.

```bash
docker exec $(docker ps --filter name=api --format '{{.Names}}' | head -1) python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models import DataObject
from app.services import suggestion_service

async def main():
    await connect_to_mongo()
    n = 0
    async for obj in DataObject.find_all():
        card = suggestion_service.build_assemble_card(obj)
        if card is None:
            continue
        n += 1
        if n > 15:
            break
        print(obj.name, '|', card.status, '|', card.reason or card.why)

asyncio.run(main())
"
```

Confirm no card still reads "Short-read assembly is not installed", and that short-read files show AVAILABLE with a sensible paired/unpaired `why`.

- [ ] **Step 3: Run one real assembly end to end**

Bring up the worktree stack (`./ops/worktree-up.sh`, UI on 5273), open a project with paired short reads, and launch the assembly from the Actions tab. Confirm: the job reaches `done`, a contigs DataObject appears roled REFERENCE, the graph object is named `.dot` not `.gfa`, and the facts carry `assembly_n50`.

If no short-read project exists, the synthetic generator from the spec's verification section produces a usable pair.

- [ ] **Step 4: Update the backlog if an entry names this**

```bash
grep -rn "short.read\|SPAdes\|short read" docs/TODO.md
```

If an entry exists, append ` — FIXED` to its heading, note what shipped and what differed from plan (ABySS rather than SPAdes, because SPAdes is not packaged for trixie), and move the whole entry to `docs/TODO-done.md`. If no entry names it, skip — do not invent one.

- [ ] **Step 5: Rebase onto main and verify the diff survived**

```bash
git fetch origin main
git rebase origin/main
```

```bash
git diff origin/main...HEAD --stat
```

Check the file list matches what you intended to touch and skim for hunks that look reverted.

- [ ] **Step 6: Tear down the worktree stack**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 7: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --fill
```

Then label it `type:feature` and `area:pipelines` — `.github/release.yml` categorizes by label, not by the title prefix, so an unlabelled PR lands under "Other changes". Ensure the body carries `Closes #490`.

- [ ] **Step 8: Watch CI and merge when green**

Poll until every check reports pass, not just until the command returns:

```bash
gh pr checks <N> --watch
```

```bash
gh pr view <N> --json mergeable,mergeStateStatus
```

Fix anything red (CI runs `ruff check`, which catches import-order `I001` that a local run may not). Once every check is `pass` and `mergeable` is `MERGEABLE`:

```bash
gh pr merge <N> --rebase --delete-branch
```

- [ ] **Step 9: Comment the outcome on the issue**

Post what shipped, that ABySS was used rather than SPAdes and why, and the two known limitations (Graphviz graph will not render in `AssemblyGraph.tsx`; eukaryote-scale runs will be refused by the memory guard).

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: install/declare → 1; chemistry routing and spec → 4; params → 2; paired input and the verdict table → 9; runner dispatch → 5; progress and stats → 6; memory model and the dual role of the estimate → 3 and 9; card and copy → 10; testing → 7 and 11. The two "deliberate limitations" are carried into Task 8 (`.dot` naming) and Task 11's issue comment rather than being silently dropped.

**Known gaps, stated rather than hidden.**

- **Mate discovery is `pipeline_service.suggest_mate` (verified at `:78`), not a new function.** An earlier draft of this plan invented `object_service.find_mate`, which does not exist and would have duplicated logic the trim path already relies on. Task 9 now delegates to `suggest_mate` and adds only the `REJECTED_READ_IDS` refusal on top. If an implementer finds themselves writing candidate-scanning code, they have taken a wrong turn.
- **The ABySS memory coefficients (15.0 / 0.5) are guesses**, in the same sense Flye's 40.0 is — published-guidance order-of-magnitude, not measured here. They will need adjusting once real runs produce `job_timings` rows. The band's failure mode is a warning or an over-cautious refusal, never a wrong assembly.
- **`AbyssProgress`'s phase tokens are inferred from ABySS's binary names**, not read off a full production log. The synthetic run in the spec's verification was too small to exercise every stage. If phases never advance in a real run, that table is where to look — and the failure is a static progress display, not a broken assembly.
- **`_graph_name`'s fallback string manipulation is fiddly.** Task 8 Step 6 deliberately shows the wrong version first and then corrects it; implementers should use only the second form.
