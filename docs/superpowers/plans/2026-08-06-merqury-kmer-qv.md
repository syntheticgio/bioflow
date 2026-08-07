# Merqury k-mer QV Assessment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add reference-free base-level accuracy (QV) assessment with Merqury as an Actions-tab QC workflow, recording QV, k-mer completeness and spectra-cn plots on the assembly.

**Architecture:** A single-tool slice modelled on the CRAQ (#63) and QUAST (#62) slices, not on `assembly_qc_registry`. Two installs (meryl 1.4.2 binary, Merqury source), pure functions in `merqury_runner.py`, a SUBPROCESS handler, a launch path that resolves a read set, an Actions card, and a facts block in the UI. The expensive artifact — the read k-mer database — is cached as a sidecar on the read object so a second assembly from the same reads does not rebuild it.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor (MongoDB), pytest, React/TypeScript, Docker. Marbl meryl 1.4.2 (prebuilt binary, both arches), Merqury 1.4.1 (shell + R + Java), bedtools, R with argparse/ggplot2/scales.

**Spec:** [`docs/superpowers/specs/2026-08-06-merqury-kmer-qv-design.md`](../specs/2026-08-06-merqury-kmer-qv-design.md)

**Issue:** [#64](https://github.com/syntheticgio/bioflow/issues/64)

---

## Before you start

Read these first — each encodes a trap this plan is shaped around:

- The spec above, especially "The premise correction: there is no source build" and "The Debian meryl trap, re-confirmed".
- `backend/app/pipelines/craq_runner.py` and `backend/app/queue/assembly_qc_handlers.py::assess_assembly_errors` — the shapes this plan copies.
- `backend/scripts/install-craq.sh` — the install-script shape, including the `apt-get purge` cleanup.
- `CLAUDE.md` on hand-maintained registries (this plan adds a `SidecarRole` member, the exact registry whose silent-skip cost STAR its eight index files) and on testing from a worktree.

**Run tests with `./backend/run-worktree-tests.sh`, never `docker compose exec api python -m pytest`.** From a worktree the latter silently tests `main`'s code.

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Record the baseline pass count before you start; every task below asserts against it.

**`worker` does not hot-reload.** After changing any handler, `docker compose restart worker` from the main checkout, or the job runs old in-memory code.

**The two hardest things in this plan are not the tool integration.** They are:

1. **The probe must reject Debian's meryl** (Task 1). `_probe` has no version-rejection hook today — you are adding one. A probe that accepts `0~20150903+r2013-9+b1` produces a green install and a runtime failure, which is this issue's explicit acceptance criterion.
2. **The read-db sidecar** (Task 3) touches `SidecarRole` and `_SIDECAR_ROLES`, the registry CLAUDE.md names as the worked example of a silent-skip failure. The enum member and the allowlist must land in the same commit.

## File Structure

| File | Responsibility | Action |
| --- | --- | --- |
| `backend/scripts/install-meryl.sh` | Arch-select meryl 1.4.2 tarball, verify SHA256, extract to `/opt/meryl` | Create |
| `backend/scripts/install-merqury.sh` | Merqury source to `/opt/merqury`, set `MERQURY` | Create |
| `backend/Dockerfile` | Invoke both scripts; add bedtools + r-cran packages | Modify (~line 353, and the apt block at line 52) |
| `backend/app/config.py` | `meryl_path`, `merqury_path` settings | Modify (~line 155) |
| `backend/app/pipelines/tools.py` | `meryl()`, `merqury()` probes, `TOOL_META` entries, `all_tools()`, `cache_clear()` | Modify |
| `backend/app/models/object.py` | `SidecarRole.MERYL_DB` | Modify (~line 147) |
| `backend/app/queue/results.py` | `_apply_assess_assembly_qv` + `_APPLIERS` entry | Modify |
| `backend/app/pipelines/merqury_runner.py` | Command builders + `.qv` / `completeness.stats` parsers | Create |
| `backend/app/queue/assembly_qc_handlers.py` | `assess_assembly_qv` handler | Modify |
| `backend/app/services/pipeline_service.py` | `launch_qv_qc`, read-set resolution, meryl-db cache lookup | Modify |
| `backend/app/api/v1/pipelines.py` | `POST /pipelines/assembly-qv` | Modify |
| `backend/app/services/suggestion_service.py` | `build_qv_card` + registration | Modify |
| `frontend/src/components/AssemblyFacts.tsx` | QV facts block | Modify |
| `backend/tests/pipelines/test_merqury_runner.py` | Runner unit tests | Create |
| `backend/tests/pipelines/test_tools.py` | Probe tests incl. Debian rejection | Modify |
| `backend/tests/services/test_suggestion_service.py` | Card tests | Modify |

Tasks 1–6 are independently committable. Task 7 is real-data verification and closeout.

---

### Task 1: Install meryl and Merqury, register both tools

**Files:**
- Create: `backend/scripts/install-meryl.sh`, `backend/scripts/install-merqury.sh`
- Modify: `backend/Dockerfile`, `backend/app/config.py`, `backend/app/pipelines/tools.py`
- Test: `backend/tests/pipelines/test_tools.py`

- [ ] **Step 1: Write the meryl install script**

Create `backend/scripts/install-meryl.sh`:

```sh
#!/bin/sh
# Install Marbl meryl from its GitHub release binaries.
#
# THE VERSION PIN IS LOAD-BEARING. Read this before changing it.
#
# meryl v1.4.2 (2026-07-21) is the FIRST release ever to ship a
# Linux-arm64 binary. v1.4.1 and v1.4 are amd64-only. Merqury's own
# README still points at v1.4.1, because it was written before v1.4.2
# existed -- following the README, or "relaxing" this pin to a floor
# like >=1.4.1, silently reintroduces an arm64 C++ source build. That is
# the exact trap that bit bwa-mem2, compleasm's release asset, and
# compleasm's biocontainer in this repo.
#
# Verified 2026-08-06 via `gh api repos/marbl/meryl/releases`:
#   v1.4.2  meryl-1.4.2.Linux-arm64.tar.xz  <- exists
#   v1.4.1  (no arm64 asset)
#   v1.4    (no arm64 asset)
#
# This is why the slice is a tarball extract rather than compleasm-priced.
#
# NOT Debian's `meryl` package. That is 0~20150903+r2013-9+b1, the Celera
# Assembler k-mer suite -- a different program with the same name. See
# tools.meryl()'s probe, which rejects it explicitly.

set -eu

MERYL_VERSION="${MERYL_VERSION:-1.4.2}"
INSTALL_DIR="/opt/meryl"

case "$(uname -m)" in
    x86_64)          MERYL_ARCH="amd64" ;;
    aarch64|arm64)   MERYL_ARCH="arm64" ;;
    *)               echo "unsupported arch: $(uname -m)" >&2; exit 1 ;;
esac

TARBALL="meryl-${MERYL_VERSION}.Linux-${MERYL_ARCH}.tar.xz"
BASE="https://github.com/marbl/meryl/releases/download/v${MERYL_VERSION}"

apt-get update
apt-get install -y --no-install-recommends curl ca-certificates xz-utils

cd /tmp
echo "Fetching ${TARBALL}..."
curl -fsSL -O "${BASE}/${TARBALL}"
curl -fsSL -O "${BASE}/SHA256SUMS"

# Verify rather than trust the download. SHA256SUMS covers every asset in
# the release, so filter to ours before checking -- `sha256sum -c` fails on
# lines naming files that are not present.
grep "${TARBALL}" SHA256SUMS > "${TARBALL}.sha256"
sha256sum -c "${TARBALL}.sha256"

mkdir -p "${INSTALL_DIR}"
tar -xJf "${TARBALL}" -C "${INSTALL_DIR}" --strip-components=1
rm -f "${TARBALL}" "${TARBALL}.sha256" SHA256SUMS

apt-get purge -y curl xz-utils
apt-get autoremove -y
rm -rf /var/lib/apt/lists/*

echo "meryl ${MERYL_VERSION} (${MERYL_ARCH}) installed:"
"${INSTALL_DIR}/bin/meryl" --version 2>&1 | head -1 || true
du -sh "${INSTALL_DIR}"
```

- [ ] **Step 2: Write the Merqury install script**

Create `backend/scripts/install-merqury.sh`:

```sh
#!/bin/sh
# Install Merqury from its GitHub source tag.
#
# Merqury publishes NO release assets -- verified 2026-08-06,
# `gh api repos/marbl/merqury/releases` returns `assets: []` on every tag,
# so there is nothing to download but the source archive.
#
# Merqury is shell + R + Java: no compilation. Its scripts locate each
# other through $MERQURY, and every one of them begins
# `source $MERQURY/util/util.sh` -- so MERQURY must be set in the image
# ENV, not merely in this script's shell. See the Dockerfile.
#
# Runtime dependencies, all installed via apt in the Dockerfile rather
# than here: bedtools, r-cran-argparse, r-cran-ggplot2, r-cran-scales,
# default-jre-headless (already present for other tools), samtools
# (already present).
#
# Note the split of what needs what: eval/qv.sh -- the QV number alone --
# needs only meryl, bedtools and awk. The R packages and the bundled
# .jar files are for spectra-cn plotting. Dropping the plots later would
# recover every r-cran-* package and lose nothing from the fact table.

set -eu

MERQURY_VERSION="${MERQURY_VERSION:-1.4.1}"
INSTALL_DIR="/opt/merqury"

apt-get update
apt-get install -y --no-install-recommends curl ca-certificates

mkdir -p "${INSTALL_DIR}"
cd /tmp
curl -fsSL -o merqury.tar.gz \
    "https://github.com/marbl/merqury/archive/refs/tags/v${MERQURY_VERSION}.tar.gz"
tar -xzf merqury.tar.gz -C "${INSTALL_DIR}" --strip-components=1
rm -f merqury.tar.gz

chmod +x "${INSTALL_DIR}"/*.sh "${INSTALL_DIR}"/eval/*.sh "${INSTALL_DIR}"/util/*.sh

# A wrapper, not a symlink: merqury.sh resolves its siblings through
# $MERQURY and must run with it set even if the caller's environment
# lacks it.
cat > /usr/local/bin/merqury <<'WRAPPER'
#!/bin/sh
export MERQURY=/opt/merqury
exec bash /opt/merqury/merqury.sh "$@"
WRAPPER
chmod +x /usr/local/bin/merqury

apt-get purge -y curl
apt-get autoremove -y
rm -rf /var/lib/apt/lists/*

echo "Merqury ${MERQURY_VERSION} installed:"
du -sh "${INSTALL_DIR}"
```

- [ ] **Step 3: Wire both into the Dockerfile**

Add `bedtools` and the three R packages to the existing apt block at `backend/Dockerfile:52-74`, after `ivar \`:

```dockerfile
        bedtools \
        r-cran-argparse \
        r-cran-ggplot2 \
        r-cran-scales \
```

Then append after the CRAQ install block (~line 349):

```dockerfile
# Merqury needs meryl, and specifically Marbl's meryl -- Debian's `meryl`
# package is the unrelated Celera Assembler k-mer suite. Both installs are
# binary/source extracts with no compilation; see each script's header for
# why the meryl version pin is load-bearing.
COPY scripts/install-meryl.sh /srv/scripts/install-meryl.sh
RUN chmod +x /srv/scripts/install-meryl.sh \
    && /srv/scripts/install-meryl.sh

COPY scripts/install-merqury.sh /srv/scripts/install-merqury.sh
RUN chmod +x /srv/scripts/install-merqury.sh \
    && /srv/scripts/install-merqury.sh

# Every Merqury script begins `source $MERQURY/util/util.sh` and fails
# immediately without it, so this belongs in the image ENV rather than in
# the install script's shell.
ENV MERQURY=/opt/merqury
ENV PATH="/opt/meryl/bin:${PATH}"
```

- [ ] **Step 4: Add the config settings**

In `backend/app/config.py`, after `craq_path` (~line 155):

```python
    # An absolute path, not a bare name, and deliberately so: Debian ships a
    # `meryl` package that is the Celera Assembler k-mer suite, a different
    # program with the same name. A bare PATH lookup would let a future
    # transitive `apt-get install meryl` shadow the correct binary with one
    # that reports a version, passes a naive probe, and fails at runtime.
    # See tools.meryl(), which also rejects that version string explicitly.
    meryl_path: str = "/opt/meryl/bin/meryl"
    merqury_path: str = "merqury"
```

- [ ] **Step 5: Write the failing probe tests**

Add to `backend/tests/pipelines/test_tools.py`:

```python
def test_meryl_probe_rejects_debian_celera_build(monkeypatch, tmp_path):
    """Debian's `meryl` is 0~20150903+r2013-9+b1 -- the Celera Assembler
    k-mer suite, not Marbl meryl. It reports a version and would pass a
    naive probe, then fail at runtime on arguments it has never heard of.
    This is the acceptance criterion on issue #64.
    """
    fake = tmp_path / "meryl"
    fake.write_text("#!/bin/sh\necho '0~20150903+r2013-9+b1'\n")
    fake.chmod(0o755)

    monkeypatch.setattr(tools.settings, "meryl_path", str(fake))
    tools.meryl.cache_clear()

    probed = tools.meryl()
    assert probed.version is None
    assert probed.error is not None
    assert "Marbl" in probed.error


def test_meryl_probe_accepts_marbl_build(monkeypatch, tmp_path):
    fake = tmp_path / "meryl"
    fake.write_text("#!/bin/sh\necho 'meryl 1.4.2'\n")
    fake.chmod(0o755)

    monkeypatch.setattr(tools.settings, "meryl_path", str(fake))
    tools.meryl.cache_clear()

    probed = tools.meryl()
    assert probed.error is None
    assert probed.version is not None
    assert "1.4.2" in probed.version
```

- [ ] **Step 6: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -k meryl -v
```

Expected: FAIL with `AttributeError: module 'app.pipelines.tools' has no attribute 'meryl'`.

- [ ] **Step 7: Implement both probes**

In `backend/app/pipelines/tools.py`, after `craq()` (~line 672):

```python
# Debian's `meryl` package is `0~20150903+r2013-9+b1` -- the Celera
# Assembler k-mer suite, an unrelated program that happens to share the
# name. Merqury needs Marbl meryl 1.3+. A probe that merely reported
# whatever version string it found would call that install green and then
# fail at runtime on arguments the binary has never heard of, which is the
# same shape as Debian's BUSCO.
_CELERA_MERYL_VERSION = re.compile(r"^0~20\d{6}")


@lru_cache(maxsize=1)
def meryl() -> Tool:
    # Verified against a real installed 1.4.2 (2026-08-06): `meryl --version`
    # prints "meryl 1.4.2" and exits zero.
    probed = _probe("meryl", settings.meryl_path, ["--version"])
    if probed.version and _CELERA_MERYL_VERSION.match(probed.version):
        return Tool(
            name="meryl",
            path=probed.path,
            version=None,
            error=(
                f"Found meryl {probed.version}, which is Debian's Celera "
                f"Assembler k-mer suite, not Marbl meryl. Merqury needs "
                f"Marbl meryl 1.3 or newer (this image installs 1.4.2 at "
                f"/opt/meryl/bin/meryl). Set MERYL_PATH to a Marbl build."
            ),
        )
    return probed


@lru_cache(maxsize=1)
def merqury() -> Tool:
    # merqury.sh prints its usage banner (which carries no version) on a bare
    # call and exits 0, so the version comes from the install directory
    # rather than from the tool. `_probe` with no version args still answers
    # the question that matters -- is it on PATH and executable.
    return _probe("merqury", settings.merqury_path, [])
```

Confirm `re` is already imported at the top of `tools.py`; add `import re` if not.

Register both in `all_tools()` beside `craq()` (~line 748):

```python
        meryl(),
        merqury(),
```

And in the `cache_clear` block (~line 1937):

```python
    meryl.cache_clear()
    merqury.cache_clear()
```

- [ ] **Step 8: Add TOOL_META entries**

`test_every_tool_is_documented` requires `homepage`, `citation`, `license`, `usage` on every entry. Add beside `"craq"` (~line 1576):

```python
    "meryl": ToolMeta(
        name="meryl",
        pipeline_types=(PipelineType.ASSEMBLY_QC,),
        homepage="https://github.com/marbl/meryl",
        repository="https://github.com/marbl/meryl",
        citation=(
            "Rhie, A., Walenz, B.P., Koren, S. et al. Merqury: reference-free "
            "quality, completeness, and phasing assessment for genome "
            "assemblies. Genome Biol 21, 245 (2020)."
        ),
        citation_url="https://doi.org/10.1186/s13059-020-02134-9",
        license="Public domain / BSD-style (see meryl's LICENSE)",
        usage=(
            "Builds the k-mer databases Merqury compares. The database built "
            "from a read set is cached as a sidecar on that read object and "
            "reused across assemblies; the one built from an assembly is "
            "rebuilt per run and discarded. Note this is Marbl meryl, not "
            "Debian's same-named Celera Assembler k-mer suite."
        ),
        runnable=True,
    ),
    "merqury": ToolMeta(
        name="merqury",
        pipeline_types=(PipelineType.ASSEMBLY_QC,),
        homepage="https://github.com/marbl/merqury",
        repository="https://github.com/marbl/merqury",
        citation=(
            "Rhie, A., Walenz, B.P., Koren, S. et al. Merqury: reference-free "
            "quality, completeness, and phasing assessment for genome "
            "assemblies. Genome Biol 21, 245 (2020)."
        ),
        citation_url="https://doi.org/10.1186/s13059-020-02134-9",
        license="Public domain (US Government work)",
        usage=(
            "Scores an assembly's base-level accuracy (QV) and k-mer "
            "completeness against the reads it was built from, with no "
            "reference genome, and renders the copy-number spectra plots. "
            "Trio and hap-mer modes are never used -- BioFlow has no "
            "parental read-set concept."
        ),
        runnable=True,
    ),
```

**Verify the license fields against each repository before committing.** CLAUDE.md is explicit that a wrong license claim on a page that reads as authoritative is worse than a blank field. `gh api repos/marbl/meryl --jq '.license'` and the same for merqury; if either returns null, read the repo's `LICENSE` file and record what it actually says.

- [ ] **Step 9: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -k "meryl or merqury or documented" -v
```

Expected: PASS, including `test_every_tool_is_documented`.

- [ ] **Step 10: Build the image and confirm the arm64 claim**

This is the step that verifies the whole cost estimate. From the **main checkout**:

```bash
docker compose build api
```

Then:

```bash
docker compose exec api sh -c '/opt/meryl/bin/meryl --version; echo "---"; uname -m'
```

Expected: a Marbl version string (`meryl 1.4.2`) and the host arch. If this fails on arm64, the spec's central premise is wrong and the plan needs revisiting before Task 2 — stop and report rather than working around it.

- [ ] **Step 11: Commit**

```bash
git add backend/scripts/install-meryl.sh backend/scripts/install-merqury.sh backend/Dockerfile backend/app/config.py backend/app/pipelines/tools.py backend/tests/pipelines/test_tools.py
git commit -m "feat(pipelines): install meryl 1.4.2 and Merqury, reject Debian's meryl (#64)"
```

---

### Task 2: The runner — command builders and parsers

**Files:**
- Create: `backend/app/pipelines/merqury_runner.py`
- Test: `backend/tests/pipelines/test_merqury_runner.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_merqury_runner.py`:

```python
from pathlib import Path

import pytest

from app.pipelines import merqury_runner


def test_meryl_count_command_includes_k_and_output():
    cmd = merqury_runner.build_meryl_count_command(
        meryl_path="/opt/meryl/bin/meryl",
        k=21,
        reads=[Path("/work/reads.fastq.gz")],
        output=Path("/work/reads.meryl"),
        threads=4,
    )
    assert cmd[0] == "/opt/meryl/bin/meryl"
    assert "count" in cmd
    assert "k=21" in cmd
    assert "output" in cmd
    assert "/work/reads.meryl" in cmd
    assert "/work/reads.fastq.gz" in cmd
    assert "threads=4" in cmd


def test_meryl_count_command_accepts_multiple_read_files():
    """Paired-end reads are two files and both k-mer sets belong in one
    database -- the QV denominator is the whole read set, not one mate.
    """
    cmd = merqury_runner.build_meryl_count_command(
        meryl_path="/opt/meryl/bin/meryl",
        k=21,
        reads=[Path("/work/r1.fastq.gz"), Path("/work/r2.fastq.gz")],
        output=Path("/work/reads.meryl"),
        threads=4,
    )
    assert "/work/r1.fastq.gz" in cmd
    assert "/work/r2.fastq.gz" in cmd


def test_merqury_command_uses_fixed_names():
    """The assembly is passed under a fixed link name, never its own.
    merqury.sh derives every output filename from the input basename, so a
    hostile or merely awkward object name would otherwise reach an output
    path. Same lesson QUAST's slice learned as a stored XSS.
    """
    cmd = merqury_runner.build_merqury_command(
        merqury_path="/usr/local/bin/merqury",
        read_db=Path("/work/reads.meryl"),
        assembly=Path("/work/assembly.fasta"),
        out_prefix="qv",
    )
    assert cmd == [
        "/usr/local/bin/merqury",
        "/work/reads.meryl",
        "/work/assembly.fasta",
        "qv",
    ]


def test_parse_qv_reads_the_assembly_row():
    """Merqury's .qv is tab-separated with no header:
    <asm>  <asm-only kmers>  <total kmers>  <QV>  <error rate>
    """
    text = "assembly\t14903\t12157105\t35.4728\t0.000283749\n"
    parsed = merqury_runner.parse_qv(text)
    assert parsed["assembly_qv"] == pytest.approx(35.4728)
    assert parsed["assembly_qv_error_rate"] == pytest.approx(0.000283749)


def test_parse_qv_values_are_floats_not_ints():
    """QUAST's slice shipped a float where an int was meant, invisibly,
    because 2 == 2.0 and every assertion was equality-only. Assert type.
    """
    text = "assembly\t0\t12157105\t60\t0\n"
    parsed = merqury_runner.parse_qv(text)
    assert isinstance(parsed["assembly_qv"], float)
    assert isinstance(parsed["assembly_qv_error_rate"], float)


def test_parse_completeness_reads_the_percentage():
    """completeness.stats is tab-separated:
    <asm>  <solid kmers found>  <total solid kmers>  <completeness %>
    """
    text = "assembly\tall\t11842013\t12157105\t97.4082\n"
    parsed = merqury_runner.parse_completeness(text)
    assert parsed["assembly_qv_completeness_pct"] == pytest.approx(97.4082)
    assert isinstance(parsed["assembly_qv_completeness_pct"], float)


def test_parse_qv_raises_on_empty_report():
    with pytest.raises(ValueError):
        merqury_runner.parse_qv("")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_merqury_runner.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.pipelines.merqury_runner'`.

- [ ] **Step 3: Write the runner**

Create `backend/app/pipelines/merqury_runner.py`:

```python
"""Command builders and output parsers for Merqury k-mer QV assessment.

Pure functions only: no I/O, no subprocess, no database. The handler in
`app.queue.assembly_qc_handlers` does the running; this module decides what
to run and what the output means, which is what makes both testable without
a tool installed.

**Input filenames never reach a command line under their own names.**
`merqury.sh` derives every output filename from its input basenames, and
`util/util.sh`'s `link` symlinks inputs under those names -- so an object
named `ev<img src=x>.fasta` would put that string into an output path. The
handler links every input under a fixed name and this module's builders take
already-fixed paths. That is QUAST's stored-XSS lesson applied before the
bug exists, the same way `craq_runner` applies it for shell metacharacters.

**k is a property of the database, not of a run.** `eval/qv.sh` reads k back
out of the read database rather than taking it as an argument:

    k=`meryl print $read_db | head -n 2 | tail -n 1 | awk '{print length($1)}'`

So a database built at one k cannot serve a run that wants another. The
caller records k alongside the cached database and rebuilds on mismatch
rather than silently reusing it.
"""

from __future__ import annotations

from pathlib import Path


def build_meryl_count_command(
    *,
    meryl_path: str,
    k: int,
    reads: list[Path],
    output: Path,
    threads: int = 4,
) -> list[str]:
    """`meryl count` over one or more read files into a single database.

    Multiple read files are deliberate: paired-end reads are two files whose
    k-mers belong in one database, because the QV denominator is the whole
    read set rather than one mate.
    """
    return [
        meryl_path,
        "count",
        f"k={k}",
        f"threads={threads}",
        "output",
        str(output),
        *(str(r) for r in reads),
    ]


def build_merqury_command(
    *,
    merqury_path: str,
    read_db: Path,
    assembly: Path,
    out_prefix: str,
) -> list[str]:
    """The top-level `merqury.sh <read.meryl> <asm.fasta> <out>` call.

    One invocation produces QV, k-mer completeness, and the spectra-cn
    plots. Trio mode (maternal/paternal hapmer databases) is never used:
    BioFlow has no parental read-set concept, and inventing one would be a
    second feature.
    """
    return [merqury_path, str(read_db), str(assembly), out_prefix]


def parse_qv(text: str) -> dict[str, float]:
    """Parse Merqury's `<out>.qv`.

    Tab-separated, no header, one row per assembly scored:

        <asm>  <asm-only kmers>  <total kmers>  <QV>  <error rate>

    Every numeric field parses as float. QV is a log-scaled quality score
    and the error rate is a fraction -- neither is ever an integer, and
    asserting the type rather than the value is what catches the class of
    bug QUAST's slice shipped, where `2 == 2.0` hid a wrong type.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 5:
            continue
        return {
            "assembly_qv": float(fields[3]),
            "assembly_qv_error_rate": float(fields[4]),
        }
    raise ValueError("no parseable row found in Merqury .qv output")


def parse_completeness(text: str) -> dict[str, float]:
    """Parse Merqury's `<out>.completeness.stats`.

    Tab-separated, no header:

        <asm>  <set>  <solid kmers found>  <total solid kmers>  <completeness %>

    The `all` row is the whole read set; per-haplotype rows appear only in
    trio mode, which this slice never runs.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 5:
            continue
        return {"assembly_qv_completeness_pct": float(fields[4])}
    raise ValueError("no parseable row found in Merqury completeness.stats")
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_merqury_runner.py -v
```

Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/merqury_runner.py backend/tests/pipelines/test_merqury_runner.py
git commit -m "feat(pipelines): merqury command builders and output parsers (#64)"
```

**The parsers above are built from Merqury's documented output formats, not from a real run.** Task 7 verifies them against real output and corrects this module if they are wrong. CRAQ's slice shipped a parser looking for a row keyed `all` when every real report keys it `Genome`, with every unit test green — because the fixtures encoded the author's assumption rather than the tool's behaviour. Expect to revisit this file.

---

### Task 3: The meryl-db sidecar role

**Files:**
- Modify: `backend/app/models/object.py`, `backend/app/queue/results.py`
- Test: `backend/tests/models/test_object.py` (or wherever `SidecarRole` exhaustiveness is asserted — find it with `grep -rn "_SIDECAR_ROLES" backend/tests/`)

**This task touches the registry CLAUDE.md names as the worked example of a silent-skip failure.** Adding STAR found that `results._SIDECAR_ROLES` skips roles it has no entry for rather than raising — costing a `build_index` job its eight index files while the full suite stayed green. `_SIDECAR_ROLES` is the *derivable* kind (`{role.value: role for role in SidecarRole}`), so the enum member is enough — but verify that, don't assume it.

- [ ] **Step 1: Confirm the allowlist is derived, not hand-listed**

```bash
grep -n "_SIDECAR_ROLES" backend/app/queue/results.py
```

Expected at line ~2128: `_SIDECAR_ROLES = {role.value: role for role in SidecarRole}`.

If it is a hand-written literal instead, this task must add the entry there too, and the plan's assumption was wrong — say so in the commit message.

- [ ] **Step 2: Write the failing test**

Add to the test file that covers `SidecarRole`:

```python
def test_meryl_db_is_a_known_sidecar_role():
    """A meryl database built from a read set is scaffolding, not something
    a person opens -- the same category as a BWA index. Absent from
    _SIDECAR_ROLES it would be silently skipped at ingest, which is how
    STAR's index lost all eight of its files while the suite stayed green.
    """
    from app.models.object import SidecarRole
    from app.queue.results import _SIDECAR_ROLES

    assert SidecarRole.MERYL_DB.value in _SIDECAR_ROLES
```

- [ ] **Step 3: Run it to verify it fails**

```bash
./backend/run-worktree-tests.sh -k meryl_db -v
```

Expected: FAIL with `AttributeError: MERYL_DB`.

- [ ] **Step 4: Add the enum member**

In `backend/app/models/object.py`, after `TBI` (~line 147):

```python
    # A meryl k-mer database built from a read set, cached on the read
    # object so a second assembly from the same reads does not rebuild it.
    # Merqury's expensive artifact: the assembly-side database is cheap and
    # stays in job scratch, but this one is derived from every read and is
    # reusable across every assembly those reads produced.
    #
    # The database is a directory, not a file. It is stored as a single
    # archive member the way STAR_INDEX stores its eight files flat, and
    # reassembled at materialize time.
    #
    # `k` is part of this database's identity, not a parameter that can vary
    # against it -- meryl's own qv.sh reads k back out of the database
    # rather than accepting it as an argument. The k a database was built at
    # is recorded in its facts, and a run wanting a different k builds a new
    # database rather than reusing this one.
    MERYL_DB = "meryl-db"
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh -k "meryl_db or sidecar" -v
```

Expected: PASS, including the existing `SidecarRole` exhaustiveness test.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/object.py backend/tests/
git commit -m "feat(models): add MERYL_DB sidecar role for cached k-mer databases (#64)"
```

---

### Task 4: The handler

**Files:**
- Modify: `backend/app/queue/assembly_qc_handlers.py`, `backend/app/queue/results.py`
- Test: `backend/tests/queue/test_assembly_qc_handlers.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/queue/test_assembly_qc_handlers.py`, matching the existing tests' fixture style in that file:

```python
def test_assess_assembly_qv_links_inputs_under_fixed_names(monkeypatch, tmp_path):
    """merqury.sh names every output from its input basenames, so the
    object's own name must never reach the command line. Assert the linked
    path, not just that the command was built.
    """
    from app.queue import assembly_qc_handlers

    captured = {}

    def fake_run(ctx, cmd, log_path):
        captured["cmd"] = cmd
        return 0

    monkeypatch.setattr(assembly_qc_handlers, "run_subprocess", fake_run)
    # ... build ctx with an assembly whose object name contains a shell
    # metacharacter and a semicolon, per the fixture pattern already used
    # by test_assess_assembly_errors_* in this file ...

    assert not any(";" in part for part in captured["cmd"])
    assert any(part.endswith("assembly.fasta") for part in captured["cmd"])
```

- [ ] **Step 2: Run it to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/queue/test_assembly_qc_handlers.py -k qv -v
```

Expected: FAIL — `assess_assembly_qv` does not exist.

- [ ] **Step 3: Write the handler**

**`_prepare_workdir`, `_resolve_input` and `_named_link` already exist** — imported into `assembly_qc_handlers.py` from `app.queue.pipeline_handlers` (line 31). Reuse them. The two helpers this task *does* add, `_link_tree` and `_resolve_read_inputs`, are new because a meryl database is a directory and a read set is a list, neither of which the single-file helpers cover.

Add to `backend/app/queue/assembly_qc_handlers.py`, following `assess_assembly_errors` (line 384) as the model. Add the link-name constants beside the existing `_CRAQ_*` ones:

```python
_MERQURY_ASSEMBLY_LINK = "assembly.fasta"
_MERQURY_READ_DB_LINK = "reads.meryl"
ASSEMBLY_QV_LEASE_SECONDS = 3600
```

Then the handler:

```python
@handler(
    "assess_assembly_qv",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=4, mem_mb=16384, io=IoClass.HEAVY),
    max_attempts=1,
)
def assess_assembly_qv(ctx: JobContext) -> dict:
    """Reference-free base-level accuracy (QV) for one assembly, with Merqury.

    Read-only: no new object, only facts merged onto the assembly that was
    scored, plus spectra-cn plots written under qc_reports/.

    **Input filenames never reach the command line.** merqury.sh derives
    every output filename from its input basenames, so an object named
    `ev<img src=x>.fasta` would otherwise put that string into an output
    path -- the same shape as the stored XSS QUAST's slice found, and
    prevented here the same way: every input is linked under a fixed name.

    **The read database may arrive prebuilt.** When the launch path resolved
    a cached MERYL_DB sidecar for this read set at this k, `read_db_path` is
    set and this handler skips the `meryl count`. Otherwise it builds one and
    reports its location in the result so the applier can ingest it as a
    sidecar for the next run. Rebuilding per assembly is the wasteful
    default this cache exists to avoid.

    **mem_mb is 16384, not CRAQ's 8192.** A meryl database over a real read
    set is memory-hungry in a way a BAM scan is not. Task 7 measures this
    against real data and corrects it.
    """
    meryl_tool = tools.require(tools.meryl())
    merqury_tool = tools.require(tools.merqury())

    work = _prepare_workdir(ctx, "assembly_qv")

    assembly = _resolve_input(ctx.payload, "assembly")
    assembly = _named_link(work, assembly, _MERQURY_ASSEMBLY_LINK)

    k = int(ctx.payload.get("k") or 21)
    threads = max(1, int(ctx.payload.get("threads") or 4))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    read_db = work / _MERQURY_READ_DB_LINK
    built_read_db = False

    cached = ctx.payload.get("read_db_path")
    if cached:
        _link_tree(Path(cached), read_db)
    else:
        read_files = _resolve_read_inputs(work, ctx.payload)
        if not read_files:
            raise PermanentError(
                "QV assessment needs the reads this assembly was built from."
            )
        ctx.progress(phase="counting", pct=None, message="building k-mer database")
        ctx.extend_lease(ASSEMBLY_QV_LEASE_SECONDS)
        count_cmd = merqury_runner.build_meryl_count_command(
            meryl_path=meryl_tool.path,
            k=k,
            reads=read_files,
            output=read_db,
            threads=threads,
        )
        code = run_subprocess(ctx, count_cmd, log_path=str(log_path))
        if code != 0:
            raise RetryableError(f"meryl count exited {code}; see {log_path}")
        built_read_db = True

    out_dir = work / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = merqury_runner.build_merqury_command(
        merqury_path=merqury_tool.path,
        read_db=read_db,
        assembly=assembly,
        out_prefix="qv",
    )

    ctx.progress(phase="scoring", pct=None, message="starting merqury")
    ctx.extend_lease(ASSEMBLY_QV_LEASE_SECONDS)

    log.info(
        "assembly_qv_started",
        job_id=ctx.job_id,
        k=k,
        read_db_cached=not built_read_db,
        threads=threads,
    )

    code = run_subprocess(ctx, cmd, log_path=str(log_path), cwd=str(out_dir))
    if code != 0:
        raise RetryableError(f"merqury exited {code}; see {log_path}")

    qv_file = out_dir / "qv.qv"
    completeness_file = out_dir / "qv.completeness.stats"
    if not qv_file.exists():
        raise RetryableError(
            f"merqury produced no qv.qv in {out_dir}; see {log_path}"
        )

    facts = merqury_runner.parse_qv(qv_file.read_text())
    if completeness_file.exists():
        facts.update(merqury_runner.parse_completeness(completeness_file.read_text()))

    facts.update(
        {
            "assembly_qv_k": k,
            "assembly_qv_read_object_id": str(ctx.payload.get("read_object_id") or ""),
            "assembly_qv_read_object_name": str(
                ctx.payload.get("read_object_name") or ""
            ),
            "assembly_qv_tool": "merqury",
            "assembly_qv_tool_version": merqury_tool.version or "",
            "assembly_qv_meryl_version": meryl_tool.version or "",
        }
    )

    report_dir = settings.qc_reports_dir / str(ctx.payload["object_id"])
    report_dir.mkdir(parents=True, exist_ok=True)
    for png in out_dir.glob("*.png"):
        shutil.copy2(png, report_dir / png.name)

    return {
        "object_id": ctx.payload["object_id"],
        "job_id": ctx.job_id,
        "facts": facts,
        "read_db_path": str(read_db) if built_read_db else None,
        "read_object_id": ctx.payload.get("read_object_id"),
        "k": k,
    }
```

Add the `_link_tree` and `_resolve_read_inputs` helpers beside the existing `_named_link` / `_resolve_input` in the same module:

```python
def _link_tree(source: Path, dest: Path) -> None:
    """Link a meryl database directory into the work dir under a fixed name.

    A meryl database is a directory, not a file, so `_named_link`'s
    single-file symlink does not apply. A symlink to the directory is
    enough: meryl reads it and never writes into it during a QV run.
    """
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    dest.symlink_to(source, target_is_directory=True)


def _resolve_read_inputs(work: Path, payload: dict) -> list[Path]:
    """Every read file in the set, linked under fixed sequential names.

    Paired-end reads are two files whose k-mers belong in one database --
    the QV denominator is the whole read set, not one mate.

    Each entry is its own mini-payload with `read_path`/`read_sha256` keys,
    so `_resolve_input` applies per entry. Fixed sequential names keep any
    object's own name off the command line, the same reason the assembly
    gets a fixed link.
    """
    resolved: list[Path] = []
    for i, entry in enumerate(payload.get("reads") or []):
        raw = _resolve_input(entry, "read")
        resolved.append(_named_link(work, raw, f"reads_{i}.fastq.gz"))
    return resolved
```

`_named_link`'s real signature is `(work: Path, target: Path, name: str | None) -> Path` (`pipeline_handlers.py:714`) — it links into the workdir as `in_<name>` and returns the target unchanged when `name` is falsy. Call it accordingly; the handler's call above passes `work` as the first argument for the same reason.

Confirm `shutil` and `Path` are imported in this module; add them if not.

- [ ] **Step 4: Add the results applier**

In `backend/app/queue/results.py`, after `_apply_assess_assembly_errors` (~line 1517):

```python
async def _apply_assess_assembly_qv(result: dict, *, owner: str) -> None:
    """Record Merqury's QV facts on the assembly they describe, and cache
    the read k-mer database as a sidecar on the read object.

    Near-copy of `_apply_assess_assembly_errors` for the facts half. The
    sidecar half is the addition: a meryl database built from a read set is
    reusable by every assembly those reads produced, and rebuilding it per
    assembly is the expense this cache exists to avoid.

    The database is ingested against the READ object, not the assembly --
    it is derived from the reads and has nothing to do with any particular
    assembly. Its `k` is recorded on it, because a database built at one k
    cannot serve a run wanting another: meryl's own qv.sh reads k back out
    of the database rather than taking it as an argument.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("assembly_qv_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "assembly_qv_applied",
        object_id=object_id,
        qv=facts.get("assembly_qv"),
        completeness=facts.get("assembly_qv_completeness_pct"),
    )

    read_db_path = result.get("read_db_path")
    read_object_id = result.get("read_object_id")
    if not read_db_path or not read_object_id:
        return

    from app.services import object_service

    try:
        await object_service.ingest_sidecar(
            path=Path(read_db_path),
            parent_id=PydanticObjectId(read_object_id),
            role=SidecarRole.MERYL_DB,
            owner=owner,
            produced_by_job=result.get("job_id"),
            facts={"meryl_db_k": result.get("k")},
        )
    except Exception:
        # A sidecar that fails to ingest costs the next run a rebuild. It
        # must never destroy the QV facts already written above -- the same
        # separate-applier discipline results.py uses for every secondary
        # output.
        log.exception("meryl_db_ingest_failed", read_object_id=read_object_id)
```

Register it in `_APPLIERS` (~line 2152), beside `"assess_assembly_errors"`:

```python
    "assess_assembly_qv": _apply_assess_assembly_qv,
```

**Check `object_service.ingest_sidecar`'s real signature before writing this call** — `grep -n "def ingest_sidecar" -A 20 backend/app/services/object_service.py`. If it differs, match the existing call sites (the STAR index appliers are the closest analogue) rather than the sketch above.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/queue/ -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/assembly_qc_handlers.py backend/app/queue/results.py backend/tests/queue/
git commit -m "feat(queue): assess_assembly_qv handler and meryl-db caching applier (#64)"
```

---

### Task 5: Launch path, route, and card

**Files:**
- Modify: `backend/app/services/pipeline_service.py`, `backend/app/api/v1/pipelines.py`, `backend/app/services/suggestion_service.py`
- Test: `backend/tests/services/test_suggestion_service.py`

- [ ] **Step 1: Write the failing card tests**

Add to `backend/tests/services/test_suggestion_service.py`:

```python
async def test_qv_card_unavailable_when_merqury_missing(monkeypatch):
    """Assert the UNAVAILABLE direction, not the available one. The image
    ships tools installed, so an available-direction assertion passes
    whether or not the patch worked -- it proves nothing about the seam.
    """
    from app.services import suggestion_service

    monkeypatch.setattr(
        suggestion_service.tools,
        "merqury",
        lambda: Tool(name="merqury", path=None, version=None, error="not found"),
    )
    card = await suggestion_service.build_qv_card(assembly_obj, read_sets=[reads_obj])
    assert not card.available
    assert "merqury" in card.unavailable_reason.lower()


async def test_qv_card_unavailable_without_reads():
    from app.services import suggestion_service

    card = await suggestion_service.build_qv_card(assembly_obj, read_sets=[])
    assert not card.available
    assert "read" in card.unavailable_reason.lower()
```

Build `assembly_obj` and `reads_obj` with the fixture helpers already used by `test_assembly_error_card_*` in that file.

- [ ] **Step 2: Run to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -k qv -v
```

Expected: FAIL — `build_qv_card` does not exist.

- [ ] **Step 3: Write the launch path**

In `backend/app/services/pipeline_service.py`, following `launch_assembly_error_qc`:

```python
async def launch_qv_qc(
    object_id: str,
    *,
    owner: str,
    read_object_id: str | None = None,
    k: int | None = None,
) -> dict:
    """Queue a Merqury QV assessment for one assembly.

    The read set is resolved here rather than in the handler, because
    choosing it is a judgment the card and dialog make -- and a wrong
    pairing produces a confidently wrong QV rather than an error, which is
    why this never guesses when the choice is ambiguous.

    Provenance is preferred where it exists: a de novo assembly carries its
    reads in `derived_from`, and that is the default rather than something
    the user re-derives.

    A cached MERYL_DB sidecar on the chosen read set at the same k is
    passed through as `read_db_path` so the handler skips the count. A
    database at a *different* k is not reusable and is ignored -- meryl's
    qv.sh reads k back out of the database rather than taking it as an
    argument, so a mismatched database cannot serve the run.
    """
    assembly = await object_service.get_object(object_id, owner=owner)
    # `reference_assembly.check_draft_assembly` is the existing validator --
    # it carries the protein.faa / cds_from_genomic.fna exclusions the align
    # card learned the hard way, which a hand-rolled FASTA check would miss.
    reference_assembly.check_draft_assembly(assembly)

    reads = await _resolve_qv_reads(assembly, read_object_id, owner=owner)
    if reads is None:
        raise ValueError(
            "QV assessment needs the reads this assembly was built from. "
            "Choose a read set."
        )

    resolved_k = int(k or DEFAULT_MERYL_K)

    read_db_path = None
    cached = await _sidecar_of_role(reads.id, SidecarRole.MERYL_DB)
    if cached is not None and cached.facts.get("meryl_db_k") == resolved_k:
        read_db_path = cached.path

    payload = {
        "object_id": str(assembly.id),
        "read_object_id": str(reads.id),
        "read_object_name": reads.name,
        "reads": _read_payload_entries(reads),
        "k": resolved_k,
        "read_db_path": read_db_path,
    }
    return await run_service.enqueue(
        "assess_assembly_qv", payload=payload, owner=owner
    )
```

Add `DEFAULT_MERYL_K = 21` as a module constant with a comment noting that Merqury ships `best_k.sh` to derive k from genome size, and that Task 7 should confirm 21 is a sane default for the sizes BioFlow sees.

- [ ] **Step 4: Add the route**

In `backend/app/api/v1/pipelines.py`, following the assembly-errors route:

```python
@router.post("/assembly-qv")
async def launch_assembly_qv(
    body: AssemblyQvRequest,
    owner: str = Depends(current_owner),
) -> dict:
    return await pipeline_service.launch_qv_qc(
        body.object_id,
        owner=owner,
        read_object_id=body.read_object_id,
        k=body.k,
    )
```

With the request model beside the other pipeline request models:

```python
class AssemblyQvRequest(BaseModel):
    object_id: str
    read_object_id: str | None = None
    k: int | None = None
```

- [ ] **Step 5: Write the card**

In `backend/app/services/suggestion_service.py`, following `build_assembly_error_card` (line 1222):

```python
def build_qv_card(obj: DataObject, read_sets: list[DataObject]) -> SuggestionCard:
    """Base-level accuracy (QV) for an assembly, from the reads it came from.

    Needs no reference genome and no alignment -- Merqury is k-mer based,
    which is what makes this the one QC axis that works for an assembly with
    nothing to compare against.

    Pairing follows the CRAQ card: auto-pair when unambiguous, ask
    otherwise. Exactly one eligible read set fires the card directly; more
    than one routes to the dialog. The card never guesses which reads an
    assembly came from, because a wrong pairing produces a confidently wrong
    number rather than a visible error.
    """
    meryl_tool = tools.meryl()
    merqury_tool = tools.merqury()
    if not merqury_tool.available:
        return _unavailable("assembly_qv", merqury_tool.error or "merqury is not installed")
    if not meryl_tool.available:
        return _unavailable("assembly_qv", meryl_tool.error or "meryl is not installed")

    if not read_sets:
        return _unavailable(
            "assembly_qv",
            "QV assessment compares an assembly against the reads it came "
            "from. Add a read set to this project first.",
        )

    ...
```

Match `_unavailable` and `SuggestionCard`'s real signatures in that module — the sketch names the shape, not the API.

Register it in the card list (~line 1546), beside `("assembly_errors", ...)`:

```python
        ("assembly_qv", lambda: build_qv_card(obj, project_read_sets)),
```

**This registration is the step that is silently skippable.** CLAUDE.md records that installing a tool without a rule that can pick it leaves a card reading "no tool installed" beside an installed tool. Nothing fails if you skip it.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/api/v1/pipelines.py backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat(pipelines): QV launch path, route, and Actions card (#64)"
```

---

### Task 6: Frontend facts block

**Files:**
- Modify: `frontend/src/components/AssemblyFacts.tsx`

- [ ] **Step 1: Add the QV block**

Follow the assembly-errors block already in that file. Render, when `assembly_qv` is present:

- **QV** as the headline number, with the error rate beside it.
- **k-mer completeness** as a percentage.
- **The read set the QV was measured against**, by name, linking to the object.
- **k**, the tool versions.

The read set is not a footnote: a QV is a statement about *this assembly against those reads*, and reads from a different individual measure real biology as error. Render it beside the number, not in a collapsed provenance section.

- [ ] **Step 2: Link the spectra-cn plots**

The PNGs land under `qc_reports/<object_id>/` and are served by the existing route. Render them as images — they are static, so unlike QUAST's report this needs no CSP exception and carries none of that slice's scripting exposure.

- [ ] **Step 3: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Verify in the browser**

From this worktree:

```bash
./ops/worktree-up.sh
```

Then open http://localhost:5273, find an assembly with QV facts, and confirm the block renders and the plots load. **This is the actual verification step for anything UI-facing** — there is no headless component-testing setup in this repo and none is expected.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/AssemblyFacts.tsx
git commit -m "feat(frontend): QV facts block and spectra-cn plots (#64)"
```

---

### Task 7: Real-data verification and closeout

**This task is not optional and it is where this plan expects to find bugs.** Every parser and every resource figure above was derived from documentation, not from a run. CRAQ's slice shipped a parser built from a careful source read that was wrong in two ways — a filename that never exists and a row key no real report uses — with every unit test green, because the fixtures encoded the author's assumptions rather than the tool's behaviour.

- [ ] **Step 1: Rebuild the stack**

From the **main checkout**:

```bash
docker compose up -d --build api web worker
```

- [ ] **Step 2: Run a real QV assessment**

Use a real project with an assembly and the reads it came from. Launch from the Actions tab.

- [ ] **Step 3: Check the parsers against real output**

```bash
docker compose exec api sh -c 'cat /data/work/*/assembly_qv/out/qv.qv; echo "---"; cat /data/work/*/assembly_qv/out/qv.completeness.stats'
```

Compare the real column layout against `parse_qv` and `parse_completeness`. **If they differ, fix the runner and rebuild the unit-test fixtures from this real output**, not from the documented format.

- [ ] **Step 4: Measure and correct the resource figures**

Record the read-db build time and size, and peak RSS:

```bash
docker compose exec api sh -c 'du -sh /data/work/*/assembly_qv/reads.meryl'
```

The db build time and size are what justify the sidecar cache's complexity — the spec calls for measuring rather than assuming. If the database turns out cheap, say so in the closeout note; the cache is still correct but the argument for it changes.

Correct `JobResources(cpu=4, mem_mb=16384)` in the handler if the real peak differs materially.

- [ ] **Step 5: Verify the cache actually works**

Run a second QV assessment against a different assembly from the same reads. Confirm from the job log that `read_db_cached=True` and that no `meryl count` ran. **This is the feature's whole point** and nothing above tests it end to end.

- [ ] **Step 6: Verify the Debian-meryl rejection against the real image**

```bash
docker compose exec api python -c "
from app.pipelines import tools
print(tools.meryl())
print(tools.merqury())
"
```

Expected: both available, meryl reporting a 1.4.x version.

- [ ] **Step 7: Check the card against a real project**

CLAUDE.md is explicit that this is worth more than another fixture — the Actions tab's suggestion rules passed a full green suite while getting two things wrong that one look at a real project exposed:

```bash
docker compose exec api python -c "
import asyncio
from app.services import suggestion_service
# resolve a real project's assembly and print its cards
"
```

- [ ] **Step 8: Run the full suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: the baseline count from "Before you start", plus the tests this plan added, zero failures.

- [ ] **Step 9: Update the docs and close out**

- Add a closeout note to the spec recording what the implementation did differently, per CLAUDE.md's rule that the delta is the most valuable sentence.
- If `docs/TODO.md` has an entry this resolves, append ` — FIXED` with what shipped and where, and move the whole entry to `docs/TODO-done.md`.
- Update [#64](https://github.com/syntheticgio/bioflow/issues/64) with what shipped and relabel to `status:ready` → closed.

- [ ] **Step 10: Commit and merge**

```bash
git add -A
git commit -m "docs: closeout notes for Merqury QV assessment (#64)"
```

Then merge to `main` and push, per CLAUDE.md — once the suite is green and `main` is clean, commit and merge without asking. Re-run the suite after merging if `main` has moved.

---

## Notes for the implementer

**The version pin is the whole cost argument.** If `install-meryl.sh` is ever "modernized" to follow Merqury's README (v1.4.1) or relaxed to a floor, the arm64 source build comes back silently. The comment in the script says this; keep it there.

**`_probe` gained a rejection path in Task 1.** It previously had no way to say "this binary exists, runs, reports a version, and is the wrong program." If a second tool ever needs the same, generalize `meryl()`'s pattern rather than copying it.

**Do not trust this plan's checkboxes as a completion signal.** Nothing ticks them. CLAUDE.md records that both surviving plans in `docs/superpowers/plans/` show zero of their 66 and 49 boxes checked while their code is demonstrably merged. Verify against the code.
