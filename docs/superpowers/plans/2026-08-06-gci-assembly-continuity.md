# GCI Assembly Continuity Inspection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add long-read assembly continuity inspection (GCI) as an Actions-tab QC workflow that consumes BioFlow-produced long-read BAMs and records continuity facts and depth plots on the assembly.

**Architecture:** A single-tool slice modelled on CRAQ (#63), not on `assembly_qc_registry`. GCI is pure Python installed from a pinned commit, so there is no build. Pure functions in `gci_runner.py`, a SUBPROCESS handler, a launch path that resolves long-read BAMs by chemistry and *refuses* the ambiguous cases, an Actions card, and a facts block in the UI. GCI consumes finished alignments and never invokes an aligner.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor (MongoDB), pytest, React/TypeScript, Docker. GCI is Python over pysam, biopython, numpy, matplotlib; samtools and minimap2 are already in the image.

**Spec:** [`docs/superpowers/specs/2026-08-06-gci-assembly-continuity-design.md`](../specs/2026-08-06-gci-assembly-continuity-design.md)

**Issue:** [#65](https://github.com/syntheticgio/bioflow/issues/65)

---

## Before you start

Read these first — each encodes a trap this plan is shaped around:

- The spec above, especially "The gating question, answered" and "Chemistry routing is stricter than CRAQ's".
- `backend/app/pipelines/craq_runner.py` and `backend/app/queue/assembly_qc_handlers.py::assess_assembly_errors` — the shapes this plan copies, including BAM index resolution.
- `backend/app/services/pipeline_service.py::reference_for_bam` (line 1685) and `read_chemistry_for_alignment` (line 777) — both are reused, not reimplemented.
- `CLAUDE.md` on testing from a worktree and on checking suggestion rules against a real database.

**Run tests with `./backend/run-worktree-tests.sh`, never `docker compose exec api python -m pytest`.** From a worktree the latter silently tests `main`'s code.

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Record the baseline pass count before you start.

**`worker` does not hot-reload.** After changing any handler, `docker compose restart worker` from the main checkout, or the job runs old in-memory code.

**Three things in this plan are easy to get wrong, and none of them are the tool integration:**

1. **`CLR` must be refused, not routed.** PacBio CLR is long-read and therefore looks eligible, but GCI's only two slots are `--hifi` and `--nano`, and CLR's error profile is nothing like HiFi's. Routing it to `--hifi` mislabels the evidence and produces a confidently wrong score. Task 4.
2. **GCI's N50s must not land in the `sequence_*` namespace.** `_parse_fasta` already computes contiguity at ingest; GCI's expected/observed N50 are a different computation from filtered depth. Writing them into the shared namespace reintroduces the "two facts supposed to agree" bug the epic recorded when it deleted `assembly_n50`. Task 2.
3. **A BAM's `.bai` must travel with it.** BioFlow's storage is content-addressed, so a BAM and its index are unrelated `DataObject`s with no path relationship. CRAQ's Task 3 review caught exactly this; reuse `launch_bam_stats`'s `bai_sha256`/`bai_path` pattern rather than guessing a sibling path. Task 3.

## File Structure

| File | Responsibility | Action |
| --- | --- | --- |
| `backend/scripts/install-gci.sh` | Clone GCI at a pinned commit to `/opt/gci`, wrapper on PATH | Create |
| `backend/Dockerfile` | Invoke the install script | Modify (~line 353) |
| `backend/app/config.py` | `gci_path` setting | Modify (~line 155) |
| `backend/app/pipelines/tools.py` | `gci()` probe, `TOOL_META["gci"]`, `all_tools()`, `cache_clear()` | Modify |
| `backend/app/pipelines/gci_runner.py` | Command builder + `.gci` parser | Create |
| `backend/app/queue/assembly_qc_handlers.py` | `assess_assembly_continuity` handler | Modify |
| `backend/app/queue/results.py` | `_apply_assess_assembly_continuity` + `_APPLIERS` entry | Modify |
| `backend/app/services/pipeline_service.py` | `launch_continuity_qc`, chemistry routing | Modify |
| `backend/app/api/v1/pipelines.py` | `POST /pipelines/assembly-continuity` | Modify |
| `backend/app/services/suggestion_service.py` | `build_continuity_card` + registration | Modify |
| `frontend/src/components/AssemblyFacts.tsx` | Continuity facts block | Modify |
| `backend/tests/pipelines/test_gci_runner.py` | Runner unit tests | Create |
| `backend/tests/services/test_suggestion_service.py` | Card tests | Modify |

Tasks 1–5 are independently committable. Task 6 is real-data verification and closeout.

---

### Task 1: Install GCI and register the tool

**Files:**
- Create: `backend/scripts/install-gci.sh`
- Modify: `backend/Dockerfile`, `backend/app/config.py`, `backend/app/pipelines/tools.py`
- Test: `backend/tests/pipelines/test_tools.py`

- [ ] **Step 1: Confirm the pinned commit is still current**

The repo's newest tag is `v1.0`, but commits have landed since, and the README documents behaviour that appears to postdate the tag — the `-mq` guidance citing issue #21. So the script pins a commit, not the tag.

Resolved 2026-08-06: `543cd4136187ff3ddd3ba4d1585626dbcdef6af6` (committed 2026-02-28), which was GCI's `main` HEAD. That SHA is already written into the script in Step 3 — this step only re-checks it:

```bash
gh api repos/yeeus/GCI/commits/main --jq '.sha'
```

If `main` has moved since, decide deliberately whether to take the newer commit (and update the comment's date) or stay on this one. Do not switch to a branch name: a "pin" that tracks `main` is not a pin.

- [ ] **Step 2: Check which Python dependencies the image already has**

```bash
docker compose exec api python -c "import pysam, Bio, numpy, matplotlib; print('all present')"
```

Install only what is missing. `pysam` and `numpy` are likely already present via other tooling; do not blanket-install all four.

- [ ] **Step 3: Write the install script**

Create `backend/scripts/install-gci.sh`:

```sh
#!/bin/sh
# Install GCI (Genome Continuity Inspector) from GitHub.
#
# There is NO BUILD. This is worth stating because issue #65 was filed
# believing otherwise -- it said "the real prerequisite is a second
# aligner", reading GCI's Requirements list as a dependency list.
#
# It is not. GCI never invokes an aligner: it consumes finished BAM/PAF
# files through --hifi and --nano. Its README marks winnowmap
# "(optional, but wanted for mapping)" -- the SAME parenthetical it gives
# minimap2, which this image has had since the alignment slice. Every
# aligner in that list is a suggestion for producing GCI's input.
#
# So GCI itself is: python3, pysam, biopython, numpy, matplotlib. MIT
# licensed. No arm64 asset check applies, because there is no asset.
#
# Pinned to a commit, not the v1.0 tag: commits have landed since that tag
# and the README documents behaviour that postdates it (the -mq guidance
# citing upstream issue #21). A "pin" that tracks main is not a pin.
#
# Resolved via `gh api repos/yeeus/GCI/commits/main --jq '.sha'` on
# 2026-08-06; that was GCI's main HEAD, committed 2026-02-28.
#
# Bioconda ships GCI too, but this image carries no conda and adding one
# for a pure-Python tool would cost far more than a pinned clone.

set -eu

GCI_COMMIT="${GCI_COMMIT:-543cd4136187ff3ddd3ba4d1585626dbcdef6af6}"
INSTALL_DIR="/opt/gci"

apt-get update
apt-get install -y --no-install-recommends git

echo "Fetching GCI ${GCI_COMMIT}..."
mkdir -p "${INSTALL_DIR}"
git -C "${INSTALL_DIR}" init -q
git -C "${INSTALL_DIR}" remote add origin https://github.com/yeeus/GCI.git
git -C "${INSTALL_DIR}" fetch --depth 1 origin "${GCI_COMMIT}"
git -C "${INSTALL_DIR}" checkout -q FETCH_HEAD

chmod +x "${INSTALL_DIR}/GCI.py"

# A wrapper rather than a symlink: GCI.py resolves its utility siblings
# from its own location.
cat > /usr/local/bin/gci <<'WRAPPER'
#!/bin/sh
exec python3 /opt/gci/GCI.py "$@"
WRAPPER
chmod +x /usr/local/bin/gci

INSTALLED_COMMIT="$(git -C "${INSTALL_DIR}" rev-parse HEAD)"

apt-get purge -y git
apt-get autoremove -y
rm -rf /var/lib/apt/lists/*

echo "GCI installed at commit ${INSTALLED_COMMIT}:"
du -sh "${INSTALL_DIR}"
```

The commit SHA above is already resolved (2026-08-06). If Step 1 found that `main` has moved and you decided to take a newer commit, update both the `GCI_COMMIT` default and the date in the comment above it — a comment claiming a resolution date that no longer matches the pin is worse than no comment.

- [ ] **Step 4: Wire into the Dockerfile**

Append after the CRAQ install block (~line 349), adding any missing pip dependencies found in Step 2:

```dockerfile
# GCI consumes finished long-read alignments; it never runs an aligner
# itself, so there is no winnowmap dependency and nothing to compile.
COPY scripts/install-gci.sh /srv/scripts/install-gci.sh
RUN chmod +x /srv/scripts/install-gci.sh \
    && /srv/scripts/install-gci.sh
```

- [ ] **Step 5: Add the config setting**

In `backend/app/config.py`, after `craq_path` (~line 155):

```python
    gci_path: str = "gci"
```

- [ ] **Step 6: Write the failing probe test**

Add to `backend/tests/pipelines/test_tools.py`:

```python
def test_gci_probe_reports_version(monkeypatch, tmp_path):
    fake = tmp_path / "gci"
    fake.write_text("#!/bin/sh\necho 'GCI v1.0'\n")
    fake.chmod(0o755)

    monkeypatch.setattr(tools.settings, "gci_path", str(fake))
    tools.gci.cache_clear()

    probed = tools.gci()
    assert probed.error is None
    assert probed.version is not None
```

- [ ] **Step 7: Run it to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -k gci -v
```

Expected: FAIL with `AttributeError: module 'app.pipelines.tools' has no attribute 'gci'`.

- [ ] **Step 8: Implement the probe and TOOL_META**

In `backend/app/pipelines/tools.py`, after `craq()`:

```python
@lru_cache(maxsize=1)
def gci() -> Tool:
    # GCI.py takes -v/--version via argparse and exits zero.
    return _probe("gci", settings.gci_path, ["--version"])
```

Register in `all_tools()` and the `cache_clear` block, then add the metadata entry:

```python
    "gci": ToolMeta(
        name="gci",
        pipeline_types=(PipelineType.ASSEMBLY_QC,),
        homepage="https://github.com/yeeus/GCI",
        repository="https://github.com/yeeus/GCI",
        citation=(
            "Chen, Quanyu, et al. GCI: a continuity inspector for complete "
            "genome assembly. Bioinformatics 40.11 (2024): btae633."
        ),
        citation_url="https://doi.org/10.1093/bioinformatics/btae633",
        license="MIT",
        usage=(
            "Scores assembly continuity from long reads aligned back to the "
            "assembly, reporting regions unsupported by read evidence. Runs "
            "against BioFlow-produced minimap2 alignments only -- upstream "
            "recommends pairing two aligners for higher sensitivity in "
            "repetitive regions, and the aligners used are recorded with "
            "the score. Whole-assembly only; regions mode and trio binning "
            "are not used."
        ),
        runnable=True,
    ),
```

The license was verified 2026-08-06 (`gh api repos/yeeus/GCI --jq '.license.spdx_id'` → `MIT`). Re-verify rather than trusting this line if any time has passed.

- [ ] **Step 9: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -k "gci or documented" -v
```

Expected: PASS, including `test_every_tool_is_documented`.

- [ ] **Step 10: Build and confirm the probe against the real image**

From the **main checkout**:

```bash
docker compose build api && docker compose exec api gci --version
```

If `--version` does not print a usable string, adjust the probe's args — the spec flags this as unverified.

- [ ] **Step 11: Commit**

```bash
git add backend/scripts/install-gci.sh backend/Dockerfile backend/app/config.py backend/app/pipelines/tools.py backend/tests/pipelines/test_tools.py
git commit -m "feat(pipelines): install GCI and register the tool (#65)"
```

---

### Task 2: The runner — command builder and parser

**Files:**
- Create: `backend/app/pipelines/gci_runner.py`
- Test: `backend/tests/pipelines/test_gci_runner.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_gci_runner.py`:

```python
from pathlib import Path

import pytest

from app.pipelines import gci_runner


def test_command_routes_hifi_bam_to_hifi_flag():
    cmd = gci_runner.build_gci_command(
        gci_path="/usr/local/bin/gci",
        assembly=Path("/work/assembly.fasta"),
        hifi_bam=Path("/work/hifi.bam"),
        nano_bam=None,
        out_dir=Path("/work/out"),
        prefix="gci",
        threads=8,
        map_qual=30,
        plot=False,
    )
    assert "--hifi" in cmd
    assert "/work/hifi.bam" in cmd
    assert "--nano" not in cmd


def test_command_routes_both_slots_when_both_present():
    cmd = gci_runner.build_gci_command(
        gci_path="/usr/local/bin/gci",
        assembly=Path("/work/assembly.fasta"),
        hifi_bam=Path("/work/hifi.bam"),
        nano_bam=Path("/work/nano.bam"),
        out_dir=Path("/work/out"),
        prefix="gci",
        threads=8,
        map_qual=30,
        plot=False,
    )
    assert "--hifi" in cmd and "--nano" in cmd


def test_command_records_map_qual():
    """-mq is not a tuning knob that can be left implicit: upstream is
    explicit that lowering it pulls in multi-mapping reads from repetitive
    regions, so two runs at different -mq are not comparable.
    """
    cmd = gci_runner.build_gci_command(
        gci_path="/usr/local/bin/gci",
        assembly=Path("/work/assembly.fasta"),
        hifi_bam=Path("/work/hifi.bam"),
        nano_bam=None,
        out_dir=Path("/work/out"),
        prefix="gci",
        threads=8,
        map_qual=50,
        plot=False,
    )
    assert "-mq" in cmd
    assert "50" in cmd


def test_command_omits_plot_flag_when_disabled():
    """One image per chromosome means a fragmented assembly produces
    hundreds of files. Plotting is gated, not default-on.
    """
    cmd = gci_runner.build_gci_command(
        gci_path="/usr/local/bin/gci",
        assembly=Path("/work/assembly.fasta"),
        hifi_bam=Path("/work/hifi.bam"),
        nano_bam=None,
        out_dir=Path("/work/out"),
        prefix="gci",
        threads=8,
        map_qual=30,
        plot=False,
    )
    assert "-p" not in cmd


def test_command_uses_fixed_input_names():
    """The assembly is linked under a fixed name so an object name never
    reaches an output path -- QUAST's stored-XSS lesson, applied here
    before the bug exists.
    """
    cmd = gci_runner.build_gci_command(
        gci_path="/usr/local/bin/gci",
        assembly=Path("/work/assembly.fasta"),
        hifi_bam=Path("/work/hifi.bam"),
        nano_bam=None,
        out_dir=Path("/work/out"),
        prefix="gci",
        threads=8,
        map_qual=30,
        plot=False,
    )
    assert not any(";" in part or "<" in part for part in cmd)


def test_parse_gci_returns_typed_facts():
    """Counts and N50s are ints, scores are floats. QUAST's slice shipped a
    float where an int was meant, invisibly, because 2 == 2.0 and every
    assertion was equality-only.
    """
    text = (
        "Chromosome\tExpected N50\tObserved N50\tExpected number of contigs\t"
        "Observed number of contigs\tGenome Continuity Index\n"
        "Genome\t12157105\t8452103\t16\t23\t41.8259\n"
    )
    parsed = gci_runner.parse_gci(text)
    assert parsed["assembly_continuity_gci"] == pytest.approx(41.8259)
    assert isinstance(parsed["assembly_continuity_gci"], float)
    assert isinstance(parsed["assembly_continuity_expected_n50"], int)
    assert isinstance(parsed["assembly_continuity_observed_contigs"], int)


def test_parse_gci_raises_on_empty():
    with pytest.raises(ValueError):
        gci_runner.parse_gci("")
```

- [ ] **Step 2: Run to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_gci_runner.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.pipelines.gci_runner'`.

- [ ] **Step 3: Write the runner**

Create `backend/app/pipelines/gci_runner.py`:

```python
"""Command builder and output parser for GCI continuity inspection.

Pure functions only: no I/O, no subprocess, no database.

**GCI never invokes an aligner.** It consumes finished BAM/PAF files
through `--hifi` and `--nano`. Its README marks winnowmap "(optional, but
wanted for mapping)" -- the same parenthetical it gives minimap2 -- so
every aligner in its Requirements list is a suggestion for producing input,
not a dependency. This module therefore builds no alignment command and the
handler runs no aligner.

**Two aligners are upstream's recommendation, and the score records which
were used.** GCI's FAQ reports that WM2+MM2 and VM+MM2 yield similar issues
and scores, and recommends WM2+MM2 because two aligners cross-check in
repetitive regions. A minimap2-only run is a supported invocation that is
less sensitive there -- so it is stored, with
`assembly_continuity_aligners` saying what produced it. That is a
deliberate divergence from CRAQ's omission rule: CRAQ omits CSE on NGS-only
runs because upstream says it is "hardly detected", while upstream here says
the scores are similar. The rule is driven by what upstream says the
degraded mode measures, not by a general preference for omitting things.

**GCI's N50s are its own.** `assembly_continuity_expected_n50` and
`_observed_n50` are computed from filtered read depth, not from contig
lengths, and must never be written into the `sequence_*` namespace that
`_parse_fasta` fills at ingest. Two facts that are supposed to agree, on
one object, is the bug the epic recorded when it deleted `assembly_n50`.
"""

from __future__ import annotations

from pathlib import Path


def build_gci_command(
    *,
    gci_path: str,
    assembly: Path,
    hifi_bam: Path | None,
    nano_bam: Path | None,
    out_dir: Path,
    prefix: str,
    threads: int = 8,
    map_qual: int = 30,
    plot: bool = False,
) -> list[str]:
    """Build the `GCI.py` invocation.

    At least one of `hifi_bam` / `nano_bam` must be set; the launch path
    enforces that and refuses the ambiguous cases rather than guessing,
    because there is no short-read slot to degrade into.

    `map_qual` is passed explicitly rather than left to GCI's default so
    that the value is always recorded as a fact: upstream is explicit that
    lowering it admits multi-mapping reads from repetitive regions, which
    makes runs at different thresholds incomparable.
    """
    cmd = [
        gci_path,
        "-r",
        str(assembly),
        "-d",
        str(out_dir),
        "-o",
        prefix,
        "-t",
        str(threads),
        "-mq",
        str(map_qual),
    ]
    if hifi_bam is not None:
        cmd += ["--hifi", str(hifi_bam)]
    if nano_bam is not None:
        cmd += ["--nano", str(nano_bam)]
    if plot:
        cmd += ["-p", "-it", "pdf"]
    return cmd


def parse_gci(text: str) -> dict[str, float | int]:
    """Parse GCI's `<prefix>.gci` summary.

    Tab-separated with a header row. The whole-assembly row is what this
    reads; per-chromosome rows are not stored as facts.

    Counts and N50s parse as int, the continuity index as float. Asserting
    the type rather than the value is what catches the class of bug QUAST's
    slice shipped, where `2 == 2.0` hid a wrong type for a whole release.
    """
    rows = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if len(rows) < 2:
        raise ValueError("no parseable data row found in GCI output")

    for row in rows[1:]:
        fields = row.split("\t")
        if len(fields) < 6:
            continue
        return {
            "assembly_continuity_expected_n50": int(float(fields[1])),
            "assembly_continuity_observed_n50": int(float(fields[2])),
            "assembly_continuity_expected_contigs": int(float(fields[3])),
            "assembly_continuity_observed_contigs": int(float(fields[4])),
            "assembly_continuity_gci": float(fields[5]),
        }
    raise ValueError("no parseable data row found in GCI output")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_gci_runner.py -v
```

Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/gci_runner.py backend/tests/pipelines/test_gci_runner.py
git commit -m "feat(pipelines): GCI command builder and .gci parser (#65)"
```

**The `.gci` column layout above is inferred from the README's prose description of the file, not from a real run.** Task 6 verifies it against upstream's own Zenodo test data and corrects this module. Expect to revisit this file — CRAQ's slice shipped a parser whose row key no real report used, with every unit test green.

---

### Task 3: The handler

**Files:**
- Modify: `backend/app/queue/assembly_qc_handlers.py`, `backend/app/queue/results.py`
- Test: `backend/tests/queue/test_assembly_qc_handlers.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/queue/test_assembly_qc_handlers.py`, matching the fixture style of the `assess_assembly_errors` tests:

```python
def test_assess_assembly_continuity_links_bam_index(monkeypatch, tmp_path):
    """A BAM's .bai must travel with it. BioFlow's storage is
    content-addressed, so a BAM and its index are unrelated DataObjects
    with no path relationship -- linking the BAM alone fails deep inside a
    pysam call rather than with a clear error. GCI's README is explicit:
    "this is necessary!!!"
    """
    from app.queue import assembly_qc_handlers
    # ... build ctx with hifi_bam_sha256 + hifi_bai_sha256 per the
    # existing test_assess_assembly_errors_* fixture pattern ...
    # assert the .bai landed beside the linked .bam
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/queue/test_assembly_qc_handlers.py -k continuity -v
```

Expected: FAIL — `assess_assembly_continuity` does not exist.

- [ ] **Step 3: Write the handler**

**The helpers below already exist — do not write them.** `_prepare_workdir`, `_resolve_input` and `_named_link` are imported into `assembly_qc_handlers.py` from `app.queue.pipeline_handlers` (line 31); `_link_bam_index` is defined in `assembly_qc_handlers.py` itself at line 550, added by the CRAQ slice for exactly this problem. Reuse all four.

Add to `backend/app/queue/assembly_qc_handlers.py`, following `assess_assembly_errors` (line 384). Constants first:

```python
_GCI_ASSEMBLY_LINK = "assembly.fasta"
_GCI_HIFI_LINK = "hifi.bam"
_GCI_NANO_LINK = "nano.bam"
ASSEMBLY_CONTINUITY_LEASE_SECONDS = 3600

# One image per chromosome, so a fragmented assembly produces hundreds of
# files. Plotting is offered below this contig count and off above it --
# a QC job that quietly writes 800 PDFs is a storage surprise.
GCI_PLOT_MAX_CONTIGS = 50
```

Then:

```python
@handler(
    "assess_assembly_continuity",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=8, mem_mb=16384, io=IoClass.HEAVY),
    max_attempts=1,
)
def assess_assembly_continuity(ctx: JobContext) -> dict:
    """Long-read continuity inspection for one assembly, with GCI.

    Read-only: no new object, only facts merged onto the assembly that was
    scored, plus optional depth plots under qc_reports/.

    **GCI runs no aligner.** It consumes the sorted, indexed BAMs the align
    pipeline already produced. A QC job that silently ran minimap2 would
    duplicate work the user can see and make the job's cost unpredictable.

    **Each BAM's .bai must be linked beside it.** Storage is
    content-addressed, so a managed BAM and its index are two unrelated
    DataObjects; the launch path supplies `{hifi,nano}_bai_sha256`/`_path`
    the way `launch_bam_stats` does, and a register-in-place BAM falls back
    to a sibling `.bai`, which is the only case where that guess is valid.
    GCI's README says of the index: "this is necessary!!!"

    **The aligners are recorded, not assumed.** Upstream recommends pairing
    winnowmap with minimap2 for sensitivity in repetitive regions; BioFlow
    supplies minimap2 alignments only, and that fact travels with the score
    rather than being silently dropped.
    """
    tool = tools.require(tools.gci())

    work = _prepare_workdir(ctx, "assembly_continuity")

    assembly = _resolve_input(ctx.payload, "assembly")
    assembly = _named_link(work, assembly, _GCI_ASSEMBLY_LINK)

    hifi_bam = None
    if ctx.payload.get("hifi_bam_path") or ctx.payload.get("hifi_bam_sha256"):
        raw = _resolve_input(ctx.payload, "hifi_bam")
        hifi_bam = _named_link(work, raw, _GCI_HIFI_LINK)
        _link_bam_index(ctx.payload, "hifi_bai", raw, hifi_bam)

    nano_bam = None
    if ctx.payload.get("nano_bam_path") or ctx.payload.get("nano_bam_sha256"):
        raw = _resolve_input(ctx.payload, "nano_bam")
        nano_bam = _named_link(work, raw, _GCI_NANO_LINK)
        _link_bam_index(ctx.payload, "nano_bai", raw, nano_bam)

    if hifi_bam is None and nano_bam is None:
        raise PermanentError(
            "Continuity inspection needs long reads aligned to this assembly."
        )

    threads = max(1, int(ctx.payload.get("threads") or 8))
    map_qual = int(ctx.payload.get("map_qual") or 30)
    plot = bool(ctx.payload.get("plot"))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    out_dir = work / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = gci_runner.build_gci_command(
        gci_path=tool.path,
        assembly=assembly,
        hifi_bam=hifi_bam,
        nano_bam=nano_bam,
        out_dir=out_dir,
        prefix="gci",
        threads=threads,
        map_qual=map_qual,
        plot=plot,
    )

    ctx.progress(phase="starting", pct=None, message="starting gci")
    ctx.extend_lease(ASSEMBLY_CONTINUITY_LEASE_SECONDS)

    log.info(
        "assembly_continuity_started",
        job_id=ctx.job_id,
        has_hifi=hifi_bam is not None,
        has_nano=nano_bam is not None,
        map_qual=map_qual,
        plot=plot,
        threads=threads,
    )

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise RetryableError(f"gci exited {code}; see {log_path}")

    gci_file = out_dir / "gci.gci"
    if not gci_file.exists():
        raise RetryableError(f"gci produced no gci.gci in {out_dir}; see {log_path}")

    facts = gci_runner.parse_gci(gci_file.read_text())

    aligners = list(ctx.payload.get("aligners") or ["minimap2"])
    facts.update(
        {
            "assembly_continuity_aligners": aligners,
            "assembly_continuity_map_qual": map_qual,
            "assembly_continuity_threshold": int(ctx.payload.get("threshold") or 0),
            "assembly_continuity_tool": "gci",
            "assembly_continuity_tool_version": tool.version or "",
        }
    )

    if plot:
        report_dir = settings.qc_reports_dir / str(ctx.payload["object_id"])
        report_dir.mkdir(parents=True, exist_ok=True)
        images = out_dir / "images"
        if images.is_dir():
            for image in images.iterdir():
                if image.suffix in {".pdf", ".png"}:
                    shutil.copy2(image, report_dir / image.name)

    return {
        "object_id": ctx.payload["object_id"],
        "job_id": ctx.job_id,
        "facts": facts,
    }
```

- [ ] **Step 4: Add the results applier**

In `backend/app/queue/results.py`, after `_apply_assess_assembly_errors`:

```python
async def _apply_assess_assembly_continuity(result: dict, *, owner: str) -> None:
    """Record GCI's continuity facts on the assembly they describe.

    Near-copy of `_apply_assess_assembly_errors`: read-only, nothing to
    ingest, and an uploaded assembly is scored exactly like one this
    application produced.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("assembly_continuity_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "assembly_continuity_applied",
        object_id=object_id,
        gci=facts.get("assembly_continuity_gci"),
    )
```

Register in `_APPLIERS` (~line 2152):

```python
    "assess_assembly_continuity": _apply_assess_assembly_continuity,
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/queue/ -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/assembly_qc_handlers.py backend/app/queue/results.py backend/tests/queue/
git commit -m "feat(queue): assess_assembly_continuity handler and applier (#65)"
```

---

### Task 4: Launch path with chemistry routing, route, and card

**Files:**
- Modify: `backend/app/services/pipeline_service.py`, `backend/app/api/v1/pipelines.py`, `backend/app/services/suggestion_service.py`
- Test: `backend/tests/services/test_suggestion_service.py`, `backend/tests/services/test_pipeline_service.py`

**This is the task where the `CLR` decision lives.** Getting it wrong produces a confidently wrong score rather than an error.

- [ ] **Step 1: Write the failing chemistry-routing tests**

**`ReadChemistry` lives in `app.pipelines.align_runner`, not in `app.models`** — verified 2026-08-06. Import it from there in both the tests and `pipeline_service`. Its members are `HIFI`, `CLR`, `ONT_SIMPLEX`, `ONT_DUPLEX`, `SHORT`, `UNKNOWN`. Note that `UNKNOWN` is a real enum member, not just a `None` return — `gci_slot_for_chemistry` must handle both.

Add to `backend/tests/services/test_pipeline_service.py`:

```python
@pytest.mark.parametrize(
    "chemistry,expected_slot",
    [
        (ReadChemistry.HIFI, "hifi"),
        (ReadChemistry.ONT_SIMPLEX, "nano"),
        (ReadChemistry.ONT_DUPLEX, "nano"),
    ],
)
def test_gci_slot_for_chemistry(chemistry, expected_slot):
    from app.services.pipeline_service import gci_slot_for_chemistry

    assert gci_slot_for_chemistry(chemistry) == expected_slot


def test_gci_slot_refuses_clr():
    """PacBio CLR is long-read and so looks eligible, but GCI has only
    --hifi and --nano and CLR's error profile is nothing like HiFi's.
    Routing it to --hifi would mislabel the evidence; GCI's filters assume
    HiFi-grade per-read accuracy. Refusing is correct.
    """
    from app.services.pipeline_service import gci_slot_for_chemistry

    assert gci_slot_for_chemistry(ReadChemistry.CLR) is None


def test_gci_slot_refuses_short_and_unknown():
    """SHORT has no slot at all -- GCI takes no short-read input. UNKNOWN
    must be refused rather than defaulted: read_chemistry_for_alignment's
    docstring says callers fall back to a conservative short-read default,
    which is right for picking an alignment preset and wrong here.

    Both the UNKNOWN member and a plain None are tested: the enum has a
    real UNKNOWN member, and the resolver can also return None when no
    chemistry was recorded at all. They are different inputs reaching the
    same refusal, and a routing function that handles one but not the
    other passes half of this test in production.
    """
    from app.services.pipeline_service import gci_slot_for_chemistry

    assert gci_slot_for_chemistry(ReadChemistry.SHORT) is None
    assert gci_slot_for_chemistry(ReadChemistry.UNKNOWN) is None
    assert gci_slot_for_chemistry(None) is None
```

- [ ] **Step 2: Run to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/services/test_pipeline_service.py -k gci_slot -v
```

Expected: FAIL — `gci_slot_for_chemistry` does not exist.

- [ ] **Step 3: Implement the routing and launch path**

In `backend/app/services/pipeline_service.py`:

```python
def gci_slot_for_chemistry(chemistry: ReadChemistry | None) -> str | None:
    """Which GCI input slot a read chemistry belongs in, or None to refuse.

    GCI has exactly two slots and no short-read input exists at all, which
    makes this stricter than CRAQ's `-ngs`/`-sms` routing:

      HIFI                      -> --hifi
      ONT_SIMPLEX, ONT_DUPLEX   -> --nano
      CLR                       -> refuse
      SHORT                     -> refuse
      UNKNOWN / None            -> refuse (the dialog asks)

    CLR is the case worth spelling out, because it is long-read and
    therefore looks eligible. PacBio CLR is not HiFi: GCI's identity and
    clipping filters assume HiFi-grade per-read accuracy, and CLR's error
    profile is nothing like it. Routing CLR to --hifi does not degrade
    gracefully, it mislabels the evidence.

    Refusing UNKNOWN follows CRAQ's rule.
    `read_chemistry_for_alignment`'s docstring says callers "fall back to
    the conservative short-read default rather than guessing" -- correct
    for picking an alignment preset, wrong here, and doubly so when SHORT
    is not even a valid input.
    """
    if chemistry == ReadChemistry.HIFI:
        return "hifi"
    if chemistry in (ReadChemistry.ONT_SIMPLEX, ReadChemistry.ONT_DUPLEX):
        return "nano"
    return None
```

Then `launch_continuity_qc`, following `launch_assembly_error_qc`'s shape: resolve each BAM through `reference_for_bam` to validate it is actually against this assembly, route by chemistry, resolve each `.bai` via `_sidecar_of_role` matching `launch_bam_stats`'s `bai_sha256`/`bai_path` pattern (`pipeline_service.py:1496-1542`), and gate `plot` on contig count against `GCI_PLOT_MAX_CONTIGS`.

- [ ] **Step 4: Add the route**

In `backend/app/api/v1/pipelines.py`:

```python
class AssemblyContinuityRequest(BaseModel):
    object_id: str
    hifi_bam_id: str | None = None
    nano_bam_id: str | None = None
    map_qual: int | None = None
    plot: bool | None = None


@router.post("/assembly-continuity")
async def launch_assembly_continuity(
    body: AssemblyContinuityRequest,
    owner: str = Depends(current_owner),
) -> dict:
    return await pipeline_service.launch_continuity_qc(
        body.object_id,
        owner=owner,
        hifi_bam_id=body.hifi_bam_id,
        nano_bam_id=body.nano_bam_id,
        map_qual=body.map_qual,
        plot=body.plot,
    )
```

- [ ] **Step 5: Write the card tests**

Add to `backend/tests/services/test_suggestion_service.py`:

```python
async def test_continuity_card_unavailable_when_gci_missing(monkeypatch):
    """Assert the UNAVAILABLE direction. The image ships tools installed,
    so the available direction passes whether or not the patch worked.
    """
    from app.services import suggestion_service

    monkeypatch.setattr(
        suggestion_service.tools,
        "gci",
        lambda: Tool(name="gci", path=None, version=None, error="not found"),
    )
    card = await suggestion_service.build_continuity_card(assembly_obj, [])
    assert not card.available


async def test_continuity_card_says_so_when_only_short_read_bams():
    """The generic "align reads first" message would send the user to
    re-run an alignment that cannot help -- GCI takes no short-read input.
    """
    from app.services import suggestion_service

    card = await suggestion_service.build_continuity_card(
        assembly_obj, [short_read_bam]
    )
    assert not card.available
    assert "long" in card.unavailable_reason.lower()
```

- [ ] **Step 6: Write the card**

In `backend/app/services/suggestion_service.py`, following `build_assembly_error_card` (line 1222), with the unavailable reasons the spec names — tool missing, no long-read BAM, short-read-only (its own specific message), unknown chemistry (available, routed to the dialog).

Register it in the card list (~line 1546):

```python
        ("assembly_continuity", lambda: build_continuity_card(obj, assembly_alignments)),
```

**This registration is the silently skippable step.** Nothing fails if you omit it; the user just never sees the card.

- [ ] **Step 7: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/services/ -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/api/v1/pipelines.py backend/app/services/suggestion_service.py backend/tests/services/
git commit -m "feat(pipelines): GCI launch path with chemistry routing, route, and card (#65)"
```

---

### Task 5: Frontend facts block

**Files:**
- Modify: `frontend/src/components/AssemblyFacts.tsx`

- [ ] **Step 1: Add the continuity block**

Follow the assembly-errors block in that file. Render, when `assembly_continuity_gci` is present:

- **The GCI score** as the headline number.
- **Expected vs observed N50 and contig counts**, as the pairs they are.
- **The aligners used**, from `assembly_continuity_aligners`, with the note that a single-aligner run undercounts issues in repetitive regions — upstream recommends pairing two aligners.
- **`-mq`**, since two runs at different thresholds are not comparable.

- [ ] **Step 2: Show the benchmark range, invent no bands**

Render the score against GCI's published benchmark range (7.26–99.99 across real T2T assemblies) so the number has context. **Do not invent quality bands.** CRAQ's card could show bands because upstream publishes an AQI scale; GCI publishes none, and a fabricated threshold on a page that reads as authoritative is worse than no threshold.

- [ ] **Step 3: Typecheck and verify in the browser**

```bash
cd frontend && npx tsc --noEmit
```

Then from this worktree:

```bash
./ops/worktree-up.sh
```

Open http://localhost:5273 and confirm the block renders. This is the actual verification step for UI work in this repo.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/AssemblyFacts.tsx
git commit -m "feat(frontend): assembly continuity facts block (#65)"
```

---

### Task 6: Real-data verification and closeout

**This task is where this plan expects to find bugs.** The `.gci` parser was built from the README's prose, not from a run.

- [ ] **Step 1: Rebuild the stack**

From the **main checkout**:

```bash
docker compose up -d --build api web worker
```

- [ ] **Step 2: Verify the parser against upstream's own test data**

GCI ships example data on Zenodo with expected outputs committed in `example/`, and the README gives an exact reproducing command. This is worth more than any fixture:

```bash
docker compose exec api sh -c 'cd /tmp && curl -fsSL -O https://zenodo.org/records/12748594/files/example.tar.gz && tar zxf example.tar.gz && python /opt/gci/GCI.py -r example/MH63.fasta --hifi example/MH63_winnowmap_hifi.subsample.bam example/MH63.minimap2_hifi.subsample.paf -d /tmp/gci_test -o MH63 -f && cat /tmp/gci_test/MH63.gci'
```

Compare the real column layout against `parse_gci`. **If it differs, fix the runner and rebuild the unit-test fixture from this real output.**

- [ ] **Step 3: Run a real continuity job**

Use a real project with an assembly and long reads aligned to it. Launch from the Actions tab, and confirm the facts land.

- [ ] **Step 4: Measure and correct the resource figures**

The FAQ has a RAM/time figure at 32 threads with a note that 16 suffices, and "reduce memory usage" is on upstream's own to-do list. Measure peak RSS and runtime, and correct `JobResources(cpu=8, mem_mb=16384)` if the real peak differs materially. Feed the numbers to `resource_estimator` expectations.

- [ ] **Step 5: Verify the CLR refusal against real objects**

```bash
docker compose exec api python -c "
from app.pipelines.align_runner import ReadChemistry
from app.services.pipeline_service import gci_slot_for_chemistry
for c in ReadChemistry:
    print(c, '->', gci_slot_for_chemistry(c))
print(None, '->', gci_slot_for_chemistry(None))
"
```

Expected: only `HIFI`, `ONT_SIMPLEX` and `ONT_DUPLEX` return a slot. `CLR`, `SHORT`, `UNKNOWN` and `None` all return `None`.

- [ ] **Step 6: Check the card against a real project**

CLAUDE.md is explicit that this is worth more than another fixture — the Actions tab's suggestion rules passed a full green suite while getting two things wrong that one look at a real project exposed. Confirm the card is dark with no long-read BAM, and that the short-read-only case shows its specific message.

- [ ] **Step 7: Run the full suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: the baseline count plus this plan's additions, zero failures.

- [ ] **Step 8: Update docs and close out**

- Add a closeout note to the spec recording what the implementation did differently.
- If `docs/TODO.md` has an entry this resolves, append ` — FIXED` and move it to `docs/TODO-done.md`.
- Update [#65](https://github.com/syntheticgio/bioflow/issues/65) and close it.
- **Consider filing the winnowmap enhancement.** After #64 lands meryl, winnowmap's only remaining blocker is winnowmap itself, and it would move GCI onto upstream's recommended two-aligner footing.

- [ ] **Step 9: Commit and merge**

```bash
git add -A
git commit -m "docs: closeout notes for GCI continuity inspection (#65)"
```

Then merge to `main` and push, per CLAUDE.md.

---

## Notes for the implementer

**If someone tells you this slice needs winnowmap, it does not.** That belief is what issue #65 was filed on, and it comes from reading GCI's Requirements list as a dependency list. GCI consumes finished alignments. The spec quotes the README verbatim; re-read it before adding a build.

**The `-mq` fact is not bureaucracy.** Upstream's own note on issue #21 is that lowering it admits multi-mapping reads from repetitive regions, so a score at `-mq 0` and a score at `-mq 30` are different measurements. Storing the threshold is what makes two runs comparable — or honestly incomparable.

**Do not trust this plan's checkboxes as a completion signal.** Nothing ticks them. Verify against the code.
