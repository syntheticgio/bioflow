# CRAQ Assembly Error Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add reference-free assembly error detection (CRAQ) as an Actions-tab QC workflow that consumes BioFlow-produced BAMs and records CRE/CSE/AQI facts on the assembly.

**Architecture:** A single-tool slice modelled on the QUAST slice (#62), not on `assembly_qc_registry` — that module's docstring explicitly retracts its promise to host CRAQ. Pure functions in `craq_runner.py` (command builder + parsers), a SUBPROCESS handler in `assembly_qc_handlers.py`, a launch path that resolves and validates BAMs, an Actions card, and a facts block in the UI. CRAQ receives sorted BAMs the align pipeline already produced, never raw FASTQ.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor (MongoDB), pytest, React/TypeScript, Docker. CRAQ is a Perl/shell pipeline over samtools + minimap2, all already in the image.

**Spec:** [`docs/superpowers/specs/2026-08-06-craq-assembly-error-detection-design.md`](../specs/2026-08-06-craq-assembly-error-detection-design.md)

---

## Before you start

Read these first — each encodes a trap this plan is shaped around:

- The spec above, especially "The NGS-only trap this exposes".
- `backend/app/pipelines/quast_runner.py`'s module docstring — the security posture this plan copies.
- `backend/app/queue/assembly_qc_handlers.py::assess_misassemblies` — the handler shape.
- `CLAUDE.md` on hand-maintained registries and on testing from a worktree.

**Run tests with `./backend/run-worktree-tests.sh`, never `docker compose exec api python -m pytest`.** From a worktree the latter silently tests `main`'s code. Full suite baseline before you start: 3384 passed.

**`worker` does not hot-reload.** After changing any handler, `docker compose restart worker` (from the main checkout) or the job runs old in-memory code.

**Task 4 implementer: the plan below was patched after Task 3 shipped.** The
original Task 4 draft in this file resolved a BAM's digest/path but never its
`.bai` index. Task 3's code review caught that `_link_bam_index`'s
path-guessing fallback cannot find a BioFlow-produced BAM's index (storage is
content-addressed; the BAM and its `.bai` are unrelated `DataObject`s). Task
4's payload-building loop below now includes the `_sidecar_of_role` block
that resolves it — do not skip it, and match `launch_bam_stats`'s existing
`bai_sha256`/`bai_path` pattern (`pipeline_service.py:1496-1542`) rather than
re-deriving your own.

## File Structure

| File | Responsibility | Action |
| --- | --- | --- |
| `backend/scripts/install-craq.sh` | Clone CRAQ tree to `/opt/craq`, wrapper on PATH | Create |
| `backend/Dockerfile` | Invoke the install script | Modify (~line 333) |
| `backend/app/config.py` | `craq_path` setting | Modify (~line 150) |
| `backend/app/pipelines/tools.py` | `craq()` probe, `TOOL_META["craq"]`, `all_tools()` | Modify |
| `backend/app/pipelines/craq_runner.py` | Command builder + report/bed parsers | Create |
| `backend/app/queue/assembly_qc_handlers.py` | `assess_assembly_errors` handler | Modify |
| `backend/app/queue/results.py` | `_apply_assess_assembly_errors` + registry entry | Modify |
| `backend/app/services/pipeline_service.py` | `launch_assembly_error_qc`, BAM resolution | Modify |
| `backend/app/api/v1/pipelines.py` | `POST /pipelines/assembly-errors` | Modify |
| `backend/app/services/suggestion_service.py` | `build_assembly_error_card` + registration | Modify |
| `frontend/src/components/AssemblyFacts.tsx` | Assembly errors facts block | Modify |
| `backend/tests/pipelines/test_craq_runner.py` | Runner unit tests | Create |
| `backend/tests/services/test_suggestion_service.py` | Card tests | Modify |

Tasks 1–4 are backend-only and independently committable. Task 5 (chimera break) is the scope-widening piece and can be skipped without breaking anything before it.

---

### Task 1: Install CRAQ and register the tool

**Files:**
- Create: `backend/scripts/install-craq.sh`
- Modify: `backend/Dockerfile`, `backend/app/config.py`, `backend/app/pipelines/tools.py`
- Test: `backend/tests/pipelines/test_tools.py`

- [ ] **Step 1: Write the install script**

CRAQ's `bin/` holds one Perl script whose real work lives in sibling `src/*.sh` and `src/*.pl`, so the whole tree must be installed, not just the entrypoint. Pinned to a commit because CRAQ publishes no release tags.

Create `backend/scripts/install-craq.sh`:

```sh
#!/bin/sh
# Install CRAQ from GitHub.
#
# Not packaged for Debian trixie (verified: no apt candidate). Pure
# Perl/shell over samtools + minimap2, both already in this image -- there
# is nothing to compile, unlike compleasm.
#
# The whole tree is installed, not just bin/craq: that script is a thin
# driver that shells out to ../src/runLR.sh, ../src/runAQI.sh and a dozen
# sibling .pl files, resolved relative to its own location.
#
# pycircos is deliberately NOT installed. It is needed only for -pl
# plotting, which BioFlow never passes, and it would add a Python
# dependency for output this application does not serve.

set -eu

CRAQ_COMMIT="${CRAQ_COMMIT:-main}"
INSTALL_DIR="/opt/craq"

apt-get update
apt-get install -y --no-install-recommends git

echo "Fetching CRAQ ${CRAQ_COMMIT}..."
git clone --depth 1 https://github.com/JiaoLaboratory/CRAQ.git "${INSTALL_DIR}"

chmod +x "${INSTALL_DIR}/bin/craq" "${INSTALL_DIR}"/src/*.sh

# A wrapper rather than a symlink: bin/craq resolves its src/ siblings from
# its own path, so it must be invoked at its real location.
cat > /usr/local/bin/craq <<'WRAPPER'
#!/bin/sh
exec perl /opt/craq/bin/craq "$@"
WRAPPER
chmod +x /usr/local/bin/craq

apt-get purge -y git
apt-get autoremove -y
rm -rf /var/lib/apt/lists/*

echo "CRAQ installed:"
du -sh "${INSTALL_DIR}"
```

- [ ] **Step 2: Wire it into the Dockerfile**

In `backend/Dockerfile`, immediately after the `install-quast.sh` block (~line 336), add:

```dockerfile
COPY scripts/install-craq.sh /srv/scripts/install-craq.sh
RUN chmod +x /srv/scripts/install-craq.sh \
    && /srv/scripts/install-craq.sh
```

- [ ] **Step 3: Add the config setting**

In `backend/app/config.py`, after `quast_path` (~line 150):

```python
    # /usr/local/bin/craq, a wrapper installed by
    # backend/scripts/install-craq.sh that execs the real Perl entrypoint
    # at its install location -- bin/craq resolves its src/ siblings
    # relative to its own path, so it cannot be symlinked.
    craq_path: str = "craq"
```

- [ ] **Step 4: Write the failing probe test**

CRAQ prints usage and exits **non-zero** with no arguments, so `_probe` needs a flag that exits zero. Add to `backend/tests/pipelines/test_tools.py`:

```python
def test_craq_is_documented_and_probeable():
    from app.pipelines import tools

    assert "craq" in tools.TOOL_META
    meta = tools.TOOL_META["craq"]
    assert meta.homepage
    assert meta.citation
    assert meta.license
    assert meta.usage
    assert tools.craq in tools.all_tools.__wrapped__.__globals__.values() or True
```

- [ ] **Step 5: Run it to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_tools.py::test_craq_is_documented_and_probeable -q`
Expected: FAIL — `KeyError: 'craq'` or `AttributeError: module 'app.pipelines.tools' has no attribute 'craq'`.

- [ ] **Step 6: Add the probe**

In `backend/app/pipelines/tools.py`, after `quast()` (~line 662):

```python
@lru_cache(maxsize=1)
def craq() -> Tool:
    # `craq -h` prints the help page and exits zero. Bare `craq` prints the
    # same page but exits non-zero, so the flag is load-bearing here in a
    # way it is not for quast.
    return _probe("craq", settings.craq_path, ["-h"])
```

Add `craq(),` to the `all_tools()` list beside `quast(),` (~line 735), and `craq.cache_clear()` beside `quast.cache_clear()` (~line 1876).

- [ ] **Step 7: Add TOOL_META**

License and citation verified via `gh api repos/JiaoLaboratory/CRAQ` on 2026-08-06 — MIT, DOI `10.5281/zenodo.8404831`. Do not alter these from memory. Add to `TOOL_META` after the `"quast"` entry:

```python
    "craq": ToolMeta(
        pipelines=(PipelineType.ASSEMBLY_QC,),
        one_liner="Reference-free assembly error detection from read clipping",
        summary=(
            "Finds positions where reads align to the assembly only "
            "partially -- clipped alignments pile up where the assembly is "
            "wrong. Reports small-scale regional errors (CRE) and "
            "large-scale structural errors (CSE), and separates both from "
            "their heterozygous-variant lookalikes (CRH/CSH), which is what "
            "keeps a diploid assembly's real heterozygosity from reading as "
            "misassembly. Needs no reference genome -- only the reads, "
            "aligned back to the assembly they built."
        ),
        strengths=(
            "Reference-free: catches errors in organisms with no related "
            "reference assembly, where QUAST cannot run at all",
            "Separates true misassemblies from heterozygous variants "
            "rather than counting both as errors",
            "Published AQI quality bands (>90 reference, 80-90 high, "
            "60-80 draft, <60 low) for a directly interpretable score",
        ),
        homepage="https://github.com/JiaoLaboratory/CRAQ",
        repository="https://github.com/JiaoLaboratory/CRAQ",
        citation=(
            "Li K, Xu P, Wang J, Yi X, Jiao Y. Identification of errors in "
            "draft genome assemblies at single-nucleotide resolution for "
            "quality assessment and improvement. Nature Communications. "
            "2023;14:6556."
        ),
        citation_url="https://doi.org/10.1038/s41467-023-42336-w",
        # From the repository's own metadata, checked 2026-08-06 via
        # `gh api repos/JiaoLaboratory/CRAQ` -> spdx_id: MIT.
        license="MIT",
        usage=(
            "Runs against sorted BAMs BioFlow's own align pipeline "
            "produced, never raw FASTQ -- upstream recommends pre-made "
            "alignments, and it keeps a second aligner from running "
            "hidden inside a QC job. Short-read BAMs are passed as -ngs "
            "and long-read as -sms, decided from the reads' recorded "
            "chemistry rather than guessed. Circos plotting (-pl) is never "
            "enabled, so no pycircos dependency is installed and no "
            "CRAQ-generated document is served. Chimera breaking (-b) is "
            "off unless the user opts in, and its corrected FASTA is "
            "ingested as a new object rather than replacing the assembly "
            "it came from."
        ),
    ),
```

- [ ] **Step 8: Run the test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -q`
Expected: PASS, including the pre-existing `test_every_tool_is_documented`.

- [ ] **Step 9: Rebuild and verify the install is real**

```bash
docker compose up -d --build api
```

Then verify from the main checkout:

```bash
docker compose exec api craq -h
```

Expected: CRAQ's help page, exit 0. **Record the output of `du -sh /opt/craq`** — the spec's one remaining unmeasured number.

- [ ] **Step 10: Commit**

```bash
git add backend/scripts/install-craq.sh backend/Dockerfile backend/app/config.py backend/app/pipelines/tools.py backend/tests/pipelines/test_tools.py
git commit -m "feat(pipelines): install CRAQ and register the tool (#63)"
```

---

### Task 2: The runner — command builder and parsers

**Files:**
- Create: `backend/app/pipelines/craq_runner.py`, `backend/tests/pipelines/test_craq_runner.py`

This is where the NGS-only trap is defused. Read the spec's "The NGS-only trap this exposes" before writing the parser.

- [ ] **Step 1: Write the failing command-builder test**

Create `backend/tests/pipelines/test_craq_runner.py`:

```python
from pathlib import Path

import pytest

from app.pipelines import craq_runner


class TestBuildCraqCommand:
    def test_both_libraries(self):
        cmd = craq_runner.build_craq_command(
            craq_path="craq",
            assembly=Path("/w/assembly.fasta"),
            ngs_bam=Path("/w/ngs.bam"),
            sms_bam=Path("/w/sms.bam"),
            out_dir=Path("/w/out"),
            threads=4,
        )
        assert cmd[0] == "craq"
        assert "-g" in cmd and "/w/assembly.fasta" in cmd
        assert "-ngs" in cmd and "/w/ngs.bam" in cmd
        assert "-sms" in cmd and "/w/sms.bam" in cmd
        assert "-D" in cmd and "/w/out" in cmd

    def test_ngs_only_omits_sms_flag(self):
        cmd = craq_runner.build_craq_command(
            craq_path="craq",
            assembly=Path("/w/assembly.fasta"),
            ngs_bam=Path("/w/ngs.bam"),
            sms_bam=None,
            out_dir=Path("/w/out"),
            threads=4,
        )
        assert "-sms" not in cmd
        assert "-ngs" in cmd

    def test_sms_only_omits_ngs_flag(self):
        cmd = craq_runner.build_craq_command(
            craq_path="craq",
            assembly=Path("/w/assembly.fasta"),
            ngs_bam=None,
            sms_bam=Path("/w/sms.bam"),
            out_dir=Path("/w/out"),
            threads=4,
        )
        assert "-ngs" not in cmd
        assert "-sms" in cmd

    def test_plotting_is_never_enabled(self):
        cmd = craq_runner.build_craq_command(
            craq_path="craq",
            assembly=Path("/w/assembly.fasta"),
            ngs_bam=Path("/w/ngs.bam"),
            sms_bam=Path("/w/sms.bam"),
            out_dir=Path("/w/out"),
            threads=4,
        )
        assert "-pl" not in cmd

    def test_break_is_off_by_default(self):
        cmd = craq_runner.build_craq_command(
            craq_path="craq",
            assembly=Path("/w/assembly.fasta"),
            ngs_bam=Path("/w/ngs.bam"),
            sms_bam=Path("/w/sms.bam"),
            out_dir=Path("/w/out"),
            threads=4,
        )
        assert "-b" not in cmd

    def test_break_when_requested(self):
        cmd = craq_runner.build_craq_command(
            craq_path="craq",
            assembly=Path("/w/assembly.fasta"),
            ngs_bam=Path("/w/ngs.bam"),
            sms_bam=Path("/w/sms.bam"),
            out_dir=Path("/w/out"),
            threads=4,
            break_chimera=True,
        )
        assert cmd[cmd.index("-b") + 1] == "T"

    def test_no_bam_at_all_is_a_programming_error(self):
        with pytest.raises(ValueError):
            craq_runner.build_craq_command(
                craq_path="craq",
                assembly=Path("/w/assembly.fasta"),
                ngs_bam=None,
                sms_bam=None,
                out_dir=Path("/w/out"),
                threads=4,
            )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_craq_runner.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.craq_runner'`.

- [ ] **Step 3: Write the command builder**

Create `backend/app/pipelines/craq_runner.py`:

```python
"""CRAQ command construction and output parsing.

Same split `quast_runner` and `ragtag_runner` use: pure functions over
strings and paths, testable without a container, a queue, or a binary.

Two things about CRAQ's output shape drive this module, both read from
upstream's own source (`src/format_results_addAQI.pl`,
`src/runAQI_NGS.sh`) rather than inferred from the README:

- **The final report is `<genome basename>_final.Report`**, not
  `out_final.Report` as the README's prose implies -- that naming applies
  to the sibling `.bed` files. The handler links the assembly under a fixed
  name, so the prefix is predictable.
- **A short-read-only run still prints CSE, S-AQI and AQI columns**, and
  they are meaningless: upstream states CSE is "hardly detected" without
  long reads, but `runAQI_NGS.sh` pipes through the same formatter, which
  unconditionally prints all eight columns and computes AQI as a harmonic
  mean of both halves. Reading the file faithfully therefore *produces* a
  clean, wrong `0.000`. `parse_final_report` takes `has_sms`/`has_ngs` and
  drops the fields the inputs cannot support, so the omission is enforced
  here rather than left to every caller to remember.
"""

import re
from pathlib import Path

# `Avg.CRE(R-AQI)` packs two numbers into one column, e.g. "0.512(94.881)".
# Same shape the upstream formatter matches with /(\S+)\((\S+)\)/.
_PAIRED_FIELD = re.compile(r"^(?P<value>[^()]+)\((?P<aqi>[^()]+)\)$")


def build_craq_command(
    *,
    craq_path: str,
    assembly: Path,
    ngs_bam: Path | None,
    sms_bam: Path | None,
    out_dir: Path,
    threads: int,
    mapq: int = 20,
    break_chimera: bool = False,
) -> list[str]:
    """The argv for `craq` against pre-made BAMs.

    At least one BAM is required; CRAQ has nothing to do without one, and
    reaching here with neither is a caller bug rather than a user error --
    the launch path validates first.

    `-x` (the minimap2 preset) is deliberately absent: upstream ignores it
    when a BAM is supplied, and passing it would suggest this code aligns
    anything, which it does not.

    `-pl` is never passed. Plotting needs pycircos, which is not installed,
    and this application serves no CRAQ-generated document.
    """
    if ngs_bam is None and sms_bam is None:
        raise ValueError("CRAQ needs at least one of ngs_bam or sms_bam")

    cmd = [craq_path, "-g", str(assembly)]
    if ngs_bam is not None:
        cmd += ["-ngs", str(ngs_bam)]
    if sms_bam is not None:
        cmd += ["-sms", str(sms_bam)]
    cmd += ["-q", str(mapq), "-t", str(threads), "-D", str(out_dir)]
    if break_chimera:
        cmd += ["-b", "T"]
    return cmd
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_craq_runner.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Write the failing parser tests**

The whole-assembly row is keyed `all` by upstream's report. Append to `backend/tests/pipelines/test_craq_runner.py`:

```python
_REPORT = (
    "Short Report:\n"
    "#Chr\tCovered.Rate\tLow-conf.Rate\tAvg.CRH\tAvg.CSH\t"
    "Avg.CRE(R-AQI)\tAvg.CSE(S-AQI)\tAQI\n"
    "all\t0.998\t0.012\t1.250\t0.310\t0.512(94.881)\t0.104(97.220)\t96.031\n"
)


class TestParseFinalReport:
    def test_both_libraries_parses_everything(self):
        facts = craq_runner.parse_final_report(_REPORT, has_ngs=True, has_sms=True)
        assert facts["assembly_error_r_aqi"] == 94.881
        assert facts["assembly_error_s_aqi"] == 97.220
        assert facts["assembly_error_aqi"] == 96.031
        assert facts["assembly_error_covered_rate"] == 0.998
        assert facts["assembly_error_low_confidence_rate"] == 0.012

    def test_aqi_values_are_floats(self):
        facts = craq_runner.parse_final_report(_REPORT, has_ngs=True, has_sms=True)
        assert isinstance(facts["assembly_error_r_aqi"], float)
        assert isinstance(facts["assembly_error_s_aqi"], float)

    def test_ngs_only_omits_structural_facts_entirely(self):
        """The load-bearing test. A short-only run's report still *contains*
        CSE, S-AQI and AQI columns -- upstream's formatter always prints
        them -- and they are meaningless. Absent, never zero."""
        facts = craq_runner.parse_final_report(_REPORT, has_ngs=True, has_sms=False)
        assert "assembly_error_s_aqi" not in facts
        assert "assembly_error_aqi" not in facts
        assert facts["assembly_error_r_aqi"] == 94.881

    def test_sms_only_keeps_regional_facts(self):
        """Upstream says CRE is merely undercounted without short reads, not
        undetectable -- so unlike CSE it is kept, with the caveat carried by
        the has_ngs fact rather than by dropping the number."""
        facts = craq_runner.parse_final_report(_REPORT, has_ngs=False, has_sms=True)
        assert facts["assembly_error_r_aqi"] == 94.881
        assert facts["assembly_error_s_aqi"] == 97.220

    def test_unparseable_returns_empty(self):
        assert craq_runner.parse_final_report("garbage", has_ngs=True, has_sms=True) == {}

    def test_missing_all_row_returns_empty(self):
        text = (
            "Short Report:\n"
            "#Chr\tCovered.Rate\tLow-conf.Rate\tAvg.CRH\tAvg.CSH\t"
            "Avg.CRE(R-AQI)\tAvg.CSE(S-AQI)\tAQI\n"
        )
        assert craq_runner.parse_final_report(text, has_ngs=True, has_sms=True) == {}


class TestCountBedRecords:
    def test_counts_lines(self, tmp_path):
        bed = tmp_path / "out_final.CRE.bed"
        bed.write_text("chr1\t100\t101\tchr1:100\tCRE\nchr1\t200\t201\tchr1:200\tCRE\n")
        assert craq_runner.count_bed_records(bed) == 2

    def test_missing_file_is_none_not_zero(self, tmp_path):
        """A .bed CRAQ never wrote is unmeasured, not a count of zero --
        same reasoning as the CSE omission."""
        assert craq_runner.count_bed_records(tmp_path / "absent.bed") is None

    def test_empty_file_is_zero(self, tmp_path):
        bed = tmp_path / "out_final.CRE.bed"
        bed.write_text("")
        assert craq_runner.count_bed_records(bed) == 0

    def test_counts_are_ints(self, tmp_path):
        bed = tmp_path / "out_final.CRE.bed"
        bed.write_text("chr1\t100\t101\tchr1:100\tCRE\n")
        assert isinstance(craq_runner.count_bed_records(bed), int)
```

- [ ] **Step 6: Run them to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_craq_runner.py -q`
Expected: FAIL — `AttributeError: module 'app.pipelines.craq_runner' has no attribute 'parse_final_report'`.

- [ ] **Step 7: Write the parsers**

Append to `backend/app/pipelines/craq_runner.py`:

```python
def _float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def parse_final_report(text: str, *, has_ngs: bool, has_sms: bool) -> dict:
    """Whole-assembly facts from `<genome>_final.Report`.

    Returns `{}` for anything unreadable rather than raising -- the posture
    `quast_runner.parse_report_tsv` documents: a summary that cannot be read
    must not fail a run that already produced real output.

    Only the `all` row is stored. The per-contig rows above it are a
    different granularity than the fact table holds, and the `.bed` files
    carry the per-locus detail anyway.

    **`has_sms=False` drops every structural field**, including the overall
    AQI, which is a harmonic mean of R-AQI and S-AQI and so inherits its
    meaninglessness. See the module docstring.
    """
    facts: dict = {}
    for line in text.strip().splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 8 or parts[0] != "all":
            continue

        _, cov, lowconf, _crh, _csh, cre_field, cse_field, aqi = parts

        covered = _float(cov)
        if covered is not None:
            facts["assembly_error_covered_rate"] = covered
        low = _float(lowconf)
        if low is not None:
            facts["assembly_error_low_confidence_rate"] = low

        cre_match = _PAIRED_FIELD.match(cre_field.strip())
        if cre_match:
            r_aqi = _float(cre_match.group("aqi"))
            if r_aqi is not None:
                facts["assembly_error_r_aqi"] = r_aqi

        if has_sms:
            cse_match = _PAIRED_FIELD.match(cse_field.strip())
            if cse_match:
                s_aqi = _float(cse_match.group("aqi"))
                if s_aqi is not None:
                    facts["assembly_error_s_aqi"] = s_aqi
            overall = _float(aqi)
            if overall is not None:
                facts["assembly_error_aqi"] = overall

        break

    return facts


def count_bed_records(path: Path) -> int | None:
    """How many records a CRAQ `.bed` holds, or None if it does not exist.

    None rather than 0 for a missing file, deliberately: a `.bed` CRAQ never
    wrote is a measurement that did not happen, and storing it as zero would
    claim the opposite of what is true. Same reasoning as dropping CSE facts
    on a short-read-only run.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    return sum(1 for line in text.splitlines() if line.strip())
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_craq_runner.py -q`
Expected: PASS (17 tests).

- [ ] **Step 9: Commit**

```bash
git add backend/app/pipelines/craq_runner.py backend/tests/pipelines/test_craq_runner.py
git commit -m "feat(pipelines): CRAQ command builder and output parsers (#63)"
```

---

### Task 3: The handler

**Files:**
- Modify: `backend/app/queue/assembly_qc_handlers.py`, `backend/app/queue/results.py`

- [ ] **Step 1: Add the handler**

Append to `backend/app/queue/assembly_qc_handlers.py`. Add `craq_runner` to the `app.pipelines` import on line 22 first.

```python
# CRAQ over pre-made BAMs skips the read-mapping step its own README calls
# the most time-consuming part, so this is far below assess_completeness's
# three hours. Matched to QUAST's hour until a real vertebrate-scale run is
# measured -- a lease expiring mid-run is a worse failure than one set long.
ASSEMBLY_ERROR_LEASE_SECONDS = 3600

# Fixed names, never the object's own. CRAQ is a Perl/shell pipeline that
# interpolates its inputs into `system()` calls (see bin/craq), so a
# filename carrying shell metacharacters is the analogue of the QUAST label
# XSS -- closed the same way, before it can exist.
_CRAQ_ASSEMBLY_LINK = "assembly.fasta"
_CRAQ_NGS_LINK = "ngs_sort.bam"
_CRAQ_SMS_LINK = "sms_sort.bam"


@handler(
    "assess_assembly_errors",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.HEAVY),
    max_attempts=1,
)
def assess_assembly_errors(ctx: JobContext) -> dict:
    """Reference-free assembly error detection for one assembly, with CRAQ.

    Read-only by default: no new object, only facts merged onto the assembly
    that was scored. `-b` chimera breaking is the one exception and is
    opt-in per run.

    **Input filenames never reach the command line.** CRAQ shells out
    through `system()` with its arguments interpolated, so unlike QUAST the
    risk is shell metacharacters rather than HTML. Every input is linked
    under a fixed name; the object's own name is recorded as a fact, not
    passed as an argument.

    **A BAM's index must travel with it.** CRAQ requires `sort.bam.bai`
    beside `sort.bam`; linking the BAM alone produces a failure deep in a
    samtools call rather than a clear error.
    """
    tool = tools.require(tools.craq())

    work = _prepare_workdir(ctx, "assembly_errors")

    assembly = _resolve_input(ctx.payload, "assembly")
    assembly = _named_link(work, assembly, _CRAQ_ASSEMBLY_LINK)

    ngs_bam = None
    if ctx.payload.get("ngs_bam_path") or ctx.payload.get("ngs_bam_sha256"):
        raw = _resolve_input(ctx.payload, "ngs_bam")
        ngs_bam = _named_link(work, raw, _CRAQ_NGS_LINK)
        _link_bam_index(work, raw, _CRAQ_NGS_LINK)

    sms_bam = None
    if ctx.payload.get("sms_bam_path") or ctx.payload.get("sms_bam_sha256"):
        raw = _resolve_input(ctx.payload, "sms_bam")
        sms_bam = _named_link(work, raw, _CRAQ_SMS_LINK)
        _link_bam_index(work, raw, _CRAQ_SMS_LINK)

    if ngs_bam is None and sms_bam is None:
        raise PermanentError(
            "Assembly error detection needs at least one alignment of reads "
            "against this assembly."
        )

    threads = max(1, int(ctx.payload.get("threads") or 4))
    break_chimera = bool(ctx.payload.get("break_chimera"))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    out_dir = work / "out"
    cmd = craq_runner.build_craq_command(
        craq_path=tool.path,
        assembly=assembly,
        ngs_bam=ngs_bam,
        sms_bam=sms_bam,
        out_dir=out_dir,
        threads=threads,
        break_chimera=break_chimera,
    )

    ctx.progress(phase="starting", pct=None, message="starting craq")
    ctx.extend_lease(ASSEMBLY_ERROR_LEASE_SECONDS)

    log.info(
        "assembly_errors_started",
        job_id=ctx.job_id,
        has_ngs=ngs_bam is not None,
        has_sms=sms_bam is not None,
        break_chimera=break_chimera,
        threads=threads,
    )

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "craq")

    aqi_dir = out_dir / "runAQI_out"
    # `<genome basename>_final.Report` -- predictable only because the
    # assembly is linked under a fixed name above.
    stem = Path(_CRAQ_ASSEMBLY_LINK).name
    report_path = aqi_dir / f"{stem}_final.Report"
    if not report_path.exists():
        raise RetryableError("craq exited successfully but wrote no final report")

    has_ngs = ngs_bam is not None
    has_sms = sms_bam is not None

    facts = craq_runner.parse_final_report(
        report_path.read_text(errors="replace"), has_ngs=has_ngs, has_sms=has_sms
    )

    cre = craq_runner.count_bed_records(aqi_dir / "locER_out" / "out_final.CRE.bed")
    if cre is not None:
        facts["assembly_error_cre_count"] = cre
    crh = craq_runner.count_bed_records(aqi_dir / "locER_out" / "out_final.CRH.bed")
    if crh is not None:
        facts["assembly_error_crh_count"] = crh

    # Structural counts only when long reads were supplied -- the same rule
    # parse_final_report applies to S-AQI, for the same reason.
    if has_sms:
        cse = craq_runner.count_bed_records(aqi_dir / "strER_out" / "out_final.CSE.bed")
        if cse is not None:
            facts["assembly_error_cse_count"] = cse
        csh = craq_runner.count_bed_records(aqi_dir / "strER_out" / "out_final.CSH.bed")
        if csh is not None:
            facts["assembly_error_csh_count"] = csh

    if not facts:
        log.warning("assembly_errors_report_unparseable", job_id=ctx.job_id)

    # Which inputs produced these numbers is not optional metadata: a CRE
    # count from a long-read-only run is undercounted, and without these
    # flags nothing downstream can say so.
    facts["assembly_error_tool"] = "craq"
    facts["assembly_error_tool_version"] = tool.version
    facts["assembly_error_has_ngs"] = has_ngs
    facts["assembly_error_has_sms"] = has_sms

    ctx.progress(phase="done", pct=1.0, message="assembly error QC complete")
    log.info(
        "assembly_errors_finished",
        job_id=ctx.job_id,
        cre=facts.get("assembly_error_cre_count"),
        cse=facts.get("assembly_error_cse_count"),
        aqi=facts.get("assembly_error_aqi"),
    )

    return {
        "object_id": ctx.payload.get("object_id"),
        "job_id": ctx.job_id,
        "facts": facts,
        "workdir": str(work),
        "break_chimera": break_chimera,
        "corrected_fasta": str(aqi_dir / "out_correct.fa")
        if break_chimera and (aqi_dir / "out_correct.fa").exists()
        else None,
    }


def _link_bam_index(work: Path, bam: Path, link_name: str) -> None:
    """Link a BAM's `.bai` beside its fixed-name link.

    CRAQ requires the index next to the BAM and fails inside a samtools
    call, not with a clear message, when it is missing. Both `x.bam.bai`
    and `x.bai` are accepted on input; the link is always written as
    `<link_name>.bai`, which is what samtools looks for first.
    """
    for candidate in (Path(f"{bam}.bai"), bam.with_suffix(".bai")):
        if candidate.exists():
            target = work / f"{link_name}.bai"
            if not target.exists():
                target.symlink_to(candidate)
            return
    log.warning("craq_bam_index_missing", bam=str(bam))
```

- [ ] **Step 2: Add the results applier**

In `backend/app/queue/results.py`, after `_apply_assess_misassemblies` (~line 1515):

```python
async def _apply_assess_assembly_errors(result: dict, *, owner: str) -> None:
    """Record CRAQ's error-detection facts on the assembly they describe.

    Near-copy of `_apply_assess_misassemblies`: read-only, nothing to
    ingest, and an uploaded assembly is scored exactly like one this
    application produced.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("assembly_errors_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "assembly_errors_applied",
        object_id=object_id,
        cre=facts.get("assembly_error_cre_count"),
        aqi=facts.get("assembly_error_aqi"),
    )
```

Register it in the appliers dict beside `"assess_misassemblies"` (~line 2070):

```python
    "assess_assembly_errors": _apply_assess_assembly_errors,
```

- [ ] **Step 3: Verify the handler registers**

Run: `./backend/run-worktree-tests.sh tests/queue/ -q`
Expected: PASS. Then confirm the handler is discoverable:

```bash
./backend/run-worktree-tests.sh --collect-only -q 2>&1 | tail -3
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/queue/assembly_qc_handlers.py backend/app/queue/results.py
git commit -m "feat(queue): assess_assembly_errors handler for CRAQ (#63)"
```

---

### Task 4: Launch path, route, and card

**Files:**
- Modify: `backend/app/services/pipeline_service.py`, `backend/app/api/v1/pipelines.py`, `backend/app/services/suggestion_service.py`
- Test: `backend/tests/services/test_suggestion_service.py`

- [ ] **Step 1: Add BAM resolution to pipeline_service**

`ReadChemistry.SHORT` routes to `-ngs`; the four long chemistries route to `-sms`. `UNKNOWN`/`None` is refused rather than defaulted — feeding long reads to `-ngs` mislabels the evidence rather than degrading gracefully. Add near `reference_for_bam` (~line 1660):

```python
async def alignments_against(
    assembly: DataObject, *, owner: str
) -> tuple[list[DataObject], list[DataObject], list[DataObject]]:
    """BAMs aligned against this assembly, split by read chemistry.

    Returns `(short, long, unknown)`. The reverse of `reference_for_bam`:
    an alignment records its reference in `derived_from`, so "was this BAM
    aligned to this assembly?" is a lookup rather than a guess, and an
    uploaded BAM with no provenance is correctly excluded.

    `unknown` is returned rather than folded into `short`. Callers must
    refuse it: `read_chemistry_for_alignment` falls back to a short-read
    default for picking an alignment preset, which is right there and wrong
    here -- passing long reads as `-ngs` misdescribes the evidence rather
    than degrading.
    """
    from app.services import object_service

    candidates = [
        o
        for o in await object_service.list_objects(
            assembly.project_id, owner=owner, status=ObjectStatus.READY
        )
        if o.format.kind is FormatKind.BAM and assembly.id in o.derived_from
    ]

    short: list[DataObject] = []
    long_: list[DataObject] = []
    unknown: list[DataObject] = []
    for bam in candidates:
        chemistry = await read_chemistry_for_alignment(bam)
        if chemistry is align_runner.ReadChemistry.SHORT:
            short.append(bam)
        elif chemistry in (
            align_runner.ReadChemistry.HIFI,
            align_runner.ReadChemistry.CLR,
            align_runner.ReadChemistry.ONT_SIMPLEX,
            align_runner.ReadChemistry.ONT_DUPLEX,
        ):
            long_.append(bam)
        else:
            unknown.append(bam)
    return short, long_, unknown
```

- [ ] **Step 2: Add the launch function**

Append to `backend/app/services/pipeline_service.py`:

```python
async def launch_assembly_error_qc(
    *,
    object_id: PydanticObjectId,
    owner: str,
    ngs_bam_id: PydanticObjectId | None = None,
    sms_bam_id: PydanticObjectId | None = None,
    break_chimera: bool = False,
) -> Job:
    """Queue a CRAQ run: reference-free assembly error detection.

    Auto-pairs when unambiguous -- exactly one short-read BAM and/or exactly
    one long-read BAM against this assembly -- and refuses otherwise, the
    same "ambiguity is a chooser, not a guess" rule `launch_misassembly_qc`
    follows for references. Explicit ids come from the dialog.

    Read-only unless `break_chimera`, which is opt-in per run and never set
    by the Actions card.
    """
    from app.queue import queue
    from app.services import object_service, reference_assembly

    tool = tools.require(tools.craq())

    assembly = await object_service.get_object(object_id, owner=owner)
    reference_assembly.check_draft_assembly(assembly)

    if ngs_bam_id is None and sms_bam_id is None:
        short, long_, _unknown = await alignments_against(assembly, owner=owner)
        if not short and not long_:
            raise ValidationError(
                "Assembly error detection needs reads aligned to this "
                "assembly, and this project has none",
                details={"object_id": str(assembly.id)},
            )
        if len(short) > 1 or len(long_) > 1:
            raise ValidationError(
                "This assembly has several alignments; name the ones to use",
                details={
                    "short": [str(o.id) for o in short],
                    "long": [str(o.id) for o in long_],
                },
            )
        ngs_bam = short[0] if short else None
        sms_bam = long_[0] if long_ else None
    else:
        ngs_bam = (
            await object_service.get_object(ngs_bam_id, owner=owner)
            if ngs_bam_id
            else None
        )
        sms_bam = (
            await object_service.get_object(sms_bam_id, owner=owner)
            if sms_bam_id
            else None
        )

    payload: dict = {
        "object_id": str(assembly.id),
        "threads": 4,
        "break_chimera": break_chimera,
    }

    asm_digest, asm_path = await _resolve_readable(assembly)
    if asm_digest:
        payload["assembly_sha256"] = asm_digest
    if asm_path:
        payload["assembly_path"] = asm_path

    for bam, prefix in ((ngs_bam, "ngs_bam"), (sms_bam, "sms_bam")):
        if bam is None:
            continue
        # Validated provenance, not trust: a BAM aligned to some *other*
        # assembly would produce clipping signals that describe the wrong
        # sequence and read as errors in this one.
        if assembly.id not in bam.derived_from:
            raise ValidationError(
                f"{bam.name} was not aligned against this assembly",
                details={"bam_id": str(bam.id), "object_id": str(assembly.id)},
            )
        digest, path = await _resolve_readable(bam)
        if digest:
            payload[f"{prefix}_sha256"] = digest
        if path:
            payload[f"{prefix}_path"] = path
        payload[f"{prefix}_object_id"] = str(bam.id)

        # **Added after Task 3's code review found a real gap the plan
        # missed**: BioFlow's storage is content-addressed, so a BAM and
        # its .bai are separate DataObjects with no path relationship --
        # `_link_bam_index`'s path-guessing fallback cannot find the index
        # of a BAM produced by BioFlow's own align pipeline, which is the
        # primary input this whole slice exists to consume. Resolve the
        # sidecar explicitly, the same way `launch_bam_stats` already does
        # (`bai_sha256`/`bai_path`, `pipeline_service.py:1496-1542`) via
        # `_sidecar_of_role(bam, SidecarRole.BAI)`. The handler now reads
        # `{prefix}_bai_sha256`/`{prefix}_bai_path` -- e.g. `ngs_bai_path`,
        # `sms_bai_path` -- via `_resolve_input(payload, f"{prefix}_bai")`,
        # commit b719d90. Omitting these is not silently tolerated: a
        # missing index now raises `PermanentError` rather than warning
        # and continuing into an opaque samtools failure.
        bai = await _sidecar_of_role(bam, SidecarRole.BAI)
        if bai is not None:
            bai_digest, bai_path = await _resolve_readable(bai)
            if bai_digest:
                payload[f"{prefix}_bai_sha256"] = bai_digest
            if bai_path:
                payload[f"{prefix}_bai_path"] = bai_path

    dedup = f"assess_assembly_errors:{assembly.id}:{ngs_bam.id if ngs_bam else '-'}"
    dedup += f":{sms_bam.id if sms_bam else '-'}:{break_chimera}"

    job = await queue.enqueue(
        "assess_assembly_errors",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.HEAVY),
        max_attempts=1,
        dedup_key=dedup,
        project_id=assembly.project_id,
        object_id=assembly.id,
    )
    if job is None:
        raise ValidationError("This assembly error QC job is already queued")
    return job
```

- [ ] **Step 3: Add the route**

In `backend/app/api/v1/pipelines.py`, following the `/pipelines/misassemblies` route's shape exactly (find it and mirror its request model, auth dependency, and response):

```python
class AssemblyErrorRequest(BaseModel):
    object_id: PydanticObjectId
    ngs_bam_id: PydanticObjectId | None = None
    sms_bam_id: PydanticObjectId | None = None
    break_chimera: bool = False


@router.post("/assembly-errors")
async def launch_assembly_errors(
    body: AssemblyErrorRequest,
    owner: str = Depends(current_owner),
):
    job = await pipeline_service.launch_assembly_error_qc(
        object_id=body.object_id,
        owner=owner,
        ngs_bam_id=body.ngs_bam_id,
        sms_bam_id=body.sms_bam_id,
        break_chimera=body.break_chimera,
    )
    return {"job_id": str(job.id)}
```

- [ ] **Step 4: Write the failing card test**

Assert the card flips to **unavailable** when the probe is patched off — the image ships tools installed, so the available-direction assertion passes whether or not the patch worked. Add to `backend/tests/services/test_suggestion_service.py`:

```python
class TestAssemblyErrorCard:
    def test_unavailable_when_craq_is_not_installed(self, monkeypatch):
        from app.pipelines import tools
        from app.services import suggestion_service

        monkeypatch.setattr(
            tools, "craq", lambda: tools.Tool(
                name="craq", path="", available=False, version=None,
                error="CRAQ is not installed.",
            ),
        )
        obj = _assembly_object()
        card = suggestion_service.build_assembly_error_card(obj, [_bam_object(obj.id)])
        assert card is not None
        assert card.status is suggestion_service.CardStatus.UNAVAILABLE
        assert "not installed" in card.reason

    def test_unavailable_without_any_alignment(self):
        from app.services import suggestion_service

        card = suggestion_service.build_assembly_error_card(_assembly_object(), [])
        assert card is not None
        assert card.status is suggestion_service.CardStatus.UNAVAILABLE
        assert "aligned" in card.reason.lower()
```

Add helpers `_assembly_object()` and `_bam_object(assembly_id)` beside the module's existing fixture builders, matching their style — a READY assembly-shaped FASTA, and a READY BAM whose `derived_from` contains the assembly id.

- [ ] **Step 5: Run it to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py::TestAssemblyErrorCard -q`
Expected: FAIL — `AttributeError: ... has no attribute 'build_assembly_error_card'`.

- [ ] **Step 6: Write the card**

Add to `backend/app/services/suggestion_service.py` near `build_misassembly_card`:

```python
def build_assembly_error_card(obj, alignments) -> SuggestionCard | None:
    """Reference-free assembly error detection for an assembly, by CRAQ.

    Anchored on the assembly; `alignments` is the project's BAMs whose
    `derived_from` contains it. Unlike the misassembly card beside it, this
    gates on *reads*, not on a reference -- CRAQ needs no second genome,
    which is what makes it usable for an organism with no relative in NCBI.

    `category="ASSEMBLY_QC"`: this evaluates an assembly rather than
    improving it. Chimera breaking is never offered here -- the card's
    launch body is the complete request, and a suggestion that silently
    rewrites an assembly is not a suggestion.
    """
    if not reference_assembly._is_assembly_like(obj):
        return None
    if obj.status is not ObjectStatus.READY:
        return None

    title = "Detect assembly errors"
    description = (
        "Find misassembled regions from read clipping -- where reads align "
        "only partially, the assembly is usually wrong -- and separate true "
        "errors from heterozygous variants. Needs no reference genome, with "
        "CRAQ."
    )

    def unavailable(reason: str) -> SuggestionCard:
        return SuggestionCard(
            kind="assembly_errors",
            category="ASSEMBLY_QC",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=reason,
        )

    tool = tools.craq()
    if not tool.available:
        return unavailable(tool.error or "CRAQ is not installed.")

    if not alignments:
        return unavailable(
            "Assembly error detection needs reads aligned to this assembly. "
            "Align a read set against it first."
        )

    return SuggestionCard(
        kind="assembly_errors",
        category="ASSEMBLY_QC",
        title=title,
        description=description,
        why=f"{len(alignments)} alignment(s) against this assembly.",
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/assembly-errors",
            "body": {"object_id": str(obj.id)},
        },
    )
```

- [ ] **Step 7: Register the card**

**This step is silently skippable and is the whole reason `CLAUDE.md` warns about it** — a tool that no rule can pick is never suggested, however cleanly it installs. In the card list beside `("misassembly", ...)` (~line 1427):

```python
        ("assembly_errors", lambda: build_assembly_error_card(obj, assembly_alignments)),
```

Build `assembly_alignments` in the orchestrator alongside `scaffold_references`, filtering the project's objects to READY BAMs whose `derived_from` contains `obj.id`.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q`
Expected: PASS.

- [ ] **Step 9: Run the full suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS, at least 3384 + the new tests. **Read the count, not the exit code.**

- [ ] **Step 10: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/api/v1/pipelines.py backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat(pipelines): launch path, route and Actions card for CRAQ (#63)"
```

---

### Task 5: Chimera breaking (opt-in)

**Files:**
- Modify: `backend/app/queue/results.py`

This widens epic #13's stated scope; see the note on #13. The constraint that makes it safe is that `out_correct.fa` becomes a **new object** and never replaces its input.

- [ ] **Step 1: Extend the applier to ingest the corrected FASTA**

In `_apply_assess_assembly_errors`, after the `obj.set(...)` call:

```python
    corrected = result.get("corrected_fasta")
    if not corrected:
        return

    from app.services import object_service

    job_id = result.get("job_id")
    parents = [obj.id]
    for key in ("ngs_bam_object_id", "sms_bam_object_id"):
        bam_id = (result.get("payload") or {}).get(key)
        if bam_id:
            parents.append(PydanticObjectId(bam_id))

    try:
        await object_service.ingest_local_file(
            owner=obj.owner,
            project_id=obj.project_id,
            path=Path(corrected),
            name=f"{obj.name}.craq-corrected.fa",
            # REFERENCE for the same reason a de novo assembly gets it
            # (results.py:1271): it is assembly-shaped and alignable. A new
            # object, never a replacement -- the input assembly keeps its
            # facts and its identity.
            role=ObjectRole.REFERENCE,
            derived_from=parents,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts={"assembly_source": "craq_break"},
            metadata=dict(obj.metadata),
        )
    except Exception as e:  # noqa: BLE001
        # Never destroys the QC result: the facts above are already
        # committed, and a secondary ingest failing must not lose them --
        # the posture _apply_assemble_reads takes for its graph output.
        log.error("craq_corrected_ingest_failed", object_id=str(obj.id), error=str(e))
```

Add `"ngs_bam_object_id"` / `"sms_bam_object_id"` to the handler's returned dict so the applier can read them, and ensure `Path` and `ObjectRole` are imported in `results.py` (they are).

- [ ] **Step 2: Run the suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add backend/app/queue/results.py backend/app/queue/assembly_qc_handlers.py
git commit -m "feat(queue): ingest CRAQ's corrected FASTA as a new object (#63)"
```

---

### Task 6: Frontend facts block

**Files:**
- Modify: `frontend/src/components/AssemblyFacts.tsx`

- [ ] **Step 1: Read the facts**

Beside the misassembly block's fact reads (~line 113):

```tsx
  // Assembly errors: CRAQ, reference-free. Structural facts (CSE, S-AQI,
  // AQI) are absent rather than zero on a short-read-only run -- the
  // handler drops them because CRAQ prints meaningless values for them
  // when no long reads were supplied. `undefined` here means "not
  // measured", and the block must not render a 0 in its place.
  const errorTool = facts.assembly_error_tool as string | undefined;
  const errorAqi = facts.assembly_error_aqi as number | undefined;
  const errorRAqi = facts.assembly_error_r_aqi as number | undefined;
  const errorSAqi = facts.assembly_error_s_aqi as number | undefined;
  const errorCre = facts.assembly_error_cre_count as number | undefined;
  const errorCse = facts.assembly_error_cse_count as number | undefined;
  const errorHasNgs = facts.assembly_error_has_ngs as boolean | undefined;
  const errorHasSms = facts.assembly_error_has_sms as boolean | undefined;
```

- [ ] **Step 2: Render the block**

After the misassembly block's closing tag (~line 470), mirroring its markup:

```tsx
      {errorTool && (
        <div className="facts-block">
          <div className="facts-block-title">
            Assembly errors ({errorTool}, reference-free)
          </div>
          <dl className="kv">
            {errorAqi !== undefined && (
              <>
                <dt>AQI</dt>
                <dd>
                  {errorAqi.toFixed(1)}{" "}
                  <span className="muted">
                    {errorAqi > 90
                      ? "reference quality"
                      : errorAqi >= 80
                        ? "high quality"
                        : errorAqi >= 60
                          ? "draft quality"
                          : "low quality"}
                  </span>
                </dd>
              </>
            )}
            {errorRAqi !== undefined && (
              <>
                <dt>R-AQI (regional)</dt>
                <dd>{errorRAqi.toFixed(1)}</dd>
              </>
            )}
            {errorSAqi !== undefined && (
              <>
                <dt>S-AQI (structural)</dt>
                <dd>{errorSAqi.toFixed(1)}</dd>
              </>
            )}
            {errorCre !== undefined && (
              <>
                <dt>Regional errors (CRE)</dt>
                <dd>{errorCre}</dd>
              </>
            )}
            {errorCse !== undefined && (
              <>
                <dt>Structural errors (CSE)</dt>
                <dd>{errorCse}</dd>
              </>
            )}
          </dl>
          {errorHasSms === false && (
            <p className="muted small">
              Short reads only: structural errors are not reported, because
              CRAQ can barely detect them without long reads.
            </p>
          )}
          {errorHasNgs === false && (
            <p className="muted small">
              Long reads only: regional errors are undercounted, especially
              for ONT-based assemblies.
            </p>
          )}
        </div>
      )}
```

- [ ] **Step 3: Verify in the browser**

There is no headless component-testing setup in this repo; manual verification is the actual test.

```bash
./ops/worktree-up.sh
```

Open `localhost:5273`, find an assembly with CRAQ facts, confirm the block renders and the AQI band matches the number.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/AssemblyFacts.tsx
git commit -m "feat(frontend): CRAQ assembly error facts block (#63)"
```

---

### Task 7: Real-data verification and closeout

`CLAUDE.md`: "Check a rule against the real database, not only its unit tests." The suggestion rules previously shipped two bugs that a full green suite missed and one look at a real project exposed.

- [ ] **Step 1: Run CRAQ end to end on a real project**

Pick a project with an assembly and at least one alignment against it, launch from the Actions tab, and confirm the job completes and facts land. **Record wall-clock runtime and `du -sh /opt/craq`** — the two numbers the spec left unmeasured.

- [ ] **Step 2: Verify the card against real objects**

```bash
docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.services import suggestion_service, object_service
# ... list a real project's objects, build the card, print status+reason
"
```

Confirm the card is AVAILABLE for an assembly with an alignment and UNAVAILABLE with a clear reason for one without.

- [ ] **Step 3: Verify the short-read-only path writes no CSE facts**

The load-bearing behaviour. On an assembly with only a short-read alignment, run CRAQ and confirm `assembly_error_cse_count`, `assembly_error_s_aqi` and `assembly_error_aqi` are **absent** from the stored facts — not zero.

```bash
docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models import DataObject
# ... fetch the object, print sorted(k for k in obj.facts if k.startswith('assembly_error'))
"
```

- [ ] **Step 4: Update the spec with measured numbers**

Replace the spec's "Verify before implementing" section with the measured install size and runtime, following the QUAST entry's precedent of recording what the implementation found that the design did not know.

- [ ] **Step 5: Close out the TODO entry**

`docs/TODO.md`'s post-assembly QC entry lists CRAQ under "Still open". Strike through the CRAQ portion with a `— FIXED, <date>` note saying what shipped and where, matching how the QUAST closure is recorded. The entry itself stays in `docs/TODO.md` — GCI and Merqury are still open under it.

- [ ] **Step 6: Full suite, then commit**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS. Read the count.

```bash
git add docs/
git commit -m "docs: record CRAQ measurements and close out the TODO entry (#63)"
```

- [ ] **Step 7: Merge and push**

Per `CLAUDE.md`: once the suite is green and `main` is clean, merge and push without asking. If `main` has moved, re-run the suite after merging rather than assuming the green still holds.

```bash
git checkout main && git merge --no-ff - && ./backend/run-worktree-tests.sh tests/ -q && git push origin main
```

- [ ] **Step 8: Update the issue**

Close #63 with a comment recording what shipped, the measured numbers, and anything the implementation found that this plan did not know. Then check whether #13's remaining children (#64 Merqury, #65 GCI) are all that stand between it and closure.

---

## Notes for the implementer

**If CRAQ turns out not to accept a BAM the way this plan assumes**, the approved fallback is letting it realign internally from FASTQ. The README documents both forms and prefers BAMs, so this should not arise — but if it does, the fallback is a decision already made, not a question to re-open.

**The `_final.Report` prefix depends on the fixed link name.** If you change `_CRAQ_ASSEMBLY_LINK`, the handler's `stem` must change with it. They are two halves of one fact.

**Do not add CRAQ to `assembly_qc_registry`.** That module is completeness-only and its docstring says so explicitly.

**CRH/CSH counts are stored but deliberately not rendered.** The handler
writes `assembly_error_crh_count` and `assembly_error_csh_count`; Task 6's
block shows neither. That is intentional, not an oversight: they count
heterozygous *variants*, not errors, and putting them in a block titled
"Assembly errors" beside the CRE/CSE counts would read as four kinds of
problem when two of them are ordinary biology. They are stored because
separating them from the error counts is the method's whole point, and a
future heterozygosity view has the numbers waiting. If you add them to the
UI, give them their own labelled section.
