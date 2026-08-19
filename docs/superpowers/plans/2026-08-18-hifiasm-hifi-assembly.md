# hifiasm HiFi/ONT-duplex Assembly Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install hifiasm 0.25.0 on both architectures and route HiFi and ONT-duplex reads to it, with primary contigs converted from its GFA output into the FASTA the rest of the pipeline requires, falling back to Flye when hifiasm is unavailable.

**Architecture:** A new optional `postprocess` hook on `AssemblerSpec` converts hifiasm's `asm.bp.p_ctg.gfa` into `assembly.fasta` between subprocess exit and `harvest()`, so `harvest()` and everything downstream stay unchanged. Chemistry routing and the Flye fallback live entirely inside `spec_for_chemistry`, which the Assemble card, `default_assembly_params`, and `launch_assembly` all already read — so they inherit the fallback with zero edits. The install is a source build on both arches (upstream ships no binaries): stock flags on amd64, a SIMDe include-redirection on arm64.

**Tech Stack:** Python 3.12 / FastAPI backend, pytest (run via `./backend/run-worktree-tests.sh` from this worktree), Docker multi-arch build (`python:3.12-slim` base), hifiasm 0.25.0 (C++/Make), SIMDe v0.8.2 (arm64 only).

**Spec:** `docs/superpowers/specs/2026-08-18-hifiasm-hifi-assembly-design.md` — read it before starting; every requirement ID (R1–R31) referenced below is defined there.

## Global Constraints

- hifiasm version pin: `0.25.0` (reports `0.25.0-r726`). SIMDe pin: `v0.8.2`.
- Source tarball SHA256 (hifiasm): `51633138865207a9d41630da9377d46e4921ad4fc5facaa1740ceccae8611f1f`
- Source tarball SHA256 (SIMDe): `ed2a3268658f2f2a9b5367628a85ccd4cf9516460ed8604eed369653d49b25fb`
- `-include linux/types.h` is required on **both** arches (trixie glibc/kernel-header bug, NOT an ARM issue). Never gate it behind the arm64 branch.
- Probe with `--version` only. `hifiasm --help` prints an error and **segfaults**; `-h` is the help flag.
- In tests, patch `assembler_registry.SPECS` / `spec_for` — **never** `tools.hifiasm` (frozen dataclass captured the function object at import time; the module-attribute patch silently reads the host machine).
- License: `MIT` (verified via GitHub API 2026-08-18). Citation: Cheng H, Concepcion GT, Feng X, Zhang H, Li H. *Nat Methods* 2021;18:170-175. `citation_url`: `https://doi.org/10.1038/s41592-020-01056-5`.
- Commit subjects: Conventional Commits, imperative, lowercase after colon, ≤72 chars.
- Run tests from this worktree with `./backend/run-worktree-tests.sh <path> -q` — plain `docker compose exec api pytest` from a worktree silently tests main's code.
- The `mode` param values are `hifi` and `ont` — mode names, not flags. Only `ont` emits a flag (`--ont`); `hifi` emits nothing.

---

### Task 1: `gfa_to_fasta` pure function

**Files:**
- Modify: `backend/app/pipelines/assembly_runner.py` (add function after `parse_abyss_stats`, around line 470)
- Test: `backend/tests/pipelines/test_assembly_runner.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `assembly_runner.gfa_to_fasta(text: str) -> str` — raises `ValueError` when the GFA holds no `S` records. Task 4's postprocess hook calls it.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_assembly_runner.py` (it already imports `assembly_runner`; reuse the module import at the top of the file — check with `grep -n "^from\|^import" backend/tests/pipelines/test_assembly_runner.py` and match its style):

```python
class TestGfaToFasta:
    """hifiasm writes contigs only as GFA -- no FASTA at all -- while
    OutputKind.CONTIGS is required and becomes the REFERENCE object
    everything downstream aligns against. This converter is what bridges
    that gap (spec R13-R15). S-line layout confirmed against a real
    0.25.0 run: S <name> <seq> LN:i:... rd:i:...
    """

    def test_converts_s_lines_and_ignores_everything_else(self):
        gfa = (
            "S\tptg000001l\tACGTACGT\tLN:i:8\trd:i:340\n"
            "L\tptg000001l\t+\tptg000002l\t-\t0M\n"
            "S\tptg000002l\tTTTT\tLN:i:4\n"
            "A\tptg000001l\t0\t+\tr1\t0\t8\tid:i:0\n"
        )
        fasta = assembly_runner.gfa_to_fasta(gfa)
        assert fasta == ">ptg000001l\nACGTACGT\n>ptg000002l\nTTTT\n"

    def test_raises_on_a_gfa_with_no_sequences(self):
        """An exit-0 hifiasm run that assembled nothing must not become a
        valid, empty FASTA that everything downstream silently aligns
        against (spec R15)."""
        with pytest.raises(ValueError, match="no sequences"):
            assembly_runner.gfa_to_fasta("H\tVN:Z:1.0\nL\ta\t+\tb\t-\t0M\n")

    def test_raises_on_empty_input(self):
        with pytest.raises(ValueError, match="no sequences"):
            assembly_runner.gfa_to_fasta("")

    def test_tolerates_s_line_with_no_tags(self):
        assert assembly_runner.gfa_to_fasta("S\tctg1\tACGT\n") == ">ctg1\nACGT\n"
```

If `pytest` is not already imported in that test file, add `import pytest`.

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py::TestGfaToFasta -q
```

Expected: 4 failures, `AttributeError: ... has no attribute 'gfa_to_fasta'`.

- [ ] **Step 3: Implement**

Add to `backend/app/pipelines/assembly_runner.py`, after `parse_abyss_stats`:

```python
def gfa_to_fasta(text: str) -> str:
    """hifiasm's primary contigs, as FASTA.

    hifiasm writes no FASTA at all -- its contigs are GFA `S` lines
    (`S <name> <seq> [tags...]`, layout confirmed against a real 0.25.0
    run). Everything downstream of assembly consumes FASTA, so this is
    the bridge, called by HIFIASM_SPEC's postprocess hook before
    `harvest()` looks for assembly.fasta.

    Raises rather than returning an empty string when there are no `S`
    records: an exit-0 run that assembled nothing must surface as the
    missing-contigs failure `harvest()` knows how to report, not as a
    valid, empty REFERENCE object that every later align silently
    accepts.
    """
    records: list[str] = []
    for line in text.splitlines():
        if not line.startswith("S\t"):
            continue
        fields = line.split("\t")
        if len(fields) < 3 or not fields[2]:
            continue
        records.append(f">{fields[1]}\n{fields[2]}\n")
    if not records:
        raise ValueError(
            "The assembly graph contains no sequences. The assembler "
            "exited successfully but produced no contigs."
        )
    return "".join(records)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py::TestGfaToFasta -q
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/assembly_runner.py backend/tests/pipelines/test_assembly_runner.py
git commit -m "feat(pipelines): convert hifiasm GFA contigs to FASTA"
```

---

### Task 2: `tools.hifiasm()` probe, config path, and `TOOL_META`

**Files:**
- Modify: `backend/app/config.py` (after `spades_path`, ~line 162)
- Modify: `backend/app/pipelines/tools.py` (probe near `spades()` ~line 619; tool list ~line 850; `TOOL_META` after the `"spades"` entry ~line 1720; `_clear_caches` ~line 2413)
- Test: `backend/tests/pipelines/test_tools.py` (existing `test_every_tool_is_documented` is the gate)

**Interfaces:**
- Consumes: `_probe(name, path, args)` (existing private helper in `tools.py`), `settings`.
- Produces: `tools.hifiasm() -> tools.Tool` (`@lru_cache(maxsize=1)`), `settings.hifiasm_path: str = "hifiasm"`, `TOOL_META["hifiasm"]`. Task 4 wires `tools.hifiasm` into `HIFIASM_SPEC.tool`.

- [ ] **Step 1: Add the setting**

In `backend/app/config.py`, directly after `spades_path: str = "spades.py"`:

```python
    # The HiFi/ONT-duplex assembler. A source build on BOTH arches --
    # upstream publishes zero release binaries and Debian does not package
    # it. See scripts/install-hifiasm.sh.
    hifiasm_path: str = "hifiasm"
```

- [ ] **Step 2: Add the probe**

In `backend/app/pipelines/tools.py`, directly after the `spades()` function:

```python
@lru_cache(maxsize=1)
def hifiasm() -> Tool:
    """hifiasm, the HiFi/ONT-duplex assembler.

    Probed with `--version` (one line to stdout, exit 0). NEVER probe with
    `--help`: verified on 0.25.0, it prints `[ERROR] unknown option` and
    then segfaults -- a probe on it would report an installed tool as
    broken. `-h` is the help flag.
    """
    return _probe("hifiasm", settings.hifiasm_path, ["--version"])
```

Add `hifiasm(),` to the tool list around line 850 (directly after `spades(),`), and `hifiasm.cache_clear()` in `_clear_caches` (directly after `spades.cache_clear()`).

- [ ] **Step 3: Add TOOL_META**

Directly after the `"spades"` entry's closing `),` in `TOOL_META`:

```python
    "hifiasm": ToolMeta(
        pipelines=(PipelineType.ASSEMBLE,),
        one_liner="De novo assembler for PacBio HiFi and ONT duplex reads",
        summary=(
            "Assembles highly accurate long reads into contigs without a "
            "reference, using phased string graphs rather than collapsing "
            "haplotypes early. On HiFi data it typically produces more "
            "contiguous, higher-quality assemblies than overlap-graph "
            "assemblers, and since 0.21.0 it also handles ONT reads."
        ),
        strengths=(
            "The standard assembler for PacBio HiFi reads",
            "String-graph approach preserves accuracy the reads already have",
            "Handles Q30+ ONT duplex reads via its ONT mode",
            "Fast: assembles a human genome in half a day on one machine",
        ),
        homepage="https://github.com/chhylp123/hifiasm",
        repository="https://github.com/chhylp123/hifiasm",
        # The 2021 paper describes the core assembler this application
        # runs. Upstream lists two more (2022 no-parental-data, 2024
        # double-graph T2T); neither mode is exposed here, so citing them
        # would put the wrong reference in a methods section -- the same
        # reasoning as FLYE_SPEC's metaFlye note.
        citation=(
            "Cheng H, Concepcion GT, Feng X, Zhang H, Li H. "
            "Haplotype-resolved de novo assembly using phased assembly "
            "graphs with hifiasm. Nat Methods. 2021."
        ),
        citation_url="https://doi.org/10.1038/s41592-020-01056-5",
        # Verified via the GitHub API (license.spdx_id) on 2026-08-18.
        license="MIT",
        usage=(
            "The assembler for PacBio HiFi and Nanopore duplex reads, "
            "chosen over Flye when the reads' chemistry says they are "
            "accurate enough for it. Produces primary contigs that become "
            "a reference you can align against, plus the assembly graph. "
            "Haplotype-resolved output (trio or Hi-C phasing) is not "
            "offered. Falls back to Flye when hifiasm is not installed."
        ),
    ),
```

- [ ] **Step 4: Run the documentation and tools tests**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -q
```

Expected: all pass, including `test_every_tool_is_documented` now covering the new entry. If it fails naming a missing field, the entry above is missing that field — fix the entry, not the test.

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/app/pipelines/tools.py
git commit -m "feat(pipelines): add hifiasm probe and tool documentation"
```

---

### Task 3: `HifiasmParams`

**Files:**
- Modify: `backend/app/pipelines/assembly_params.py` (new class after `SpadesParams`; `_BY_ASSEMBLER`; the "Reachable:" comment in `from_dict`)
- Test: `backend/tests/pipelines/test_assembly_params.py`

**Interfaces:**
- Consumes: `BaseAssemblyParams._shared`, `assembler_registry.modes_for` (existing; reads mode choices off the spec's `mode` field — Task 4 adds that field, see Step 3 note on ordering).
- Produces: `HifiasmParams(assembler=Assembler.HIFIASM, mode: str = "hifi")` with `as_dict()` and `from_dict()`; `from_dict({"assembler": "hifiasm", ...})` dispatches to it. Task 5's command builder asserts `isinstance(params, HifiasmParams)` and reads `params.mode`, `params.threads`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_assembly_params.py` (match the file's existing import style):

```python
class TestHifiasmParams:
    def test_from_dict_defaults(self):
        params = assembly_params.from_dict({"assembler": "hifiasm"})
        assert isinstance(params, assembly_params.HifiasmParams)
        assert params.assembler is Assembler.HIFIASM
        assert params.mode == "hifi"
        assert params.threads == 8

    def test_ont_mode_round_trips(self):
        params = assembly_params.from_dict(
            {"assembler": "hifiasm", "mode": "ont", "threads": 4}
        )
        assert params.mode == "ont"
        assert params.as_dict()["mode"] == "ont"

    def test_unknown_mode_is_refused(self):
        """Validated against the registry's declared modes, not a list
        here, so adding a mode is one edit (same shape as FlyeParams)."""
        with pytest.raises(ValidationError):
            assembly_params.from_dict({"assembler": "hifiasm", "mode": "pacbio-raw"})

    def test_hifiasm_is_no_longer_refused_as_not_installed(self):
        """The from_dict 'not installed in this build' refusal must be gone
        for hifiasm -- it has a params class now. SPAdes gained one earlier;
        after this, every Assembler member dispatches."""
        params = assembly_params.from_dict({"assembler": "hifiasm"})
        assert params.assembler is Assembler.HIFIASM
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_params.py::TestHifiasmParams -q
```

Expected: failures — `from_dict` raises "hifiasm is not installed in this build" (the placeholder refusal this task removes).

- [ ] **Step 3: Implement**

Add after `SpadesParams` in `backend/app/pipelines/assembly_params.py`:

```python
@dataclass
class HifiasmParams(BaseAssemblyParams):
    """hifiasm parameters.

    `mode` is a mode *name*, not a flag: `hifi` means hifiasm's flagless
    default and only `ont` emits `--ont` (see assembly_runner's builder).
    The obvious encoding -- empty string meaning "no flag" -- breaks
    twice: `data.get("mode") or default` below coerces "" to the default,
    making an explicit HiFi choice indistinguishable from an unset one,
    and modes_for() would have to admit "" as a legal round-trippable
    value. Same split SPADES_MODES' `standard` uses for a mode that is
    likewise not a flag.

    No purge-level (-l) field, deliberately: it materially changes the
    primary assembly for homozygous vs heterozygous genomes, but exposing
    it is a parameter-surface decision independent of getting the tool
    installed and routed. See the spec's out-of-scope list.
    """

    assembler: Assembler = Assembler.HIFIASM
    mode: str = "hifi"

    def as_dict(self) -> dict:
        return {**super().as_dict(), "mode": self.mode}

    @classmethod
    def from_dict(cls, data: dict) -> "HifiasmParams":
        from app.pipelines import assembler_registry

        mode = data.get("mode") or "hifi"
        valid = assembler_registry.modes_for(Assembler.HIFIASM)
        if mode not in valid:
            raise ValidationError(
                f"Unknown hifiasm input mode {mode!r}",
                details={"valid": sorted(valid)},
            )
        return cls(assembler=Assembler.HIFIASM, mode=mode, **cls._shared(data))
```

Register it:

```python
_BY_ASSEMBLER = {
    Assembler.FLYE: FlyeParams,
    Assembler.ABYSS: AbyssParams,
    Assembler.SPADES: SpadesParams,
    Assembler.HIFIASM: HifiasmParams,
}
```

Update the now-false "Reachable:" comment above the `params_class is None` check in `from_dict` to:

```python
    # Unreachable today -- every Assembler member has a params class since
    # hifiasm gained one (#617) -- and kept because the enum will grow
    # again. The message says what would be true then: the name is known,
    # the tool is not here, rather than "unknown assembler", which would
    # send someone looking for a typo.
```

**Ordering note:** `test_unknown_mode_is_refused` and the mode validation depend on `modes_for(Assembler.HIFIASM)` returning `{"hifi", "ont"}`, which requires Task 4's `mode` field on `HIFIASM_SPEC`. Until Task 4 lands, `modes_for` returns an empty frozenset and **every** mode is refused — so after this step, `test_unknown_mode_is_refused` passes but the other three still fail. That is expected; run the full class only to confirm the dispatch works, and re-run at the end of Task 4 for green.

- [ ] **Step 4: Run tests — expect partial**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_params.py::TestHifiasmParams -q
```

Expected: `test_unknown_mode_is_refused` passes; the other three fail with `Unknown hifiasm input mode 'hifi'` (empty `modes_for` until Task 4). Do NOT work around this by hardcoding a mode list in the params class.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/assembly_params.py backend/tests/pipelines/test_assembly_params.py
git commit -m "feat(pipelines): add hifiasm parameter class"
```

---

### Task 4: Registry — postprocess hook, filled-in `HIFIASM_SPEC`, chemistry routing with Flye fallback

**Files:**
- Modify: `backend/app/pipelines/assembler_registry.py` (the `AssemblerSpec` dataclass, `HIFIASM_SPEC`, `spec_for_chemistry`)
- Test: `backend/tests/pipelines/test_assembler_registry.py`

**Interfaces:**
- Consumes: `tools.hifiasm` (Task 2), `assembly_runner.gfa_to_fasta` (Task 1, via a function-body import — see Step 3).
- Produces: `AssemblerSpec.postprocess: Callable[[Path], None] | None = None`; `HIFIASM_SPEC` with `tool`, `mode_flags={HIFI: "hifi", ONT_DUPLEX: "ont"}`, `outputs`, `fields` (mode select `hifi`/`ont`), `postprocess`, and empty `unavailable_reason`; `spec_for_chemistry` returning hifiasm for HIFI/ONT_DUPLEX when available, Flye otherwise. Task 6's handler calls `spec.postprocess(out_dir)`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_assembler_registry.py`:

```python
class TestHifiasmRouting:
    """spec_for_chemistry owns both the preference AND the fallback (spec
    R19), so the card, default params, and launch cannot drift apart.

    Patches SPECS -- never tools.hifiasm; see spec_for's docstring.
    """

    @staticmethod
    def _with_hifiasm(monkeypatch, available: bool):
        class _Probe:
            pass

        _Probe.available = available
        real = assembler_registry.SPECS[Assembler.HIFIASM]
        patched = dataclasses.replace(real, tool=lambda: _Probe())
        monkeypatch.setitem(assembler_registry.SPECS, Assembler.HIFIASM, patched)

    def test_hifi_routes_to_hifiasm_when_installed(self, monkeypatch):
        self._with_hifiasm(monkeypatch, available=True)
        spec = assembler_registry.spec_for_chemistry(ReadChemistry.HIFI)
        assert spec.assembler is Assembler.HIFIASM

    def test_ont_duplex_routes_to_hifiasm_when_installed(self, monkeypatch):
        self._with_hifiasm(monkeypatch, available=True)
        spec = assembler_registry.spec_for_chemistry(ReadChemistry.ONT_DUPLEX)
        assert spec.assembler is Assembler.HIFIASM

    def test_hifi_falls_back_to_flye_when_hifiasm_is_absent(self, monkeypatch):
        """The direction that fails when the seam breaks: asserting the
        hifiasm choice would pass whether or not the patch worked, because
        the image ships hifiasm."""
        self._with_hifiasm(monkeypatch, available=False)
        spec = assembler_registry.spec_for_chemistry(ReadChemistry.HIFI)
        assert spec.assembler is Assembler.FLYE

    def test_ont_simplex_and_clr_stay_on_flye(self, monkeypatch):
        """hifiasm's --ont is documented for R10 reads and ReadChemistry
        cannot tell R10 simplex from R9 -- routing simplex would infer a
        fact the reads do not carry. Duplex has no such ambiguity (Q30+,
        R10-era by construction). See spec R19's note."""
        self._with_hifiasm(monkeypatch, available=True)
        for chem in (ReadChemistry.ONT_SIMPLEX, ReadChemistry.CLR):
            assert (
                assembler_registry.spec_for_chemistry(chem).assembler
                is Assembler.FLYE
            )


class TestHifiasmSpec:
    def test_declares_contigs_as_required_fasta(self):
        spec = assembler_registry.SPECS[Assembler.HIFIASM]
        contigs = [o for o in spec.outputs if o.kind is OutputKind.CONTIGS]
        assert len(contigs) == 1
        assert contigs[0].required
        assert contigs[0].filename == "assembly.fasta"

    def test_declares_the_real_gfa_as_the_graph(self):
        """The graph a user opens in Bandage is the file hifiasm wrote,
        not a re-serialization (spec R16)."""
        spec = assembler_registry.SPECS[Assembler.HIFIASM]
        graphs = [o for o in spec.outputs if o.kind is OutputKind.GRAPH]
        assert [g.filename for g in graphs] == [
            f"{assembler_registry.ASSEMBLY_NAME_PREFIX}.bp.p_ctg.gfa"
        ]

    def test_offers_exactly_two_modes(self):
        assert assembler_registry.modes_for(Assembler.HIFIASM) == frozenset(
            {"hifi", "ont"}
        )

    def test_postprocess_writes_fasta_beside_the_gfa(self, tmp_path):
        gfa = tmp_path / f"{assembler_registry.ASSEMBLY_NAME_PREFIX}.bp.p_ctg.gfa"
        gfa.write_text("S\tptg000001l\tACGT\tLN:i:4\n")
        spec = assembler_registry.SPECS[Assembler.HIFIASM]
        spec.postprocess(tmp_path)
        assert (tmp_path / "assembly.fasta").read_text() == ">ptg000001l\nACGT\n"

    def test_postprocess_raises_when_the_gfa_is_missing(self, tmp_path):
        spec = assembler_registry.SPECS[Assembler.HIFIASM]
        with pytest.raises(FileNotFoundError):
            spec.postprocess(tmp_path)

    def test_no_unavailable_reason_anymore(self):
        assert assembler_registry.SPECS[Assembler.HIFIASM].unavailable_reason == ""
```

Confirm the file's imports cover `dataclasses`, `pytest`, `OutputKind`, and `ReadChemistry` (`from app.pipelines.align_runner import ReadChemistry` if absent — check the existing imports first; `test_long_reads_still_route_to_flye` already uses chemistries, so most are present).

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh "tests/pipelines/test_assembler_registry.py::TestHifiasmRouting" "tests/pipelines/test_assembler_registry.py::TestHifiasmSpec" -q
```

Expected: all fail (routing returns Flye for HIFI; spec has empty outputs, no postprocess).

- [ ] **Step 3: Implement**

In `backend/app/pipelines/assembler_registry.py`:

**(a)** Add the hook field to `AssemblerSpec`, after `fields`:

```python
    # Runs after a zero exit and before harvest(), for an assembler whose
    # native output is not what the pipeline consumes. hifiasm writes
    # contigs only as GFA; this is where they become the assembly.fasta
    # its outputs tuple declares. Takes the run's out_dir.
    postprocess: Callable[[Path], None] | None = None
```

Add `from pathlib import Path` to the module imports.

**(b)** Add the postprocess function, before `HIFIASM_SPEC`:

```python
def _hifiasm_postprocess(out_dir: Path) -> None:
    """asm.bp.p_ctg.gfa -> assembly.fasta, in place.

    The import is inside the function because assembly_runner imports this
    module (for ASSEMBLY_NAME_PREFIX); a module-level import here would be
    circular. The cycle is broken at call time, when both modules exist.

    A missing GFA raises FileNotFoundError and an empty one ValueError --
    the handler folds both into the same missing-contigs failure a
    harvest() miss produces, because that is what they are.
    """
    from app.pipelines import assembly_runner

    gfa = out_dir / f"{ASSEMBLY_NAME_PREFIX}.bp.p_ctg.gfa"
    fasta = assembly_runner.gfa_to_fasta(gfa.read_text(errors="replace"))
    (out_dir / "assembly.fasta").write_text(fasta)
```

**(c)** Replace the placeholder `HIFIASM_SPEC` entirely:

```python
HIFIASM_SPEC = AssemblerSpec(
    assembler=Assembler.HIFIASM,
    tool=tools.hifiasm,
    # Mode NAMES, not flags: hifi is hifiasm's flagless default, and only
    # ont emits --ont (see assembly_runner._hifiasm_command). An empty
    # string meaning "no flag" breaks params round-tripping -- see
    # HifiasmParams' docstring.
    #
    # ONT_DUPLEX and not ONT_SIMPLEX, deliberately: --ont is documented
    # for R10 reads, duplex is Q30+ and R10-era by construction, while
    # ReadChemistry cannot tell R10 simplex from R9 -- routing simplex
    # would infer a fact the reads do not carry, the same trap FLYE_SPEC's
    # conservative nano-raw default avoids.
    mode_flags={
        ReadChemistry.HIFI: "hifi",
        ReadChemistry.ONT_DUPLEX: "ont",
    },
    layout="single",
    # Higher genome coefficient than Flye's 40: hifiasm holds all-vs-all
    # read overlaps. Published guidance, not measured on this hardware --
    # the same caveat every model in this file carries. Beware small-input
    # tests: the default -f37 Bloom filter allocates a fixed ~16GB table
    # regardless of input size, so a tiny smoke run needs -f0 or it OOMs
    # in a way that reads as a real memory requirement (it is not).
    memory_model=AssemblyMemoryModel(bytes_per_genome_base=60.0, fixed_overhead_mb=4096),
    outputs=(
        # Written by _hifiasm_postprocess, not by the tool: hifiasm emits
        # no FASTA at all. Declared here so harvest() stays a pure
        # filename->path lookup.
        Output(kind=OutputKind.CONTIGS, filename="assembly.fasta", required=True),
        # The tool's own file, so the graph someone opens in Bandage is
        # what hifiasm wrote rather than a re-serialization.
        Output(
            kind=OutputKind.GRAPH,
            filename=f"{ASSEMBLY_NAME_PREFIX}.bp.p_ctg.gfa",
        ),
    ),
    fields=(
        *_SHARED_FIELDS,
        ParamField(
            key="mode",
            label="Input mode",
            kind="select",
            default="hifi",
            group="biology",
            help=(
                "What kind of accurate long reads these are. Set from the "
                "detected chemistry. HiFi is hifiasm's native input; ONT "
                "covers Q30+ duplex reads."
            ),
            choices=(
                Choice(value="hifi", label="PacBio HiFi (<1% error)"),
                Choice(value="ont", label="Nanopore duplex / R10 (Q30+)"),
            ),
        ),
    ),
    postprocess=_hifiasm_postprocess,
)
```

**(d)** Rewrite `spec_for_chemistry`'s body (keep the docstring's promise, update its prose):

```python
def spec_for_chemistry(chemistry: ReadChemistry | None) -> AssemblerSpec | None:
    """The assembler to use for these reads, or None if there is not one.

    ABySS for short reads; hifiasm for the two high-accuracy long-read
    chemistries (HiFi, ONT duplex) when it is installed, falling back to
    Flye when it is not; Flye for everything else long. The fallback
    lives HERE rather than in the card so the card, default params, and
    launch all inherit it from one place and cannot disagree -- this
    function remains the single place that changes.

    None only for unknown chemistry -- a missing fact the user can supply
    by running QC.
    """
    if chemistry is None or chemistry is ReadChemistry.UNKNOWN:
        return None
    if chemistry is ReadChemistry.SHORT:
        return SPECS[Assembler.ABYSS]
    hifiasm = SPECS[Assembler.HIFIASM]
    if chemistry in hifiasm.mode_flags and hifiasm.available():
        return hifiasm
    flye = SPECS[Assembler.FLYE]
    if chemistry in flye.mode_flags:
        return flye
    return None
```

**(e)** Delete the now-stale comment block above the old `HIFIASM_SPEC` ("Declared so the API can say...") — SPADES_SPEC no longer needs it either, but only touch the hifiasm one; and update the stale parenthetical in `test_assembler_registry.py`'s `TestExhaustiveness.test_every_installable_assembler_has_a_command_builder` — the comment `# Declared-but-not-installed (hifiasm, spades) is exempt` becomes `# Declared-but-not-installed is exempt (none today; the branch guards the enum's future growth):`.

- [ ] **Step 4: Run the FULL registry test file plus the params class**

Per CLAUDE.md, exhaustiveness pairs must run as a whole file, not one test at a time — `TestExhaustiveness` now exercises hifiasm (its `spec.tool is None` exemption no longer skips it) and **will fail until Task 5 adds the command builder**:

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembler_registry.py tests/pipelines/test_assembly_params.py -q
```

Expected: `TestHifiasmRouting`, `TestHifiasmSpec`, and all four `TestHifiasmParams` tests pass (Task 3's deferred three go green now). `test_every_installable_assembler_has_a_command_builder` FAILS with `ValueError: No command builder for hifiasm` — that failure is Task 5's failing test, arriving for free. Everything else passes.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/assembler_registry.py backend/tests/pipelines/test_assembler_registry.py
git commit -m "feat(pipelines): route HiFi and ONT-duplex assembly to hifiasm with Flye fallback"
```

(Committing with one known-red exhaustiveness test is acceptable only because Task 5 is its other half and follows immediately; do not stop between these two tasks.)

---

### Task 5: `_hifiasm_command` builder

**Files:**
- Modify: `backend/app/pipelines/assembly_runner.py` (imports; `build_assembly_command` dispatch; new `_hifiasm_command` after `_spades_command`)
- Test: `backend/tests/pipelines/test_assembly_runner.py`

**Interfaces:**
- Consumes: `HifiasmParams` (Task 3: `.mode` in `{"hifi","ont"}`, `.threads`), `ASSEMBLY_NAME_PREFIX` (existing).
- Produces: `build_assembly_command(assembler=Assembler.HIFIASM, ...)` returns a hifiasm argv. The green `TestExhaustiveness` file is the cross-check.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_assembly_runner.py`:

```python
class TestHifiasmCommand:
    def _cmd(self, mode: str) -> list[str]:
        params = assembly_params.from_dict(
            {"assembler": "hifiasm", "mode": mode, "threads": 4}
        )
        return assembly_runner.build_assembly_command(
            assembler=Assembler.HIFIASM,
            tool_path="/opt/hifiasm/hifiasm",
            reads=Path("/work/reads.fastq.gz"),
            out_dir=Path("/work/out"),
            params=params,
        )

    def test_hifi_mode_emits_no_preset_flag(self):
        """HiFi is hifiasm's flagless default (confirmed against
        `hifiasm -h`: --ont is the only preset option). Spec R31."""
        cmd = self._cmd("hifi")
        assert "--ont" not in cmd
        assert "--hifi" not in cmd  # not a real flag; must not be invented

    def test_ont_mode_emits_the_flag(self):
        assert "--ont" in self._cmd("ont")

    def test_output_prefix_makes_filenames_knowable(self):
        """-o names every output file, which is what lets HIFIASM_SPEC
        declare asm.bp.p_ctg.gfa statically -- same reason ABySS pins
        the prefix."""
        cmd = self._cmd("hifi")
        i = cmd.index("-o")
        assert cmd[i + 1] == f"/work/out/{assembler_registry.ASSEMBLY_NAME_PREFIX}"

    def test_threads_and_reads_are_passed(self):
        cmd = self._cmd("hifi")
        assert cmd[0] == "/opt/hifiasm/hifiasm"
        i = cmd.index("-t")
        assert cmd[i + 1] == "4"
        assert cmd[-1] == "/work/reads.fastq.gz"
```

Check the test file imports `assembly_params`, `Assembler`, `assembler_registry`, and `Path`; add any missing.

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py::TestHifiasmCommand -q
```

Expected: `ValueError: No command builder for hifiasm`.

- [ ] **Step 3: Implement**

In `backend/app/pipelines/assembly_runner.py`, add `HifiasmParams` to the existing `assembly_params` import block, add a dispatch branch in `build_assembly_command` after the SPAdes branch:

```python
    if assembler is Assembler.HIFIASM:
        assert isinstance(params, HifiasmParams)
        return _hifiasm_command(
            tool_path=tool_path, reads=reads, out_dir=out_dir, params=params
        )
```

and add after `_spades_command`:

```python
def _hifiasm_command(
    *, tool_path: str, reads: Path, out_dir: Path, params: HifiasmParams
) -> list[str]:
    """hifiasm takes conventional flags; `-o` is a filename PREFIX, not a
    directory, so every output is knowable in advance -- which is what
    lets HIFIASM_SPEC declare `asm.bp.p_ctg.gfa` statically.

    `mode` is a name, not a flag: `hifi` is hifiasm's flagless default
    and only `ont` emits `--ont`. No memory ceiling and no mate:
    hifiasm has no `-m` equivalent, and a paired long-read assembly is
    not a thing -- the same asymmetry _flye_command documents.
    """
    cmd = [
        tool_path,
        "-o",
        str(out_dir / assembler_registry.ASSEMBLY_NAME_PREFIX),
        "-t",
        str(params.threads),
    ]
    if params.mode == "ont":
        cmd.append("--ont")
    cmd.append(str(reads))
    return cmd
```

- [ ] **Step 4: Run the runner file AND the full registry file**

The registry's `TestExhaustiveness` left red at the end of Task 4 must go green now — running only the new class would repeat the #355 mistake CLAUDE.md documents:

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_assembly_runner.py tests/pipelines/test_assembler_registry.py -q
```

Expected: everything passes, including `test_every_installable_assembler_has_a_command_builder`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/assembly_runner.py backend/tests/pipelines/test_assembly_runner.py
git commit -m "feat(pipelines): build hifiasm command lines"
```

---

### Task 6: Handler postprocess call and dialog defaults

**Files:**
- Modify: `backend/app/queue/assembly_handlers.py` (~line 150, between `run_subprocess` result check and `harvest`)
- Modify: `backend/app/services/pipeline_service.py` (`default_assembly_params`, ~line 4546)
- Test: `backend/tests/pipelines/test_assembly_runner.py` gains nothing here; the handler edit is covered by the postprocess tests from Task 4 plus manual verification in Task 8. The `default_assembly_params` edit is covered below.

**Interfaces:**
- Consumes: `spec.postprocess` (Task 4), `HifiasmParams.mode` default (Task 3).
- Produces: `assemble_reads` runs the hook before harvest; `default_assembly_params` emits `{"assembler": "hifiasm", "mode": <from chemistry>, "threads": 8}` for hifiasm — no `iterations`, which is a Flye knob.

- [ ] **Step 1: Wire postprocess into the handler**

In `backend/app/queue/assembly_handlers.py`, the current code after the subprocess is:

```python
    code = run_subprocess(ctx, cmd, log_path=str(log_path), parser=progress)
    if code != 0:
        raise _failure(code, log_path, assembler.value)

    try:
        found = assembly_runner.harvest(out_dir, spec.outputs)
    except FileNotFoundError as e:
```

Change it to:

```python
    code = run_subprocess(ctx, cmd, log_path=str(log_path), parser=progress)
    if code != 0:
        raise _failure(code, log_path, assembler.value)

    try:
        # hifiasm writes contigs only as GFA; its spec's hook converts
        # them to the assembly.fasta harvest() looks for. Inside the same
        # try as harvest because its failures ARE harvest failures: a
        # missing or sequence-free GFA is "exited cleanly, produced no
        # contigs", whichever code path notices first.
        if spec.postprocess is not None:
            spec.postprocess(out_dir)
        found = assembly_runner.harvest(out_dir, spec.outputs)
    except (FileNotFoundError, ValueError) as e:
```

(The `except` body is unchanged — it already raises `RetryableError(str(e))` with the right reasoning in its comment.)

- [ ] **Step 2: Branch dialog defaults away from the Flye-only `iterations`**

In `backend/app/services/pipeline_service.py`, `default_assembly_params` currently has one `else` for all single-layout assemblers that hardcodes `"iterations": 1` — a Flye knob hifiasm does not have. Replace the `if spec.layout == "paired": ... else: ...` params construction with:

```python
    if spec.layout == "paired":
        # ABySS: no chemistry-graded mode to look up (mode_flags is empty by
        # design -- see ABYSS_SPEC), so the dialog's one real knob is k. Mirror
        # AbyssParams' own default rather than duplicating a magic number.
        params: dict = {
            "assembler": spec.assembler.value,
            "k": assembly_params_module.AbyssParams.k,
            "threads": 8,
        }
    else:
        params = {
            "assembler": spec.assembler.value,
            "mode": assembler_registry.mode_for_chemistry(spec, chemistry),
            "threads": 8,
        }
        if spec.assembler is Assembler.FLYE:
            # Flye's own polishing rounds; hifiasm has no equivalent and a
            # default carrying it would show a knob the params class ignores.
            params["iterations"] = 1
```

Check the module already imports `Assembler` (`from app.pipelines.assemblers import Assembler`) — add it if not.

- [ ] **Step 3: Write the failing test for the defaults**

`default_assembly_params` is async and reads chemistry off the object; find how existing tests for it fake the object — `grep -rn "default_assembly_params" backend/tests` — and follow that pattern. If no test exists, add to `backend/tests/services/test_suggestion_service.py`'s style a minimal direct test in `backend/tests/pipelines/test_assembly_params.py` is NOT appropriate (wrong module); instead add to whichever service-test file the grep reveals, or create `backend/tests/services/test_default_assembly_params.py`:

```python
"""Dialog defaults per assembler (pipeline_service.default_assembly_params)."""

import dataclasses
from unittest.mock import AsyncMock, patch

import pytest

from app.pipelines import assembler_registry
from app.pipelines.align_runner import ReadChemistry
from app.pipelines.assemblers import Assembler
from app.services import pipeline_service


def _spec(assembler: Assembler):
    class _Available:
        available = True

    return dataclasses.replace(
        assembler_registry.SPECS[assembler], tool=lambda: _Available()
    )


@pytest.mark.anyio
async def test_hifiasm_defaults_carry_mode_but_not_iterations():
    """iterations is a Flye knob; a hifiasm default carrying it would show
    a value the params class silently ignores."""
    with (
        patch.object(
            assembler_registry,
            "spec_for_chemistry",
            return_value=_spec(Assembler.HIFIASM),
        ),
        patch.object(
            pipeline_service, "read_chemistry", return_value=ReadChemistry.HIFI
        ),
        # AsyncMock, not return_value alone: infer_genome_size is awaited,
        # and a plain Mock's return value is not awaitable.
        patch.object(
            pipeline_service,
            "infer_genome_size",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        params = await pipeline_service.default_assembly_params(object())
    assert params["assembler"] == "hifiasm"
    assert params["mode"] == "hifi"
    assert "iterations" not in params


@pytest.mark.anyio
async def test_flye_defaults_still_carry_iterations():
    with (
        patch.object(
            assembler_registry,
            "spec_for_chemistry",
            return_value=_spec(Assembler.FLYE),
        ),
        patch.object(
            pipeline_service, "read_chemistry", return_value=ReadChemistry.ONT_SIMPLEX
        ),
        patch.object(
            pipeline_service,
            "infer_genome_size",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        params = await pipeline_service.default_assembly_params(object())
    assert params["iterations"] == 1
```

Adjust the async test decorator to whatever this repo's suite actually uses — `grep -rn "async def test" backend/tests/services | head -3` and copy the marker/fixture pattern from a neighboring async test (it may be `@pytest.mark.asyncio` or none at all under an auto mode). Same for `infer_genome_size`'s patch target: confirm with `grep -n "infer_genome_size" backend/app/services/pipeline_service.py` that it is module-level there.

- [ ] **Step 4: Run the tests**

```bash
./backend/run-worktree-tests.sh tests/services/test_default_assembly_params.py -q
```

Expected: 2 passed. If the hifiasm one fails with `KeyError` in `mode_for_chemistry`, the patched spec lost its `mode_flags` — the `dataclasses.replace` keeps them; check the chemistry patch took.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/assembly_handlers.py backend/app/services/pipeline_service.py backend/tests/services/test_default_assembly_params.py
git commit -m "feat(pipelines): run spec postprocess before harvest and fit dialog defaults per assembler"
```

---

### Task 7: Suggestion-card tests (no production change)

**Files:**
- Test: `backend/tests/services/test_suggestion_service.py` (append to `TestAssembleCard`)

**Interfaces:**
- Consumes: `spec_for_chemistry` routing (Task 4). `build_assemble_card` needs **no edit** — it already renders `spec.assembler.value` in the title and derives `why` from `mode_for_chemistry`. The issue asked for a card rule; the routing change IS the rule. These tests pin R29/R30.

- [ ] **Step 1: Write the tests**

Append inside `TestAssembleCard` (note this class patches `spec_for_chemistry` directly for card-shape tests, but R30's point is the *composition* — real `spec_for_chemistry` + patched `SPECS` — so these go through `SPECS`):

```python
    @staticmethod
    def _hifiasm_in_specs(monkeypatch, available: bool):
        class _Probe:
            pass

        _Probe.available = available

        class _FlyeProbe:
            available = True

        real_h = assembler_registry.SPECS[Assembler.HIFIASM]
        real_f = assembler_registry.SPECS[Assembler.FLYE]
        monkeypatch.setitem(
            assembler_registry.SPECS,
            Assembler.HIFIASM,
            dataclasses.replace(real_h, tool=lambda: _Probe()),
        )
        monkeypatch.setitem(
            assembler_registry.SPECS,
            Assembler.FLYE,
            dataclasses.replace(real_f, tool=lambda: _FlyeProbe()),
        )

    def test_hifi_card_names_hifiasm_when_installed(self, monkeypatch):
        """Through the real spec_for_chemistry: the routing change IS the
        card rule #617 asked for (spec R29)."""
        self._hifiasm_in_specs(monkeypatch, available=True)
        card = build_assemble_card(_fake_obj(facts={"qc_read_chemistry": "hifi"}))
        assert card.status is CardStatus.AVAILABLE
        assert "hifiasm" in card.title

    def test_hifi_card_falls_back_to_flye_not_unavailable(self, monkeypatch):
        """The direction that fails when the seam breaks (spec R30): with
        hifiasm absent the card must offer Flye, never read 'hifiasm is
        not installed' beside an installed Flye."""
        self._hifiasm_in_specs(monkeypatch, available=False)
        card = build_assemble_card(_fake_obj(facts={"qc_read_chemistry": "hifi"}))
        assert card.status is CardStatus.AVAILABLE
        assert "flye" in card.title
        assert "hifiasm" not in card.title
```

Check `Assembler` and `dataclasses` are imported in the test file (line 15 already imports `Assembler`; `dataclasses` is used by `TestAssembleCard._installed`, so both exist).

- [ ] **Step 2: Run the whole file**

```bash
./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q
```

Expected: all pass, the two new ones included, with **zero production edits**. If `test_hifi_card_names_hifiasm_when_installed` fails, do not patch the card — the routing (Task 4) is where the bug is.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/services/test_suggestion_service.py
git commit -m "test(services): assemble card prefers hifiasm for HiFi and falls back to flye"
```

---

### Task 8: `install-hifiasm.sh` and the Dockerfile layer

**Files:**
- Create: `backend/scripts/install-hifiasm.sh`
- Modify: `backend/Dockerfile` (new layer directly after the SPAdes layer, ~line 277; the apt-block comment ~line 123)
- Modify: `backend/app/pipelines/assemblers.py` (the stale enum comment)

**Interfaces:**
- Consumes: nothing from other tasks (independent; can run in parallel with 3–7).
- Produces: `hifiasm` on `PATH` inside the image at `/opt/hifiasm/hifiasm`, symlinked to `/usr/local/bin/hifiasm`, satisfying `settings.hifiasm_path = "hifiasm"` (Task 2).

- [ ] **Step 1: Write the install script**

Create `backend/scripts/install-hifiasm.sh`:

```sh
#!/bin/sh
# Install hifiasm 0.25.0 -- a SOURCE BUILD ON BOTH ARCHITECTURES.
#
# Unlike SPAdes (binary on amd64) there is nothing to vendor: verified
# 2026-08-18 via the GitHub releases API, hifiasm's releases carry ZERO
# assets, for any platform, and Debian trixie does not package it. Both
# branches below compile the same pinned source tarball.
#
# THE arm64 BRANCH IS A SIMDe PORT. Upstream's Makefile hardcodes
# -msse4.2 -mpopcnt and Levenshtein_distance.h includes four x86
# intrinsic headers; on aarch64 we vendor SIMDe v0.8.2 (the approach of
# upstream's own open PR #931), redirect those includes, and build with
# -march=armv8-a+simd. Verified 2026-08-18: builds in 21s, assembles a
# synthetic genome into one contig. Far cheaper than bwa-mem2's
# sse2neon path -- no downloaded patches, no safestringlib.
#
# `-include linux/types.h` IS NOT AN ARM FLAG. It works around a trixie
# glibc/kernel-header ordering bug (linux/sched/types.h reached before
# __u32 exists) that reproduces IDENTICALLY on amd64 with SIMDe absent
# -- verified both ways 2026-08-18. Moving it into the arm64 branch
# breaks the amd64 build.
#
# CHECKSUMS ARE PINNED HERE, NOT FETCHED. GitHub source tarballs for a
# tag are stable; a version bump means updating THREE constants below.
# A stale hash fails this script loudly, which is the intended failure.

set -eu

HIFIASM_VERSION="${HIFIASM_VERSION:-0.25.0}"
SIMDE_VERSION="0.8.2"
INSTALL_DIR="/opt/hifiasm"

HIFIASM_SHA256="51633138865207a9d41630da9377d46e4921ad4fc5facaa1740ceccae8611f1f"
SIMDE_SHA256="ed2a3268658f2f2a9b5367628a85ccd4cf9516460ed8604eed369653d49b25fb"

# ca-certificates and curl are installed persistently in the base tool
# layer and must not be purged here -- see install-spades.sh's note.
apt-get update
BUILD_PACKAGES="g++ make zlib1g-dev"
apt-get install -y --no-install-recommends curl ca-certificates ${BUILD_PACKAGES}

cd /tmp
TARBALL="hifiasm-${HIFIASM_VERSION}.tar.gz"
curl -fsSL -o "${TARBALL}" \
    "https://github.com/chhylp123/hifiasm/archive/refs/tags/${HIFIASM_VERSION}.tar.gz"
echo "${HIFIASM_SHA256}  ${TARBALL}" | sha256sum -c -
tar -xzf "${TARBALL}"
cd "hifiasm-${HIFIASM_VERSION}"

# Both arches: upstream flags minus the x86-only ones, plus the trixie
# header workaround. The arm64 branch swaps the SIMD story.
COMMON_FLAGS="-g -O3 -fomit-frame-pointer -Wall -include linux/types.h"

case "$(uname -m)" in
    x86_64)
        FLAGS="${COMMON_FLAGS} -msse4.2 -mpopcnt"
        ;;
    aarch64|arm64)
        SIMDE_TARBALL="simde-${SIMDE_VERSION}.tar.gz"
        curl -fsSL -o "/tmp/${SIMDE_TARBALL}" \
            "https://github.com/simd-everywhere/simde/archive/refs/tags/v${SIMDE_VERSION}.tar.gz"
        echo "${SIMDE_SHA256}  /tmp/${SIMDE_TARBALL}" | sha256sum -c -
        tar -xzf "/tmp/${SIMDE_TARBALL}" -C /tmp
        # Only the simde/ header tree goes on the include path. Putting
        # the release root there would shadow nothing today, but the
        # narrower path is what was verified.
        mkdir -p third_party/include
        mv "/tmp/simde-${SIMDE_VERSION}/simde" third_party/include/simde
        # Redirect the four x86 intrinsic headers in the one file that
        # includes them. sse4.2.h covers emmintrin/nmmintrin/smmintrin;
        # avx2.h covers the immintrin uses.
        sed -i \
            -e 's|#include "emmintrin.h"|#include <simde/x86/sse4.2.h>|' \
            -e 's|#include "nmmintrin.h"||' \
            -e 's|#include "smmintrin.h"||' \
            -e 's|#include <immintrin.h>|#include <simde/x86/avx2.h>|' \
            Levenshtein_distance.h
        # The sed must have taken: a leftover x86 include means upstream
        # moved the includes and this port needs re-verifying.
        if grep -l 'emmintrin\|nmmintrin\|smmintrin\|immintrin' Levenshtein_distance.h; then
            echo "x86 intrinsic includes survived the redirection" >&2
            exit 1
        fi
        FLAGS="${COMMON_FLAGS} -march=armv8-a+simd -Ithird_party/include -DSIMDE_ENABLE_NATIVE_ALIASES"
        ;;
    *)
        echo "unsupported arch: $(uname -m)" >&2
        exit 1
        ;;
esac

make -j"$(nproc)" CXXFLAGS="${FLAGS}" CFLAGS="${FLAGS}"
mkdir -p "${INSTALL_DIR}"
cp hifiasm "${INSTALL_DIR}/hifiasm"
strip "${INSTALL_DIR}/hifiasm"

# A symlink is fine here, unlike spades.py: hifiasm is one binary with no
# sibling lookup relative to its own path.
ln -sf "${INSTALL_DIR}/hifiasm" /usr/local/bin/hifiasm

cd /tmp
rm -rf "hifiasm-${HIFIASM_VERSION}" "${TARBALL}" \
    "simde-${SIMDE_VERSION}" "simde-${SIMDE_VERSION}.tar.gz" 2>/dev/null || true
apt-get purge -y ${BUILD_PACKAGES}
apt-get autoremove -y
apt-get clean
rm -rf /var/lib/apt/lists/*

# Assert rather than announce: a mismatch means the pin and the build
# disagree, and that must fail the image build, not the first run.
# --version, never --help: --help segfaults on 0.25.0.
INSTALLED="$(/usr/local/bin/hifiasm --version 2>&1 | head -1)"
echo "hifiasm ${INSTALLED}"
case "${INSTALLED}" in
    "${HIFIASM_VERSION}"*) ;;
    *) echo "expected hifiasm ${HIFIASM_VERSION}, got: ${INSTALLED}" >&2; exit 1 ;;
esac
du -sh "${INSTALL_DIR}"
```

- [ ] **Step 2: Add the Dockerfile layer**

In `backend/Dockerfile`, directly after the SPAdes layer's `RUN` (before the `# --- NCBI Datasets CLI ---` block):

```dockerfile
# --- hifiasm ----------------------------------------------------------------
#
# The HiFi/ONT-duplex assembler, preferred over Flye for those two
# chemistries. A source build on BOTH arches -- upstream publishes zero
# release binaries and trixie does not package it -- with a SIMDe port on
# arm64. Cheap either way: ~21s to compile, 3-4MB installed. See
# scripts/install-hifiasm.sh's header and
# docs/superpowers/specs/2026-08-18-hifiasm-hifi-assembly-design.md.
ARG HIFIASM_VERSION=0.25.0
COPY scripts/install-hifiasm.sh /srv/scripts/install-hifiasm.sh
RUN chmod +x /srv/scripts/install-hifiasm.sh \
    && HIFIASM_VERSION="${HIFIASM_VERSION}" \
       /srv/scripts/install-hifiasm.sh
```

- [ ] **Step 3: Update the two stale comments**

In `backend/Dockerfile`'s apt-block comment (~line 123), replace:

```
# the default for short reads. SPAdes is the better choice on isolates and is
```
context line stays; the sentence to replace is the hifiasm one two lines above it:

`# samtools, both already above. hifiasm is not packaged at all and would need`
`# a source build with the same arm64 SIMD problem bwa-mem2 has.`

becomes:

```
# samtools, both already above. hifiasm is not packaged at all and is built
# from source in its own layer below -- the arm64 SIMD problem turned out
# far cheaper than bwa-mem2's (a SIMDe include redirection, no patches).
```

In `backend/app/pipelines/assemblers.py`, the `HIFIASM` enum member's comment:

```python
    # Declared, not installed. Not packaged for Debian; needs a source build
    # with the arm64 SIMD problem bwa-mem2 already has a script for.
    HIFIASM = "hifiasm"
```

becomes:

```python
    # Built from source on both arches (upstream ships no binaries; not in
    # Debian), with a SIMDe port on arm64. See scripts/install-hifiasm.sh.
    HIFIASM = "hifiasm"
```

- [ ] **Step 4: Build and verify both arches locally**

```bash
docker build --platform linux/arm64 -f backend/Dockerfile backend/ -t bioflow-hifiasm-check:arm64
```

```bash
docker run --rm --platform linux/arm64 bioflow-hifiasm-check:arm64 hifiasm --version
```

Expected: `0.25.0-r726`. Then the amd64 leg (emulated; slow but this layer is 21s of compile — the cost is the earlier layers, which are cached after one run):

```bash
docker build --platform linux/amd64 -f backend/Dockerfile backend/ -t bioflow-hifiasm-check:amd64
```

```bash
docker run --rm --platform linux/amd64 bioflow-hifiasm-check:amd64 hifiasm --version
```

Expected: `0.25.0-r726`. If either build fails in the hifiasm layer, read the failure against the script's header comments before changing flags — the two known traps (`--help` probe, arm64-gating the types.h flag) are both documented there.

- [ ] **Step 5: Commit**

```bash
git add backend/scripts/install-hifiasm.sh backend/Dockerfile backend/app/pipelines/assemblers.py
git commit -m "feat(pipelines): install hifiasm 0.25.0 from source on both arches"
```

---

### Task 9: End-to-end verification and full suite

**Files:**
- None created; this task is verification and the PR.

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Full backend suite from the worktree**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: same count as a clean baseline plus the new tests, zero failures. Read the count, not the exit code. A mid-run death with EXIT=137 is host memory pressure from concurrent stacks, not a test failure — check `./ops/worktree-up.sh --list` for orphans before re-running.

- [ ] **Step 2: Real-stack smoke test (success criterion 3)**

Bring up the worktree stack and run an assembly through the app:

```bash
./ops/worktree-up.sh
```

UI on 5273. In a project holding (or after uploading) a HiFi FASTQ — small real HiFi data if available; otherwise generate synthetic HiFi-like reads (the spec's verification used 1500 × 12 kb perfect reads over a 60 kb genome, written as FASTQ with `~` quality) and upload that. Run QC so chemistry is established, then launch the Assemble card. Verify:

- The card title reads "De novo assembly -- hifiasm".
- The job completes; a contigs FASTA object and a `.gfa` graph object appear.
- The contigs object's facts show a contig count (from the FASTA parse; hifiasm has no INFO_TABLE, so no coverage/circularity facts — that is correct, not a gap).

**Memory note for small synthetic inputs:** hifiasm's default `-f37` Bloom filter allocates ~16 GB regardless of input size. If the worker OOMs on a tiny test genome (rc 137 in the job log), that is the filter, not the integration — it does not reproduce on real-sized data. Do not add `-f0` to the production command builder for this.

- [ ] **Step 3: Tear the stack down**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 4: Rebase on main, verify the diff, open the PR**

```bash
git fetch origin main && git rebase origin/main
```

```bash
git diff origin/main...HEAD --stat
```

Check the file list is exactly: the spec, this plan, `assembly_runner.py`, `tools.py`, `config.py`, `assembly_params.py`, `assembler_registry.py`, `assembly_handlers.py`, `pipeline_service.py`, `assemblers.py`, `Dockerfile`, `install-hifiasm.sh`, and the five test files. Then:

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --title "feat(pipelines): add hifiasm for HiFi and ONT duplex assembly" --body "$(cat <<'EOF'
Adds hifiasm 0.25.0 as the assembler for PacBio HiFi and ONT duplex reads, with automatic fallback to Flye when it is unavailable.

**Why:** hifiasm is the standard, higher-accuracy assembler for HiFi data; the registry has carried its declared-but-not-installed spec since the original de novo assembly design, waiting for exactly this.

Design: `docs/superpowers/specs/2026-08-18-hifiasm-hifi-assembly-design.md`. Three findings there correct the issue: upstream ships zero binaries (source build on both arches), the arm64 SIMD port is a cheap SIMDe include-redirection rather than bwa-mem2's patch stack, and a trixie header bug needs `-include linux/types.h` on BOTH arches. hifiasm also writes no FASTA at all — a new `postprocess` hook on `AssemblerSpec` converts its GFA primary contigs before `harvest()`, keeping the handler generic.

Closes #617

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Label it: `gh pr edit <N> --add-label "type:feature" --add-label "area:pipelines"`.

- [ ] **Step 5: Watch CI, merge, clean up**

Poll `gh pr checks <N>` until every check reports pass (not pending). CI builds both arches natively, which is also the non-emulated amd64 verification. On green and `MERGEABLE`:

```bash
gh pr merge <N> --rebase --delete-branch
```

Then update issue #617 (comment noting the merge and the spec/plan paths; the `Closes` line handles state), and remove this worktree per CLAUDE.md (bring down anything still running first).

---

## Self-Review Notes

- **Spec coverage:** R1–R8 → Task 8; R9–R11 → Task 2; R12 → Task 6; R13–R16 → Tasks 1+4; R17–R19a → Tasks 4+5; R20–R22 → Task 3; R23–R24 → Task 4 outputs / no-op (handler's existing `else` covers progress); R25–R26 → Task 1; R27–R28 → Task 4; R29–R30 → Task 7; R31 → Task 5.
- **Known cross-task red:** Task 4 leaves `TestExhaustiveness` red until Task 5 (its command builder). Tasks 4 and 5 must be executed back-to-back; the plan says so at both ends.
- **Deferred green:** Task 3 leaves three of its four tests red until Task 4 adds the `mode` field `modes_for` reads. Expected and annotated inline.
