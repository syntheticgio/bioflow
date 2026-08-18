# SPAdes Short-Read Assembly Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install SPAdes 4.3.0 in the backend image on both architectures and make it a selectable short-read assembler.

**Architecture:** SPAdes ships no Linux-arm64 binary, so the install branches on `TARGETARCH` the way bwa-mem2 already does: vendored binary tarball on amd64, source build via `spades_compile.sh` on arm64. Everything above the install is filling in seams that #490 already built -- `SPADES_SPEC` is a declared-but-unavailable placeholder, and the runner dispatches per assembler.

**Tech Stack:** Debian trixie, Docker multi-arch build, Python 3.12, pytest.

**Spec:** `docs/superpowers/specs/2026-08-18-spades-short-read-assembly-design.md`

## Global Constraints

- **SPAdes version is pinned to 4.3.0.** It is the release whose notes carry the ARM/Linux GFA fix. Do not relax the pin to a floor.
- **SHA-256 constants, verified 2026-08-18:**
  - `SPAdes-4.3.0-Linux.tar.gz` -> `e88a8c533c8614dd4b7c5788cfcd46427848a0575267f97c690a75fd2a343034`
  - `SPAdes-4.3.0.tar.gz` -> `09671ca39f9c6d2479d9fc168100bfd089b4a24002d51b815386d2b24d424456`
- **License is `GPL-2.0-only`.** Verified against upstream's own LICENSE file: "GNU General Public License, Version 2, dated June 1991". GitHub's API reports `NOASSERTION`; the LICENSE file is the authority. **Do not write GPL-3.0** -- ABySS's entry next to it is GPL-3.0 and copying it is the likely error.
- **Citation is the Current Protocols paper**, which upstream's README names as "our latest paper": Prjibelski A, Antipov D, Meleshko D, Lapidus A, Korobeynikov A. Using SPAdes De Novo Assembler. Curr Protoc Bioinformatics. 2020. `https://doi.org/10.1002/cpbi.102`
- **`ca-certificates` must not be purged** by the install script. The bwa-mem2 block records that purging it silently broke later layers' HTTPS.
- **Never run bare `docker compose` from this worktree.** A `PreToolUse` hook blocks it. Use `./ops/worktree-up.sh`.
- **Run tests with `./backend/run-worktree-tests.sh`, not `docker compose exec api`** -- the latter silently tests main's code from a worktree.
- Commit subjects are Conventional Commits, imperative, lowercase after the colon, no trailing period.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/scripts/install-spades.sh` | **Create.** Arch-branching install: verify + extract on amd64, compile on arm64. |
| `backend/Dockerfile` | **Modify.** New late layer invoking the script; wrapper at `/usr/local/bin/spades.py`; correct the stale SPAdes comment at :124. |
| `backend/app/config.py` | **Modify.** Add `spades_path` setting. |
| `backend/app/pipelines/tools.py` | **Modify.** Add `spades()` probe and its `TOOL_META` entry. |
| `backend/app/pipelines/assembly_params.py` | **Modify.** Add `SpadesParams`; register in `_BY_ASSEMBLER`. |
| `backend/app/pipelines/assembler_registry.py` | **Modify.** Fill in `SPADES_SPEC`. |
| `backend/app/pipelines/assembly_runner.py` | **Modify.** Rename `bloom_bytes` -> `memory_bytes`; add `_spades_command`. |
| `backend/app/queue/assembly_handlers.py` | **Modify.** Forward the renamed payload key. |
| `backend/app/services/pipeline_service.py` | **Modify.** Emit the renamed payload key. |
| `backend/tests/pipelines/test_assembly_runner.py` | **Modify.** Command-builder tests. |
| `backend/tests/pipelines/test_assembler_registry.py` | **Modify.** Spec and routing tests. |
| `backend/tests/pipelines/test_assembly_params.py` | **Modify.** Params validation tests. |

**Task order rationale:** the rename (Task 1) lands first and alone, so it is a mechanical diff nobody has to read alongside a behaviour change. Params and registry (Tasks 2-3) are pure Python with no image dependency. The command builder (Task 4) needs the params. The install (Tasks 5-6) is last because it is the slowest to iterate on and nothing in Python imports it.

---

### Task 1: Rename `bloom_bytes` to `memory_bytes`

Pure rename, no behaviour change. SPAdes will read this same number to set a memory ceiling, and a key named `bloom_bytes` would tell the next reader that SPAdes has a Bloom filter. Implements **R13**.

**Files:**
- Modify: `backend/app/pipelines/assembly_runner.py:40,44,61,91,100,101`
- Modify: `backend/app/queue/assembly_handlers.py:106`
- Modify: `backend/app/services/pipeline_service.py:4375`
- Test: `backend/tests/pipelines/test_assembly_runner.py:27`

**Interfaces:**
- Consumes: nothing.
- Produces: `build_assembly_command(*, assembler, tool_path, reads, out_dir, params, mate=None, memory_bytes=None) -> list[str]`. Payload key `"memory_bytes"`.

- [ ] **Step 1: Find every occurrence**

```bash
grep -rn "bloom_bytes" backend/app backend/tests
```

Expected: 6 in `assembly_runner.py`, 1 in `assembly_handlers.py`, 1 in `pipeline_service.py`, 1 in `test_assembly_runner.py`.

- [ ] **Step 2: Rename the parameter and payload key**

In `assembly_runner.py`, rename the keyword argument `bloom_bytes` to `memory_bytes` in `build_assembly_command` and in `_abyss_command`, and update the two lines in `_abyss_command`'s body:

```python
    bloom_mb = MIN_BLOOM_MB
    if memory_bytes:
        bloom_mb = max(MIN_BLOOM_MB, int(memory_bytes / (1024 * 1024)))
```

Update `build_assembly_command`'s docstring line that reads "`mate` and `bloom_bytes` are ABySS-only" to name `memory_bytes` and say it is read by ABySS as a Bloom budget and by SPAdes as a memory ceiling.

In `assembly_handlers.py`:

```python
        memory_bytes=ctx.payload.get("memory_bytes"),
```

In `pipeline_service.py`, rename the payload key and keep the comment accurate:

```python
        # The memory ceiling for this run, derived from the same estimate that
        # decided it could proceed -- one number, not two that must agree.
        # ABySS spends it as a Bloom filter budget; SPAdes as `-m`.
        "memory_bytes": (estimate * 1024 * 1024) if estimate else None,
```

- [ ] **Step 3: Update the test helper**

In `test_assembly_runner.py`, change `bloom_bytes=2 * 1024**3` to `memory_bytes=2 * 1024**3` in `_abyss_cmd`'s defaults, and `_abyss_cmd(bloom_bytes=3 * 1024**3)` to `_abyss_cmd(memory_bytes=3 * 1024**3)` in `test_abyss_command_always_sets_bloom_budget`.

- [ ] **Step 4: Verify no occurrences remain**

```bash
grep -rn "bloom_bytes" backend/app backend/tests
```

Expected: no output. `MIN_BLOOM_MB` and the local `bloom_mb` stay -- those name ABySS's actual Bloom filter, which is correct.

- [ ] **Step 5: Run the assembly tests**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py tests/queue/test_assembly_handlers.py -q
```

Expected: all pass. A rename that changed behaviour shows up here.

- [ ] **Step 6: Commit**

```bash
git add backend/app backend/tests
git commit -m "refactor(pipelines): name the assembly memory budget for the quantity, not ABySS's use of it"
```

---

### Task 2: `SpadesParams`

Implements **R10** (three modes) and the omissions the spec records (no `--frugal`, no `-k`).

**Files:**
- Modify: `backend/app/pipelines/assembly_params.py:161-186`
- Test: `backend/tests/pipelines/test_assembly_params.py`

**Interfaces:**
- Consumes: `BaseAssemblyParams._shared(data) -> dict`, `ValidationError`.
- Produces: `SpadesParams(assembler=Assembler.SPADES, mode: str = "isolate", threads: int, genome_size: int | None, genome_size_source: str)`, with `as_dict()` and `from_dict()`. Valid modes: `"isolate"`, `"careful"`, `"standard"`.

`"standard"` is BioFlow's name for passing neither flag. SPAdes has no `--standard`; the command builder maps it to no flag at all.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/pipelines/test_assembly_params.py`:

```python
class TestSpadesParams:
    def test_defaults_to_isolate(self):
        params = assembly_params.from_dict({"assembler": "spades"})
        assert isinstance(params, assembly_params.SpadesParams)
        assert params.mode == "isolate"

    def test_accepts_the_three_declared_modes(self):
        for mode in ("isolate", "careful", "standard"):
            params = assembly_params.from_dict(
                {"assembler": "spades", "mode": mode}
            )
            assert params.mode == mode

    def test_rejects_an_unknown_mode(self):
        with pytest.raises(ValidationError):
            assembly_params.from_dict({"assembler": "spades", "mode": "meta"})

    def test_rejects_frugal_which_is_deliberately_not_offered(self):
        """--frugal's own manual says it changes results unpredictably."""
        with pytest.raises(ValidationError):
            assembly_params.from_dict({"assembler": "spades", "mode": "frugal"})

    def test_round_trips_through_as_dict(self):
        params = assembly_params.from_dict(
            {"assembler": "spades", "mode": "careful", "threads": 12}
        )
        restored = assembly_params.from_dict(params.as_dict())
        assert restored == params
```

- [ ] **Step 2: Run to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_params.py -k Spades -q
```

Expected: FAIL -- `module 'app.pipelines.assembly_params' has no attribute 'SpadesParams'`.

- [ ] **Step 3: Implement**

Add after `AbyssParams` in `assembly_params.py`:

```python
# SPAdes' running modes, as BioFlow offers them. `standard` is not a SPAdes
# flag: it is this dialog's name for passing neither `--isolate` nor
# `--careful`, which upstream documents as mutually incompatible. Modelled as
# one select rather than two checkboxes so the UI cannot express the invalid
# combination.
SPADES_MODES = frozenset({"isolate", "careful", "standard"})


@dataclass
class SpadesParams(BaseAssemblyParams):
    """SPAdes parameters.

    No `k` field, deliberately: SPAdes selects k from read length
    automatically, unlike ABySS. Exposing it invites a hand-set value that is
    worse than the automatic one.

    No `--frugal` either -- its own documentation says it "affects the
    assembly results in an unpredictable way".
    """

    assembler: Assembler = Assembler.SPADES
    mode: str = "isolate"

    def as_dict(self) -> dict:
        return {**super().as_dict(), "mode": self.mode}

    @classmethod
    def from_dict(cls, data: dict) -> "SpadesParams":
        mode = data.get("mode") or "isolate"
        if mode not in SPADES_MODES:
            raise ValidationError(
                f"Unknown SPAdes mode {mode!r}",
                details={"valid": sorted(SPADES_MODES)},
            )
        return cls(assembler=Assembler.SPADES, mode=mode, **cls._shared(data))
```

Then register it -- `_BY_ASSEMBLER` is a hand-maintained registry keyed by an enum, and an assembler missing from it is rejected as unknown rather than failing loudly:

```python
_BY_ASSEMBLER = {
    Assembler.FLYE: FlyeParams,
    Assembler.ABYSS: AbyssParams,
    Assembler.SPADES: SpadesParams,
}
```

- [ ] **Step 4: Run to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_params.py -q
```

Expected: PASS, including the pre-existing Flye and ABySS tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/assembly_params.py backend/tests/pipelines/test_assembly_params.py
git commit -m "feat(pipelines): add SPAdes assembly parameters with three running modes"
```

---

### Task 3: Fill in `SPADES_SPEC`

Implements **R8**, **R9**, **R14**.

**Files:**
- Modify: `backend/app/pipelines/assembler_registry.py` (the `SPADES_SPEC` block)
- Test: `backend/tests/pipelines/test_assembler_registry.py`

**Interfaces:**
- Consumes: `tools.spades` (Task 6 -- until then `available()` returns False, which is the current behaviour and breaks nothing), `SPADES_MODES` from Task 2.
- Produces: `SPADES_SPEC` with `outputs` and `fields` populated; `modes_for(Assembler.SPADES) -> frozenset({"isolate", "careful", "standard"})`.

**Note on ordering:** this task references `tools.spades`, which Task 6 creates. Do Task 3 with `tool=None` still in place and only the outputs/fields filled in; Task 6 flips `tool` to `tools.spades`. That keeps each task's tests green on its own.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/pipelines/test_assembler_registry.py`:

```python
def test_spades_declares_contigs_as_required_output():
    spec = assembler_registry.spec_for(Assembler.SPADES)
    kinds = {o.kind: o for o in spec.outputs}
    assert kinds[OutputKind.CONTIGS].required is True
    assert kinds[OutputKind.CONTIGS].filename == "contigs.fasta"
    assert kinds[OutputKind.GRAPH].filename == "assembly_graph_with_scaffolds.gfa"


def test_spades_offers_exactly_the_three_modes():
    assert assembler_registry.modes_for(Assembler.SPADES) == frozenset(
        {"isolate", "careful", "standard"}
    )


def test_spades_does_not_offer_a_kmer_field():
    """SPAdes picks k from read length; ABySS does not, which is why only
    ABySS has the field."""
    spec = assembler_registry.spec_for(Assembler.SPADES)
    assert not any(f.key == "k" for f in spec.fields)


def test_short_reads_still_route_to_abyss_after_spades_is_installed():
    """Installing an assembler makes it selectable. Promoting it to the
    default changes every existing user's results and is a separate decision."""
    spec = assembler_registry.spec_for_chemistry(ReadChemistry.SHORT)
    assert spec.assembler is Assembler.ABYSS
```

- [ ] **Step 2: Run to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembler_registry.py -k spades -q
```

Expected: FAIL -- `SPADES_SPEC.outputs` is currently `()`, so the `kinds[...]` lookup raises `KeyError`.

- [ ] **Step 3: Implement**

Replace the `SPADES_SPEC` block in `assembler_registry.py`:

```python
SPADES_SPEC = AssemblerSpec(
    assembler=Assembler.SPADES,
    # Flipped to tools.spades once the binary is installed -- see the
    # install-spades.sh task. Until then `available()` returns False and the
    # card reads as not installed.
    tool=None,
    # Empty by construction, like ABySS: SPAdes has no read-accuracy mode
    # flag, and `spec_for_chemistry` does not reach it by chemistry lookup.
    mode_flags={},
    layout="paired",
    memory_model=AssemblyMemoryModel(
        # Published guidance, not measured on this hardware -- the same caveat
        # FLYE_SPEC and ABYSS_SPEC both carry. SPAdes holds more per genome
        # base than ABySS because it is not a Bloom-filter assembler: its
        # graph is held outright rather than in a bounded filter.
        bytes_per_genome_base=90.0,
        bytes_per_read_base=0.6,
        fixed_overhead_mb=4096,
    ),
    outputs=(
        # Filenames confirmed against a real 4.3.0 run of the bundled
        # test dataset, not read from documentation.
        Output(kind=OutputKind.CONTIGS, filename="contigs.fasta", required=True),
        Output(
            kind=OutputKind.GRAPH,
            filename="assembly_graph_with_scaffolds.gfa",
        ),
        Output(kind=OutputKind.CONTIGS, filename="scaffolds.fasta"),
    ),
    fields=(
        *_SHARED_FIELDS,
        ParamField(
            key="mode",
            label="Running mode",
            kind="select",
            default="isolate",
            group="biology",
            help=(
                "Isolate is recommended for high-coverage bacterial isolates "
                "and is the usual choice. Careful reduces mismatches and short "
                "indels but is only for small genomes. The two cannot be "
                "combined."
            ),
            choices=(
                Choice(value="isolate", label="Isolate (high-coverage, recommended)"),
                Choice(value="careful", label="Careful (small genomes only)"),
                Choice(value="standard", label="Standard"),
            ),
        ),
    ),
    unavailable_reason="SPAdes is not installed in this build.",
)
```

- [ ] **Step 4: Run to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembler_registry.py -q
```

Expected: PASS, including `TestExhaustiveness` -- run the whole file, not just the new tests. Per CLAUDE.md, a registry's completeness and no-double-classification tests only catch a collision when run together.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/assembler_registry.py backend/tests/pipelines/test_assembler_registry.py
git commit -m "feat(pipelines): declare SPAdes outputs and running modes in the registry"
```

---

### Task 4: The SPAdes command builder

Implements **R11**, **R12**.

**Files:**
- Modify: `backend/app/pipelines/assembly_runner.py`
- Test: `backend/tests/pipelines/test_assembly_runner.py`

**Interfaces:**
- Consumes: `SpadesParams` (Task 2), `memory_bytes` (Task 1).
- Produces: `_spades_command(*, tool_path, reads, out_dir, params, mate, memory_bytes) -> list[str]`; `MIN_SPADES_MEMORY_GB = 4`.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/pipelines/test_assembly_runner.py`, beside `_abyss_cmd`:

```python
def _spades_cmd(**kwargs):
    defaults = dict(
        assembler=Assembler.SPADES,
        tool_path="/usr/local/bin/spades.py",
        reads=Path("/work/r1.fastq.gz"),
        out_dir=Path("/work/out"),
        params=SpadesParams(mode="isolate", threads=4),
        mate=Path("/work/r2.fastq.gz"),
        memory_bytes=8 * 1024**3,
    )
    defaults.update(kwargs)
    return assembly_runner.build_assembly_command(**defaults)


class TestSpadesCommand:
    def test_pairs_mates_as_separate_flags(self):
        """Unlike abyss-pe's single `in=` variable, SPAdes takes -1 and -2."""
        cmd = _spades_cmd()
        assert "-1" in cmd
        assert cmd[cmd.index("-1") + 1] == "/work/r1.fastq.gz"
        assert "-2" in cmd
        assert cmd[cmd.index("-2") + 1] == "/work/r2.fastq.gz"

    def test_falls_back_to_single_end(self):
        cmd = _spades_cmd(mate=None)
        assert "-s" in cmd
        assert cmd[cmd.index("-s") + 1] == "/work/r1.fastq.gz"
        assert "-1" not in cmd

    def test_isolate_mode_passes_the_flag(self):
        assert "--isolate" in _spades_cmd(params=SpadesParams(mode="isolate"))

    def test_careful_mode_passes_the_flag(self):
        cmd = _spades_cmd(params=SpadesParams(mode="careful"))
        assert "--careful" in cmd
        assert "--isolate" not in cmd

    def test_standard_mode_passes_neither_flag(self):
        cmd = _spades_cmd(params=SpadesParams(mode="standard"))
        assert "--isolate" not in cmd
        assert "--careful" not in cmd

    def test_memory_ceiling_is_in_whole_gigabytes(self):
        """-m is in GB and SPAdes terminates on reaching it."""
        cmd = _spades_cmd(memory_bytes=8 * 1024**3)
        assert "-m" in cmd
        assert cmd[cmd.index("-m") + 1] == "8"

    def test_memory_ceiling_is_floored_when_no_estimate_exists(self):
        """Never inherit upstream's 250GB default: a run with no estimate
        would then die late rather than never starting."""
        cmd = _spades_cmd(memory_bytes=None)
        assert cmd[cmd.index("-m") + 1] == str(assembly_runner.MIN_SPADES_MEMORY_GB)

    def test_tiny_estimate_is_raised_to_the_floor(self):
        cmd = _spades_cmd(memory_bytes=100 * 1024**2)
        assert cmd[cmd.index("-m") + 1] == str(assembly_runner.MIN_SPADES_MEMORY_GB)
```

Add `SpadesParams` to that file's existing import from `app.pipelines.assembly_params`.

- [ ] **Step 2: Run to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py -k Spades -q
```

Expected: FAIL with `ValueError: No command builder for spades`.

- [ ] **Step 3: Implement**

Add the constant beside `MIN_BLOOM_MB` in `assembly_runner.py`:

```python
# SPAdes' `-m` is a hard ceiling in gigabytes: it terminates on reaching it,
# and its own default is 250GB. A run with no estimate must still get a real
# number, or it inherits that default and dies deep into a run on a
# workstation rather than never starting.
MIN_SPADES_MEMORY_GB = 4
```

Add the dispatch branch in `build_assembly_command`, before the final `raise`:

```python
    if assembler is Assembler.SPADES:
        assert isinstance(params, SpadesParams)
        return _spades_command(
            tool_path=tool_path,
            reads=reads,
            out_dir=out_dir,
            params=params,
            mate=mate,
            memory_bytes=memory_bytes,
        )
```

Add `SpadesParams` to the module's import from `app.pipelines.assembly_params`, then the builder:

```python
def _spades_command(
    *,
    tool_path: str,
    reads: Path,
    out_dir: Path,
    params: SpadesParams,
    mate: Path | None,
    memory_bytes: int | None,
) -> list[str]:
    """`spades.py` takes conventional flags, unlike abyss-pe's Make variables.

    `-m` is the one that matters: it is a ceiling SPAdes enforces by
    terminating, not a hint, so it is always passed and always floored.
    """
    memory_gb = MIN_SPADES_MEMORY_GB
    if memory_bytes:
        memory_gb = max(MIN_SPADES_MEMORY_GB, int(memory_bytes / (1024**3)))

    cmd = [tool_path, "-o", str(out_dir), "-t", str(params.threads), "-m", str(memory_gb)]

    # `standard` is BioFlow's name for neither flag; SPAdes has no such option.
    if params.mode == "isolate":
        cmd.append("--isolate")
    elif params.mode == "careful":
        cmd.append("--careful")

    if mate is not None:
        cmd += ["-1", str(reads), "-2", str(mate)]
    else:
        cmd += ["-s", str(reads)]
    return cmd
```

- [ ] **Step 4: Run to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py -q
```

Expected: PASS, Flye and ABySS tests included.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/assembly_runner.py backend/tests/pipelines/test_assembly_runner.py
git commit -m "feat(pipelines): build SPAdes commands with a floored memory ceiling"
```

---

### Task 5: `install-spades.sh` and the Dockerfile layer

Implements **R1**-**R6**. The slowest task to iterate on: an arm64 build is ~2 minutes plus image context.

**Files:**
- Create: `backend/scripts/install-spades.sh`
- Modify: `backend/Dockerfile` (new layer near Clair3; comment at :124)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `/usr/local/bin/spades.py` on PATH, reporting `SPAdes genome assembler v4.3.0`.

- [ ] **Step 1: Write the install script**

Create `backend/scripts/install-spades.sh`:

```sh
#!/bin/sh
# Install SPAdes 4.3.0, per architecture.
#
# THE ARCHITECTURE SPLIT IS LOAD-BEARING. Read this before simplifying it.
#
# SPAdes ships exactly one Linux release asset, `SPAdes-4.3.0-Linux.tar.gz`,
# and it is x86-64 ONLY -- verified 2026-08-18 by reading its ELF header:
#   spades-core: ELF 64-bit LSB executable, x86-64
# The macOS assets are arch-qualified (Darwin-arm64 / Darwin-x86_64); the
# Linux one is not, and upstream's install docs list compatible *distributions*
# with no architecture caveat, which is why this needs checking rather than
# reading. Vendoring that tarball alone leaves arm64 -- half of what
# release.yml publishes -- with no SPAdes at all.
#
# SPAdes does support ARM from source: upstream issue #1062 ("Support Apple
# m1") is closed, and 4.3.0's release notes fix an ARM/Linux GFA bug. The
# arm64 branch below builds it, verified 2026-08-18 at 124s on 24 cores with
# no patches -- unlike bwa-mem2, no sse2neon translation is needed.
#
# THE VERSION PIN IS LOAD-BEARING: 4.3.0 is the release carrying that
# ARM/Linux fix. Relaxing it to a floor reintroduces a fixed bug.
#
# CHECKSUMS ARE PINNED HERE, NOT FETCHED. Unlike meryl, SPAdes publishes no
# SHA256SUMS asset -- verified 2026-08-18, the release has four assets, none
# checksum-shaped. A hash committed in a reviewed script is stronger than one
# downloaded from beside the tarball anyway. NOTE: a version bump means
# updating THREE constants below, not one. A stale hash fails this script
# loudly, which is the intended failure.

set -eu

SPADES_VERSION="${SPADES_VERSION:-4.3.0}"
INSTALL_DIR="/opt/spades"
BASE="https://github.com/ablab/spades/releases/download/v${SPADES_VERSION}"

BINARY_SHA256="e88a8c533c8614dd4b7c5788cfcd46427848a0575267f97c690a75fd2a343034"
SOURCE_SHA256="09671ca39f9c6d2479d9fc168100bfd089b4a24002d51b815386d2b24d424456"

apt-get update
apt-get install -y --no-install-recommends curl ca-certificates

cd /tmp

case "$(uname -m)" in
    x86_64)
        TARBALL="SPAdes-${SPADES_VERSION}-Linux.tar.gz"
        echo "Fetching ${TARBALL} (prebuilt, amd64)..."
        curl -fsSL -O "${BASE}/${TARBALL}"
        echo "${BINARY_SHA256}  ${TARBALL}" > "${TARBALL}.sha256"
        sha256sum -c "${TARBALL}.sha256"
        mkdir -p "${INSTALL_DIR}"
        tar -xzf "${TARBALL}" -C "${INSTALL_DIR}" --strip-components=1
        rm -f "${TARBALL}" "${TARBALL}.sha256"
        BUILD_PACKAGES=""
        ;;
    aarch64|arm64)
        TARBALL="SPAdes-${SPADES_VERSION}.tar.gz"
        echo "Fetching ${TARBALL} (source, arm64 has no published binary)..."
        curl -fsSL -O "${BASE}/${TARBALL}"
        echo "${SOURCE_SHA256}  ${TARBALL}" > "${TARBALL}.sha256"
        sha256sum -c "${TARBALL}.sha256"
        BUILD_PACKAGES="g++ cmake make zlib1g-dev libbz2-dev"
        apt-get install -y --no-install-recommends ${BUILD_PACKAGES}
        tar -xzf "${TARBALL}"
        cd "SPAdes-${SPADES_VERSION}"
        PREFIX="${INSTALL_DIR}" ./spades_compile.sh
        cd /tmp
        rm -rf "SPAdes-${SPADES_VERSION}" "${TARBALL}" "${TARBALL}.sha256"
        ;;
    *)
        echo "unsupported arch: $(uname -m)" >&2
        exit 1
        ;;
esac

# ca-certificates is deliberately NOT purged: later layers need working HTTPS,
# and purging it here broke that silently once already -- see the bwa-mem2
# block in the Dockerfile.
apt-get purge -y curl ${BUILD_PACKAGES}
apt-get autoremove -y
apt-get clean
rm -rf /var/lib/apt/lists/*

# A wrapper, not a symlink. spades.py locates its sibling binaries
# (spades-core, spades-hammer) relative to its own path, so a symlink into
# /usr/local/bin sends it looking for them there -- the same trap the
# bwa-mem2 block documents.
printf '#!/bin/sh\nexec "%s/bin/spades.py" "$@"\n' "${INSTALL_DIR}" \
    > /usr/local/bin/spades.py
chmod +x /usr/local/bin/spades.py

# Assert rather than announce: a version mismatch here means the pin and the
# installed tree disagree, and that must fail the build, not the first run.
INSTALLED="$(/usr/local/bin/spades.py --version 2>&1 | head -1)"
echo "${INSTALLED}"
case "${INSTALLED}" in
    *"${SPADES_VERSION}"*) ;;
    *) echo "expected SPAdes ${SPADES_VERSION}, got: ${INSTALLED}" >&2; exit 1 ;;
esac
du -sh "${INSTALL_DIR}"
```

- [ ] **Step 2: Add the Dockerfile layer**

Add after the Clair3 block in `backend/Dockerfile`, matching its "own layer, late" rationale:

```dockerfile
# --- SPAdes ----------------------------------------------------------------
#
# The better short-read assembler on bacterial isolates, and the reason this
# is its own late layer is size: ~193MB built, ~196MB vendored. An edit above
# should not reinstall it.
#
# Not packaged for trixie, and -- unlike every other vendored tool here --
# with no Linux-arm64 release asset either, so the install script branches on
# architecture: prebuilt binary on amd64, source build on arm64. See
# scripts/install-spades.sh's header and
# docs/superpowers/specs/2026-08-18-spades-short-read-assembly-design.md.
ARG SPADES_VERSION=4.3.0
COPY scripts/install-spades.sh /srv/scripts/install-spades.sh
RUN chmod +x /srv/scripts/install-spades.sh \
    && SPADES_VERSION="${SPADES_VERSION}" \
       /srv/scripts/install-spades.sh
```

- [ ] **Step 3: Correct the now-stale comment**

The comment at `backend/Dockerfile:124` says SPAdes "needs a vendored upstream tarball", which this change proves is only half true. Replace that sentence:

```dockerfile
# abyss is the short-read assembler (2.3.10, paired-end de Bruijn) and stays
# the default for short reads. SPAdes is the better choice on isolates and is
# now installed too -- not from apt (trixie has no candidate, verified
# 2026-08-17) and not from a single vendored tarball either, since upstream
# publishes no Linux-arm64 binary. See the SPAdes layer below.
```

- [ ] **Step 4: Build for arm64 and verify**

```bash
docker build --platform linux/arm64 -t bioflow-spades-check backend/
```

Expected: build succeeds; the SPAdes layer prints `SPAdes genome assembler v4.3.0` and a size around 193M. A failure here that names `spades_compile.sh` is upstream's script changing -- the pin should prevent it.

- [ ] **Step 5: Verify the binary is genuinely arm64 and runs**

```bash
docker run --rm --platform linux/arm64 bioflow-spades-check sh -c 'spades.py --version && file /opt/spades/bin/spades-core'
```

Expected: `SPAdes genome assembler v4.3.0`, and `ELF 64-bit LSB pie executable, ARM aarch64`. If `file` is absent from the image, use `head -c 20 /opt/spades/bin/spades-core | od -c | head -2` and confirm the ELF magic.

- [ ] **Step 6: Run a real assembly on the bundled test data**

```bash
docker run --rm --platform linux/arm64 bioflow-spades-check sh -c \
  'spades.py --isolate -1 /opt/spades/share/spades/test_dataset/ecoli_1K_1.fq.gz -2 /opt/spades/share/spades/test_dataset/ecoli_1K_2.fq.gz -m 4 -t 2 -o /tmp/asm >/dev/null 2>&1; ls /tmp/asm/contigs.fasta /tmp/asm/scaffolds.fasta /tmp/asm/assembly_graph_with_scaffolds.gfa'
```

Expected: all three filenames listed. This is what confirms Task 3's declared output filenames against the real tool rather than documentation.

- [ ] **Step 7: Build for amd64**

```bash
docker build --platform linux/amd64 -t bioflow-spades-check-amd64 backend/
```

Expected: succeeds, taking the vendored-binary branch. Watch for the checksum line passing -- a mismatch means the pinned constant and the published asset disagree, and the build must stop rather than continue.

- [ ] **Step 8: Commit**

```bash
git add backend/scripts/install-spades.sh backend/Dockerfile
git commit -m "feat(pipelines): install SPAdes 4.3.0, prebuilt on amd64 and built from source on arm64"
```

---

### Task 6: Probe, `TOOL_META`, and flipping the spec available

Implements **R7**, **R8**, and the `/help/software` completeness test.

**Files:**
- Modify: `backend/app/config.py` (beside `abyss_path`, around :161)
- Modify: `backend/app/pipelines/tools.py` (probe beside `abyss()` at :605; `TOOL_META` entry)
- Modify: `backend/app/pipelines/assembler_registry.py` (`SPADES_SPEC.tool`)
- Test: `backend/tests/pipelines/test_assembler_registry.py`

**Interfaces:**
- Consumes: `_probe(name, configured, version_args) -> Tool`; `settings.spades_path`.
- Produces: `tools.spades() -> Tool`.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/pipelines/test_assembler_registry.py`:

```python
def test_spades_card_goes_unavailable_when_the_probe_is_off(monkeypatch):
    """Patch spec_for, never tools.spades: AssemblerSpec is frozen and
    captured the function object at import time, so patching the module
    attribute never reaches spec.tool.

    Asserted in the unavailable direction on purpose -- the image ships
    SPAdes installed, so asserting availability passes whether or not the
    patch worked."""
    from app.pipelines import tools

    missing = tools.Tool(
        name="spades", path=None, version=None, error="not installed"
    )
    real = assembler_registry.spec_for(Assembler.SPADES)
    patched = dataclasses.replace(real, tool=lambda: missing)
    monkeypatch.setattr(
        assembler_registry, "spec_for", lambda a: patched if a is Assembler.SPADES else real
    )

    assert assembler_registry.spec_for(Assembler.SPADES).available() is False
```

Add `import dataclasses` at the top of that file if absent.

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembler_registry.py -k probe_is_off -q
```

Expected: FAIL -- `tools.Tool` has no `spades` probe yet, so the import in the test body raises `AttributeError` at `tools.spades` only after Step 3, and before Step 3 the test fails constructing the patched spec. Either way it is red before the implementation and green after Step 4.

If this test passes *before* Step 4, the patch is not reaching `spec.tool` -- that is the frozen-dataclass trap the docstring names, not a working test.

- [ ] **Step 3: Add the setting and the probe**

In `backend/app/config.py`, beside `abyss_path`:

```python
    spades_path: str = "spades.py"
```

In `backend/app/pipelines/tools.py`, after `abyss()`:

```python
@lru_cache(maxsize=1)
def spades() -> Tool:
    """SPAdes, the short-read assembler.

    A conventional CLI, unlike `abyss-pe` -- `--version` writes one line to
    stdout and exits 0, with none of the Make-wrapper stderr noise abyss()
    documents.
    """
    return _probe("spades", settings.spades_path, ["--version"])
```

- [ ] **Step 4: Flip the spec**

In `assembler_registry.py`, in `SPADES_SPEC`, replace `tool=None` and its two-line comment with:

```python
    tool=tools.spades,
```

- [ ] **Step 5: Add the `TOOL_META` entry**

`test_every_tool_is_documented` requires `homepage`, `citation`, `license`, and `usage` on every entry. Add beside `"abyss"` in `TOOL_META`:

```python
    "spades": ToolMeta(
        pipelines=(PipelineType.ASSEMBLE,),
        one_liner="De novo assembler for short paired-end reads",
        summary=(
            "Assembles short paired-end reads into contigs without a "
            "reference, using multi-sized de Bruijn graphs. Generally "
            "produces more contiguous assemblies than ABySS on bacterial "
            "isolates, at a higher memory cost -- its graph is held outright "
            "rather than in a bounded Bloom filter."
        ),
        strengths=(
            "Bacterial isolates and other high-coverage short-read data",
            "Selects k-mer sizes automatically from read length",
            "Built-in read error correction before assembly",
        ),
        homepage="https://ablab.github.io/spades/",
        repository="https://github.com/ablab/spades",
        # The Current Protocols paper, which upstream's README names as "our
        # latest paper". The 2012 Bankevich paper describes the original
        # single-cell algorithm and is not what a 4.x run reflects.
        citation=(
            "Prjibelski A, Antipov D, Meleshko D, Lapidus A, Korobeynikov A. "
            "Using SPAdes De Novo Assembler. Curr Protoc Bioinformatics. 2020."
        ),
        citation_url="https://doi.org/10.1002/cpbi.102",
        # Verified against upstream's own LICENSE file on 2026-08-18: "GNU
        # General Public License, Version 2, dated June 1991". GitHub's API
        # reports NOASSERTION for this repo, and ABySS's entry above is
        # GPL-3.0 -- neither is this tool's license.
        license="GPL-2.0-only",
        usage=(
            "Assembles short paired-end reads into contigs with no "
            "reference. BioFlow runs it in isolate mode by default, paired "
            "when it can identify both mates and single-end when it cannot, "
            "and passes a memory ceiling derived from the same estimate that "
            "guards the run -- SPAdes terminates on reaching that ceiling "
            "rather than exceeding it."
        ),
        runnable=True,
    ),
```

- [ ] **Step 6: Run the full suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: all pass, including `test_every_tool_is_documented` and the assembler `TestExhaustiveness` classes. Run the whole suite here rather than a subset -- this task touches a registry that several completeness tests read.

- [ ] **Step 7: Commit**

```bash
git add backend/app/config.py backend/app/pipelines/tools.py backend/app/pipelines/assembler_registry.py backend/tests/pipelines/test_assembler_registry.py
git commit -m "feat(pipelines): probe SPAdes and make it a selectable assembler"
```

---

### Task 7: End-to-end check against the running stack

CLAUDE.md's "check a rule against the real database" note: the suggestion rules passed a full green suite while getting two things wrong that one look at a real project exposed.

**Files:** none -- verification only.

- [ ] **Step 1: Bring up the worktree stack**

```bash
./ops/worktree-up.sh
```

Expected: UI on 5273, API on 8100. Never plain `docker compose` from a worktree -- a hook blocks it, because it would silently repoint the main stack at this branch.

- [ ] **Step 2: Confirm the API reports SPAdes as installed**

```bash
curl -s localhost:8100/api/v1/pipelines/tools | python3 -c "import json,sys; print([t for t in json.load(sys.stdin) if t['name']=='spades'])"
```

Expected: one entry, `available: true`, version naming 4.3.0. If it reads unavailable, the container is running an image built before Task 5 -- rebuild rather than debugging the probe.

- [ ] **Step 3: Run one real paired-end assembly through the UI**

Open localhost:5273, pick a project with a short-read paired FASTQ, open the Assemble action, and choose SPAdes in the dialog. Confirm: the dialog offers exactly three modes with Isolate selected; there is no k-mer field; the run completes and produces a contigs object.

- [ ] **Step 4: Confirm the memory ceiling reached the command**

```bash
docker compose -p bioflow-issue-519-brainstorm-57c9dd logs worker | grep -- "-m"
```

Expected: the assembled command line includes `-m <N>` with N at least 4. This is the one requirement (R11/R12) that unit tests can only prove in isolation -- they cannot show the number survived the payload.

- [ ] **Step 5: Bring the stack down**

```bash
./ops/worktree-up.sh --down
```

A stack you brought up for testing is yours to bring back down. Leaving it running is what caused the 2026-08-12 incident where four orphaned stacks wiped each other's test databases mid-run.

- [ ] **Step 6: Update the issue**

Comment on #519 with what shipped, and confirm the label reflects the state.

---

## Self-Review

**Spec coverage:**

| Requirement | Task |
|---|---|
| R1 install 4.3.0 both arches | 5 |
| R2 checksum both tarballs | 5 (Step 1) |
| R3 arm64 source build | 5 (Step 1) |
| R4 purge build tooling | 5 (Step 1) |
| R5 version assertion | 5 (Step 1) |
| R6 wrapper not symlink | 5 (Step 1) |
| R7 probe `--version` | 6 |
| R8 `tool=tools.spades` | 6 (Step 4) |
| R9 output filenames | 3 |
| R10 three modes | 2, 3 |
| R11 `-m` from estimate | 4 |
| R12 floored `-m` | 4 |
| R13 rename to `memory_bytes` | 1 |
| R14 routing unchanged | 3 |
| T1 patch `spec_for` | 6 (Step 1) |
| T2 unavailable direction | 6 (Step 1) |
| T3 mode + `-m` builder tests | 4 |
| T4 floor with no estimate | 4 |
| T5 full `TestExhaustiveness` | 3 (Step 4), 6 (Step 6) |

No gaps.

**Placeholder scan:** none. Every code step carries the actual code; every test step carries the actual assertions.

**Type consistency:** `memory_bytes` is the keyword from Task 1 onward, used identically in Tasks 1 and 4. `SpadesParams(mode=...)` is defined in Task 2 and used in Task 4. `SPADES_MODES` values (`isolate`/`careful`/`standard`) match the `Choice` values in Task 3 and the branches in Task 4's builder. `MIN_SPADES_MEMORY_GB` is defined in Task 4 and referenced only there. `tools.spades` is created in Task 6 and referenced by Task 3's note, which is why Task 3 leaves `tool=None` for Task 6 to flip.

**One known ordering wrinkle, stated rather than hidden:** Task 3's new tests pass with `tool=None`, and Task 6's probe test is the one that requires the flip. An executor doing Task 3 alone will see a `SPADES_SPEC` that declares outputs but still reads as unavailable -- that is correct at that point, not a bug.
