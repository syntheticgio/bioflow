# cutadapt and Trimmomatic Runners — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** cutadapt and Trimmomatic become real, runnable trim tools — selectable in the tool selector, launchable from `TrimDialog`, and executed by `trim_reads` — alongside fastp, closing the gap both `pipeline-tool-additions-qc.md` and `tool-selector-implementation.md` left open.

**Architecture:** Both tools are probed already (`tools.py:156-166`) and both binaries are already in the Docker image — nothing to add there. What's missing is a runner module per tool (mirroring `fastp_runner.py`: command builder, report/stdout parser, params dataclass), a dispatch branch in the `trim_reads` handler keyed on a new `tool` field threaded through the payload, and a params-shape switch in the frontend. `PipelineRun` gains a `tool` field so which tool ran a trim is queryable, not just buried in output-object facts.

**Tech Stack:** Python 3 (FastAPI-style app, Beanie/Mongo, asyncio job queue), pytest; React + TypeScript, TanStack Query.

---

## Before you start — read this

This plan assumes you've read `backend/app/pipelines/fastp_runner.py`,
`backend/app/queue/pipeline_handlers.py` (the `trim_reads` handler), and
`backend/app/services/pipeline_service.py` (`launch_trim`). Every new file in
this plan mirrors an existing one; where this plan says "mirror X", go look at
X first if the shape isn't obvious from the code shown here.

**Key facts verified before writing this plan** (do not re-derive these):

- Both `cutadapt` and `trimmomatic` are **already installed** in
  `backend/Dockerfile` (apt packages). **No Dockerfile change is needed.**
- Both are **already probed**: `tools.cutadapt()` and `tools.trimmomatic()`
  exist in `backend/app/pipelines/tools.py:155-166`, both `lru_cache`d and
  already cleared by `reset_cache()`.
- `settings.trimmomatic_path` defaults to `"TrimmomaticSE"` — the Debian
  package installs **`/usr/bin/TrimmomaticPE`** and **`/usr/bin/TrimmomaticSE`**
  as separate wrapper scripts around one JAR; there is no bare `trimmomatic`
  entry point. A trim runner needs to pick PE vs SE by read layout.
- The Debian `trimmomatic` package also installs adapter FASTA files under
  **`/usr/share/trimmomatic/`**: `TruSeq3-PE.fa`, `TruSeq3-PE-2.fa`,
  `TruSeq3-SE.fa`, `TruSeq2-PE.fa`, `TruSeq2-SE.fa`, `NexteraPE-PE.fa`. These
  are the `ILLUMINACLIP:<file>:...` argument.
- cutadapt supports `-j`/`--cores` (0 = autodetect), `-a`/`-A` for R1/R2 3'
  adapters, `-q`/`-Q` for R1/R2 quality trimming, `-m`/`--minimum-length`
  (colon-separated for R1:R2), `-o`/`-p` for R1/R2 output, and **`--json=path`**
  for a structured report (added cutadapt 3.5; the Docker image's version must
  be checked — see Task 1).
- cutadapt's JSON report has stable top-level keys: `cutadapt_version`,
  `read_counts` (`input`, `output`, `filtered.too_short`, `read1_with_adapter`,
  `read2_with_adapter`), `basepair_counts` (`input`, `output`,
  `quality_trimmed`). This plan's `parse_report` reads only these scalars,
  matching how `fastp_runner.parse_report` keeps only the scalar summary and
  not fastp's per-cycle curves.
- **Trimmomatic has no structured report at all** — no JSON, no fixed-format
  summary file. It prints one human-readable completion line to stderr, e.g.
  `Input Read Pairs: 100000 Both Surviving: 97688 (97.69%) Forward Only
  Surviving: 1200 (1.20%) Reverse Only Surviving: 800 (0.80%) Dropped: 312
  (0.31%)` for PE, or `Input Reads: 100000 Surviving: 97688 (97.69%) Dropped:
  2312 (2.31%)` for SE. This plan's Trimmomatic report parser regex-matches
  that line rather than reading a file. **Verify the exact wording against the
  Docker image's installed version in Task 1** before trusting the regex in
  Task 5 — Trimmomatic's stdout format is not contractually stable across
  versions the way fastp's JSON is.
- `PipelineRun`/`RunJob` (`backend/app/models/run.py`) have **no existing
  tool-identity field**. `RunJobRole.TRIM` is a role, not a tool name. This
  plan adds `tool: str` to `PipelineRun` (Task 8).
- `results._apply_trim_reads` (`backend/app/queue/results.py:227-294`) already
  reads `result.get("tool", "fastp")` generically into
  `provenance["trimmed_by"]` — **no change needed there**. The handler just
  needs to return the real tool name instead of the literal `"fastp"`.
- `PipelineToolSelector.tsx` already renders cutadapt/Trimmomatic cards as
  disabled with "not yet supported" using `tool.runnable` from `TOOL_META`.
  **No frontend change needed there** — flipping `runnable=True` in
  `TOOL_META` (Task 9) makes the cards selectable automatically.
- `launchTrim` in `frontend/src/api/client.ts` is a thin passthrough of
  `TrimRequest` as JSON — **no change needed there** either. Only the
  `TrimRequest`/`TrimParams` types and `TrimDialog.tsx` need updating.

---

## File structure

| File | Change |
|---|---|
| `backend/app/pipelines/cutadapt_runner.py` | **Create.** Command builder, `CutadaptParams`, JSON report parser. |
| `backend/app/pipelines/trimmomatic_runner.py` | **Create.** Command builder, `TrimmomaticParams`, stdout summary parser. |
| `backend/app/config.py` | Add `trimmomatic_pe_path`, `trimmomatic_se_path`, `trimmomatic_adapters_dir`. |
| `backend/app/pipelines/tools.py` | Flip `cutadapt`/`trimmomatic` `TOOL_META.runnable` to `True`; delete stale comments. |
| `backend/app/queue/pipeline_handlers.py` | `trim_reads` dispatches on `ctx.payload.get("tool")` to one of three private per-tool functions. |
| `backend/app/services/pipeline_service.py` | `launch_trim()` gains a `tool` parameter; `default_params()` takes a `tool` parameter; new `_check_tool_runnable` helper. |
| `backend/app/api/v1/pipelines.py` | `TrimRequest` gains `tool: str = "fastp"`; `GET /defaults` takes `?tool=`. |
| `backend/app/models/run.py` | `PipelineRun` gains `tool: str`. |
| `frontend/src/api/types.ts` | `TrimRequest.tool`; `CutadaptParams`/`TrimmomaticParams`; `TrimDefaults.params` becomes tool-shaped. |
| `frontend/src/api/client.ts` | `trimDefaults(tool)` takes a tool argument. |
| `frontend/src/components/TrimDialog.tsx` | Renders the right field set per `selectedTool`; removes the "only fastp can be launched today" banner. |
| `backend/tests/pipelines/test_cutadapt_runner.py` | **Create.** Mirrors `test_fastp_runner.py`. |
| `backend/tests/pipelines/test_trimmomatic_runner.py` | **Create.** Mirrors `test_fastp_runner.py`. |
| `backend/tests/pipelines/test_tools.py` | Update `test_cutadapt_and_trimmomatic_are_not_runnable_yet` → inverted. |
| `backend/tests/pipelines/test_launch_rules.py` | Extend for tool-parameterized `launch_trim`/`default_params`. |

---

## Task 1: Confirm the Docker image's cutadapt version supports `--json`

**Files:**
- Read: `backend/Dockerfile`

- [ ] **Step 1: Check the installed cutadapt version**

The `--json` report flag was added in cutadapt 3.5. Debian trixie's `cutadapt`
apt package version must be at or above that. Build the image and check:

```bash
docker build -t bio-pipeliner-backend -f backend/Dockerfile backend
docker run --rm bio-pipeliner-backend cutadapt --version
docker run --rm bio-pipeliner-backend sh -c "TrimmomaticPE 2>&1 | head -5; echo ---; TrimmomaticSE 2>&1 | head -5"
docker run --rm bio-pipeliner-backend ls /usr/share/trimmomatic/
```

Expected: a cutadapt version ≥ 3.5 (Debian trixie ships 4.x as of this
writing), and the adapter FASTA listing from `/usr/share/trimmomatic/`
matching what's documented above (`TruSeq3-PE.fa` etc — confirm the exact
filenames present, since this plan's `TrimmomaticParams` default references
one by name).

- [ ] **Step 2: Capture one real completion line from each tool for the test fixtures**

Run a tiny real trim inside the container (a handful of reads is enough) and
capture stdout/stderr verbatim — Task 5's Trimmomatic stdout parser and Task
3's cutadapt JSON parser tests must be built from real tool output, the same
way `test_fastp_runner.py`'s docstring insists ("copied from real fastp 0.24.0
runs rather than invented"). If you don't have a FASTQ fixture handy, `printf`
a few synthetic reads into a `.fastq` file — the exact sequence content
doesn't matter, only that the tool runs and prints its real summary format.

No commit for this task — it's a recon step whose output (the confirmed
version numbers and captured stdout) feeds directly into Tasks 3 and 5.

---

## Task 2: Config — Trimmomatic paths and adapter directory

**Files:**
- Modify: `backend/app/config.py:46-61`

- [ ] **Step 1: Add the new settings**

Replace the existing `trimmomatic_path` line (and its comment) with:

```python
# Debian ships no bare `trimmomatic`: the package installs TrimmomaticPE
# and TrimmomaticSE as separate entry points around the JAR. The runner
# picks between them by read layout (paired vs single-end).
trimmomatic_path: str = "TrimmomaticSE"  # kept for the version probe only
trimmomatic_pe_path: str = "TrimmomaticPE"
trimmomatic_se_path: str = "TrimmomaticSE"
# Adapter FASTA files the Debian package installs alongside the binaries.
trimmomatic_adapters_dir: str = "/usr/share/trimmomatic"
```

Leave `trimmomatic_path` in place — `tools.trimmomatic()` still probes through
it and nothing in this plan changes that.

- [ ] **Step 2: Commit**

```bash
git add backend/app/config.py
git commit -m "feat: add Trimmomatic PE/SE paths and adapter directory setting"
```

---

## Task 3: cutadapt runner — params, command builder, report parser

**Files:**
- Create: `backend/app/pipelines/cutadapt_runner.py`
- Test: `backend/tests/pipelines/test_cutadapt_runner.py`

- [ ] **Step 1: Write the failing tests**

```python
"""cutadapt command construction and JSON report extraction.

The sample JSON in these tests is drawn from cutadapt's own documented report
schema (stable since 3.5) rather than invented -- the parsing exists to
survive that exact shape.
"""

import json
from pathlib import Path

import pytest

from app.pipelines import cutadapt_runner
from app.pipelines.cutadapt_runner import CutadaptParams


def cmd_for(**kw):
    defaults = dict(
        cutadapt_path="/usr/bin/cutadapt",
        r1_in=Path("in_R1.fastq.gz"),
        r1_out=Path("out_R1.fastq.gz"),
        json_out=Path("cutadapt.json"),
        params=CutadaptParams(),
    )
    defaults.update(kw)
    return cutadapt_runner.build_command(**defaults)


def flag_value(cmd: list[str], flag: str) -> str | None:
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


class TestBuildCommand:
    def test_single_end_has_no_second_input_or_output(self):
        cmd = cmd_for()
        assert "-p" not in cmd
        assert "-A" not in cmd
        assert flag_value(cmd, "-o") == "out_R1.fastq.gz"
        assert cmd[-1] == "in_R1.fastq.gz"

    def test_paired_end_passes_both_sides(self):
        cmd = cmd_for(
            r2_in=Path("in_R2.fastq.gz"), r2_out=Path("out_R2.fastq.gz")
        )
        assert flag_value(cmd, "-o") == "out_R1.fastq.gz"
        assert flag_value(cmd, "-p") == "out_R2.fastq.gz"
        assert cmd[-2:] == ["in_R1.fastq.gz", "in_R2.fastq.gz"]

    def test_paired_input_without_an_output_is_rejected(self):
        with pytest.raises(ValueError, match="second output"):
            cmd_for(r2_in=Path("in_R2.fastq.gz"))

    def test_json_report_path_is_passed(self):
        cmd = cmd_for(json_out=Path("report.cutadapt.json"))
        assert "--json=report.cutadapt.json" in cmd

    def test_quality_and_length_thresholds_are_passed(self):
        cmd = cmd_for(params=CutadaptParams(quality_cutoff=20, min_length=50))
        assert flag_value(cmd, "-q") == "20"
        assert flag_value(cmd, "-m") == "50"

    def test_cores_are_passed(self):
        assert flag_value(cmd_for(params=CutadaptParams(threads=8)), "-j") == "8"

    def test_adapter_r1_uses_lowercase_a(self):
        cmd = cmd_for(params=CutadaptParams(adapter_r1="AGATCGGAAGAGC"))
        assert flag_value(cmd, "-a") == "AGATCGGAAGAGC"
        assert "-A" not in cmd

    def test_adapter_r2_uses_uppercase_a_and_requires_pairing(self):
        cmd = cmd_for(
            r2_in=Path("in_R2.fastq.gz"),
            r2_out=Path("out_R2.fastq.gz"),
            params=CutadaptParams(
                adapter_r1="AGATCGGAAGAGC", adapter_r2="AGATCGGAAGAGC"
            ),
        )
        assert flag_value(cmd, "-A") == "AGATCGGAAGAGC"

    def test_no_adapter_given_omits_a_and_bigA(self):
        """Unlike fastp, cutadapt has no auto-detect mode -- omitting the
        adapter just runs quality trimming with no adapter search."""
        cmd = cmd_for()
        assert "-a" not in cmd
        assert "-A" not in cmd

    def test_minlen_defaults_to_one(self):
        """Without -m, cutadapt keeps zero-length reads, which breaks
        downstream tools -- CutadaptParams defaults min_length to 1."""
        assert CutadaptParams().min_length == 1


class TestCutadaptParams:
    def test_round_trip(self):
        p = CutadaptParams(quality_cutoff=25, min_length=30, threads=2)
        assert CutadaptParams.from_dict(p.as_dict()) == p

    def test_unknown_keys_are_ignored(self):
        assert CutadaptParams.from_dict({"bogus": 1}) == CutadaptParams()

    def test_none_values_fall_back_to_defaults(self):
        assert CutadaptParams.from_dict({"quality_cutoff": None}) == CutadaptParams()


SAMPLE_REPORT = {
    "tag": "Cutadapt report",
    "schema_version": [0, 3],
    "cutadapt_version": "4.9",
    "cores": 4,
    "input": {"path1": "in_R1.fastq.gz", "path2": "in_R2.fastq.gz", "paired": True},
    "read_counts": {
        "input": 100000,
        "filtered": {"too_short": 251},
        "output": 97688,
        "read1_with_adapter": 2254,
        "read2_with_adapter": 2201,
    },
    "basepair_counts": {
        "input": 10100000,
        "quality_trimmed": 842048,
        "output": 9037053,
    },
}


class TestParseReport:
    def test_extracts_scalar_summary(self, tmp_path):
        path = tmp_path / "cutadapt.json"
        path.write_text(json.dumps(SAMPLE_REPORT))

        report = cutadapt_runner.parse_report(path)

        assert report["tool"] == "cutadapt"
        assert report["tool_version"] == "4.9"
        assert report["before"]["total_reads"] == 100000
        assert report["before"]["total_bases"] == 10100000
        assert report["after"]["total_reads"] == 97688
        assert report["after"]["total_bases"] == 9037053
        assert report["filtering"]["too_short_reads"] == 251
        assert report["adapters"]["trimmed_reads_r1"] == 2254
        assert report["adapters"]["trimmed_reads_r2"] == 2201

    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert cutadapt_runner.parse_report(tmp_path / "nope.json") == {}

    def test_malformed_json_returns_empty_dict(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        assert cutadapt_runner.parse_report(path) == {}


class TestOutputName:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("sample_R1.fastq.gz", "sample_R1.trimmed.fastq.gz"),
            ("sample.fq", "sample.trimmed.fq"),
            ("sample_R2.fastq", "sample_R2.trimmed.fastq"),
        ],
    )
    def test_preserves_suffix_and_mate_token(self, source, expected):
        assert cutadapt_runner.output_name(source) == expected
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd backend && python -m pytest tests/pipelines/test_cutadapt_runner.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.pipelines.cutadapt_runner'`

- [ ] **Step 3: Write the runner**

```python
"""Building and observing a cutadapt run.

Kept separate from the job handler so the parts worth testing -- command
construction, report extraction -- are pure functions over strings and dicts,
with no queue or filesystem involved. Mirrors fastp_runner.py's shape.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)


@dataclass
class CutadaptParams:
    """User-facing knobs. min_length defaults to 1, not 0: cutadapt keeps
    zero-length reads unless told otherwise, and downstream tools choke on
    them the same way an unset fastp --length_required would not, since
    fastp's own default there is already nonzero."""

    quality_cutoff: int = 20
    min_length: int = 1
    adapter_r1: str | None = None
    adapter_r2: str | None = None
    threads: int = 4

    def as_dict(self) -> dict:
        return {
            "quality_cutoff": self.quality_cutoff,
            "min_length": self.min_length,
            "adapter_r1": self.adapter_r1,
            "adapter_r2": self.adapter_r2,
            "threads": self.threads,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "CutadaptParams":
        raw = raw or {}
        known = {f for f in cls().as_dict()}
        return cls(**{k: v for k, v in raw.items() if k in known and v is not None})


def build_command(
    *,
    cutadapt_path: str,
    r1_in: Path,
    r1_out: Path,
    json_out: Path,
    params: CutadaptParams,
    r2_in: Path | None = None,
    r2_out: Path | None = None,
) -> list[str]:
    """Assemble the cutadapt invocation.

    Unlike fastp, cutadapt has no --verbose progress stream and no adapter
    auto-detection -- an adapter must be given explicitly or none is searched
    for, which is a real behavioral difference from fastp's
    detect_adapter_for_pe default, not an oversight here.
    """
    paired = r2_in is not None
    if paired and r2_out is None:
        raise ValueError("paired input requires a second output path")

    cmd = [
        cutadapt_path,
        f"--json={json_out}",
        "-j",
        str(params.threads),
        "-q",
        str(params.quality_cutoff),
        "-m",
        str(params.min_length),
    ]

    if params.adapter_r1:
        cmd += ["-a", params.adapter_r1]
    if paired and params.adapter_r2:
        cmd += ["-A", params.adapter_r2]

    cmd += ["-o", str(r1_out)]
    if paired:
        cmd += ["-p", str(r2_out)]

    cmd.append(str(r1_in))
    if paired:
        cmd.append(str(r2_in))

    return cmd


def parse_report(path: Path) -> dict:
    """Extract the before/after comparison from cutadapt's JSON.

    Only the scalar summary is kept, matching fastp_runner.parse_report --
    the full report also carries per-adapter trimmed-length histograms that
    belong in a future HTML/detail view, not in every object's facts.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        log.warning("cutadapt_report_unreadable", path=str(path), error=str(e))
        return {}

    reads = raw.get("read_counts", {})
    bases = raw.get("basepair_counts", {})
    filtered = reads.get("filtered", {})

    report = {
        "tool": "cutadapt",
        "tool_version": raw.get("cutadapt_version"),
        "before": {
            "total_reads": reads.get("input"),
            "total_bases": bases.get("input"),
        },
        "after": {
            "total_reads": reads.get("output"),
            "total_bases": bases.get("output"),
        },
        "filtering": {
            "too_short_reads": filtered.get("too_short"),
            "too_long_reads": filtered.get("too_long"),
        },
    }

    r1_adapter = reads.get("read1_with_adapter")
    r2_adapter = reads.get("read2_with_adapter")
    if r1_adapter is not None or r2_adapter is not None:
        report["adapters"] = {
            "trimmed_reads_r1": r1_adapter,
            "trimmed_reads_r2": r2_adapter,
        }

    return report


def output_name(source_name: str) -> str:
    """Name for a trimmed file, derived from its source. Identical rule to
    fastp_runner.output_name -- both tools produce the same kind of output,
    so the naming convention that keeps mate detection working is shared."""
    name = source_name
    suffixes = ""
    for ext in (".gz", ".bz2", ".zst"):
        if name.lower().endswith(ext):
            suffixes = name[-len(ext) :] + suffixes
            name = name[: -len(ext)]
            break
    for ext in (".fastq", ".fq"):
        if name.lower().endswith(ext):
            suffixes = name[-len(ext) :] + suffixes
            name = name[: -len(ext)]
            break

    if not suffixes:
        suffixes = ".fastq.gz"
    return f"{name}.trimmed{suffixes}"
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd backend && python -m pytest tests/pipelines/test_cutadapt_runner.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/cutadapt_runner.py backend/tests/pipelines/test_cutadapt_runner.py
git commit -m "feat: add cutadapt command builder and report parser"
```

---

## Task 4: Verify the cutadapt JSON schema against the real Docker binary

**Files:**
- None modified — verification only, folds into Task 3 if done first.

- [ ] **Step 1: Run cutadapt for real inside the built image and diff its JSON against `SAMPLE_REPORT`**

```bash
docker run --rm -v "$PWD/backend/tests/fixtures:/data" bio-pipeliner-backend \
  sh -c "cd /tmp && printf '@r1\nACGTACGTACGT\n+\nIIIIIIIIIIII\n' > in.fastq && \
         cutadapt --json=out.json -q 20 -m 1 -o out.fastq in.fastq && cat out.json"
```

Confirm `read_counts.input`, `read_counts.output`, `basepair_counts.input`,
`basepair_counts.output`, and `cutadapt_version` are present with the same
shape assumed in Task 3's `SAMPLE_REPORT`. If the installed cutadapt version
uses a different schema (`schema_version` other than `[0, 3]`), update
`parse_report` and the test fixture together before moving on — do not let
Task 3's tests pass against invented JSON that the real binary doesn't
produce.

No commit — this step only confirms Task 3's assumptions; if it uncovers a
mismatch, fix it as part of Task 3 and note the correction in that commit
instead of creating a new one here.

---

## Task 5: Trimmomatic runner — params, command builder, stdout summary parser

**Files:**
- Create: `backend/app/pipelines/trimmomatic_runner.py`
- Test: `backend/tests/pipelines/test_trimmomatic_runner.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Trimmomatic command construction and stdout summary parsing.

Trimmomatic has no structured report format -- no JSON, no fixed-layout
stats file -- so parse_summary reads the one completion line it prints to
stderr. The sample lines here are Trimmomatic's own documented wording;
confirm them against the Docker image's installed version before trusting
this in production (see plans/cutadapt-trimmomatic-runners.md Task 1).
"""

from pathlib import Path

import pytest

from app.pipelines import trimmomatic_runner
from app.pipelines.trimmomatic_runner import TrimmomaticParams


def cmd_for(**kw):
    defaults = dict(
        trimmomatic_pe_path="/usr/bin/TrimmomaticPE",
        trimmomatic_se_path="/usr/bin/TrimmomaticSE",
        adapters_dir="/usr/share/trimmomatic",
        r1_in=Path("in_R1.fastq.gz"),
        r1_out=Path("out_R1.fastq.gz"),
        params=TrimmomaticParams(),
    )
    defaults.update(kw)
    return trimmomatic_runner.build_command(**defaults)


class TestBuildCommand:
    def test_single_end_uses_the_se_binary(self):
        cmd = cmd_for()
        assert cmd[0] == "/usr/bin/TrimmomaticSE"
        assert "in_R1.fastq.gz" in cmd
        assert "out_R1.fastq.gz" in cmd

    def test_paired_end_uses_the_pe_binary_and_four_outputs(self):
        cmd = cmd_for(
            r2_in=Path("in_R2.fastq.gz"),
            r2_out=Path("out_R2.fastq.gz"),
            unpaired_r1_out=Path("unpaired_R1.fastq.gz"),
            unpaired_r2_out=Path("unpaired_R2.fastq.gz"),
        )
        assert cmd[0] == "/usr/bin/TrimmomaticPE"
        # PE takes two inputs then FOUR outputs: paired1, unpaired1, paired2, unpaired2.
        assert "in_R1.fastq.gz" in cmd
        assert "in_R2.fastq.gz" in cmd
        assert "out_R1.fastq.gz" in cmd
        assert "unpaired_R1.fastq.gz" in cmd
        assert "out_R2.fastq.gz" in cmd
        assert "unpaired_R2.fastq.gz" in cmd

    def test_paired_input_without_unpaired_outputs_is_rejected(self):
        with pytest.raises(ValueError, match="unpaired output"):
            cmd_for(r2_in=Path("in_R2.fastq.gz"), r2_out=Path("out_R2.fastq.gz"))

    def test_threads_flag_precedes_the_inputs(self):
        cmd = cmd_for(params=TrimmomaticParams(threads=6))
        assert "-threads" in cmd
        assert cmd[cmd.index("-threads") + 1] == "6"

    def test_illuminaclip_step_uses_the_configured_adapter_file(self):
        cmd = cmd_for(params=TrimmomaticParams(adapter_file="TruSeq3-SE.fa"))
        clip_steps = [a for a in cmd if a.startswith("ILLUMINACLIP:")]
        assert clip_steps == ["ILLUMINACLIP:/usr/share/trimmomatic/TruSeq3-SE.fa:2:30:10"]

    def test_no_adapter_file_omits_illuminaclip(self):
        cmd = cmd_for(params=TrimmomaticParams(adapter_file=None))
        assert not any(a.startswith("ILLUMINACLIP:") for a in cmd)

    def test_sliding_window_and_minlen_steps(self):
        cmd = cmd_for(
            params=TrimmomaticParams(
                sliding_window_size=4, sliding_window_quality=15, min_length=36
            )
        )
        assert "SLIDINGWINDOW:4:15" in cmd
        assert "MINLEN:36" in cmd

    def test_paired_end_picks_the_pe_adapter_file_by_default(self):
        """TruSeq3-PE.fa for paired input, TruSeq3-SE.fa for single-end --
        using the wrong one is a silent quality regression, not an error."""
        se_cmd = cmd_for()
        assert "ILLUMINACLIP:/usr/share/trimmomatic/TruSeq3-SE.fa:2:30:10" in se_cmd

        pe_cmd = cmd_for(
            r2_in=Path("in_R2.fastq.gz"),
            r2_out=Path("out_R2.fastq.gz"),
            unpaired_r1_out=Path("unpaired_R1.fastq.gz"),
            unpaired_r2_out=Path("unpaired_R2.fastq.gz"),
        )
        assert "ILLUMINACLIP:/usr/share/trimmomatic/TruSeq3-PE.fa:2:30:10" in pe_cmd


class TestTrimmomaticParams:
    def test_round_trip(self):
        p = TrimmomaticParams(min_length=50, threads=2)
        assert TrimmomaticParams.from_dict(p.as_dict()) == p

    def test_unknown_keys_are_ignored(self):
        assert TrimmomaticParams.from_dict({"bogus": 1}) == TrimmomaticParams()


# Wording per Trimmomatic's own documented output. Reconfirm against the
# Docker image's installed version -- see the module docstring.
PE_SUMMARY_LINE = (
    "Input Read Pairs: 100000 Both Surviving: 97688 (97.69%) Forward Only "
    "Surviving: 1200 (1.20%) Reverse Only Surviving: 800 (0.80%) Dropped: "
    "312 (0.31%)"
)
SE_SUMMARY_LINE = "Input Reads: 100000 Surviving: 97688 (97.69%) Dropped: 2312 (2.31%)"


class TestParseSummary:
    def test_paired_end_line(self):
        report = trimmomatic_runner.parse_summary(PE_SUMMARY_LINE, paired=True)
        assert report["tool"] == "trimmomatic"
        assert report["before"]["total_reads"] == 100000
        assert report["after"]["total_reads"] == 97688
        assert report["filtering"]["dropped_reads"] == 312

    def test_single_end_line(self):
        report = trimmomatic_runner.parse_summary(SE_SUMMARY_LINE, paired=False)
        assert report["before"]["total_reads"] == 100000
        assert report["after"]["total_reads"] == 97688
        assert report["filtering"]["dropped_reads"] == 2312

    def test_unmatched_text_returns_empty_dict(self):
        assert trimmomatic_runner.parse_summary("garbage output", paired=False) == {}


class TestOutputName:
    def test_preserves_suffix(self):
        assert (
            trimmomatic_runner.output_name("sample_R1.fastq.gz")
            == "sample_R1.trimmed.fastq.gz"
        )
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd backend && python -m pytest tests/pipelines/test_trimmomatic_runner.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.pipelines.trimmomatic_runner'`

- [ ] **Step 3: Write the runner**

```python
"""Building and observing a Trimmomatic run.

Trimmomatic ships as two separate binaries -- TrimmomaticPE and
TrimmomaticSE -- around one JAR, with no combined entry point and no
structured report; both differences from fastp/cutadapt are real, not
oversights, and drive the shape below.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)

# Trimmomatic prints exactly one of these to stderr on completion, depending
# on PE vs SE mode. Reconfirm this wording against the Docker image's
# installed version before relying on it -- see the module's test file.
_PE_SUMMARY_RE = re.compile(
    r"Input Read Pairs:\s*(\d+)\s+Both Surviving:\s*(\d+)\s*\([\d.]+%\)"
    r".*?Dropped:\s*(\d+)",
    re.IGNORECASE | re.DOTALL,
)
_SE_SUMMARY_RE = re.compile(
    r"Input Reads:\s*(\d+)\s+Surviving:\s*(\d+)\s*\([\d.]+%\)\s+Dropped:\s*(\d+)",
    re.IGNORECASE,
)


@dataclass
class TrimmomaticParams:
    """User-facing knobs. adapter_file names a FASTA under
    settings.trimmomatic_adapters_dir -- TruSeq3 is the modern HiSeq/MiSeq
    adapter set and Trimmomatic's own quick-start default."""

    quality_leading: int = 3
    quality_trailing: int = 3
    sliding_window_size: int = 4
    sliding_window_quality: int = 15
    min_length: int = 36  # Trimmomatic's own documented default
    adapter_file: str | None = "TruSeq3-SE.fa"
    threads: int = 4

    def as_dict(self) -> dict:
        return {
            "quality_leading": self.quality_leading,
            "quality_trailing": self.quality_trailing,
            "sliding_window_size": self.sliding_window_size,
            "sliding_window_quality": self.sliding_window_quality,
            "min_length": self.min_length,
            "adapter_file": self.adapter_file,
            "threads": self.threads,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "TrimmomaticParams":
        raw = raw or {}
        known = {f for f in cls().as_dict()}
        return cls(**{k: v for k, v in raw.items() if k in known and v is not None})


def build_command(
    *,
    trimmomatic_pe_path: str,
    trimmomatic_se_path: str,
    adapters_dir: str,
    r1_in: Path,
    r1_out: Path,
    params: TrimmomaticParams,
    r2_in: Path | None = None,
    r2_out: Path | None = None,
    unpaired_r1_out: Path | None = None,
    unpaired_r2_out: Path | None = None,
) -> list[str]:
    """Assemble the TrimmomaticPE/SE invocation.

    Picks the binary and the default adapter file by read layout: a PE
    adapter file used on single-end reads (or vice versa) matches nothing and
    silently does no clipping, which is worse than an error -- so the default
    tracks `paired` rather than being one fixed filename.
    """
    paired = r2_in is not None
    if paired and (unpaired_r1_out is None or unpaired_r2_out is None):
        raise ValueError("paired input requires both unpaired output paths")

    adapter_file = params.adapter_file
    if paired and adapter_file == "TruSeq3-SE.fa":
        adapter_file = "TruSeq3-PE.fa"

    if paired:
        cmd = [trimmomatic_pe_path, "-threads", str(params.threads)]
        cmd += [str(r1_in), str(r2_in)]
        cmd += [str(r1_out), str(unpaired_r1_out), str(r2_out), str(unpaired_r2_out)]
    else:
        cmd = [trimmomatic_se_path, "-threads", str(params.threads)]
        cmd += [str(r1_in), str(r1_out)]

    if adapter_file:
        cmd.append(
            f"ILLUMINACLIP:{adapters_dir.rstrip('/')}/{adapter_file}:2:30:10"
        )

    cmd += [
        f"LEADING:{params.quality_leading}",
        f"TRAILING:{params.quality_trailing}",
        f"SLIDINGWINDOW:{params.sliding_window_size}:{params.sliding_window_quality}",
        f"MINLEN:{params.min_length}",
    ]

    return cmd


def parse_summary(text: str, *, paired: bool) -> dict:
    """Extract read counts from Trimmomatic's one completion line.

    No JSON, no stats file -- this is the only structured-ish output
    Trimmomatic produces, so the report is built by regex over the process's
    captured stderr rather than by reading a file, unlike every other
    runner's parse_report/parse_summary.
    """
    pattern = _PE_SUMMARY_RE if paired else _SE_SUMMARY_RE
    match = pattern.search(text)
    if not match:
        log.warning("trimmomatic_summary_unparsed", paired=paired)
        return {}

    total_in, total_out, dropped = (int(g) for g in match.groups())
    return {
        "tool": "trimmomatic",
        "before": {"total_reads": total_in},
        "after": {"total_reads": total_out},
        "filtering": {"dropped_reads": dropped},
    }


def output_name(source_name: str) -> str:
    """Identical rule to fastp_runner.output_name and cutadapt_runner.output_name."""
    name = source_name
    suffixes = ""
    for ext in (".gz", ".bz2", ".zst"):
        if name.lower().endswith(ext):
            suffixes = name[-len(ext) :] + suffixes
            name = name[: -len(ext)]
            break
    for ext in (".fastq", ".fq"):
        if name.lower().endswith(ext):
            suffixes = name[-len(ext) :] + suffixes
            name = name[: -len(ext)]
            break

    if not suffixes:
        suffixes = ".fastq.gz"
    return f"{name}.trimmed{suffixes}"
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd backend && python -m pytest tests/pipelines/test_trimmomatic_runner.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/trimmomatic_runner.py backend/tests/pipelines/test_trimmomatic_runner.py
git commit -m "feat: add Trimmomatic command builder and stdout summary parser"
```

---

## Task 6: Flip `runnable` in `TOOL_META`

**Files:**
- Modify: `backend/app/pipelines/tools.py:273-307`
- Modify: `backend/tests/pipelines/test_tools.py`

- [ ] **Step 1: Update the failing test first**

In `backend/tests/pipelines/test_tools.py`, find
`test_cutadapt_and_trimmomatic_are_not_runnable_yet` (around line 256) and
replace it:

```python
class TestToolMeta:
    def test_cutadapt_and_trimmomatic_are_runnable(self):
        assert tools.TOOL_META["cutadapt"].runnable is True
        assert tools.TOOL_META["trimmomatic"].runnable is True
```

(Keep the class's other tests as they are — only this one method changes.)

- [ ] **Step 2: Run to verify it fails**

```bash
cd backend && python -m pytest tests/pipelines/test_tools.py::TestToolMeta -v
```

Expected: FAIL — `runnable is False`.

- [ ] **Step 3: Flip the flags**

In `backend/app/pipelines/tools.py`, change the `"cutadapt"` entry:

```python
    "cutadapt": ToolMeta(
        pipelines=(PipelineType.TRIM,),
        summary=(
            "Flexible adapter, primer, and barcode trimmer for all sequencing "
            "platforms. Supports anchored adapters, linked adapters, "
            "demultiplexing by barcode, and adapter patterns fastp cannot "
            "express."
        ),
        strengths=(
            "Demultiplexing: split reads by barcode/index",
            "Linked adapter trimming for paired-end reads",
            "Anchored 5'/3' adapter matching for amplicon-seq",
            "Poly-A tail trimming for RNA-seq",
            "Works on any platform (Illumina, PacBio, ONT)",
        ),
    ),
```

(Delete the `runnable=False` line and its comment — `runnable` defaults to
`True` on `ToolMeta`.)

And the `"trimmomatic"` entry:

```python
    "trimmomatic": ToolMeta(
        pipelines=(PipelineType.TRIM,),
        summary=(
            "Classic sliding-window quality trimmer for Illumina paired-end "
            "and single-end reads. The longest-established tool in the field "
            "and still widely cited."
        ),
        strengths=(
            "Sliding-window quality trimming: aggressive on trailing bases",
            "Gold standard for legacy Illumina pipeline comparisons",
            "Simple paired-end model: keeps R1/R2 in sync",
            "Plays well with Nextera/TruSeq adapter FASTA files",
        ),
    ),
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd backend && python -m pytest tests/pipelines/test_tools.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/tools.py backend/tests/pipelines/test_tools.py
git commit -m "feat: mark cutadapt and Trimmomatic as runnable now that handlers exist"
```

Note: don't commit this before Task 7 lands if you're executing tasks
strictly in order and running the full test suite between commits —
`PipelineToolSelector` will make these tools selectable in the UI as soon as
this merges, and until Task 7's dispatch exists, selecting them would 500 at
launch. If your workflow runs the whole suite (not just this file) before
each commit, this task is safe to land after Task 7 instead; reorder if
preferred, nothing here depends on Task 7's code existing yet.

---

## Task 7: `trim_reads` handler — dispatch on `tool`

**Files:**
- Modify: `backend/app/queue/pipeline_handlers.py`

- [ ] **Step 1: Understand the current handler shape**

Re-read `trim_reads` in `backend/app/queue/pipeline_handlers.py:42-161` (shown
in full during planning) before editing — this task restructures it into a
dispatcher plus three private per-tool functions, each returning the same
dict shape the caller (`results._apply_trim_reads`) already expects:
`object_id`, `mate_object_id`, `project_id`, `job_id`, `outputs`, `report`,
`params`, `tool`, `tool_version`, `html_path`, `workdir`.

- [ ] **Step 2: Replace `trim_reads` with a dispatcher and three tool functions**

Replace the entire body of `trim_reads` (lines 42-161) with:

```python
@handler(
    "trim_reads",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=4, mem_mb=2048, io=IoClass.HEAVY),
    max_attempts=2,
)
def trim_reads(ctx: JobContext) -> dict:
    """Adapter-trim and quality-filter a FASTQ file or an R1/R2 pair.

    Runs off the event loop in a worker thread, so it cannot touch the
    database: it resolves its inputs from the payload and returns a plain dict
    for `results._apply_trim_reads` to persist. See queue/results.py.

    Dispatches on the payload's `tool` (default "fastp") to one of three
    private functions below, each of which owns its own tool's command
    construction, progress reporting, and report parsing -- mirroring how
    run_qc dispatches on `platform`.

    Idempotent by construction. Delivery is at-least-once, and a drain during
    shutdown requeues a running job, so a second attempt must converge rather
    than collide with the first. Each attempt gets its own scratch directory,
    which is removed on entry -- a partial run leaves nothing behind that a
    retry could mistake for its own output.
    """
    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("trim_reads requires an 'object_id'")

    tool = (ctx.payload.get("tool") or "fastp").lower()
    dispatch = {
        "fastp": _run_fastp_trim,
        "cutadapt": _run_cutadapt_trim,
        "trimmomatic": _run_trimmomatic_trim,
    }
    run = dispatch.get(tool)
    if run is None:
        raise PermanentError(f"trim_reads has no code path for tool {tool!r}")

    return run(ctx, object_id)


def _resolve_trim_inputs(ctx: JobContext, work: Path) -> tuple[Path, Path | None, bool]:
    """Resolve and name-link R1 (and R2, if present) for any trim tool.

    Shared across all three tool paths: every one of them needs its input
    symlinked under its real filename for the same reason fastp does --
    gzip-vs-text sniffing from a managed blob's extensionless hash name.
    """
    r1_in = _resolve_input(ctx.payload, "r1")
    r2_in = _resolve_input(ctx.payload, "r2") if ctx.payload.get("r2_sha256") else None
    paired = r2_in is not None

    r1_in = _named_link(work, r1_in, ctx.payload.get("r1_name"))
    if paired:
        r2_in = _named_link(work, r2_in, ctx.payload.get("r2_name"))
    return r1_in, r2_in, paired


def _run_fastp_trim(ctx: JobContext, object_id: str) -> dict:
    """fastp trim -- the original trim_reads body, unchanged in behavior."""
    fastp = tools.require(tools.fastp())

    work = _prepare_workdir(ctx)
    r1_in, r2_in, paired = _resolve_trim_inputs(ctx, work)

    out_dir = work / "out"
    out_dir.mkdir(exist_ok=True)

    r1_name = fastp_runner.output_name(ctx.payload.get("r1_name") or r1_in.name)
    r1_out = out_dir / r1_name
    r2_out = None
    r2_name = None
    if paired:
        r2_name = fastp_runner.output_name(ctx.payload.get("r2_name") or r2_in.name)
        r2_out = out_dir / r2_name

    json_out = work / "fastp.json"
    html_out = work / "fastp.html"
    params = fastp_runner.TrimParams.from_dict(ctx.payload.get("params"))

    cmd = fastp_runner.build_command(
        fastp_path=fastp.path,
        r1_in=r1_in,
        r1_out=r1_out,
        r2_in=r2_in,
        r2_out=r2_out,
        json_out=json_out,
        html_out=html_out,
        params=params,
    )

    progress = fastp_runner.TrimProgress(expected_reads=ctx.payload.get("expected_reads"))
    ctx.progress(phase="starting", pct=0.0, message="starting fastp")

    def on_line(line: str) -> None:
        if progress.feed(line):
            ctx.progress(pct=progress.pct, phase=progress.phase, message=progress.message())

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log.info("trim_started", job_id=ctx.job_id, tool="fastp", paired=paired, cmd=" ".join(cmd))

    code = run_subprocess(ctx, cmd, log_path=str(log_path), on_line=on_line)
    if code != 0:
        raise _failure(code, log_path, tool="fastp")

    for produced in filter(None, (r1_out, r2_out)):
        if not produced.exists() or produced.stat().st_size == 0:
            raise RetryableError(f"fastp exited 0 but produced no output at {produced.name}")

    ctx.progress(phase="reporting", pct=fastp_runner.MAX_MEASURED_PCT, message="reading report")
    report = fastp_runner.parse_report(json_out)

    outputs = [{"tmp_path": str(r1_out), "name": r1_name, "mate": "R1" if paired else None}]
    if paired:
        outputs.append({"tmp_path": str(r2_out), "name": r2_name, "mate": "R2"})

    ctx.progress(phase="done", pct=1.0, message="trimming complete")
    log.info("trim_finished", job_id=ctx.job_id, tool="fastp", outputs=len(outputs))

    return {
        "object_id": object_id,
        "mate_object_id": ctx.payload.get("mate_object_id"),
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "outputs": outputs,
        "report": report,
        "params": params.as_dict(),
        "tool": "fastp",
        "tool_version": fastp.version,
        "html_path": str(html_out) if html_out.exists() else None,
        "workdir": str(work),
    }


def _run_cutadapt_trim(ctx: JobContext, object_id: str) -> dict:
    """cutadapt trim.

    No --verbose progress stream exists for cutadapt, so ctx.progress only
    reports "starting" and "done" -- the same as run_qc's NanoPlot path,
    which has the same limitation for the same reason (no line-oriented
    progress output to parse).
    """
    tool = tools.require(tools.cutadapt())

    work = _prepare_workdir(ctx)
    r1_in, r2_in, paired = _resolve_trim_inputs(ctx, work)

    out_dir = work / "out"
    out_dir.mkdir(exist_ok=True)

    r1_name = cutadapt_runner.output_name(ctx.payload.get("r1_name") or r1_in.name)
    r1_out = out_dir / r1_name
    r2_out = None
    r2_name = None
    if paired:
        r2_name = cutadapt_runner.output_name(ctx.payload.get("r2_name") or r2_in.name)
        r2_out = out_dir / r2_name

    json_out = work / "cutadapt.json"
    params = cutadapt_runner.CutadaptParams.from_dict(ctx.payload.get("params"))

    cmd = cutadapt_runner.build_command(
        cutadapt_path=tool.path,
        r1_in=r1_in,
        r1_out=r1_out,
        r2_in=r2_in,
        r2_out=r2_out,
        json_out=json_out,
        params=params,
    )

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(phase="starting", pct=0.0, message="starting cutadapt")
    log.info("trim_started", job_id=ctx.job_id, tool="cutadapt", paired=paired, cmd=" ".join(cmd))

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, tool="cutadapt")

    for produced in filter(None, (r1_out, r2_out)):
        if not produced.exists() or produced.stat().st_size == 0:
            raise RetryableError(f"cutadapt exited 0 but produced no output at {produced.name}")

    ctx.progress(phase="reporting", pct=0.95, message="reading report")
    report = cutadapt_runner.parse_report(json_out)

    outputs = [{"tmp_path": str(r1_out), "name": r1_name, "mate": "R1" if paired else None}]
    if paired:
        outputs.append({"tmp_path": str(r2_out), "name": r2_name, "mate": "R2"})

    ctx.progress(phase="done", pct=1.0, message="trimming complete")
    log.info("trim_finished", job_id=ctx.job_id, tool="cutadapt", outputs=len(outputs))

    return {
        "object_id": object_id,
        "mate_object_id": ctx.payload.get("mate_object_id"),
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "outputs": outputs,
        "report": report,
        "params": params.as_dict(),
        "tool": "cutadapt",
        "tool_version": tool.version,
        "html_path": None,
        "workdir": str(work),
    }


def _run_trimmomatic_trim(ctx: JobContext, object_id: str) -> dict:
    """Trimmomatic trim.

    No JSON, no progress stream: the report comes from regexing the one
    completion line Trimmomatic writes to stderr on exit, captured into the
    same log file run_subprocess already writes -- see
    trimmomatic_runner.parse_summary.
    """
    tool = tools.require(tools.trimmomatic())

    work = _prepare_workdir(ctx)
    r1_in, r2_in, paired = _resolve_trim_inputs(ctx, work)

    out_dir = work / "out"
    out_dir.mkdir(exist_ok=True)

    r1_name = trimmomatic_runner.output_name(ctx.payload.get("r1_name") or r1_in.name)
    r1_out = out_dir / r1_name
    r2_out = None
    r2_name = None
    unpaired_r1_out = None
    unpaired_r2_out = None
    if paired:
        r2_name = trimmomatic_runner.output_name(ctx.payload.get("r2_name") or r2_in.name)
        r2_out = out_dir / r2_name
        unpaired_r1_out = out_dir / f"unpaired.{r1_name}"
        unpaired_r2_out = out_dir / f"unpaired.{r2_name}"

    params = trimmomatic_runner.TrimmomaticParams.from_dict(ctx.payload.get("params"))

    cmd = trimmomatic_runner.build_command(
        trimmomatic_pe_path=settings.trimmomatic_pe_path,
        trimmomatic_se_path=settings.trimmomatic_se_path,
        adapters_dir=settings.trimmomatic_adapters_dir,
        r1_in=r1_in,
        r1_out=r1_out,
        r2_in=r2_in,
        r2_out=r2_out,
        unpaired_r1_out=unpaired_r1_out,
        unpaired_r2_out=unpaired_r2_out,
        params=params,
    )

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(phase="starting", pct=0.0, message="starting Trimmomatic")
    log.info("trim_started", job_id=ctx.job_id, tool="trimmomatic", paired=paired, cmd=" ".join(cmd))

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, tool="trimmomatic")

    for produced in filter(None, (r1_out, r2_out)):
        if not produced.exists() or produced.stat().st_size == 0:
            raise RetryableError(f"Trimmomatic exited 0 but produced no output at {produced.name}")

    ctx.progress(phase="reporting", pct=0.95, message="reading summary")
    log_text = _log_tail(log_path, lines=20, max_chars=4000)
    report = trimmomatic_runner.parse_summary(log_text, paired=paired)

    outputs = [{"tmp_path": str(r1_out), "name": r1_name, "mate": "R1" if paired else None}]
    if paired:
        outputs.append({"tmp_path": str(r2_out), "name": r2_name, "mate": "R2"})

    ctx.progress(phase="done", pct=1.0, message="trimming complete")
    log.info("trim_finished", job_id=ctx.job_id, tool="trimmomatic", outputs=len(outputs))

    return {
        "object_id": object_id,
        "mate_object_id": ctx.payload.get("mate_object_id"),
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "outputs": outputs,
        "report": report,
        "params": params.as_dict(),
        "tool": "trimmomatic",
        "tool_version": tool.version,
        "html_path": None,
        "workdir": str(work),
    }
```

- [ ] **Step 3: Update imports**

At the top of `backend/app/queue/pipeline_handlers.py`, change:

```python
from app.pipelines import fastp_runner, tools
```

to:

```python
from app.pipelines import cutadapt_runner, fastp_runner, tools, trimmomatic_runner
```

- [ ] **Step 4: Note on `_log_tail`'s existing signature**

`_log_tail(path, *, lines=5, max_chars=600)` already exists
(`pipeline_handlers.py:540-546`) and is reused verbatim above with wider
`lines`/`max_chars` — Trimmomatic's completion line can be preceded by
per-step progress text, so a 600-char/5-line tail risks truncating the
summary line itself. No change needed to `_log_tail`'s definition, only to
how `_run_trimmomatic_trim` calls it.

- [ ] **Step 5: Run the pipeline handler tests plus the full pipelines test directory**

```bash
cd backend && python -m pytest tests/pipelines/ tests/queue/ -v
```

Expected: all PASS. (There is no dedicated
`tests/queue/test_pipeline_handlers.py` in this codebase — see the note under
Task 10 — so this mainly re-runs the runner unit tests and confirms nothing
else broke on import.)

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/pipeline_handlers.py
git commit -m "feat: dispatch trim_reads across fastp, cutadapt, and Trimmomatic"
```

---

## Task 8: `PipelineRun.tool` field

**Files:**
- Modify: `backend/app/models/run.py`
- Modify: `backend/app/services/pipeline_service.py` (`launch_trim`)

- [ ] **Step 1: Read the current `PipelineRun` model**

Find the `PipelineRun` class in `backend/app/models/run.py` (it has `params:
dict` per the exploration notes; read the surrounding fields — `kind`,
`project_id`, `label`, `inputs`, `params`, timestamps — before adding to it).

- [ ] **Step 2: Add the field**

Add a `tool: str | None = None` field to `PipelineRun`, placed beside `params`
(alignment runs leave it `None` — only trim runs populate it, since alignment
already names its tool via the `aligner` key inside `params`).

```python
    # Which tool actually ran this trim. None for non-trim runs (alignment
    # already names its tool via `params["aligner"]`, so this would be
    # redundant there rather than merely unset).
    tool: str | None = None
```

- [ ] **Step 3: Thread it through `launch_trim`**

In `backend/app/services/pipeline_service.py`, `launch_trim` currently calls
`run_service.create_run(kind=RunKind.TRIM, ..., params=payload["params"])`
around line 206-212. Add `tool=tool` (the new parameter from Task 9) to that
call:

```python
    run = await run_service.create_run(
        kind=RunKind.TRIM,
        project_id=obj.project_id,
        label=_trim_label(obj, mate),
        inputs=_trim_inputs(obj, mate),
        params=payload["params"],
        tool=tool,
    )
```

(This edit depends on `launch_trim` already having a `tool` parameter — do
this step together with Task 9, not before it. If your workflow strictly
serializes tasks, do Task 9 first and come back to finish this line.)

- [ ] **Step 4: Check `run_service.create_run`'s signature**

`run_service.create_run` is called with keyword arguments throughout the
codebase (`kind=`, `project_id=`, `label=`, `inputs=`, `params=`). Find its
definition (likely `backend/app/services/run_service.py`) and add a `tool:
str | None = None` parameter that passes straight through to
`PipelineRun(..., tool=tool)`. Follow the exact pattern the existing
`params` parameter uses there.

- [ ] **Step 5: Run the run-service and pipeline-service tests**

```bash
cd backend && python -m pytest tests/services/ tests/pipelines/test_launch_rules.py -v
```

Expected: all PASS (no test currently asserts on `PipelineRun.tool`, so
nothing should break; Task 11 adds coverage for the new field).

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/run.py backend/app/services/pipeline_service.py backend/app/services/run_service.py
git commit -m "feat: record which tool a trim run used on PipelineRun"
```

---

## Task 9: `launch_trim` and `default_params` take a `tool` parameter

**Files:**
- Modify: `backend/app/services/pipeline_service.py`
- Test: `backend/tests/pipelines/test_launch_rules.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/pipelines/test_launch_rules.py` (mirror the existing
`TestDefaults` class's style — check the file's imports and fixtures first,
since it uses a `FakeObject` stand-in with no real database):

```python
class TestToolAwareDefaults:
    def test_fastp_is_the_default_tool(self):
        assert pipeline_service.default_params() == pipeline_service.default_params("fastp")

    def test_cutadapt_defaults_have_cutadapt_shaped_keys(self):
        params = pipeline_service.default_params("cutadapt")
        assert "quality_cutoff" in params
        assert "unqualified_percent_limit" not in params  # fastp-only key

    def test_trimmomatic_defaults_have_trimmomatic_shaped_keys(self):
        params = pipeline_service.default_params("trimmomatic")
        assert "sliding_window_size" in params
        assert "quality_cutoff" not in params  # cutadapt-only key

    def test_unknown_tool_raises(self):
        with pytest.raises(ValidationError, match="Unknown trim tool"):
            pipeline_service.default_params("not-a-real-tool")
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd backend && python -m pytest tests/pipelines/test_launch_rules.py::TestToolAwareDefaults -v
```

Expected: FAIL — `default_params()` takes no arguments yet.

- [ ] **Step 3: Update `default_params` and add `_check_tool_runnable`**

In `backend/app/services/pipeline_service.py`, replace the existing
`default_params` (lines 62-65) with:

```python
_TRIM_PARAM_TYPES = {
    "fastp": fastp_runner.TrimParams,
    "cutadapt": cutadapt_runner.CutadaptParams,
    "trimmomatic": trimmomatic_runner.TrimmomaticParams,
}


def default_params(tool: str = "fastp") -> dict:
    """Server-owned defaults for the named trim tool, so the form does not
    encode its own copy. Raises for any tool this application has no runner
    for -- the same "does this application call it" question TOOL_META's
    `runnable` flag answers, checked here rather than trusted from the
    caller."""
    params_cls = _TRIM_PARAM_TYPES.get(tool)
    if params_cls is None:
        raise ValidationError(f"Unknown trim tool: {tool!r}")
    if params_cls is fastp_runner.TrimParams:
        return params_cls(threads=settings.pipeline_default_threads).as_dict()
    return params_cls(threads=settings.pipeline_default_threads).as_dict()


def _check_tool_runnable(tool: str) -> None:
    """Assert a trim tool both exists and has an actual code path.

    Distinct from `tools.require`, which only checks the binary is usable --
    an unrecognized tool name would pass that check trivially (it is simply
    absent from `all_tools()`) and reach the queue with no handler branch to
    run it.
    """
    if tool not in _TRIM_PARAM_TYPES:
        raise ValidationError(f"Unknown trim tool: {tool!r}")


def _trim_tool(tool: str):
    return {
        "fastp": tools.fastp,
        "cutadapt": tools.cutadapt,
        "trimmomatic": tools.trimmomatic,
    }[tool]()
```

- [ ] **Step 4: Add the import**

At the top of `pipeline_service.py`, change:

```python
from app.pipelines import align_runner, aligners, fastp_runner, pairing, tools
```

to:

```python
from app.pipelines import (
    align_runner,
    aligners,
    cutadapt_runner,
    fastp_runner,
    pairing,
    tools,
    trimmomatic_runner,
)
```

- [ ] **Step 5: Thread `tool` through `launch_trim`**

Change the signature (currently lines 106-112):

```python
async def launch_trim(
    *,
    object_id: PydanticObjectId,
    mate_object_id: PydanticObjectId | None = None,
    params: dict | None = None,
    paired: bool = True,
    tool: str = "fastp",
):
```

Replace the hardcoded tool check near the top of the function body:

```python
    tools.require(tools.fastp())
```

with:

```python
    _check_tool_runnable(tool)
    tools.require(_trim_tool(tool))
```

Replace the params-building block (currently around lines 148-156):

```python
    r1_digest, r1_path = await _resolve_readable(obj)
    params_cls = _TRIM_PARAM_TYPES[tool]
    merged_params = params_cls.from_dict(
        {"threads": settings.pipeline_default_threads, **(params or {})}
    ).as_dict()
    payload: dict = {
        "object_id": str(obj.id),
        "project_id": str(obj.project_id),
        "r1_name": obj.name,
        "tool": tool,
        "params": merged_params,
    }
```

The rest of `launch_trim` (mate resolution, dedup key, `queue.enqueue`,
`run_service.create_run`) is unchanged except:

- The dedup key already includes `_params_fingerprint(payload["params"])`,
  which now varies by tool automatically since each tool's params dict has
  different keys — no change needed to the dedup key format itself, but
  confirm two trims of the same object with different `tool` values produce
  different dedup keys (they will, since the fingerprinted dict differs).
- `queue.enqueue("trim_reads", payload=payload, ...)` is unchanged — `payload`
  now simply carries a `"tool"` key that `trim_reads` (Task 7) reads.
- The `run_service.create_run(...)` call gets `tool=tool` per Task 8 Step 3.

- [ ] **Step 6: Run to verify it passes**

```bash
cd backend && python -m pytest tests/pipelines/test_launch_rules.py -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/pipelines/test_launch_rules.py
git commit -m "feat: launch_trim and default_params take a tool parameter"
```

---

## Task 10: API — `TrimRequest.tool` and tool-aware `GET /defaults`

**Files:**
- Modify: `backend/app/api/v1/pipelines.py`
- Test: create `backend/tests/api/test_pipelines_trim_tool.py`

- [ ] **Step 1: Write the failing tests**

Check `backend/tests/api/` for the existing API test conventions (client
fixture name, how a `DataObject` fixture is built) before writing this —
mirror whatever pattern the existing trim/QC API tests use. If no directory
exists yet at `backend/tests/api/`, check `backend/tests/` for wherever the
FastAPI test client fixture is defined and place this file alongside the
other pipeline API tests instead.

```python
"""API surface for tool-aware trim requests."""

import pytest


class TestTrimDefaultsTool:
    async def test_defaults_for_fastp_is_the_bare_endpoint(self, client):
        resp = await client.get("/api/v1/pipelines/defaults")
        assert resp.status_code == 200
        assert "quality_threshold" in resp.json()["params"]

    async def test_defaults_for_cutadapt(self, client):
        resp = await client.get("/api/v1/pipelines/defaults?tool=cutadapt")
        assert resp.status_code == 200
        assert "quality_cutoff" in resp.json()["params"]

    async def test_defaults_for_unknown_tool_is_a_client_error(self, client):
        resp = await client.get("/api/v1/pipelines/defaults?tool=not-a-tool")
        assert resp.status_code == 422 or resp.status_code == 400
```

Adjust the route prefix and fixture name (`client`) to match whatever the
existing test files in this directory actually use — this plan cannot see
the fixture definition, so confirm against a neighboring test file
(`test_pipelines.py` or similar) before running this.

- [ ] **Step 2: Run to verify it fails**

```bash
cd backend && python -m pytest tests/api/test_pipelines_trim_tool.py -v
```

Expected: FAIL — `?tool=` is accepted but ignored (200 for all three, since
`trim_defaults` doesn't look at query params yet).

- [ ] **Step 3: Update the endpoints**

In `backend/app/api/v1/pipelines.py`, add `tool` to `TrimRequest`:

```python
class TrimRequest(BaseModel):
    object_id: PydanticObjectId
    mate_object_id: PydanticObjectId | None = None
    paired: bool = True
    params: dict = Field(default_factory=dict)
    tool: str = "fastp"
```

Update `launch_trim`:

```python
@router.post("/trim", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_trim(body: TrimRequest) -> JobOut:
    """Queue an adapter-trimming run over a FASTQ file or an R1/R2 pair."""
    job = await pipeline_service.launch_trim(
        object_id=body.object_id,
        mate_object_id=body.mate_object_id,
        params=body.params,
        paired=body.paired,
        tool=body.tool,
    )
    return JobOut.of(job)
```

Update `trim_defaults`:

```python
@router.get("/defaults")
async def trim_defaults(tool: str = "fastp") -> dict:
    """Default trim parameters for the given tool, owned by the server so the
    form does not encode its own copy."""
    return {
        "params": pipeline_service.default_params(tool),
        "max_threads": settings.pipeline_default_threads,
    }
```

`pipeline_service.default_params` already raises `ValidationError` for an
unrecognized tool (Task 9); confirm the app's existing exception handler maps
`ValidationError` to a 4xx response the same way it does for every other
`ValidationError` raise in this router — check `app/errors.py` or the
exception-handler registration if unsure, rather than assuming.

- [ ] **Step 4: Run to verify it passes**

```bash
cd backend && python -m pytest tests/api/test_pipelines_trim_tool.py -v
```

Expected: all PASS.

- [ ] **Step 5: Run the full backend test suite**

```bash
cd backend && python -m pytest -v
```

Expected: all PASS. This is the first point in the plan where every backend
piece (runners, dispatch, service, API) is wired together — a good place to
catch any signature mismatch between tasks before moving to the frontend.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/pipelines.py backend/tests/api/test_pipelines_trim_tool.py
git commit -m "feat: accept a tool on POST /pipelines/trim and GET /pipelines/defaults"
```

---

## Task 11: Frontend types — tool-shaped params

**Files:**
- Modify: `frontend/src/api/types.ts`

- [ ] **Step 1: Add the two new params interfaces and update existing ones**

In `frontend/src/api/types.ts`, after the existing `TrimParams` interface
(around line 362), add:

```typescript
/** Mirrors cutadapt_runner.CutadaptParams. */
export interface CutadaptParams {
  quality_cutoff: number;
  min_length: number;
  adapter_r1: string | null;
  adapter_r2: string | null;
  threads: number;
}

/** Mirrors trimmomatic_runner.TrimmomaticParams. */
export interface TrimmomaticParams {
  quality_leading: number;
  quality_trailing: number;
  sliding_window_size: number;
  sliding_window_quality: number;
  min_length: number;
  adapter_file: string | null;
  threads: number;
}

export type TrimToolParams = TrimParams | CutadaptParams | TrimmomaticParams;
```

Update `TrimDefaults` (currently line 364-367):

```typescript
export interface TrimDefaults {
  params: TrimToolParams;
  max_threads: number;
}
```

Update `TrimRequest` (currently line 613-618):

```typescript
export interface TrimRequest {
  object_id: string;
  mate_object_id?: string | null;
  paired?: boolean;
  params?: Partial<TrimToolParams>;
  tool?: string;
}
```

`TrimReport.tool` (line 500) is already a bare `string` and needs no change —
it already accepts `"cutadapt"`/`"trimmomatic"` as valid values, same as it
does `"fastp"` today.

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```

Expected: errors in `TrimDialog.tsx` (Task 13 fixes these) — everything
else should be clean. If other files error, something referenced
`TrimDefaults.params` or `TrimRequest.params` assuming the old bare
`TrimParams` shape; fix those call sites' type annotations too before moving
on.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/types.ts
git commit -m "feat: add CutadaptParams and TrimmomaticParams frontend types"
```

---

## Task 12: Frontend client — `trimDefaults(tool)`

**Files:**
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Update `trimDefaults`**

Find the existing definition (per the exploration notes, it's a one-liner:
`trimDefaults: () => request<TrimDefaults>("/pipelines/defaults")`) and
change it to take a tool argument:

```typescript
  trimDefaults: (tool: string = "fastp") =>
    request<TrimDefaults>(`/pipelines/defaults?tool=${encodeURIComponent(tool)}`),
```

`launchTrim` needs no change — it already forwards the full `TrimRequest`
body as JSON, and `TrimRequest.tool` (Task 11) rides along automatically once
`TrimDialog.tsx` (Task 13) starts setting it.

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/client.ts
git commit -m "feat: trimDefaults takes a tool argument"
```

---

## Task 13: `TrimDialog.tsx` — per-tool field sets

**Files:**
- Modify: `frontend/src/components/TrimDialog.tsx`

- [ ] **Step 1: Remove the "only fastp can be launched today" gate**

Delete the `selectedTool`-doc-comment block (lines 22-28) and replace it with
a plain doc comment; delete the warning banner block (lines 100-105) entirely.

- [ ] **Step 2: Compute the active tool and query its defaults**

Replace:

```typescript
  const { data: defaults } = useQuery({
    queryKey: ["pipelines", "defaults"],
    queryFn: api.trimDefaults,
    staleTime: 60_000,
  });
```

with:

```typescript
  const activeTool = selectedTool ?? "fastp";

  const { data: defaults } = useQuery({
    queryKey: ["pipelines", "defaults", activeTool],
    queryFn: () => api.trimDefaults(activeTool),
    staleTime: 60_000,
  });
```

- [ ] **Step 3: Replace the fastp-only tool lookup and launch call**

Replace:

```typescript
  const params = { ...defaults?.params, ...overrides } as TrimParams;
  const fastp = tools?.tools.find((t) => t.name === "fastp");
  const usePair = paired && mate != null;
```

with:

```typescript
  const params = { ...defaults?.params, ...overrides };
  const activeToolInfo = tools?.tools.find((t) => t.name === activeTool);
  const usePair = paired && mate != null;
```

Replace the `launchTrim` call's `params: overrides` with `params:
overrides, tool: activeTool`:

```typescript
  const launch = useMutation({
    mutationFn: () =>
      api.launchTrim({
        object_id: object.id,
        mate_object_id: usePair ? mate!.object_id : null,
        paired: usePair,
        params: overrides,
        tool: activeTool,
      }),
```

Replace every other `fastp?.available` / `fastp.error` reference (the
`ready` computation and the error box) with `activeToolInfo?.available` /
`activeToolInfo?.error`:

```typescript
  const ready = defaults != null && activeToolInfo?.available === true;
```

```tsx
        {activeToolInfo && !activeToolInfo.available && (
          <div className="error-box" style={{ marginBottom: 12 }}>
            {activeToolInfo.error ?? `${activeTool} is not available`}
          </div>
        )}
```

- [ ] **Step 4: Remove the now-unused `selectedTool !== "fastp"` subtitle check**

Replace the `<h2>` block's conditional subtitle:

```tsx
        <h2>
          Trim reads
          {activeTool !== "fastp" && (
            <span className="dialog-tool-subtitle"> — {activeTool}</span>
          )}
          {onBack && (
            <button type="button" className="dialog-tool-back" onClick={onBack}>
              change tool
            </button>
          )}
        </h2>
```

- [ ] **Step 5: Branch the parameter fields by tool**

The current `trim-fields` block (min length / quality threshold / threads)
and the `advanced` block (adapter / dedup / polyG) are fastp-specific. Split
rendering into a helper that switches on `activeTool`. Replace the two
existing `<div className="trim-fields">...</div>` blocks and the advanced
toggle with:

```tsx
        {activeTool === "fastp" && (
          <>
            <div className="trim-fields">
              <label>
                <span>Min length</span>
                <input
                  type="number"
                  min={1}
                  value={(params as TrimParams).min_length ?? 15}
                  onChange={(e) => setOverrides((o) => ({ ...o, min_length: Number(e.target.value) }))}
                />
                <small>Reads shorter than this after trimming are discarded.</small>
              </label>
              <label>
                <span>Quality threshold</span>
                <input
                  type="number"
                  min={0}
                  max={40}
                  value={(params as TrimParams).quality_threshold ?? 15}
                  onChange={(e) => setOverrides((o) => ({ ...o, quality_threshold: Number(e.target.value) }))}
                />
                <small>Phred score below which a base counts as unqualified.</small>
              </label>
              <label>
                <span>Threads</span>
                <input
                  type="number"
                  min={1}
                  max={16}
                  value={(params as TrimParams).threads ?? 4}
                  onChange={(e) => setOverrides((o) => ({ ...o, threads: Number(e.target.value) }))}
                />
                <small>More threads finish sooner but compete with other work.</small>
              </label>
            </div>

            <button
              type="button"
              className="trim-advanced-toggle"
              onClick={() => setAdvanced((a) => !a)}
              aria-expanded={advanced}
            >
              <span className="trim-chevron">{advanced ? "▾" : "▸"}</span>
              Adapters and filtering
            </button>

            {advanced && (
              <div className="trim-fields">
                <label className="trim-wide">
                  <span>Adapter sequence (read 1)</span>
                  <input
                    type="text"
                    placeholder={usePair ? "auto-detected by overlap analysis" : "auto-detected"}
                    value={(params as TrimParams).adapter_r1 ?? ""}
                    onChange={(e) => setOverrides((o) => ({ ...o, adapter_r1: e.target.value || null }))}
                  />
                  <small>
                    Leave empty unless you know the sequence — for paired reads
                    fastp detects it from the overlap, which is more reliable.
                  </small>
                </label>
                <label className="trim-check trim-wide">
                  <input
                    type="checkbox"
                    checked={(params as TrimParams).dedup ?? false}
                    onChange={(e) => setOverrides((o) => ({ ...o, dedup: e.target.checked }))}
                  />
                  <span>Remove duplicate reads</span>
                </label>
                <label className="trim-check trim-wide">
                  <input
                    type="checkbox"
                    checked={(params as TrimParams).trim_poly_g === true}
                    onChange={(e) => setOverrides((o) => ({ ...o, trim_poly_g: e.target.checked ? true : null }))}
                  />
                  <span>
                    Force polyG trimming
                    <small style={{ display: "block" }}>
                      Off by default because fastp enables it automatically for
                      two-colour instruments.
                    </small>
                  </span>
                </label>
              </div>
            )}
          </>
        )}

        {activeTool === "cutadapt" && (
          <div className="trim-fields">
            <label>
              <span>Min length</span>
              <input
                type="number"
                min={1}
                value={(params as CutadaptParams).min_length ?? 1}
                onChange={(e) => setOverrides((o) => ({ ...o, min_length: Number(e.target.value) }))}
              />
              <small>Reads shorter than this after trimming are discarded.</small>
            </label>
            <label>
              <span>Quality cutoff</span>
              <input
                type="number"
                min={0}
                max={40}
                value={(params as CutadaptParams).quality_cutoff ?? 20}
                onChange={(e) => setOverrides((o) => ({ ...o, quality_cutoff: Number(e.target.value) }))}
              />
              <small>3' quality trimming threshold (cutadapt's -q).</small>
            </label>
            <label className="trim-wide">
              <span>Adapter sequence (read 1)</span>
              <input
                type="text"
                placeholder="required — cutadapt has no auto-detection"
                value={(params as CutadaptParams).adapter_r1 ?? ""}
                onChange={(e) => setOverrides((o) => ({ ...o, adapter_r1: e.target.value || null }))}
              />
              <small>
                Unlike fastp, cutadapt does not detect adapters automatically —
                leave empty only if you want quality trimming with no adapter
                search.
              </small>
            </label>
            <label>
              <span>Threads</span>
              <input
                type="number"
                min={1}
                max={16}
                value={(params as CutadaptParams).threads ?? 4}
                onChange={(e) => setOverrides((o) => ({ ...o, threads: Number(e.target.value) }))}
              />
            </label>
          </div>
        )}

        {activeTool === "trimmomatic" && (
          <div className="trim-fields">
            <label>
              <span>Min length</span>
              <input
                type="number"
                min={1}
                value={(params as TrimmomaticParams).min_length ?? 36}
                onChange={(e) => setOverrides((o) => ({ ...o, min_length: Number(e.target.value) }))}
              />
              <small>Reads shorter than this are dropped (MINLEN).</small>
            </label>
            <label>
              <span>Sliding window quality</span>
              <input
                type="number"
                min={0}
                max={40}
                value={(params as TrimmomaticParams).sliding_window_quality ?? 15}
                onChange={(e) => setOverrides((o) => ({ ...o, sliding_window_quality: Number(e.target.value) }))}
              />
              <small>Average quality required within the sliding window.</small>
            </label>
            <label>
              <span>Threads</span>
              <input
                type="number"
                min={1}
                max={16}
                value={(params as TrimmomaticParams).threads ?? 4}
                onChange={(e) => setOverrides((o) => ({ ...o, threads: Number(e.target.value) }))}
              />
            </label>
          </div>
        )}
```

Remove the now-orphaned `advanced` state's usage outside the fastp branch —
`const [advanced, setAdvanced] = useState(false);` stays (fastp still uses
it), no change needed to that line itself.

- [ ] **Step 6: Update imports**

```typescript
import type { CutadaptParams, DataObject, TrimmomaticParams, TrimParams } from "../api/types";
```

- [ ] **Step 7: Type-check**

```bash
cd frontend && npx tsc --noEmit
```

Expected: clean.

- [ ] **Step 8: Manual verification in the browser**

Start the dev server, open a project with a FASTQ file, click **Trim** →
select **cutadapt** in the tool selector → confirm the cutadapt-specific
fields render (min length, quality cutoff, adapter, threads — not fastp's
polyG/dedup checkboxes) → launch → confirm the job appears in Activity and
completes. Repeat for **trimmomatic**. Repeat once more for **fastp** to
confirm the original path still works unchanged.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/components/TrimDialog.tsx
git commit -m "feat: TrimDialog renders per-tool parameter fields for cutadapt and Trimmomatic"
```

---

## Task 14: Full-stack verification

**Files:** none — verification only.

- [ ] **Step 1: Run the full backend suite**

```bash
cd backend && python -m pytest -v
```

Expected: all PASS, including every new file from Tasks 3, 5, 6, 9, 10.

- [ ] **Step 2: Run backend linting**

```bash
cd backend && ruff check . && ruff format --check .
```

(Match whatever lint command this repo actually uses — check
`backend/pyproject.toml` or a `Makefile`/`justfile` if `ruff` isn't it.)

- [ ] **Step 3: Type-check the frontend**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 4: Build the Docker image and confirm the tool panel reports both tools as available and runnable**

```bash
docker build -t bio-pipeliner-backend -f backend/Dockerfile backend
```

Start the stack (however this project normally runs it — check for a
`docker-compose.yml` or dev script), open the app, go to the tool selection
screen for **Trim**, and confirm both cutadapt and Trimmomatic cards are now
selectable (not greyed out) with real version numbers shown.

- [ ] **Step 5: End-to-end trim through each tool against a real FASTQ**

Using a small real or synthetic FASTQ already in the project (or one from an
SRA download, if the SRA downloader plan's test accession is still handy),
run one trim through fastp, one through cutadapt, one through Trimmomatic.
For each, confirm:

- The job completes and the run shows `succeeded` in Activity.
- The trimmed output object appears with `facts.trimmed_by` matching the
  tool used (fastp / cutadapt / trimmomatic) — this is
  `results._apply_trim_reads`'s existing `provenance["trimmed_by"]` field,
  unchanged by this plan, now finally receiving a real value instead of
  always defaulting to `"fastp"`.
- The `PipelineRun`'s `tool` field (Task 8) matches, visible via whatever the
  activity view or a direct API call to the run shows.

No commit for this task — it's verification, not code. If any step fails,
fix the underlying task and re-run from Step 1.

---

## Self-review notes (for whoever executes this)

- **Spec coverage:** every gap named in `pipeline-tool-additions-qc.md`'s
  `runnable=False` comment and `tool-selector-implementation.md`'s "not yet
  supported" reason string is addressed: a real command builder, report
  parser, and handler dispatch for both tools (Tasks 3, 5, 7); the
  `TOOL_META.runnable` flip that makes them selectable (Task 6); and the
  params-shape problem in `TrimDialog.tsx` that the "only fastp can be
  launched today" banner was standing in for (Task 13).
- **What this plan deliberately does not build:** an HTML report for cutadapt
  or Trimmomatic (neither tool produces one the way fastp/FastQC do — Task
  7's handlers set `html_path: None` for both), and a Trimmomatic JSON/stats
  file (does not exist — Task 5's regex-over-stdout approach is the ceiling
  of what's available, not a shortcut taken under time pressure). If a
  methods-section-grade Trimmomatic report ever matters, the fix is a
  `-trimlog` file parse (documented format: read name, surviving length,
  first/last surviving base, amount trimmed from each end, per read) rather
  than a JSON report, since Trimmomatic has no JSON mode to add.
- **Risk flagged explicitly in the plan text:** Trimmomatic's stdout
  completion-line wording is not a versioned, stable contract the way
  fastp's JSON schema or cutadapt's `--json` schema are. Task 1 and Task 5's
  module docstring both call out that the regex must be checked against the
  actual Docker-image binary before this is trusted in production, not just
  against the upstream doc's quick-start example used to write the tests.
