# Medaka Long-Read Polishing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give ONT-only assemblies a working polish path via Medaka, so a project assembled from long reads is not stuck with raw assembler output.

**Architecture:** A pure-function runner (`medaka_runner.py`) builds the `medaka_consensus` argv and parses its model line; a SUBPROCESS handler runs it and computes a draft-vs-consensus diff for the changed-position fact; a new `polish_long` card gates on long reads exactly as `polish` gates on short reads. Medaka performs its own minimap2 alignment internally, so no aligner command is constructed here.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor, pytest, Docker (micromamba into an isolated conda prefix), bioconda `medaka=2.2.2`.

**Spec:** `docs/superpowers/specs/2026-08-18-medaka-long-read-polishing-design.md`

## Global Constraints

- **`pytorch-cpu=2.9.*` must be pinned explicitly** in the micromamba create. Bare `pytorch` resolves to CUDA builds (libtorch ~885MB compressed vs ~61MB CPU). No error if omitted — the image just grows ~1GB.
- **`MEDAKA_VERSION=2.2.2`**, installed to prefix `/opt/medaka/env`, binary at `/opt/medaka/env/bin/medaka_consensus`.
- **Both architectures.** bioconda ships `linux-aarch64` for medaka 2.0.1–2.2.2. Do not add an arm64 skip branch.
- **`-f` is passed unconditionally** to `medaka_consensus`. Without it, Medaka reuses stale outputs in an existing directory and exits zero.
- **Never construct a minimap2 command for Medaka.** It resolves its own alignment parameters per-model via `medaka tools get_alignment_params`.
- **`is_long_read` is written positively, never as `not is_short_read`.** Unknown platform + unknown chemistry returns `False`.
- **Card tests assert the UNAVAILABLE direction.** The image ships tools installed, so an "available" assertion passes whether or not the patch worked.
- Commit subjects are Conventional Commits, imperative, lowercase after the colon, no trailing period, ~65 chars.
- Run backend tests from the worktree with `./backend/run-worktree-tests.sh`, never `docker compose exec api`.

---

### Task 1: `medaka_runner` command construction

**Files:**
- Create: `backend/app/pipelines/medaka_runner.py`
- Test: `backend/tests/pipelines/test_medaka_runner.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces:
  - `build_consensus_command(*, medaka_path: str, draft: Path, reads: Path, outdir: Path, threads: int = 1, bacteria: bool = False) -> list[str]`
  - `CONSENSUS_FILENAME: str = "consensus.fasta"`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_medaka_runner.py`:

```python
"""Medaka command construction and output parsing.

Medaka differs from Polypolish in three ways that this file pins down,
because each is a plausible "fix" that breaks the tool silently:

- It writes an output *directory*, not stdout, so there is no redirect
  wrapper here the way `polypolish_runner.redirect_stdout` exists there.
- It builds its own minimap2 call from model-dependent parameters
  (`medaka tools get_alignment_params`), so this module must never
  construct alignment arguments.
- Without `-f` it reuses whatever consensus is already in the output
  directory and exits zero, returning a previous run's assembly.
"""

from pathlib import Path

from app.pipelines import medaka_runner as runner


class TestConsensusCommand:
    def test_force_flag_is_always_present(self):
        """Without -f, medaka reuses stale outputs and exits zero.

        That returns a *previous* run's assembly while reporting success,
        which is why this is asserted on the argv rather than trusted to
        survive a future tidy-up.
        """
        argv = runner.build_consensus_command(
            medaka_path="medaka_consensus",
            draft=Path("draft.fasta"),
            reads=Path("reads.fastq"),
            outdir=Path("/work/out"),
        )
        assert "-f" in argv

    def test_draft_reads_and_outdir_are_passed(self):
        argv = runner.build_consensus_command(
            medaka_path="medaka_consensus",
            draft=Path("draft.fasta"),
            reads=Path("reads.fastq"),
            outdir=Path("/work/out"),
        )
        assert argv[0] == "medaka_consensus"
        assert argv[argv.index("-d") + 1] == "draft.fasta"
        assert argv[argv.index("-i") + 1] == "reads.fastq"
        assert argv[argv.index("-o") + 1] == "/work/out"

    def test_threads_are_passed(self):
        argv = runner.build_consensus_command(
            medaka_path="medaka_consensus",
            draft=Path("d.fasta"),
            reads=Path("r.fastq"),
            outdir=Path("/out"),
            threads=8,
        )
        assert argv[argv.index("-t") + 1] == "8"

    def test_bacteria_absent_by_default(self):
        """--bacteria is a dialog opt-in, never a default.

        ONT labels the bacterial model a research release; defaulting it on
        would silently apply it to eukaryotic drafts.
        """
        argv = runner.build_consensus_command(
            medaka_path="medaka_consensus",
            draft=Path("d.fasta"),
            reads=Path("r.fastq"),
            outdir=Path("/out"),
        )
        assert "--bacteria" not in argv

    def test_bacteria_present_when_requested(self):
        argv = runner.build_consensus_command(
            medaka_path="medaka_consensus",
            draft=Path("d.fasta"),
            reads=Path("r.fastq"),
            outdir=Path("/out"),
            bacteria=True,
        )
        assert "--bacteria" in argv

    def test_no_alignment_arguments_are_constructed(self):
        """Medaka resolves its own minimap2 preset from the model.

        Constructing one here would override a model-dependent choice with
        a fixed guess -- the inverse of Polypolish's mandatory `-a`.
        """
        argv = runner.build_consensus_command(
            medaka_path="medaka_consensus",
            draft=Path("d.fasta"),
            reads=Path("r.fastq"),
            outdir=Path("/out"),
        )
        joined = " ".join(argv)
        assert "-x" not in argv
        assert "map-ont" not in joined
        assert "minimap2" not in joined
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_medaka_runner.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.medaka_runner'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/pipelines/medaka_runner.py`:

```python
"""Medaka command construction and output parsing.

Same split `polypolish_runner` and `ivar_runner` use: pure functions over
strings and paths, testable without a container, a queue, or a binary.

Three shapes here are load-bearing and easy to "fix" into something wrong,
all three from Medaka's own `medaka_consensus` wrapper script. See
`docs/superpowers/specs/2026-08-18-medaka-long-read-polishing-design.md`:

- **Medaka writes a directory, not stdout.** The wrapper runs minimap2,
  `medaka inference` and `medaka sequence`, then leaves
  `<outdir>/consensus.fasta`. There is no stdout to capture, which is why
  nothing here mirrors `polypolish_runner.redirect_stdout` -- wrapping this
  argv in a shell redirect would write an empty file beside a correct
  consensus the handler then ignores.
- **The alignment parameters belong to the model, not to us.**
  `medaka_consensus` calls `medaka tools get_alignment_params --model
  $MODEL` and hands the result to minimap2, because the right preset
  depends on which network will consume the alignment. Polypolish's `-a` is
  mandatory and hardcoded; this is the opposite case, where building the
  aligner call ourselves would override a model-dependent choice with a
  fixed guess.
- **`-f` is not optional.** Without it the wrapper prints "WARNING: Output
  ... already exists, may use old results" and returns whatever consensus
  is already there, exiting zero. The handler prepares a fresh workdir per
  job, so this should never trigger -- but the failure it prevents is a job
  returning a *previous* run's assembly and reporting success.
"""

from pathlib import Path

# What `medaka_consensus` names its output inside the directory it is given.
# The `-p/--prefix` option could change this; BioFlow does not pass it, so
# the default is the contract between the runner and the handler.
CONSENSUS_FILENAME = "consensus.fasta"


def build_consensus_command(
    *,
    medaka_path: str,
    draft: Path,
    reads: Path,
    outdir: Path,
    threads: int = 1,
    bacteria: bool = False,
) -> list[str]:
    """The argv for `medaka_consensus`.

    `-f` is unconditional -- see the module docstring. `--bacteria` is an
    opt-in the launch dialog surfaces, never a default: ONT ships that model
    as a research release, and applying it to a eukaryotic draft because it
    happened to be the default would be a silent quality choice made on the
    user's behalf.

    No model is passed. Medaka inspects the basecaller metadata in the reads
    and resolves one itself; the handler records which one it chose, since
    the fallback to a legacy default is invisible in the output.
    """
    argv = [
        medaka_path,
        "-i",
        str(reads),
        "-d",
        str(draft),
        "-o",
        str(outdir),
        "-t",
        str(threads),
        "-f",
    ]
    if bacteria:
        argv.append("--bacteria")
    return argv
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_medaka_runner.py -q
```

Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/medaka_runner.py backend/tests/pipelines/test_medaka_runner.py
git commit -m "feat(pipelines): build medaka_consensus commands"
```

---

### Task 2: Model-line parsing

**Files:**
- Modify: `backend/app/pipelines/medaka_runner.py`
- Test: `backend/tests/pipelines/test_medaka_runner.py`

**Interfaces:**
- Consumes: `medaka_runner` from Task 1.
- Produces: `parse_model_line(text: str) -> dict` returning `{}` or a dict with keys `polish_model: str` and `polish_model_auto_resolved: bool`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_medaka_runner.py`:

```python
# Medaka announces its model choice on stderr before inference. The two
# shapes below are what distinguish a run that read basecaller metadata
# from one that fell back -- a distinction invisible in the consensus.
AUTO_RESOLVED_STDERR = """
[13:22:04 - MdlStore] Model r1041_e82_400bps_sup_v5.0.0 resolved from input file.
[13:22:04 - Predict] Setting tensorflow threads to 8.
"""

FALLBACK_STDERR = """
[13:22:04 - MdlStore] Could not resolve model from input data.
[13:22:04 - MdlStore] Using default consensus model r1041_e82_400bps_sup_v4.2.0.
[13:22:04 - Predict] Setting tensorflow threads to 8.
"""


class TestModelLine:
    def test_auto_resolved_model_is_named(self):
        facts = runner.parse_model_line(AUTO_RESOLVED_STDERR)
        assert facts["polish_model"] == "r1041_e82_400bps_sup_v5.0.0"
        assert facts["polish_model_auto_resolved"] is True

    def test_fallback_model_is_flagged(self):
        """A fallback succeeds with worse output and no error.

        The consensus alone cannot show it happened, so if this flag is
        wrong the run is undiagnosable after the fact.
        """
        facts = runner.parse_model_line(FALLBACK_STDERR)
        assert facts["polish_model"] == "r1041_e82_400bps_sup_v4.2.0"
        assert facts["polish_model_auto_resolved"] is False

    def test_unparseable_returns_empty(self):
        """A missed fact is a blank field; raising would discard a
        consensus that already exists on disk."""
        assert runner.parse_model_line("no model information here") == {}
        assert runner.parse_model_line("") == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_medaka_runner.py::TestModelLine -q
```

Expected: FAIL with `AttributeError: module 'app.pipelines.medaka_runner' has no attribute 'parse_model_line'`

- [ ] **Step 3: Write the implementation**

Add to `backend/app/pipelines/medaka_runner.py` (imports go at the top of the file):

```python
import re
```

```python
# Medaka names its model on stderr before inference starts. Two shapes
# matter and they mean different things:
#
#   Model r1041_e82_400bps_sup_v5.0.0 resolved from input file.
#   Using default consensus model r1041_e82_400bps_sup_v4.2.0.
#
# The first means basecaller metadata was present and read. The second
# means it was not, and Medaka fell back to a legacy default -- succeeding,
# with worse output, and no error anywhere. Nothing in the resulting
# consensus reveals which happened, which is the whole reason these facts
# are recorded.
_RESOLVED_RE = re.compile(r"Model\s+(\S+)\s+resolved from", re.IGNORECASE)
_DEFAULT_RE = re.compile(r"[Uu]sing default\s+\S*\s*model\s+(\S+)")


def parse_model_line(text: str) -> dict:
    """`polish_model` facts from Medaka's own stderr.

    Returns {} for anything unparseable rather than raising, the same
    posture `polypolish_runner.parse_polish_stderr` takes. The cost of a
    missed fact is a blank field; the cost of raising is discarding a
    consensus that already exists on disk.

    The fallback branch is checked first. A run that falls back can also
    mention resolution in a nearby line, and reporting such a run as
    auto-resolved would hide exactly the case these facts exist to expose.
    """
    fallback = _DEFAULT_RE.search(text)
    if fallback:
        return {
            "polish_model": fallback.group(1).rstrip("."),
            "polish_model_auto_resolved": False,
        }

    resolved = _RESOLVED_RE.search(text)
    if resolved:
        return {
            "polish_model": resolved.group(1).rstrip("."),
            "polish_model_auto_resolved": True,
        }

    return {}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_medaka_runner.py -q
```

Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/medaka_runner.py backend/tests/pipelines/test_medaka_runner.py
git commit -m "feat(pipelines): record which model medaka resolved"
```

---

### Task 3: `count_changed_positions`

**Files:**
- Modify: `backend/app/pipelines/medaka_runner.py`
- Test: `backend/tests/pipelines/test_medaka_runner.py`

**Interfaces:**
- Consumes: `medaka_runner` from Tasks 1–2.
- Produces: `count_changed_positions(draft: Path, consensus: Path) -> dict` returning keys `polish_changed_positions: int`, `polish_length_delta: int`, `polish_contigs_compared: int`, `polish_contigs_unmatched: int`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_medaka_runner.py`:

```python
def _write_fasta(path: Path, records: list[tuple[str, str]]) -> Path:
    """Write records as FASTA, wrapping at 60 columns.

    Line wrapping is deliberate: a comparison implemented per-line rather
    than per-sequence passes on single-line fixtures and fails on real
    tool output, which always wraps.
    """
    with open(path, "w") as fh:
        for name, seq in records:
            fh.write(f">{name}\n")
            for i in range(0, len(seq), 60):
                fh.write(seq[i : i + 60] + "\n")
    return path


class TestChangedPositions:
    def test_identical_sequences_report_zero(self, tmp_path):
        seq = "ACGT" * 50
        draft = _write_fasta(tmp_path / "d.fasta", [("ctg1", seq)])
        cons = _write_fasta(tmp_path / "c.fasta", [("ctg1", seq)])

        facts = runner.count_changed_positions(draft, cons)

        assert facts["polish_changed_positions"] == 0
        assert facts["polish_contigs_compared"] == 1

    def test_known_substitutions_are_recovered_exactly(self, tmp_path):
        """The count is the evidence that polishing did anything.

        Medaka prints no tally of its own, so if this number is wrong there
        is nothing else on the object to contradict it.
        """
        seq = list("ACGT" * 50)
        draft = _write_fasta(tmp_path / "d.fasta", [("ctg1", "".join(seq))])
        for pos in (10, 42, 99):
            seq[pos] = "A" if seq[pos] != "A" else "C"
        cons = _write_fasta(tmp_path / "c.fasta", [("ctg1", "".join(seq))])

        facts = runner.count_changed_positions(draft, cons)

        assert facts["polish_changed_positions"] == 3

    def test_length_change_is_reported_separately(self, tmp_path):
        """An indel is not a substitution count.

        Folding a length change into `polish_changed_positions` would make
        a one-base insertion look like every downstream base changed.
        """
        draft = _write_fasta(tmp_path / "d.fasta", [("ctg1", "ACGT" * 50)])
        cons = _write_fasta(tmp_path / "c.fasta", [("ctg1", "ACGT" * 50 + "AAA")])

        facts = runner.count_changed_positions(draft, cons)

        assert facts["polish_length_delta"] == 3

    def test_contig_missing_from_consensus_does_not_raise(self, tmp_path):
        """Degrade to a visible count, never to an exception.

        The facts exist to make failures visible; a parser that raises
        would discard a consensus that is already on disk.
        """
        draft = _write_fasta(
            tmp_path / "d.fasta", [("ctg1", "ACGT" * 20), ("ctg2", "TTTT" * 20)]
        )
        cons = _write_fasta(tmp_path / "c.fasta", [("ctg1", "ACGT" * 20)])

        facts = runner.count_changed_positions(draft, cons)

        assert facts["polish_contigs_unmatched"] == 1
        assert facts["polish_contigs_compared"] == 1

    def test_multiline_wrapping_does_not_affect_the_count(self, tmp_path):
        seq = "ACGTACGTGG" * 30
        draft = _write_fasta(tmp_path / "d.fasta", [("ctg1", seq)])
        changed = seq[:5] + ("A" if seq[5] != "A" else "C") + seq[6:]
        cons = _write_fasta(tmp_path / "c.fasta", [("ctg1", changed)])

        facts = runner.count_changed_positions(draft, cons)

        assert facts["polish_changed_positions"] == 1

    def test_header_description_is_ignored_when_matching(self, tmp_path):
        """Medaka appends its own description to contig headers.

        Matching on the full header line would find zero shared contigs and
        silently report a polish that changed nothing.
        """
        seq = "ACGT" * 40
        draft = _write_fasta(tmp_path / "d.fasta", [("ctg1", seq)])
        cons = _write_fasta(tmp_path / "c.fasta", [("ctg1 medaka consensus", seq)])

        facts = runner.count_changed_positions(draft, cons)

        assert facts["polish_contigs_compared"] == 1
        assert facts["polish_contigs_unmatched"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_medaka_runner.py::TestChangedPositions -q
```

Expected: FAIL with `AttributeError: module 'app.pipelines.medaka_runner' has no attribute 'count_changed_positions'`

- [ ] **Step 3: Write the implementation**

Add to `backend/app/pipelines/medaka_runner.py`:

```python
def _read_fasta(path: Path) -> dict[str, str]:
    """Contig name -> sequence, uppercased.

    Keyed on the first whitespace-delimited token of the header, not the
    whole line: Medaka appends its own description to headers, and matching
    on the full line would find zero shared contigs and report a polish
    that changed nothing.

    Same streaming shape `gc_tracks.py` uses. Uppercased because soft-
    masking is a claim about repeats, not about bases, and a draft that
    disagrees with the consensus only in case has not been polished.
    """
    contigs: dict[str, str] = {}
    name: str | None = None
    buf: list[str] = []

    with open(path, errors="replace") as fh:
        for line in fh:
            stripped = line.rstrip("\n\r")
            if stripped.startswith(">"):
                if name is not None:
                    contigs[name] = "".join(buf)
                parts = stripped[1:].split()
                name = parts[0] if parts else ""
                buf = []
            elif name is not None:
                buf.append(stripped.strip().upper())

    if name is not None:
        contigs[name] = "".join(buf)
    return contigs


def count_changed_positions(draft: Path, consensus: Path) -> dict:
    """How much the consensus differs from the draft it was built from.

    Medaka, unlike Polypolish, prints no per-contig tally -- it writes a
    consensus and stops. Without this number a run that changed nothing
    would be indistinguishable from one that corrected a thousand errors,
    and "polishing complete" would be the only evidence on the object.

    **Alignment-free by design.** An aligner in the fact-gathering path
    would be a second failure surface for a number that exists to make
    failures visible. Medaka preserves contig identity and order, so a
    name-keyed comparison is well-defined. Where a contig's length changed,
    the substitutions over the shared prefix are still counted and the
    difference is reported as `polish_length_delta` rather than being
    forced into a substitution count that would be meaningless -- a
    one-base insertion would otherwise read as every downstream base having
    changed.

    A contig in the draft with no counterpart in the consensus is counted
    in `polish_contigs_unmatched` rather than raising. Degrading to a
    visible number beats discarding a consensus that is already on disk.
    """
    try:
        draft_contigs = _read_fasta(draft)
        consensus_contigs = _read_fasta(consensus)
    except OSError:
        return {}

    changed = 0
    delta = 0
    compared = 0
    unmatched = 0

    for name, draft_seq in draft_contigs.items():
        polished_seq = consensus_contigs.get(name)
        if polished_seq is None:
            unmatched += 1
            continue
        compared += 1
        delta += len(polished_seq) - len(draft_seq)
        changed += sum(
            1 for a, b in zip(draft_seq, polished_seq, strict=False) if a != b
        )

    return {
        "polish_changed_positions": changed,
        "polish_length_delta": delta,
        "polish_contigs_compared": compared,
        "polish_contigs_unmatched": unmatched,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_medaka_runner.py -q
```

Expected: PASS, 15 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/medaka_runner.py backend/tests/pipelines/test_medaka_runner.py
git commit -m "feat(pipelines): count positions medaka changed in the draft"
```

---

### Task 4: Install Medaka into the image

**Files:**
- Create: `backend/scripts/install-medaka.sh`
- Modify: `backend/Dockerfile` (append a new layer after the SPAdes layer, around line 275)
- Modify: `backend/app/config.py:179` area (add beside `polypolish_path`)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `/opt/medaka/env/bin/medaka_consensus` on PATH; `settings.medaka_path`.

- [ ] **Step 1: Write the install script**

Create `backend/scripts/install-medaka.sh`:

```sh
#!/bin/sh
# Install Medaka, ONT's neural-network consensus tool, into the image.
#
# Bioconda is the right distribution here for the same reasons it is for
# Clair3: there is no Debian package, and building from source needs a
# pinned PyTorch toolchain. micromamba gives us the conda package without
# dragging a full conda installation into the image -- the binary is
# downloaded, used once, and deleted.
#
# Unlike Polypolish, this installs on arm64 too. bioconda publishes
# linux-aarch64 builds of medaka (2.0.1 through 2.2.2, checked 2026-08-18),
# and since Polypolish is x86-64-only, Medaka is the *only* polishing path
# available on Apple Silicon.
#
# pytorch-cpu is pinned deliberately and must stay pinned. conda-forge's
# bare `pytorch` resolves preferentially to CUDA builds, which pull libtorch
# in at roughly 885MB compressed against roughly 61MB for the CPU build
# (both measured from the conda-forge index on 2026-08-18). Nothing errors
# if this pin is dropped -- the image simply grows by about a gigabyte to
# ship CUDA kernels into a container that has no GPU and never asks for one.
# This is the same shape as the flye-samtools shim: a one-line install
# detail whose omission produces no error and a badly wrong result.

set -eu

MEDAKA_VERSION="${MEDAKA_VERSION:-2.2.2}"
INSTALL_DIR="/opt/medaka"

# micromamba publishes per-arch builds; picking the wrong one yields an
# "exec format error" that reads like a corrupt download.
case "$(uname -m)" in
    aarch64|arm64) MAMBA_ARCH="linux-aarch64" ;;
    x86_64|amd64)  MAMBA_ARCH="linux-64" ;;
    *)
        echo "ERROR: unsupported architecture $(uname -m) for Medaka" >&2
        exit 1
        ;;
esac

mkdir -p "${INSTALL_DIR}"

# Download to a file and check it before unpacking -- piping curl into tar
# hides which half failed. Same helper and same reasoning as
# install-clair3.sh.
fetch() {
    url="$1"
    dest="$2"
    if ! curl -fsSL --retry 3 --retry-delay 2 -o "${dest}" "${url}"; then
        echo "ERROR: download failed: ${url}" >&2
        exit 1
    fi
    if [ ! -s "${dest}" ]; then
        echo "ERROR: downloaded an empty file: ${url}" >&2
        exit 1
    fi
}

echo "Installing micromamba (${MAMBA_ARCH})..."
fetch "https://micro.mamba.pm/api/micromamba/${MAMBA_ARCH}/latest" /tmp/micromamba.tar.bz2
tar -xj -C /tmp bin/micromamba < /tmp/micromamba.tar.bz2
rm -f /tmp/micromamba.tar.bz2

echo "Creating Medaka ${MEDAKA_VERSION} environment..."
/tmp/bin/micromamba create -y -p "${INSTALL_DIR}/env" \
    -c conda-forge -c bioconda \
    "medaka=${MEDAKA_VERSION}" \
    "pytorch-cpu=2.9.*"

rm -rf /tmp/bin/micromamba
# The conda env carries package caches and static archives nothing needs.
rm -rf "${INSTALL_DIR}/env/pkgs" "${INSTALL_DIR}/env/conda-meta"

# Fail the build here rather than at first job. A probe that finds nothing
# on PATH reads as a broken install to the user; a build that never
# produced the binary should say so while someone is watching.
"${INSTALL_DIR}/env/bin/medaka" --version

# Guard the pin. If a future dependency bump reintroduces a CUDA torch, the
# build fails here rather than silently shipping ~1GB of unusable kernels.
if ls "${INSTALL_DIR}/env/lib/python3."*/site-packages/torch/lib/libtorch_cuda* >/dev/null 2>&1; then
    echo "ERROR: a CUDA build of torch was installed; pytorch-cpu pin failed" >&2
    exit 1
fi
```

- [ ] **Step 2: Add the Dockerfile layer**

In `backend/Dockerfile`, immediately after the SPAdes layer (the block ending with `/srv/scripts/install-spades.sh`), add:

```dockerfile
# --- Medaka ----------------------------------------------------------------
#
# ONT's neural-network consensus tool, and the only polishing path for
# long-read assemblies -- Polypolish above is short-read-only. Its own late
# layer for the same reason Clair3 and SPAdes are late: it is large (medaka
# plus a CPU torch) and slow, and an edit anywhere above should not
# reinstall it.
#
# Installed on both architectures, unlike Polypolish: bioconda ships
# linux-aarch64 builds, which makes this the only polisher available on
# Apple Silicon. See scripts/install-medaka.sh for why pytorch-cpu is
# pinned there, and
# docs/superpowers/specs/2026-08-18-medaka-long-read-polishing-design.md.
ARG MEDAKA_VERSION=2.2.2
COPY scripts/install-medaka.sh /srv/scripts/install-medaka.sh
RUN chmod +x /srv/scripts/install-medaka.sh \
    && MEDAKA_VERSION="${MEDAKA_VERSION}" \
       /srv/scripts/install-medaka.sh
ENV PATH="/opt/medaka/env/bin:${PATH}"
```

- [ ] **Step 3: Add the config setting**

In `backend/app/config.py`, beside `polypolish_path` (line ~179), add:

```python
    # Two settings rather than one derived from the other. `medaka_path` is
    # the wrapper script jobs exec; `medaka_binary_path` is the sibling
    # binary the probe versions, because the wrapper has no --version of its
    # own and would fall through to its usage block. Deriving one from the
    # other by string surgery was the first draft and is exactly the kind of
    # thing that breaks silently when a path changes.
    #
    # Absolute rather than bare names: medaka lives in its own conda prefix
    # so its pinned torch and its vendored minimap2/samtools stay out of the
    # image's own PATH resolution. The ENV in the Dockerfile puts the prefix
    # on PATH; these stay absolute so a probe does not depend on that
    # ordering.
    medaka_path: str = "/opt/medaka/env/bin/medaka_consensus"
    medaka_binary_path: str = "/opt/medaka/env/bin/medaka"
```

- [ ] **Step 4: Build the image and verify the binary runs**

```bash
docker build -f backend/Dockerfile -t bioflow-medaka-check backend
```

Then confirm the install and that the pin held:

```bash
docker run --rm bioflow-medaka-check sh -c "medaka --version && du -sh /opt/medaka"
```

Expected: a version line printing `medaka 2.2.2`, and a total size on the order of a few hundred MB — **not** multiple GB. A multi-GB figure means the `pytorch-cpu` pin did not take.

- [ ] **Step 5: Commit**

```bash
git add backend/scripts/install-medaka.sh backend/Dockerfile backend/app/config.py
git commit -m "feat(pipelines): install medaka on both architectures"
```

---

### Task 5: Tool probe and metadata

**Files:**
- Modify: `backend/app/pipelines/tools.py` (probe beside `polypolish()` ~line 664; `TOOL_META` entry beside `"polypolish"` ~line 2107)
- Modify: `backend/app/pipelines/sources.py`
- Test: `backend/tests/pipelines/test_tools_metadata.py` (existing `test_every_tool_is_documented` covers it)

**Interfaces:**
- Consumes: `settings.medaka_path` from Task 4.
- Produces: `tools.medaka() -> Tool`; `TOOL_META["medaka"]`.

- [ ] **Step 1: Add the probe**

In `backend/app/pipelines/tools.py`, after `polypolish()`:

```python
@lru_cache(maxsize=1)
def medaka() -> Tool:
    """Medaka, ONT's neural-network consensus tool.

    Probes `medaka` rather than `medaka_consensus`: the wrapper script is
    what jobs invoke, but it takes no `--version` of its own and would fall
    through to its usage block, which `_clean_version` would then scrape a
    line of into the version field -- the same trap clair3() documents.
    `medaka --version` prints "medaka 2.2.2" and exits zero.

    No arm64 special-casing, unlike polypolish(): bioconda ships
    linux-aarch64 builds, so this is the one polisher that works on Apple
    Silicon.
    """
    return _probe("medaka", settings.medaka_binary_path, ["--version"])
```

The probe versions `medaka_binary_path` while the handler execs
`medaka_path`. They are two settings rather than one derived from the other
because string surgery on a path breaks silently when the path changes.

- [ ] **Step 2: Add the TOOL_META entry**

In `backend/app/pipelines/tools.py`, beside `"polypolish"`:

```python
    "medaka": ToolMeta(
        pipelines=(PipelineType.REFERENCE_ASSEMBLY,),
        one_liner="Neural-network polishing of long-read assemblies",
        summary=(
            "Corrects residual base errors in a long-read assembly using "
            "the long reads it was built from. A neural network trained on "
            "ONT basecaller output predicts the true consensus from the "
            "pileup, which is what makes it effective on the homopolymer "
            "runs long-read assemblers systematically get wrong -- the "
            "error class short-read polishers cannot help with when no "
            "short reads exist."
        ),
        strengths=(
            "The only polishing path for a project with no short reads",
            "Trained per basecaller model, so it corrects the specific "
            "error profile of the chemistry that produced the reads",
            "Performs its own alignment with a model-appropriate minimap2 "
            "preset, so there is no aligner choice to get wrong",
        ),
        homepage="https://github.com/nanoporetech/medaka",
        repository="https://github.com/nanoporetech/medaka",
        # Medaka has no accompanying paper. Upstream asks that the software
        # be cited directly, so this is the repository rather than a
        # fabricated reference -- citation_url is deliberately left empty
        # for the same reason.
        citation=(
            "Oxford Nanopore Technologies. medaka: sequence correction "
            "provided by ONT Research. https://github.com/nanoporetech/medaka"
        ),
        # From the repository's own LICENCE.md, checked 2026-08-18 rather
        # than recalled. This is *not* an OSI-standard license -- it is
        # ONT's own -- and it is recorded verbatim rather than normalized to
        # something familiar-looking. A page that reads as authoritative
        # saying "MIT" here would be worse than saying nothing.
        license="Oxford Nanopore Technologies PLC. Public License Version 1.0",
        usage=(
            "BioFlow runs medaka_consensus over a draft assembly and the "
            "long reads it was built from, storing the polished consensus "
            "as a new object beside the draft rather than replacing it. "
            "Medaka performs its own alignment internally using a minimap2 "
            "preset chosen by the model, so no aligner is configured here. "
            "The model is normally resolved from basecaller metadata in the "
            "reads; when that metadata is absent Medaka falls back to a "
            "default model without erroring, so the resolved model and "
            "whether it was auto-selected are both recorded as facts on the "
            "output. ONT's bacterial-methylation model is available as an "
            "opt-in at launch and is a research release."
        ),
    ),
```

- [ ] **Step 3: Add the sources.py entry**

`sources.py` backs `/help/sources` and has its own completeness test. Add an entry for medaka mirroring the shape of the existing `polypolish` entry in that file — read the neighbouring entry and match its field names exactly, using the homepage and license strings above.

- [ ] **Step 4: Run the metadata tests**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tools_metadata.py -q
```

Expected: PASS, including `test_every_tool_is_documented`.

- [ ] **Step 5: Verify the probe against the real image**

```bash
docker run --rm bioflow-medaka-check python -c "from app.pipelines import tools; print(tools.medaka())"
```

Expected: a `Tool` with a non-None `path`, a version of `2.2.2`, and `error=None`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/pipelines/tools.py backend/app/pipelines/sources.py
git commit -m "feat(pipelines): probe and document medaka"
```

---

### Task 6: `is_long_read` and `long_read_sets`

**Files:**
- Modify: `backend/app/services/reference_assembly.py` (after `short_read_sets`, ~line 355)
- Test: `backend/tests/services/test_reference_assembly_foundation.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `is_long_read(obj: DataObject) -> bool`; `long_read_sets(objects: list[DataObject]) -> list[list[DataObject]]`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/services/test_reference_assembly_foundation.py`, matching that file's existing fixture helpers for building a `DataObject`:

```python
class TestIsLongRead:
    def test_nanopore_platform_is_long(self):
        obj = _fastq(facts={"qc_platform": "OXFORD_NANOPORE"})
        assert reference_assembly.is_long_read(obj) is True

    def test_illumina_platform_is_not_long(self):
        obj = _fastq(facts={"qc_platform": "ILLUMINA"})
        assert reference_assembly.is_long_read(obj) is False

    def test_nanopore_with_short_chemistry_is_still_long(self):
        """Platform beats chemistry, the same way it does in is_short_read.

        A real MinION run (ERR16145610.fastq) infers `short` chemistry
        because chemistry is inferred from read *lengths*. Trusting that
        would send ONT reads to a short-read polisher.
        """
        obj = _fastq(
            facts={"qc_platform": "OXFORD_NANOPORE", "qc_read_chemistry": "short"}
        )
        assert reference_assembly.is_long_read(obj) is True

    def test_unknown_platform_and_chemistry_is_not_long(self):
        """Unknown stays unknown -- deliberately unlike is_short_read.

        is_short_read counts an unlabelled FASTQ as short because
        _qc_platform defaults to ILLUMINA. Inheriting that default here
        would make every unlabelled file look like a Medaka candidate too,
        and both cards would fire on data neither can vouch for.
        """
        obj = _fastq(facts={})
        assert reference_assembly.is_long_read(obj) is False

    def test_non_fastq_is_not_long(self):
        """Not the negation of is_short_read.

        `not is_short_read(protein_fasta)` is True; that is the protein.faa
        mistake in a new costume.
        """
        obj = _fasta()
        assert reference_assembly.is_long_read(obj) is False

    def test_unknown_platform_with_long_chemistry_is_long(self):
        obj = _fastq(facts={"qc_read_chemistry": "long"})
        assert reference_assembly.is_long_read(obj) is True


class TestLongReadSets:
    def test_groups_one_nanopore_file_as_one_set(self):
        objs = [_fastq(facts={"qc_platform": "OXFORD_NANOPORE"})]
        assert len(reference_assembly.long_read_sets(objs)) == 1

    def test_excludes_short_reads(self):
        objs = [_fastq(facts={"qc_platform": "ILLUMINA"})]
        assert reference_assembly.long_read_sets(objs) == []
```

Note: `_fastq()` / `_fasta()` are that file's existing helpers. If their names differ, use whatever the file already defines rather than adding new ones — and if a helper takes no `facts` argument, extend it rather than constructing `DataObject` inline in each test.

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/services/test_reference_assembly_foundation.py -k "LongRead" -q
```

Expected: FAIL with `AttributeError: module 'app.services.reference_assembly' has no attribute 'is_long_read'`

- [ ] **Step 3: Write the implementation**

Add to `backend/app/services/reference_assembly.py`, after `short_read_sets`:

```python
# --- Long reads for Medaka polishing ---------------------------------------


def is_long_read(obj: DataObject) -> bool:
    """Whether a FASTQ is long-read data.

    **Written positively, and deliberately not `not is_short_read(obj)`.**
    That negation is the single most tempting wrong edit here.
    `is_short_read` returns False for a protein FASTA, for a FASTQ whose
    platform is unknown and whose chemistry is not `short`, and for genuine
    long reads alike -- so negating it would hand Medaka every non-short
    object in the project. That is the `protein.faa` mistake in a new
    costume.

    Precedence is `is_short_read`'s, for its reasons: a known long-read
    platform is decisive regardless of inferred chemistry, because
    chemistry is inferred from read *lengths* and a nanopore run carrying
    short reads infers `short` (`ERR16145610.fastq`, a real MinION run).
    Chemistry only votes when the platform is unknown.

    **Unknown stays unknown**, which is a deliberate asymmetry with the
    sibling function. `is_short_read` counts an unlabelled FASTQ as short,
    because `_qc_platform` defaults to ILLUMINA and that module declines to
    second-guess the default -- without it, an uploaded Illumina FASTQ with
    no metadata would never get a polish card. Inheriting that default here
    would invert its meaning: every unlabelled file would look like a Medaka
    candidate as well, and the two polish cards would both fire on data
    neither can vouch for. The residual cost is that an uploaded ONT FASTQ
    with no metadata gets no Medaka card until QC runs -- a missing offer
    rather than a wrong run.
    """
    if obj.format.kind is not FormatKind.FASTQ:
        return False

    # Lazily imported for the same circularity reason is_short_read
    # documents: pipeline_service imports this module.
    from app.services.pipeline_service import _qc_platform

    platform = _qc_platform(obj)
    if platform in LONG_READ_PLATFORMS:
        return True
    if platform in SHORT_READ_PLATFORMS:
        return False

    return (obj.facts or {}).get("qc_read_chemistry") == "long"


def long_read_sets(objects: list[DataObject]) -> list[list[DataObject]]:
    """The ready long-read sets among a project's objects.

    Same grouping as `short_read_sets` -- a set is what one polish run
    consumes -- though in practice a long-read set is a single file, since
    ONT and PacBio data is unpaired. `group_read_sets` is reused rather
    than special-cased so a mate-linked long-read pair, if one ever
    appears, is one candidate rather than two.
    """
    ready = [
        obj
        for obj in objects
        if obj.status is ObjectStatus.READY and is_long_read(obj)
    ]
    return group_read_sets(ready)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/services/test_reference_assembly_foundation.py -q
```

Expected: PASS.

- [ ] **Step 5: Check the rule against the real database**

Per `CLAUDE.md`'s "check a rule against the real database" note, and specifically because `is_short_read`'s docstring records that a fixture-only rule was wrong here before. From the **main checkout root**, not the worktree:

```bash
docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models.object import DataObject
from app.services import reference_assembly as ra

async def main():
    await connect_to_mongo()
    for obj in await DataObject.find_all().to_list():
        if obj.format.kind.value != 'fastq':
            continue
        print(obj.name, (obj.facts or {}).get('qc_platform'),
              (obj.facts or {}).get('qc_read_chemistry'),
              'long=', ra.is_long_read(obj), 'short=', ra.is_short_read(obj))
asyncio.run(main())
"
```

Expected: `ERR16145610.fastq` (a MinION run whose inferred chemistry is `short`) reports `long=True short=False`. No object may report `long=True short=True`. Record the output in the PR description.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/reference_assembly.py backend/tests/services/test_reference_assembly_foundation.py
git commit -m "feat(services): identify long-read sets for polishing"
```

---

### Task 7: The `polish_long_assembly` handler

**Files:**
- Modify: `backend/app/queue/reference_assembly_handlers.py` (after `polish_assembly`, ~line 345)
- Modify: `backend/app/queue/results.py:3005` (register the applier)

**Interfaces:**
- Consumes: `medaka_runner` (Tasks 1–3), `tools.medaka` (Task 5).
- Produces: handler `polish_long_assembly`, returning `{"job_id", "draft_object_id", "reads_object_id", "output": {"tmp_path", "name"}, "facts"}`.

- [ ] **Step 1: Add `polish_tool` to the existing Polypolish handler**

In `polish_assembly`, beside the other fact assignments (~line 334):

```python
    facts["polish_tool"] = "polypolish"
```

With two polishers writing the same fact namespace onto the same object role, a run that does not name its tool is unreadable.

- [ ] **Step 2: Write the handler**

Add to `backend/app/queue/reference_assembly_handlers.py`, importing `medaka_runner` alongside the existing pipeline imports at line 33:

```python
# Medaka's cost is inference, which is CPU-bound here by construction of
# the pytorch-cpu pin. Unlike polish_assembly -- where the comment
# correctly notes peak RSS describes bwa-mem2's index and therefore scales
# with the *draft* -- Medaka's peak scales with batch size and model and is
# near-flat in draft size, while runtime scales with depth times draft
# length. A memory-model fit that assumes the Polypolish shape is wrong in
# both directions.
POLISH_LONG_LEASE_SECONDS = 8 * 3600


@handler(
    "polish_long_assembly",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=8, mem_mb=16384, io=IoClass.HEAVY),
    # Deterministic tool, deterministic input: a retry fails identically.
    # Same reasoning as polish_assembly.
    max_attempts=1,
)
def polish_long_assembly(ctx: JobContext) -> dict:
    """Correct residual base errors in a draft assembly using long reads.

    One stage, not Polypolish's five: `medaka_consensus` runs minimap2,
    `medaka inference` and `medaka sequence` itself. That is why no aligner
    is resolved here and no alignment command is built -- Medaka picks its
    minimap2 preset from the model it resolves, and overriding that would
    replace a model-dependent choice with a fixed guess.

    The alignment being internal is also what makes provenance answerable
    "by construction" rather than by validation, exactly as it is for
    `polish_assembly`: the reads are aligned to this draft inside the job,
    so the alignment target cannot be anything else.

    Two facts are recorded that have no Polypolish counterpart, and both
    exist because Medaka fails quietly rather than loudly. It resolves its
    network from basecaller metadata in the reads and falls back to a legacy
    default when it finds none -- succeeding, with worse output, and no
    error -- so `polish_model` and `polish_model_auto_resolved` are what
    make that diagnosable afterwards. And it prints no per-contig tally the
    way Polypolish does, so `polish_changed_positions` is computed from the
    draft and the consensus rather than parsed.
    """
    tool = tools.require(tools.medaka())

    work = _prepare_workdir(ctx, "polish_long")

    draft = _resolve_input(ctx.payload, "draft")
    draft = _named_link(work, draft, ctx.payload.get("draft_name"))

    if ctx.payload.get("reads_object_id") is None:
        raise PermanentError("polish_long_assembly requires a long-read file")
    reads = _resolve_input(ctx.payload, "reads")
    reads = _named_link(work, reads, ctx.payload.get("reads_name"))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.extend_lease(POLISH_LONG_LEASE_SECONDS)

    threads = max(1, int(ctx.payload.get("threads") or 8))
    bacteria = bool(ctx.payload.get("bacteria"))

    outdir = work / "medaka"
    ctx.progress(phase="polishing", pct=0.1, message="polishing with medaka")

    # Medaka announces its resolved model on stderr before inference. Same
    # collector shape polish_assembly uses for Polypolish's summary:
    # run_subprocess merges stderr into the line stream, and on_line is the
    # only way to see those lines rather than only writing them to log_path.
    output_lines: list[str] = []
    code = run_subprocess(
        ctx,
        medaka_runner.build_consensus_command(
            medaka_path=tool.path,
            draft=draft,
            reads=reads,
            outdir=outdir,
            threads=threads,
            bacteria=bacteria,
        ),
        log_path=str(log_path),
        on_line=output_lines.append,
    )
    if code != 0:
        raise _failure(code, log_path, "medaka_consensus")

    consensus = outdir / medaka_runner.CONSENSUS_FILENAME
    if not consensus.exists() or consensus.stat().st_size == 0:
        raise RetryableError("medaka exited successfully but wrote no consensus")

    facts = medaka_runner.parse_model_line("\n".join(output_lines))
    facts.update(medaka_runner.count_changed_positions(draft, consensus))
    facts["polish_tool"] = "medaka"
    facts["polish_tool_version"] = tool.version
    facts["polish_bacteria_mode"] = bacteria
    facts["polish_read_files"] = 1

    ctx.progress(phase="done", pct=1.0, message="polishing complete")
    log.info(
        "polish_long_finished",
        job_id=ctx.job_id,
        changed=facts.get("polish_changed_positions"),
        model=facts.get("polish_model"),
        auto=facts.get("polish_model_auto_resolved"),
    )

    return {
        "job_id": ctx.job_id,
        "draft_object_id": ctx.payload.get("draft_object_id"),
        "reads_object_id": ctx.payload.get("reads_object_id"),
        "output": {"tmp_path": str(consensus), "name": "consensus.fasta"},
        "facts": facts,
    }
```

- [ ] **Step 3: Register the results applier**

In `backend/app/queue/results.py`, beside `"polish_assembly": _apply_polish_assembly` (line 3005):

```python
    "polish_long_assembly": _apply_polish_assembly,
```

`_apply_polish_assembly` is already generic over the facts dict and reads `draft_object_id`, `reads_object_id`, and `mate_object_id` — all of which this handler's result carries or omits harmlessly. It needs no changes.

- [ ] **Step 4: Verify the handler is registered**

```bash
./backend/run-worktree-tests.sh tests/queue -q -k "handler or registry"
```

Expected: PASS. The handler registry test should now see `polish_long_assembly`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/reference_assembly_handlers.py backend/app/queue/results.py
git commit -m "feat(queue): polish long-read assemblies with medaka"
```

---

### Task 8: `launch_polish_long` and the API route

**Files:**
- Modify: `backend/app/services/pipeline_service.py` (after `launch_polish`, ~line 5640)
- Modify: `backend/app/api/v1/pipelines.py` (after the `/polish` route, ~line 1612)

**Interfaces:**
- Consumes: `reference_assembly.long_read_sets` (Task 6), handler `polish_long_assembly` (Task 7).
- Produces: `launch_polish_long(*, draft_object_id, owner, reads_object_id=None, bacteria=False, resource_override=False) -> Job`; `POST /pipelines/polish-long`.

- [ ] **Step 1: Add the launcher**

In `backend/app/services/pipeline_service.py`, after `launch_polish`. Add `POLISH_LONG_MEM_MB = 16384` beside `POLISH_MEM_MB` (line ~1413):

```python
async def launch_polish_long(
    *,
    draft_object_id: PydanticObjectId,
    owner: str,
    reads_object_id: PydanticObjectId | None = None,
    bacteria: bool = False,
    resource_override: bool = False,
) -> Job:
    """Queue a Medaka run: long reads correcting a draft assembly.

    Same provenance shape as `launch_polish` -- the handler aligns the reads
    to the draft itself, so the alignment target is correct by construction
    rather than by check. Unlike Polypolish, the aligner is not ours to
    name: Medaka resolves its own minimap2 preset from the model.

    Reads are resolved from the project when not named explicitly, and only
    when the choice is unambiguous. `reference_assembly.long_read_sets` is
    what decides which candidates are eligible, and there is no mate slot --
    ONT and PacBio data is unpaired.

    `bacteria` opts into ONT's bacterial-methylation model. It is a
    parameter rather than an inference: nothing in the object graph reliably
    says a draft is a bacterial isolate, and ONT ships that model as a
    research release.
    """
    from app.queue import queue
    from app.services import object_service, reference_assembly, run_service

    refuse_if_over_budget(
        declared_mb=POLISH_LONG_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    tool = tools.require(tools.medaka())

    draft = await object_service.get_object(draft_object_id, owner=owner)
    reference_assembly.check_draft_assembly(draft)

    if reads_object_id is None:
        candidates = reference_assembly.long_read_sets(
            await object_service.list_objects(
                draft.project_id, owner=owner, status=ObjectStatus.READY
            )
        )
        if not candidates:
            raise ValidationError(
                "Polishing with Medaka needs long reads, and this project "
                "has none",
                details={"draft_id": str(draft.id)},
            )
        if len(candidates) > 1:
            raise ValidationError(
                "This project has several long-read sets; name the one to "
                "polish with",
                details={
                    "draft_id": str(draft.id),
                    "candidates": [
                        [str(o.id) for o in group] for group in candidates
                    ],
                },
            )
        chosen = candidates[0][0]
    else:
        chosen = await object_service.get_object(reads_object_id, owner=owner)
        if not reference_assembly.is_long_read(chosen):
            raise ValidationError(
                f"{chosen.name!r} is not long-read data; Medaka corrects a "
                "draft using the long reads it was assembled from, and its "
                "models are trained on long-read error profiles",
                details={"object_id": str(chosen.id)},
            )

    draft_digest, draft_path = await _resolve_readable(draft)
    payload: dict = {
        "draft_object_id": str(draft.id),
        "draft_name": draft.name,
        "threads": 8,
        "bacteria": bacteria,
    }
    if draft_digest:
        payload["draft_sha256"] = draft_digest
    if draft_path:
        payload["draft_path"] = draft_path

    reads_digest, reads_path = await _resolve_readable(chosen)
    payload["reads_object_id"] = str(chosen.id)
    payload["reads_name"] = chosen.name
    if reads_digest:
        payload["reads_sha256"] = reads_digest
    if reads_path:
        payload["reads_path"] = reads_path

    run = await run_service.create_run(
        kind=RunKind.REFERENCE_ASSEMBLY,
        project_id=draft.project_id,
        label=f"Polish {draft.name} (Medaka)",
        inputs=[
            RunInput(
                object_id=draft.id,
                name=draft.name,
                role=RunInputRole.DRAFT_ASSEMBLY,
            ),
            RunInput(
                object_id=chosen.id,
                name=chosen.name,
                role=RunInputRole.READS,
            ),
        ],
        params={"threads": payload["threads"], "bacteria": bacteria},
        owner=owner,
        tool="medaka",
    )

    job = await queue.enqueue(
        "polish_long_assembly",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(
            cpu=8, mem_mb=POLISH_LONG_MEM_MB, io=IoClass.HEAVY
        ),
        max_attempts=1,
        dedup_key=f"polish_long:{draft.id}:{chosen.id}",
        project_id=draft.project_id,
        object_id=draft.id,
        resource_override=resource_override,
    )
    if job is None:
        await run_service.discard_run(run.id, owner=run.owner)
        raise ConflictError(
            "Medaka polishing is already queued or running for this assembly",
            details={"object_id": str(draft.id)},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.POLISH)
    log.info(
        "polish_long_launched",
        job_id=str(job.id),
        run_id=str(run.id),
        draft_id=str(draft.id),
        reads_id=str(chosen.id),
        bacteria=bacteria,
        tool_version=tool.version,
    )
    return job
```

- [ ] **Step 2: Add the API route**

In `backend/app/api/v1/pipelines.py`, after `launch_polish_route`:

```python
class PolishLongRequest(BaseModel):
    draft_object_id: PydanticObjectId
    # Optional: omitted, the launch resolves the project's one long-read set
    # and refuses when there is more than one. No mate slot -- ONT and PacBio
    # data is unpaired.
    reads_object_id: PydanticObjectId | None = None
    # ONT's bacterial-methylation model. A research release by upstream's own
    # labelling, so it is opt-in rather than inferred from the draft.
    bacteria: bool = False
    resource_override: bool = False


@router.post(
    "/polish-long", response_model=JobOut, status_code=status.HTTP_201_CREATED
)
async def launch_polish_long_route(
    body: PolishLongRequest, owner: OwnerDep
) -> JobOut:
    """Queue a Medaka run: long reads correcting a draft assembly.

    No alignment is supplied and none is configured. Medaka runs minimap2
    itself with a preset chosen by the model it resolves."""
    job = await pipeline_service.launch_polish_long(
        draft_object_id=body.draft_object_id,
        reads_object_id=body.reads_object_id,
        bacteria=body.bacteria,
        owner=owner,
        resource_override=body.resource_override,
    )
    return JobOut.of(job)
```

- [ ] **Step 3: Run the API and service tests**

```bash
./backend/run-worktree-tests.sh tests/api tests/services/test_pipeline_service.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/api/v1/pipelines.py
git commit -m "feat(api): launch medaka polishing from a draft and long reads"
```

---

### Task 9: Node type registration

**Files:**
- Modify: `backend/app/pipelines/node_types.py` (`_launch_polish_long` beside `_launch_polish` ~line 285; `NODE_TYPES` entry beside `"polish"` ~line 744)
- Test: `backend/tests/pipelines/test_node_types.py` (existing `TestExhaustiveness`)

**Interfaces:**
- Consumes: `pipeline_service.launch_polish_long` (Task 8).
- Produces: `NODE_TYPES["polish_long"]`.

- [ ] **Step 1: Add the launcher adapter**

In `backend/app/pipelines/node_types.py`, after `_launch_polish`:

```python
async def _launch_polish_long(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_polish_long(
        draft_object_id=inputs["draft"],
        owner=owner,
        reads_object_id=inputs.get("reads"),
        bacteria=bool(params.get("bacteria")),
    )
```

- [ ] **Step 2: Add the NodeTypeSpec**

Beside `"polish"`:

```python
    "polish_long": NodeTypeSpec(
        label="Polish (Medaka)",
        launch_name="pipeline_service.launch_polish_long",
        launch=_launch_polish_long,
        run_kind=RunKind.REFERENCE_ASSEMBLY,
        run_tool="medaka",
        inputs=(
            PortSpec(
                "draft",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
            # Optional: when unwired, the launcher auto-picks the project's
            # one unambiguous long-read set and refuses if there is more
            # than one. No mate port -- long-read data is unpaired.
            PortSpec("reads", PortType(format=FormatKind.FASTQ), required=False),
        ),
        outputs=(
            PortSpec(
                "polished",
                PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE),
            ),
        ),
    ),
```

- [ ] **Step 3: Run the whole exhaustiveness class, not one test**

Per `CLAUDE.md`: a fix that adds a spec entry can collide with one that adds an exclusion, and only the partition-completeness test catches it (#355, fixed in #366).

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py::TestExhaustiveness -v
```

Expected: PASS, including both `test_every_launch_function_is_classified` and `test_no_launcher_is_both_used_and_excluded`. `launch_polish_long` must appear in `NODE_TYPES` and **not** in `EXCLUDED_LAUNCHES`.

- [ ] **Step 4: Commit**

```bash
git add backend/app/pipelines/node_types.py
git commit -m "feat(pipelines): register the medaka polish node type"
```

---

### Task 10: The `polish_long` suggestion card

**Files:**
- Modify: `backend/app/services/suggestion_service.py` (card builder after `build_polish_card` ~line 1215; orchestrator `long_read_sets` beside `read_sets` ~line 2190; card tuple ~line 2056)
- Test: `backend/tests/services/test_suggestion_service.py`

**Interfaces:**
- Consumes: `reference_assembly.long_read_sets` (Task 6), `tools.medaka` (Task 5).
- Produces: `build_polish_long_card(obj, long_read_sets) -> SuggestionCard | None`, card kind `"polish_long"`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/services/test_suggestion_service.py`, matching that file's existing fixture helpers:

```python
class TestPolishLongCard:
    def test_unavailable_when_medaka_is_missing(self, monkeypatch):
        """Assert the UNAVAILABLE direction, per CLAUDE.md.

        The image ships tools installed, so an "available" assertion passes
        whether or not the patch worked. This is the direction that fails
        when the seam breaks.
        """
        monkeypatch.setattr(
            tools, "medaka", lambda: Tool("medaka", None, None, "not installed")
        )
        card = suggestion_service.build_polish_long_card(
            _ready_assembly(), [[_ont_fastq()]]
        )
        assert card.status is CardStatus.UNAVAILABLE
        assert "not installed" in card.reason

    def test_unavailable_with_no_long_reads(self):
        card = suggestion_service.build_polish_long_card(_ready_assembly(), [])
        assert card.status is CardStatus.UNAVAILABLE
        assert "long reads" in card.reason

    def test_unavailable_with_several_long_read_sets(self):
        """Ambiguity is unavailable, not a guess.

        Cards launch directly with the body they carry, so a card that
        picked one of several sets would silently polish with whichever it
        chose -- producing a plausible assembly that is quietly wrong.
        """
        card = suggestion_service.build_polish_long_card(
            _ready_assembly(), [[_ont_fastq()], [_ont_fastq()]]
        )
        assert card.status is CardStatus.UNAVAILABLE
        assert "2" in card.reason

    def test_available_with_exactly_one_long_read_set(self):
        card = suggestion_service.build_polish_long_card(
            _ready_assembly(), [[_ont_fastq()]]
        )
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/polish-long"

    def test_no_card_for_a_non_assembly(self):
        assert suggestion_service.build_polish_long_card(_fastq_obj(), []) is None


class TestPolishCardsDoNotCollide:
    """Success criterion 3: the two cards never offer a broken combination.

    They gate on mutually exclusive chemistry predicates, so this is a
    property of the structure rather than a rule anything enforces -- which
    is exactly why it is worth a test that would catch the structure
    changing.
    """

    def test_short_read_project_gets_polypolish_not_medaka(self):
        obj = _ready_assembly()
        assert (
            suggestion_service.build_polish_card(obj, [[_illumina_fastq()]]).status
            is CardStatus.AVAILABLE
        )
        assert (
            suggestion_service.build_polish_long_card(obj, []).status
            is CardStatus.UNAVAILABLE
        )

    def test_long_read_project_gets_medaka_not_polypolish(self):
        obj = _ready_assembly()
        assert (
            suggestion_service.build_polish_long_card(obj, [[_ont_fastq()]]).status
            is CardStatus.AVAILABLE
        )
        assert (
            suggestion_service.build_polish_card(obj, []).status
            is CardStatus.UNAVAILABLE
        )
```

Note: `_ready_assembly()`, `_ont_fastq()`, `_illumina_fastq()`, `_fastq_obj()` are that file's existing helpers or straightforward additions in its established style. Build an ONT FASTQ with `facts={"qc_platform": "OXFORD_NANOPORE"}` and an Illumina one with `facts={"qc_platform": "ILLUMINA"}`.

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -k "PolishLong or Collide" -q
```

Expected: FAIL with `AttributeError: ... has no attribute 'build_polish_long_card'`

- [ ] **Step 3: Write the card builder**

In `backend/app/services/suggestion_service.py`, after `build_polish_card`:

```python
def build_polish_long_card(obj, long_read_sets) -> SuggestionCard | None:
    """Long-read polishing of a draft assembly, by Medaka.

    The sibling of `build_polish_card`, and deliberately a separate card
    rather than a smarter version of that one. The two tools take different
    reads, produce different facts, and fail for different reasons; merging
    them would make the description, reason string, launch body and node
    ports all conditional, and would force a project holding both
    chemistries to have one tool chosen for it behind the user's back --
    the guess `build_polish_card`'s docstring exists to forbid.

    Keeping them separate is also what makes "never offer a broken
    combination" structural rather than enforced: the two cards gate on
    mutually exclusive predicates over the same read objects, so a
    short-read project sees Polypolish, an ONT project sees Medaka, and a
    hybrid project sees both as separate legitimate offers. There is no
    combining step to get wrong.

    Gated on the reads being long, for the mirror of the reason
    `build_polish_card` gates on the reads being short: Medaka's models are
    trained on long-read error profiles, so running it on Illumina data is
    meaningless rather than merely unusual.

    **Ambiguity is unavailable, not a guess** -- same rule, same reason.
    """
    if not reference_assembly._is_assembly_like(obj):
        return None
    if obj.status is not ObjectStatus.READY:
        return None

    title = "Polish assembly (long reads)"
    description = (
        "Correct residual base errors in this assembly using the long reads "
        "it was built from, with Medaka."
    )

    def unavailable(reason: str) -> SuggestionCard:
        return SuggestionCard(
            kind="polish_long",
            category="REFERENCE_ASSEMBLY",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=reason,
        )

    tool = tools.medaka()
    if not tool.available:
        return unavailable(tool.error or "Medaka is not installed.")

    if not long_read_sets:
        return unavailable(
            "Medaka polishing needs long reads, and this project has none."
        )
    if len(long_read_sets) > 1:
        return unavailable(
            f"This project has {len(long_read_sets)} long-read sets. "
            "Polishing needs a specific one, and picking for you could "
            "correct this assembly with the wrong sample's reads."
        )

    chosen = long_read_sets[0]
    body = {
        "draft_object_id": str(obj.id),
        "reads_object_id": str(chosen[0].id),
    }

    return SuggestionCard(
        kind="polish_long",
        category="REFERENCE_ASSEMBLY",
        title=title,
        description=description,
        why=f"Long reads: {', '.join(o.name for o in chosen)}.",
        status=CardStatus.AVAILABLE,
        launch={"endpoint": "/pipelines/polish-long", "body": body},
    )
```

- [ ] **Step 4: Wire the orchestrator**

Beside the existing `read_sets` resolution (~line 2190):

```python
    long_reads = None
    if project_objects is not None:
        # The long-read counterpart of `read_sets` above. Kept out of the
        # synchronous card builder the same way, and resolved separately
        # rather than derived from `read_sets` -- `long_read_sets` is not
        # the complement of `short_read_sets`, since an unlabelled file is
        # in neither.
        try:
            long_reads = reference_assembly.long_read_sets(project_objects)
        except Exception:  # noqa: BLE001 - a filter failure loses one card, not the grid
            long_reads = None
```

And in the card tuple list (~line 2056), beside the `polish` entry:

```python
    ("polish_long", lambda obj, ctx: build_polish_long_card(obj, ctx.long_reads)),
```

Add `long_reads` to the context object the tuple's `ctx` refers to, matching how `read_sets` is carried there.

- [ ] **Step 5: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat(services): offer medaka polishing for long-read projects"
```

---

### Task 11: End-to-end verification with planted errors

**Files:**
- Create: `backend/tests/pipelines/test_medaka_end_to_end.py`

**Interfaces:**
- Consumes: everything from Tasks 1–10.
- Produces: nothing consumed by later tasks.

This satisfies **R5** and success criterion 4: proof the polish changed positions, not merely that the job completed.

- [ ] **Step 1: Write the end-to-end test**

Create `backend/tests/pipelines/test_medaka_end_to_end.py`:

```python
"""Medaka actually corrects planted errors, not merely exits zero.

Mirrors what #23 did for Polypolish. The bar is that the polish *changed
the right positions* -- a test asserting completion would pass for a run
that returned the draft unmodified, which is precisely the failure
`polish_changed_positions` exists to expose.

Marked slow and skipped when medaka is not installed, so it runs in the
image and does not break a host-side collection.
"""

import subprocess

import pytest

from app.config import settings
from app.pipelines import medaka_runner, tools

pytestmark = pytest.mark.slow

# Substitutions only. `count_changed_positions` reports length changes
# separately, so an indel-carrying draft would make the equality assertion
# below ill-defined -- see R5 in the spec.
PLANTED = (150, 400, 900, 1500, 2100)


def _synthetic_genome(length: int = 3000) -> str:
    """A deterministic non-repetitive sequence.

    Seeded rather than random: a flaky genome makes a failure impossible to
    reproduce, and a repetitive one makes the aligner rather than the
    polisher the thing under test.
    """
    import random

    rng = random.Random(618)
    return "".join(rng.choice("ACGT") for _ in range(length))


def _plant(seq: str, positions) -> str:
    chars = list(seq)
    for pos in positions:
        chars[pos] = "A" if chars[pos] != "A" else "C"
    return "".join(chars)


@pytest.mark.skipif(not tools.medaka().available, reason="medaka not installed")
def test_medaka_corrects_planted_errors(tmp_path):
    truth = _synthetic_genome()
    draft_seq = _plant(truth, PLANTED)

    draft = tmp_path / "draft.fasta"
    draft.write_text(f">ctg1\n{draft_seq}\n")

    # Reads are generated from the *truth*, so the planted errors exist only
    # in the draft and Medaka has consistent evidence against every one.
    reads = tmp_path / "reads.fastq"
    with open(reads, "w") as fh:
        read_len, step = 500, 25
        n = 0
        for start in range(0, len(truth) - read_len, step):
            chunk = truth[start : start + read_len]
            fh.write(f"@read{n}\n{chunk}\n+\n{'I' * len(chunk)}\n")
            n += 1

    outdir = tmp_path / "out"
    argv = medaka_runner.build_consensus_command(
        medaka_path=settings.medaka_path,
        draft=draft,
        reads=reads,
        outdir=outdir,
        threads=2,
    )
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
    assert proc.returncode == 0, proc.stderr[-3000:]

    consensus = outdir / medaka_runner.CONSENSUS_FILENAME
    assert consensus.exists() and consensus.stat().st_size > 0

    facts = medaka_runner.count_changed_positions(draft, consensus)

    # The assertion that matters: positions changed, and the count is the
    # planted count. Completion alone is explicitly not the bar.
    assert facts["polish_changed_positions"] > 0
    assert facts["polish_changed_positions"] == len(PLANTED)

    # And the corrections went the right way -- a polish that changed the
    # planted positions to the wrong bases would satisfy the count above.
    polished = medaka_runner._read_fasta(consensus)["ctg1"]
    for pos in PLANTED:
        assert polished[pos] == truth[pos].upper()
```

- [ ] **Step 2: Run it inside the image**

The worktree runner mounts the worktree's source onto the running stack, but medaka lives in the image built in Task 4. Run against that image:

```bash
docker run --rm -v "$PWD/backend:/srv" -w /srv bioflow-medaka-check python -m pytest tests/pipelines/test_medaka_end_to_end.py -v
```

Expected: PASS. If Medaka reports it cannot resolve a model from the synthetic FASTQ, that is the expected fallback path — the run should still complete and correct the planted bases, and `parse_model_line` should report `polish_model_auto_resolved: False` for it.

- [ ] **Step 3: Confirm the whole suite is green**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Read the count, not just the exit code.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/pipelines/test_medaka_end_to_end.py
git commit -m "test(pipelines): prove medaka corrects planted errors"
```

---

### Task 12: Close out documentation and ship

**Files:**
- Modify: `docs/TODO.md` / `docs/TODO-done.md` (only if an entry covers this work)
- Modify: `backend/app/services/provenance_prompt.py:138` (no change needed — `medaka` is already in the known-tool list; verify)

- [ ] **Step 1: Verify the provenance list already covers medaka**

```bash
grep -n "medaka" backend/app/services/provenance_prompt.py
```

Expected: `medaka` already present at line 138. No change needed — this is the list the issue cited as evidence the tool was anticipated.

- [ ] **Step 2: Check for a TODO entry covering this work**

```bash
grep -n -i "medaka\|racon\|long-read polish" docs/TODO.md
```

If an entry exists, append ` — FIXED` to its heading, add a note saying what shipped and where the code lives, note what the implementation did differently from the entry's plan, and move the whole entry to `docs/TODO-done.md`. If no entry exists, skip this step.

- [ ] **Step 3: Rebase onto main and verify the work survived**

```bash
git fetch origin main
```

```bash
git rebase origin/main
```

```bash
git diff origin/main...HEAD --stat
```

Confirm the file list matches the tasks above and nothing looks reverted or missing.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --title "feat(pipelines): add Medaka long-read assembly polishing" --body "$(cat <<'EOF'
Closes #618.

ONT-only projects had no polishing path at all: `polish_assembly` is
Polypolish, which needs short reads, so a Flye assembly from long reads
ended with raw assembler output. This adds Medaka as a sibling path.

## Decisions worth knowing

**Medaka alone, not Racon-then-Medaka.** Medaka's own README lists
"improved accuracy over graph-based methods (e.g. Racon)" as a feature,
and current ONT guidance for R10 runs it directly on the draft. Racon is
cheap as a package and expensive as surface.

**`pytorch-cpu` is pinned, and that pin is load-bearing.** conda-forge's
bare `pytorch` resolves to CUDA builds -- libtorch at ~885MB compressed
against ~61MB CPU. Nothing errors if the pin is dropped; the image just
grows ~1GB to ship GPU kernels into a container with no GPU. The install
script fails the build if a CUDA torch ever reappears.

**Installed on arm64 too**, unlike Polypolish. bioconda ships
linux-aarch64 medaka, so this is the only polisher available on Apple
Silicon.

**`polish_changed_positions` is computed, not parsed.** Medaka prints no
per-contig tally, so without a computed diff a run that changed nothing
would be indistinguishable from one that corrected a thousand errors.

**Medaka's license is ONT's own PLC Public License v1.0**, not
OSI-standard -- the first such entry on /help/software, recorded verbatim.

`polish_assembly` also gains `polish_tool = "polypolish"`: with two
polishers writing the same fact namespace onto the same object role, an
unnamed run is unreadable.

Design: `docs/superpowers/specs/2026-08-18-medaka-long-read-polishing-design.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Label the PR**

```bash
gh pr edit --add-label "type:feature" --add-label "area:pipelines"
```

- [ ] **Step 6: Poll CI until every check reports pass**

```bash
gh pr checks --watch
```

Then confirm mergeability:

```bash
gh pr view --json mergeable,mergeStateStatus
```

A `pending` read seconds after creation means the run has not started, not that you may stop watching. If `ruff` fails on import order (`I001`), apply the minimal fix ruff itself suggests, push, and re-poll.

- [ ] **Step 7: Merge once green**

```bash
gh pr merge --rebase --delete-branch
```

- [ ] **Step 8: Tear down the worktree stack if one was brought up**

```bash
./ops/worktree-up.sh --down
```

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| R1 — installs both arches, passes `test_every_tool_is_documented` | 4, 5 |
| R2 — ONT project gets a working polish end to end | 7, 8, 11 |
| R3 — cards distinguish long from short, no broken combination | 10 |
| R4 — records model and whether auto-resolved | 2, 7 |
| R5 — records changed positions, test proves non-zero and equal to planted | 3, 11 |
| R6 — Racon out of scope | not implemented, by design |
| R7 — GPU out of scope | 4 (`pytorch-cpu` pin) |
| Spec: `-f` unconditional | 1 |
| Spec: no minimap2 command constructed | 1 |
| Spec: `is_long_read` positive, unknown stays unknown | 6 |
| Spec: `polish_tool` added to Polypolish too | 7 |
| Spec: results applier reuse | 7 |
| Spec: registries (TOOL_META, NODE_TYPES, cards, config) | 4, 5, 9, 10 |
| Spec: `sources.py` entry | 5 |
| Spec: real-database check of `is_long_read` | 6 |

**Placeholder scan:** One instruction is intentionally descriptive rather than literal — Task 5 Step 3 (`sources.py`), which says to match the neighbouring `polypolish` entry's field names rather than reproducing a schema this plan has not read. The homepage, license and citation strings it needs are given verbatim in Step 2. Task 10 Step 4's context-object wiring likewise says to match how `read_sets` is carried, since the exact attribute plumbing is local to that function. Both are read-the-neighbour instructions with the values supplied, not deferred decisions.

**Type consistency:** `build_consensus_command`, `parse_model_line`, `count_changed_positions`, and `CONSENSUS_FILENAME` are used in Task 7 exactly as defined in Tasks 1–3. `is_long_read` / `long_read_sets` are used in Tasks 8 and 10 exactly as defined in Task 6. `launch_polish_long`'s signature matches its callers in Tasks 9 and 10. The fact keys written in Task 7 (`polish_model`, `polish_model_auto_resolved`, `polish_changed_positions`, `polish_length_delta`, `polish_contigs_compared`, `polish_contigs_unmatched`, `polish_tool`, `polish_tool_version`, `polish_bacteria_mode`, `polish_read_files`) match those produced in Tasks 2–3 and asserted in Task 11.

**Fixed during review:** the first draft had Task 5's probe reach the `medaka` binary by `settings.medaka_path.replace("_consensus", "")`, with Task 11 reversing it. String surgery on a path breaks silently when the path changes, so Task 4 now defines `medaka_binary_path` alongside `medaka_path` and each of the three sites reads the one it means.
