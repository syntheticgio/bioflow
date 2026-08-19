# Kraken2 Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Species-level read classification (Kraken2 + Bracken) with on-demand
database download, taxonomy facts, and an organism-metadata mismatch check.

**Architecture:** Kraken2 and Bracken ship bundled in the backend image. The
classification database is one of three pre-built Langmead indexes fetched on
demand into `BIOINFO_HOME/kraken_dbs/<key>` by a `download_kraken_db` job
(mirroring `download_lineage`); `launch_classify_reads` chains behind it via
`depends_on` when the database is absent. One `classify_reads` job runs
Kraken2 then Bracken (non-fatal), stores a `taxonomy` fact (plus
`taxonomy_mismatch` when metadata disagrees) on the reads object via a
`results.py` applier, and copies the raw reports to
`qc_reports/<object_id>/kraken2/`. No PipelineRun — this follows the
`launch_annotate_genome` / `launch_gc_tracks` precedent (facts-only launchers
live in `EXCLUDED_LAUNCHES`; the Actions-tab card is the launch surface).

**Tech Stack:** FastAPI + Beanie backend, Redis queue with
`HandlerMode.SUBPROCESS` handlers, React frontend. Kraken2 (C++), Bracken.

**Spec:** `docs/superpowers/specs/2026-08-18-kraken2-classification-design.md`

## Global Constraints

- Commit subjects are Conventional Commits, imperative, lowercase after the
  colon, ~65 chars (see CLAUDE.md). Scope `pipelines` unless noted.
- Backend tests run from the worktree via `./backend/run-worktree-tests.sh
  tests/... -q` — never `docker compose exec api` from a worktree.
- `worker` does not hot-reload: after handler changes, `docker compose
  restart worker` (from the main checkout) or use the worktree stack.
- License/citation values in `TOOL_META` must be verified against each
  project's own repository at implementation time, never recalled
  (CLAUDE.md "Adding a pipeline tool"). Unverifiable optional fields stay
  empty.
- Registry md5/URL values must be copied from the published index, never
  invented (Task 2 shows how).
- Facts key names are exactly `taxonomy` and `taxonomy_mismatch` (spec
  K2-H2).

**Spec deviation (recorded here and amended in the spec):** spec K2-C1/K2-C4
originally said `classify_reads` gets a run record and a `NODE_TYPES` entry.
The repo's settled pattern for facts-only QC launchers
(`launch_annotate_genome`, `launch_gc_tracks`, issue #371) is: no
PipelineRun, `EXCLUDED_LAUNCHES` classification, suggestion card as launch
surface. This plan follows the precedent. `download_kraken_db` still gets a
real `NodeTypeSpec`, mirroring `download_lineage`.

---

### Task 1: Tool probes, TOOL_META, Dockerfile install

**Files:**
- Modify: `backend/app/config.py` (near `bakta_path`, ~line 165)
- Modify: `backend/app/pipelines/tools.py` (probe near `bakta()` ~line 630; `TOOL_META` entries at the end of the dict; `probe_cache_clear` block ~line 2428)
- Modify: `backend/Dockerfile`
- Test: `backend/tests/pipelines/test_tools.py` (existing `test_every_tool_is_documented` covers the new entries automatically — confirm, don't duplicate)

**Interfaces:**
- Produces: `tools.kraken2() -> Tool`, `tools.bracken() -> Tool` (cached probes, same shape as `tools.bakta()`); `settings.kraken2_path`, `settings.bracken_path`.

- [ ] **Step 1: Verify license and citation against the upstream repos**

Fetch and read (do not recall):
- `https://github.com/DerrickWood/kraken2` — LICENSE file and README citation.
- `https://github.com/jenniferlu717/Bracken` — LICENSE file and README citation.

Expected (to be confirmed, not assumed): Kraken2 MIT, Wood DE, Lu J,
Langmead B. "Improved metagenomic analysis with Kraken 2." *Genome Biology*
2019;20:257, doi:10.1186/s13059-019-1891-0. Bracken GPL-3.0, Lu J, Breitwieser
FP, Thielen P, Salzberg SL. "Bracken: estimating species abundance in
metagenomics data." *PeerJ Computer Science* 2017;3:e104,
doi:10.7717/peerj-cs.104. If a value cannot be confirmed, leave the optional
field (`repository`, `citation_url`) empty rather than guessing.

- [ ] **Step 2: Add settings paths**

In `backend/app/config.py`, next to `bakta_path`:

```python
    kraken2_path: str = "kraken2"
    bracken_path: str = "bracken"
```

- [ ] **Step 3: Add probes in `tools.py`**

Next to `bakta()` (~line 630), following its exact shape (an
`@functools.cache`-decorated function calling `_probe`; copy the decorator
style used by the neighbors):

```python
@cache
def kraken2() -> Tool:
    """k-mer taxonomic classifier; database delivered on demand, not bundled."""
    return _probe("kraken2", settings.kraken2_path, ["--version"])


@cache
def bracken() -> Tool:
    """Abundance re-estimation over a Kraken2 report. Non-fatal companion:
    classify_reads ships Kraken2-only results when this is missing."""
    return _probe("bracken", settings.bracken_path, ["-v"])
```

Note: verify Bracken's version flag by running the installed binary
(`bracken -v` on current releases; adjust if the installed version differs).
Add `kraken2.cache_clear()` and `bracken.cache_clear()` to the cache-clear
block at ~line 2428.

- [ ] **Step 4: Add TOOL_META entries**

At the end of the `TOOL_META` dict, after `"bakta"`. Match the field style of
the bakta entry exactly. Use the same `PipelineType` member the existing QC
tools use (check the `fastp`/`fastqc` entries and reuse their member):

```python
    "kraken2": ToolMeta(
        pipelines=(PipelineType.QC,),  # match the member fastqc/fastp use
        one_liner="k-mer taxonomic classification of sequencing reads",
        summary=(
            "Kraken2 assigns a taxonomic label to each sequencing read by "
            "matching k-mers against a reference database, producing a "
            "per-taxon report of what organisms a sample contains. Used "
            "for verifying sample identity and detecting cross-species "
            "contamination (e.g. human reads in a microbial sample)."
        ),
        strengths=(
            "Classifies millions of reads per minute",
            "Detects cross-species contamination and mislabeled samples",
            "Standard report format consumed by Krona and Pavian",
        ),
        homepage="https://github.com/DerrickWood/kraken2",
        repository="https://github.com/DerrickWood/kraken2",
        citation="<verbatim from Step 1>",
        citation_url="<doi URL from Step 1>",
        license="<from Step 1>",
        usage=(
            "Classifies FASTQ reads against an on-demand reference "
            "database chosen at launch (Standard-8 by default), reporting "
            "per-taxon read percentages. The database is several GB and "
            "is downloaded on first use rather than shipped in the image. "
            "Results feed the taxonomy panel and the organism-metadata "
            "mismatch check."
        ),
        delivery=Delivery.BUNDLED,
    ),
    "bracken": ToolMeta(
        pipelines=(PipelineType.QC,),
        one_liner="Bayesian species-abundance re-estimation from Kraken2 reports",
        summary=(
            "Bracken redistributes reads that Kraken2 assigned at genus "
            "level or above down to species using a Bayesian re-estimation "
            "over the database's k-mer distribution, turning a raw "
            "classification report into species-level abundance estimates."
        ),
        strengths=(
            "Species-level abundances from higher-rank assignments",
            "Runs in seconds over an existing report",
        ),
        homepage="https://github.com/jenniferlu717/Bracken",
        repository="https://github.com/jenniferlu717/Bracken",
        citation="<verbatim from Step 1>",
        citation_url="<doi URL from Step 1>",
        license="<from Step 1>",
        usage=(
            "Runs automatically inside the classification job after "
            "Kraken2, refining the report into species-level abundances. "
            "Failure is non-fatal: the job ships Kraken2-only results "
            "with a note when Bracken cannot run."
        ),
        delivery=Delivery.BUNDLED,
    ),
```

- [ ] **Step 5: Install in the Dockerfile**

Both are on bioconda/apt-unfriendly paths; use release tarballs the way the
image handles other source-shy tools. Kraken2 publishes a plain
`install_kraken2.sh` in its release tarball (C++ build, needs g++ which the
image's build stage has); Bracken likewise (`install_bracken.sh`). Follow the
compleasm pattern: a pinned-version `ARG`, an install script under
`backend/scripts/`, a `RUN` invoking it. Confirm both build on arm64 (plain
C++/shell — expected fine; if Kraken2's build fails on arm64, record findings
in the PR and stop for discussion rather than shipping amd64-only).

```dockerfile
ARG KRAKEN2_VERSION=2.1.3
ARG BRACKEN_VERSION=2.9
COPY scripts/install-kraken2.sh /srv/scripts/install-kraken2.sh
RUN chmod +x /srv/scripts/install-kraken2.sh \
    && KRAKEN2_VERSION="${KRAKEN2_VERSION}" \
       BRACKEN_VERSION="${BRACKEN_VERSION}" \
       /srv/scripts/install-kraken2.sh
```

Write `backend/scripts/install-kraken2.sh` modeled on
`backend/scripts/install-compleasm.sh` (read it first): download the two
release tarballs from GitHub, run each tool's own installer script into
`/usr/local/`, symlink `kraken2`, `kraken2-build`, `bracken`,
`bracken-build` onto PATH, and smoke-test `kraken2 --version` and
`bracken -v` (adjust the bracken flag to whatever Step 3 verified).

- [ ] **Step 6: Rebuild and verify probes**

From the worktree: `./ops/worktree-up.sh` (brings up the worktree stack with
the new image). Then verify inside its api container that
`tools.kraken2().available` and `tools.bracken().available` are both True
(one `docker exec <worktree-api-container> python -c ...`).

- [ ] **Step 7: Run the docs test**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -q`
Expected: PASS, including `test_every_tool_is_documented` now covering
kraken2 and bracken.

- [ ] **Step 8: Commit**

```bash
git add backend/app/config.py backend/app/pipelines/tools.py backend/Dockerfile backend/scripts/install-kraken2.sh
git commit -m "feat(pipelines): install kraken2 and bracken with documented probes"
```

---

### Task 2: Database registry

**Files:**
- Create: `backend/app/pipelines/kraken_db_registry.py`
- Modify: `backend/app/config.py` (add `kraken_dbs_dir` property next to `lineages_dir`, ~line 410)
- Test: `backend/tests/pipelines/test_kraken_db_registry.py`

**Interfaces:**
- Produces:
  - `KrakenDbSpec` frozen dataclass: `key: str`, `label: str`, `url: str`, `download_bytes: int`, `mem_mb: int`, `md5: str`, `description: str`
  - `KRAKEN_DBS: dict[str, KrakenDbSpec]` with keys `"standard-8"`, `"pluspf-8"`, `"viral"`
  - `DEFAULT_DB = "standard-8"`
  - `db_present(key: str) -> bool`
  - `settings.kraken_dbs_dir -> Path` (= `bioinfo_home / "kraken_dbs"`)

- [ ] **Step 1: Fetch the real URLs, sizes, and md5s**

From `https://benlangmead.github.io/aws-indexes/k2`, pick the most recent
dated snapshot of each of: Standard-8, PlusPF-8, Viral. The tarballs live at
`https://genome-idx.s3.amazonaws.com/kraken/k2_<name>_<date>.tar.gz` and each
has a sibling `.md5` file. Copy URL, byte size (from a `curl -sI` on the
tarball, `Content-Length`), and md5 verbatim:

```bash
curl -sI https://genome-idx.s3.amazonaws.com/kraken/k2_standard_08gb_20240112.tar.gz | grep -i content-length
curl -s  https://genome-idx.s3.amazonaws.com/kraken/k2_standard_08gb_20240112.tar.gz.md5
```

(Adjust the date component to the newest snapshot listed on the index page;
never invent any of these three values. `mem_mb`: the 8 GB-capped databases
load in ~8 GB — use 9216 for standard-8/pluspf-8 as headroom; viral is
~0.6 GB — use 1024.)

- [ ] **Step 2: Write the failing tests**

```python
"""Internal consistency of the Kraken2 database registry.

Keys are this repo's own strings, not an enum's members, so the
registry-audit exhaustiveness pattern does not apply; these assert the
analogous internal-consistency invariants instead (spec K2-D4).
"""

from app.pipelines.kraken_db_registry import DEFAULT_DB, KRAKEN_DBS, db_present


def test_default_key_is_registered():
    assert DEFAULT_DB in KRAKEN_DBS


def test_every_entry_is_complete():
    for key, spec in KRAKEN_DBS.items():
        assert spec.key == key
        assert spec.label
        assert spec.url.startswith("https://")
        assert spec.url.endswith(".tar.gz")
        assert spec.download_bytes > 0
        assert spec.mem_mb > 0
        assert len(spec.md5) == 32
        assert spec.description


def test_urls_are_unique_and_pinned():
    urls = [s.url for s in KRAKEN_DBS.values()]
    assert len(urls) == len(set(urls))
    # A pinned snapshot has a date component; "latest" aliases drift.
    for url in urls:
        assert "latest" not in url


def test_db_present_requires_all_three_k2d_files(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings.__class__, "kraken_dbs_dir", property(lambda self: tmp_path), raising=False)
    d = tmp_path / "standard-8"
    d.mkdir()
    assert not db_present("standard-8")
    for name in ("hash.k2d", "opts.k2d", "taxo.k2d"):
        (d / name).write_bytes(b"x")
    assert db_present("standard-8")
```

(If the suite has an established way to point `settings` at tmp dirs — check
how `test_*` files near `tests/pipelines/` monkeypatch settings paths and
copy that instead of the `property` trick above.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_kraken_db_registry.py -q`
Expected: FAIL with `ModuleNotFoundError: app.pipelines.kraken_db_registry`

- [ ] **Step 4: Implement**

`backend/app/config.py`, next to `lineages_dir`:

```python
    @property
    def kraken_dbs_dir(self) -> Path:
        """Kraken2 classification databases, shared across every project.

        Reference data fetched by `download_kraken_db`, the same class of
        thing as `lineages_dir`: a classification job must not depend on
        the network, so what it reads here must already exist.
        """
        return self.bioinfo_home / "kraken_dbs"
```

`backend/app/pipelines/kraken_db_registry.py`:

```python
"""The Kraken2 databases this application offers, and where they live.

Three pre-built indexes from the Langmead k2 collection
(https://benlangmead.github.io/aws-indexes/k2), pinned to dated snapshots.
Full-size databases are deliberately absent: they need on the order of
100 GB RAM to load, which guarantees an OOM on the local machines this
application targets (spec 2026-08-18-kraken2-classification-design.md).

`mem_mb` is the in-RAM load size for the classify job's `JobResources` --
known a priori from the database, never fitted from the memory model,
because a model fit from unrelated jobs would under-provision exactly
into an OOM (spec K2-C3).
"""

from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class KrakenDbSpec:
    key: str
    label: str
    url: str            # pinned dated snapshot, never a "latest" alias
    download_bytes: int
    mem_mb: int         # in-RAM load size, with headroom
    md5: str            # of the tarball, from the index's own .md5 file
    description: str    # one line: what this database can see


DEFAULT_DB = "standard-8"

KRAKEN_DBS: dict[str, KrakenDbSpec] = {
    "standard-8": KrakenDbSpec(
        key="standard-8",
        label="Standard-8",
        url="<from Step 1>",
        download_bytes=0,  # <from Step 1>
        mem_mb=9216,
        md5="<from Step 1>",
        description=(
            "Archaea, bacteria, viruses, plasmids, human, and common "
            "vector contaminants -- the default for identity and "
            "contamination screening."
        ),
    ),
    "pluspf-8": KrakenDbSpec(
        key="pluspf-8",
        label="PlusPF-8",
        url="<from Step 1>",
        download_bytes=0,  # <from Step 1>
        mem_mb=9216,
        md5="<from Step 1>",
        description="Standard plus protozoa and fungi.",
    ),
    "viral": KrakenDbSpec(
        key="viral",
        label="Viral",
        url="<from Step 1>",
        download_bytes=0,  # <from Step 1>
        mem_mb=1024,
        md5="<from Step 1>",
        description=(
            "Viruses only -- small and fast, but blind to bacterial or "
            "human contamination."
        ),
    ),
}


def db_present(key: str) -> bool:
    """Whether this database is fully on disk.

    Checks the three .k2d files Kraken2 needs rather than the directory:
    the download handler extracts into a .partial dir and renames on
    success, so a bare directory with missing files means a bug rather
    than an in-flight download, and either way it must read as absent.
    """
    d = settings.kraken_dbs_dir / key
    return all((d / name).is_file() for name in ("hash.k2d", "opts.k2d", "taxo.k2d"))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_kraken_db_registry.py -q`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/pipelines/kraken_db_registry.py backend/app/config.py backend/tests/pipelines/test_kraken_db_registry.py
git commit -m "feat(pipelines): registry of three pinned kraken2 databases"
```

---

### Task 3: Runner — command construction

**Files:**
- Create: `backend/app/pipelines/kraken_runner.py`
- Test: `backend/tests/pipelines/test_kraken_runner.py`

**Interfaces:**
- Produces:
  - `build_kraken2_command(*, kraken2_path: str, db_dir: Path, reads: Path, mate: Path | None, report: Path, output: Path, threads: int, gzipped: bool) -> list[str]`
  - `build_bracken_command(*, bracken_path: str, db_dir: Path, report: Path, output: Path, read_len: int) -> list[str]`

- [ ] **Step 1: Write the failing tests**

```python
"""kraken_runner: pure functions, no binaries -- the quast_runner split."""

from pathlib import Path

from app.pipelines import kraken_runner


def test_kraken2_single_end_command():
    cmd = kraken_runner.build_kraken2_command(
        kraken2_path="kraken2",
        db_dir=Path("/data/kraken_dbs/standard-8"),
        reads=Path("/work/reads.fastq"),
        mate=None,
        report=Path("/work/report.txt"),
        output=Path("/dev/null"),
        threads=4,
        gzipped=False,
    )
    assert cmd[0] == "kraken2"
    assert "--db" in cmd and cmd[cmd.index("--db") + 1] == "/data/kraken_dbs/standard-8"
    assert "--report" in cmd
    assert "--paired" not in cmd
    assert "--gzip-compressed" not in cmd
    assert cmd[-1] == "/work/reads.fastq"


def test_kraken2_paired_gzipped_command():
    cmd = kraken_runner.build_kraken2_command(
        kraken2_path="kraken2",
        db_dir=Path("/db"),
        reads=Path("/work/r1.fastq.gz"),
        mate=Path("/work/r2.fastq.gz"),
        report=Path("/work/report.txt"),
        output=Path("/dev/null"),
        threads=8,
        gzipped=True,
    )
    assert "--paired" in cmd
    assert "--gzip-compressed" in cmd
    assert cmd[-2:] == ["/work/r1.fastq.gz", "/work/r2.fastq.gz"]
    assert "--memory-mapping" not in cmd  # memory is budgeted, not mapped (spec K2-R2)


def test_bracken_command():
    cmd = kraken_runner.build_bracken_command(
        bracken_path="bracken",
        db_dir=Path("/db"),
        report=Path("/work/report.txt"),
        output=Path("/work/bracken.tsv"),
        read_len=150,
    )
    assert cmd[0] == "bracken"
    assert cmd[cmd.index("-d") + 1] == "/db"
    assert cmd[cmd.index("-i") + 1] == "/work/report.txt"
    assert cmd[cmd.index("-o") + 1] == "/work/bracken.tsv"
    assert cmd[cmd.index("-r") + 1] == "150"
    assert cmd[cmd.index("-l") + 1] == "S"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_kraken_runner.py -q`
Expected: FAIL with module not found

- [ ] **Step 3: Implement**

```python
"""Kraken2/Bracken command construction and report parsing.

Same split ``quast_runner`` and ``bakta_runner`` use: pure functions over
strings and paths, testable without a container, a queue, or a binary.
"""

from __future__ import annotations

from pathlib import Path


def build_kraken2_command(
    *,
    kraken2_path: str,
    db_dir: Path,
    reads: Path,
    mate: Path | None,
    report: Path,
    output: Path,
    threads: int,
    gzipped: bool,
) -> list[str]:
    """The argv for ``kraken2`` over one read set.

    ``--memory-mapping`` is deliberately absent: it trades a full in-RAM
    load for page-in on every access, which is the slow path.  Memory is
    budgeted honestly instead, via the registry's per-database ``mem_mb``
    (spec K2-C3).  ``output`` is normally /dev/null -- the per-read
    assignments are enormous and nothing here consumes them; the report is
    the deliverable.
    """
    cmd = [
        kraken2_path,
        "--db", str(db_dir),
        "--threads", str(threads),
        "--report", str(report),
        "--output", str(output),
    ]
    if gzipped:
        cmd.append("--gzip-compressed")
    if mate is not None:
        cmd.append("--paired")
        cmd.extend([str(reads), str(mate)])
    else:
        cmd.append(str(reads))
    return cmd


def build_bracken_command(
    *,
    bracken_path: str,
    db_dir: Path,
    report: Path,
    output: Path,
    read_len: int,
) -> list[str]:
    """The argv for ``bracken`` over an existing Kraken2 report.

    ``-l S``: species-level re-estimation, the rank the taxonomy fact and
    the mismatch check consume.  ``read_len`` comes from the reads object's
    stored stats, defaulting to 100 (spec K2-R3) -- Bracken only accepts
    lengths its database distribution was built for, and the pre-built
    databases ship distributions for 50..300 in steps of 50, so the caller
    rounds to the nearest of those.
    """
    return [
        bracken_path,
        "-d", str(db_dir),
        "-i", str(report),
        "-o", str(output),
        "-r", str(read_len),
        "-l", "S",
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_kraken_runner.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/kraken_runner.py backend/tests/pipelines/test_kraken_runner.py
git commit -m "feat(pipelines): kraken2 and bracken command construction"
```

---

### Task 4: Runner — report parsers

**Files:**
- Modify: `backend/app/pipelines/kraken_runner.py`
- Modify: `backend/tests/pipelines/test_kraken_runner.py`

**Interfaces:**
- Produces:
  - `parse_kraken_report(text: str) -> list[dict]` — rows with keys `pct` (float), `clade_reads` (int), `direct_reads` (int), `rank` (str), `taxid` (int), `name` (str, whitespace-stripped)
  - `parse_bracken_output(text: str) -> list[dict]` — rows with keys `name` (str), `taxid` (int), `fraction` (float)

- [ ] **Step 1: Write the failing tests**

The fixtures below are the real formats (Kraken2 report: six tab-separated
columns, name indented by two spaces per level; Bracken: TSV with header).

```python
KRAKEN_REPORT = """\
 12.50\t1250\t1250\tU\t0\tunclassified
 87.50\t8750\t0\tR\t1\troot
 87.40\t8740\t12\tR1\t131567\t  cellular organisms
 87.00\t8700\t0\tD\t2\t    Bacteria
 86.20\t8620\t8000\tS\t562\t          Escherichia coli
  1.10\t110\t110\tS\t1280\t          Staphylococcus aureus
"""

BRACKEN_OUTPUT = """\
name\ttaxonomy_id\ttaxonomy_lvl\tkraken_assigned_reads\tadded_reads\tnew_est_reads\tfraction_total_reads
Escherichia coli\t562\tS\t8000\t620\t8620\t0.98514
Staphylococcus aureus\t1280\tS\t110\t20\t130\t0.01486
"""


def test_parse_kraken_report():
    rows = kraken_runner.parse_kraken_report(KRAKEN_REPORT)
    assert len(rows) == 6
    unclassified = rows[0]
    assert unclassified == {
        "pct": 12.5, "clade_reads": 1250, "direct_reads": 1250,
        "rank": "U", "taxid": 0, "name": "unclassified",
    }
    ecoli = next(r for r in rows if r["taxid"] == 562)
    assert ecoli["name"] == "Escherichia coli"  # indentation stripped
    assert ecoli["rank"] == "S"
    assert ecoli["pct"] == 86.2


def test_parse_kraken_report_garbage_is_empty():
    assert kraken_runner.parse_kraken_report("") == []
    assert kraken_runner.parse_kraken_report("not\ta\treport") == []
    # A malformed line is skipped, not fatal
    assert len(kraken_runner.parse_kraken_report(KRAKEN_REPORT + "bad line\n")) == 6


def test_parse_bracken_output():
    rows = kraken_runner.parse_bracken_output(BRACKEN_OUTPUT)
    assert rows == [
        {"name": "Escherichia coli", "taxid": 562, "fraction": 0.98514},
        {"name": "Staphylococcus aureus", "taxid": 1280, "fraction": 0.01486},
    ]


def test_parse_bracken_garbage_is_empty():
    assert kraken_runner.parse_bracken_output("") == []
    assert kraken_runner.parse_bracken_output("no\ttabs\there\n") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_kraken_runner.py -q`
Expected: new tests FAIL with AttributeError

- [ ] **Step 3: Implement**

```python
def parse_kraken_report(text: str) -> list[dict]:
    """Rows of Kraken2's six-column report.

    Columns: percentage of reads in the clade, clade read count, reads
    assigned directly to this taxon, rank code, NCBI taxid, and the name
    indented two spaces per tree level (stripped here -- the report is a
    flat fact source, not a tree render).

    Returns ``[]`` for anything unparseable rather than raising, the
    posture ``quast_runner.parse_report_tsv`` documents: a report that
    cannot be read must not fail a run that already produced real output.
    """
    rows: list[dict] = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        try:
            rows.append(
                {
                    "pct": float(fields[0]),
                    "clade_reads": int(fields[1]),
                    "direct_reads": int(fields[2]),
                    "rank": fields[3].strip(),
                    "taxid": int(fields[4]),
                    "name": fields[5].strip(),
                }
            )
        except ValueError:
            continue
    return rows


def parse_bracken_output(text: str) -> list[dict]:
    """Rows of Bracken's species table: name, taxid, abundance fraction.

    Same empty-on-garbage posture as ``parse_kraken_report``.
    """
    rows: list[dict] = []
    lines = text.splitlines()
    for line in lines[1:]:  # skip header
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        try:
            rows.append(
                {
                    "name": fields[0].strip(),
                    "taxid": int(fields[1]),
                    "fraction": float(fields[6]),
                }
            )
        except ValueError:
            continue
    return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_kraken_runner.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/kraken_runner.py backend/tests/pipelines/test_kraken_runner.py
git commit -m "feat(pipelines): parse kraken2 and bracken reports"
```

---

### Task 5: Runner — top taxa and organism mismatch

**Files:**
- Modify: `backend/app/pipelines/kraken_runner.py`
- Modify: `backend/tests/pipelines/test_kraken_runner.py`

**Interfaces:**
- Produces:
  - `top_taxa(kraken_rows: list[dict], bracken_rows: list[dict]) -> dict` — the `taxonomy` fact payload: `{"taxa": [{"name", "rank", "taxid", "pct"}...], "unclassified_pct": float, "bracken_used": bool}`. Taxa are the top 10 by abundance plus every taxon at >= 1%, Bracken preferred, Kraken2 species rows (rank "S") the fallback (spec K2-R6).
  - `organism_mismatch(metadata_organism: str | None, kraken_rows: list[dict]) -> dict | None` — `None` when metadata absent or genus found among dominant taxa (>= 5% clade reads); else `{"claimed": str, "dominant": [{"name", "pct"}...]}` (spec K2-R7).

- [ ] **Step 1: Write the failing tests**

```python
def _kr(pct, clade, direct, rank, taxid, name):
    return {"pct": pct, "clade_reads": clade, "direct_reads": direct,
            "rank": rank, "taxid": taxid, "name": name}


def test_top_taxa_prefers_bracken():
    kraken = [
        _kr(12.5, 1250, 1250, "U", 0, "unclassified"),
        _kr(86.2, 8620, 8000, "S", 562, "Escherichia coli"),
    ]
    bracken = [
        {"name": "Escherichia coli", "taxid": 562, "fraction": 0.985},
        {"name": "Staphylococcus aureus", "taxid": 1280, "fraction": 0.015},
    ]
    result = kraken_runner.top_taxa(kraken, bracken)
    assert result["bracken_used"] is True
    assert result["unclassified_pct"] == 12.5
    assert result["taxa"][0] == {
        "name": "Escherichia coli", "rank": "S", "taxid": 562, "pct": 98.5,
    }


def test_top_taxa_falls_back_to_kraken_species():
    kraken = [
        _kr(12.5, 1250, 1250, "U", 0, "unclassified"),
        _kr(86.2, 8620, 8000, "S", 562, "Escherichia coli"),
        _kr(0.5, 50, 50, "S", 1280, "Staphylococcus aureus"),
    ]
    result = kraken_runner.top_taxa(kraken, [])
    assert result["bracken_used"] is False
    assert [t["name"] for t in result["taxa"]] == [
        "Escherichia coli", "Staphylococcus aureus",
    ]


def test_top_taxa_keeps_top_ten_plus_one_percent():
    # 12 species at 0.5% each after two big ones: top 10 kept, plus all >=1%
    kraken = [_kr(30.0, 3000, 3000, "S", 100 + i, f"Species {i}") for i in range(2)]
    kraken += [_kr(0.5, 50, 50, "S", 200 + i, f"Minor {i}") for i in range(12)]
    result = kraken_runner.top_taxa(kraken, [])
    assert len(result["taxa"]) == 10


def test_mismatch_fires_when_genus_absent():
    kraken = [
        _kr(94.0, 9400, 9000, "S", 1280, "Staphylococcus aureus"),
        _kr(2.0, 200, 200, "S", 562, "Escherichia coli"),
    ]
    result = kraken_runner.organism_mismatch("Escherichia coli", kraken)
    assert result == {
        "claimed": "Escherichia coli",
        "dominant": [{"name": "Staphylococcus aureus", "pct": 94.0}],
    }


def test_mismatch_silent_when_genus_dominant():
    kraken = [_kr(94.0, 9400, 9000, "S", 562, "Escherichia coli")]
    assert kraken_runner.organism_mismatch("Escherichia coli K-12", kraken) is None


def test_mismatch_silent_without_metadata():
    kraken = [_kr(94.0, 9400, 9000, "S", 1280, "Staphylococcus aureus")]
    assert kraken_runner.organism_mismatch(None, kraken) is None
    assert kraken_runner.organism_mismatch("  ", kraken) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_kraken_runner.py -q`
Expected: new tests FAIL with AttributeError

- [ ] **Step 3: Implement**

```python
# The fact payload's selection rule (spec K2-R6): enough taxa to see the
# picture, few enough to stay a fact rather than a report.
_TOP_N = 10
_MIN_PCT = 1.0
# A taxon is "dominant" for the mismatch check at >= 5% of clade reads
# (spec K2-R7): low enough to catch a heavily contaminated sample, high
# enough that trace noise never accuses the metadata.
_DOMINANT_PCT = 5.0


def top_taxa(kraken_rows: list[dict], bracken_rows: list[dict]) -> dict:
    """The ``taxonomy`` fact payload.

    Bracken's species fractions are preferred; Kraken2's own species rows
    (rank ``S``) are the fallback when Bracken was skipped (spec K2-R6).
    Selection: top 10 by abundance, plus every taxon at >= 1% -- which for
    a clean single-organism sample is one row, and for a contaminated one
    is the evidence.
    """
    unclassified = next(
        (r["pct"] for r in kraken_rows if r["rank"] == "U"), 0.0
    )
    if bracken_rows:
        candidates = [
            {
                "name": r["name"],
                "rank": "S",
                "taxid": r["taxid"],
                "pct": round(r["fraction"] * 100, 2),
            }
            for r in bracken_rows
        ]
        used = True
    else:
        candidates = [
            {"name": r["name"], "rank": r["rank"], "taxid": r["taxid"], "pct": r["pct"]}
            for r in kraken_rows
            if r["rank"] == "S"
        ]
        used = False

    candidates.sort(key=lambda t: t["pct"], reverse=True)
    taxa = [t for i, t in enumerate(candidates) if i < _TOP_N or t["pct"] >= _MIN_PCT]
    return {"taxa": taxa, "unclassified_pct": unclassified, "bracken_used": used}


def organism_mismatch(
    metadata_organism: str | None, kraken_rows: list[dict]
) -> dict | None:
    """Whether the reads disagree with ``metadata["organism"]``.

    Genus-level on purpose: strain and species names in metadata are too
    free-form to match reliably, and a genus-level miss is already a real
    problem.  Absent metadata means no check and no fact -- "not stated"
    and "wrong" are different claims (spec K2-R7).  Returns the evidence
    dict for the ``taxonomy_mismatch`` fact, or None.
    """
    if not metadata_organism or not metadata_organism.strip():
        return None
    claimed_genus = metadata_organism.strip().split()[0].lower()

    dominant = [
        r for r in kraken_rows
        if r["rank"] == "S" and r["pct"] >= _DOMINANT_PCT
    ]
    if not dominant:
        # Nothing classified confidently enough to accuse the metadata.
        return None
    for row in dominant:
        if row["name"].strip().split()[0].lower() == claimed_genus:
            return None
    return {
        "claimed": metadata_organism.strip(),
        "dominant": [{"name": r["name"], "pct": r["pct"]} for r in dominant],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_kraken_runner.py -q`
Expected: PASS (all runner tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/kraken_runner.py backend/tests/pipelines/test_kraken_runner.py
git commit -m "feat(pipelines): taxonomy fact selection and organism mismatch check"
```

---

### Task 6: Database download job

**Files:**
- Create: `backend/app/queue/kraken_handlers.py`
- Modify: `backend/app/queue/handlers.py` (add the registration import beside `lineage_handlers`)
- Modify: `backend/app/services/pipeline_service.py` (add `launch_kraken_db_download` next to `launch_lineage_download`, ~line 4843)
- Modify: `backend/app/pipelines/node_types.py` (add `download_kraken_db` NodeTypeSpec next to `download_lineage`, ~line 685)
- Test: `backend/tests/queue/test_kraken_handlers.py`
- Test: existing `backend/tests/pipelines/test_node_types.py` `TestExhaustiveness` (run the whole class)

**Interfaces:**
- Consumes: `KRAKEN_DBS`, `db_present`, `settings.kraken_dbs_dir` (Task 2)
- Produces:
  - `pipeline_service.launch_kraken_db_download(*, db_key: str, owner: str) -> Job` — enqueues `"download_kraken_db"` with `dedup_key=f"download_kraken_db:{db_key}"`
  - handler `download_kraken_db` — downloads, md5-verifies, extracts to `<key>.partial`, renames into place

- [ ] **Step 1: Read the neighbors first**

Read `backend/app/queue/lineage_handlers.py` in full (105 lines) and
`pipeline_service.launch_lineage_download` (~line 4843). The new code is a
deliberate near-copy; read `backend/app/queue/handlers.py`'s import block to
see how registration-side-effect imports are written there.

- [ ] **Step 2: Write the failing tests**

Unit-test the pure parts: md5 verification and the atomic-extract layout.
Structure the handler so those are testable functions, subprocess-free:

```python
"""download_kraken_db's verify-and-promote steps, without the network."""

import hashlib
import tarfile

import pytest

from app.errors import PermanentError
from app.queue import kraken_handlers


def _tarball_with(tmp_path, inner_files):
    src = tmp_path / "src"
    src.mkdir()
    for name in inner_files:
        (src / name).write_bytes(b"data-" + name.encode())
    tb = tmp_path / "db.tar.gz"
    with tarfile.open(tb, "w:gz") as tf:
        for name in inner_files:
            tf.add(src / name, arcname=name)
    return tb


def test_verify_md5_accepts_matching(tmp_path):
    tb = _tarball_with(tmp_path, ["hash.k2d"])
    digest = hashlib.md5(tb.read_bytes()).hexdigest()
    kraken_handlers.verify_md5(tb, digest)  # no raise


def test_verify_md5_rejects_mismatch(tmp_path):
    tb = _tarball_with(tmp_path, ["hash.k2d"])
    with pytest.raises(PermanentError):
        kraken_handlers.verify_md5(tb, "0" * 32)


def test_extract_and_promote_is_atomic(tmp_path):
    tb = _tarball_with(tmp_path, ["hash.k2d", "opts.k2d", "taxo.k2d"])
    final = tmp_path / "dbs" / "standard-8"
    kraken_handlers.extract_and_promote(tb, final)
    assert (final / "hash.k2d").is_file()
    assert not (final.parent / "standard-8.partial").exists()


def test_extract_failure_leaves_no_final_dir(tmp_path):
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"this is not a tarball")
    final = tmp_path / "dbs" / "standard-8"
    with pytest.raises(Exception):
        kraken_handlers.extract_and_promote(bad, final)
    assert not final.exists()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/queue/test_kraken_handlers.py -q`
Expected: FAIL with module not found

- [ ] **Step 4: Implement the handler module**

`backend/app/queue/kraken_handlers.py`:

```python
"""Downloading a Kraken2 classification database.

Modelled on `lineage_handlers`: fetches reference data shared across every
project, not something derived from one object.  There is no applier -- a
successful run leaves files under `settings.kraken_dbs_dir / <key>` and
nothing else changes state; `launch_classify_reads` checks presence via
`kraken_db_registry.db_present` and chains behind this job when absent.

Unlike compleasm, Kraken2 has no self-managing downloader, so integrity is
this handler's own job: verify the tarball's md5 against the registry,
extract into `<key>.partial`, and rename into place only on success -- a
killed or corrupt download never half-presents (spec K2-N3).

Imported by `handlers.py` for the `@handler` registration side effects.
"""

import hashlib
import shutil
import tarfile
import urllib.request
from pathlib import Path

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines.kraken_db_registry import KRAKEN_DBS
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

_DOWNLOAD_LEASE_SECONDS = 2 * 3600  # 7.5 GB on a slow line takes a while


def verify_md5(tarball: Path, expected: str) -> None:
    """Raise PermanentError when the tarball does not match the registry.

    Permanent rather than retryable on its own: the *job* retries by
    re-downloading (max_attempts=3), but a mismatched file must never be
    extracted, and the message must say which database and why.
    """
    h = hashlib.md5()
    with tarball.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    got = h.hexdigest()
    if got != expected:
        raise PermanentError(
            f"downloaded database failed md5 verification "
            f"(expected {expected}, got {got}) -- corrupt or altered download"
        )


def extract_and_promote(tarball: Path, final_dir: Path) -> None:
    """Extract into `<final>.partial`, rename to `final_dir` on success.

    The rename is the commit point: `db_present()` reads the final path, so
    an interrupted extraction is invisible to every consumer.  The k2
    tarballs place their .k2d files at the archive root.
    """
    partial = final_dir.parent / (final_dir.name + ".partial")
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)
    try:
        with tarfile.open(tarball, "r:gz") as tf:
            tf.extractall(partial, filter="data")
        if final_dir.exists():
            shutil.rmtree(final_dir)
        partial.rename(final_dir)
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise


@handler(
    "download_kraken_db",
    mode=HandlerMode.SUBPROCESS,
    # USER_INTERACTIVE: someone pressed the classify card and is waiting,
    # the same reasoning download_lineage gives.
    job_class=JobClass.USER_INTERACTIVE,
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
    max_attempts=3,
)
def download_kraken_db(ctx: JobContext) -> dict:
    """Fetch one classification database into the shared store.

    Idempotent: an already-present database returns immediately, so the
    dedup collapse in `launch_kraken_db_download` plus this check means a
    re-run is a fast no-op rather than a duplicate 7.5 GB download.
    """
    from app.pipelines.kraken_db_registry import db_present

    key = (ctx.payload.get("db_key") or "").strip()
    spec = KRAKEN_DBS.get(key)
    if spec is None:
        raise PermanentError(f"unknown kraken database {key!r}")

    if db_present(key):
        return {"db_key": key, "already_present": True}

    settings.kraken_dbs_dir.mkdir(parents=True, exist_ok=True)
    tarball = settings.kraken_dbs_dir / f"{key}.tar.gz.partial"

    ctx.progress(phase="downloading", pct=None, message=f"downloading {spec.label}")
    ctx.extend_lease(_DOWNLOAD_LEASE_SECONDS)
    log.info("kraken_db_download_started", job_id=ctx.job_id, db_key=key)

    try:
        with urllib.request.urlopen(spec.url, timeout=60) as resp, tarball.open("wb") as out:
            copied = 0
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                copied += len(chunk)
                if spec.download_bytes:
                    ctx.progress(
                        phase="downloading",
                        pct=min(copied / spec.download_bytes, 0.99),
                        message=f"downloading {spec.label}",
                    )
    except Exception:
        tarball.unlink(missing_ok=True)
        raise  # retryable: the usual failure is the network

    try:
        ctx.progress(phase="verifying", pct=None, message="verifying checksum")
        verify_md5(tarball, spec.md5)
        ctx.progress(phase="extracting", pct=None, message="extracting database")
        extract_and_promote(tarball, settings.kraken_dbs_dir / key)
    finally:
        tarball.unlink(missing_ok=True)

    if not db_present(key):
        raise PermanentError(
            f"{spec.label} extracted but its .k2d files are missing -- "
            "the tarball layout may have changed upstream"
        )

    ctx.progress(phase="done", pct=1.0, message=f"{spec.label} ready")
    log.info("kraken_db_download_finished", job_id=ctx.job_id, db_key=key)
    return {"db_key": key, "path": str(settings.kraken_dbs_dir / key)}
```

Check how the sibling download handlers fetch over HTTP first — if the repo
has an established helper (e.g. whatever `uniprot_handlers` or
`ncbi_assembly_handlers` use for streaming downloads with progress), use
that instead of raw `urllib`, keeping the verify/extract/promote functions
exactly as tested.

Add to `backend/app/queue/handlers.py`'s import block, beside
`lineage_handlers`:

```python
from app.queue import kraken_handlers  # noqa: F401  -- @handler registration
```

(match the exact comment style of the neighboring imports).

- [ ] **Step 5: Add the launcher**

In `pipeline_service.py`, directly after `launch_lineage_download`:

```python
async def launch_kraken_db_download(*, db_key: str, owner: str) -> Job:
    """Queue fetching one Kraken2 classification database.

    A dependency of `launch_classify_reads`, not something it fetches
    inline -- a classification job must not depend on the network partway
    through, the same reasoning `launch_lineage_download` records.
    """
    from app.pipelines.kraken_db_registry import KRAKEN_DBS
    from app.queue import queue

    if db_key not in KRAKEN_DBS:
        raise ValidationError(
            f"Unknown Kraken2 database {db_key!r}",
            details={"db_key": db_key},
        )

    return await queue.enqueue(
        "download_kraken_db",
        owner=owner,
        payload={"db_key": db_key},
        job_class=JobClass.USER_INTERACTIVE,
        resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
        max_attempts=3,
        # One download per database at a time, project-agnostic: the store
        # is shared, so two projects requesting the same database collapse
        # into one job rather than downloading 7.5 GB twice concurrently.
        dedup_key=f"download_kraken_db:{db_key}",
    )
```

- [ ] **Step 6: Register the node type**

In `node_types.py`, directly after the `"download_lineage"` entry, a
near-copy with the same comment structure:

```python
    "download_kraken_db": NodeTypeSpec(
        label="Download Kraken2 database",
        launch_name="pipeline_service.launch_kraken_db_download",
        launch=_launch_kraken_db_download,
        # No PipelineRun: fetches a project-agnostic, shared reference
        # dataset from the network, not something derived from an object in
        # a project -- the download_lineage shape exactly.
        run_kind=None,
        # No object inputs: `db_key` is a string chosen in a dialog.
        inputs=(),
        # No DataObject either -- the dataset lands under
        # settings.kraken_dbs_dir, outside the object model, and is consumed
        # by launch_classify_reads checking db_present().
        outputs=(),
    ),
```

with the adapter beside the other `_launch_*` helpers:

```python
async def _launch_kraken_db_download(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_kraken_db_download(
        db_key=params["db_key"], owner=owner
    )
```

- [ ] **Step 7: Run the tests**

Run: `./backend/run-worktree-tests.sh tests/queue/test_kraken_handlers.py tests/pipelines/test_node_types.py -q`
Expected: PASS — the new handler tests, and the *entire* node_types file
including `TestExhaustiveness` (both partition tests; the #355/#366 lesson).

- [ ] **Step 8: Commit**

```bash
git add backend/app/queue/kraken_handlers.py backend/app/queue/handlers.py backend/app/services/pipeline_service.py backend/app/pipelines/node_types.py backend/tests/queue/test_kraken_handlers.py
git commit -m "feat(pipelines): on-demand kraken2 database download job"
```

---

### Task 7: Classification job — launcher, handler, applier

**Files:**
- Modify: `backend/app/services/pipeline_service.py` (add `launch_classify_reads`)
- Modify: `backend/app/queue/kraken_handlers.py` (add the `classify_reads` handler)
- Modify: `backend/app/queue/results.py` (applier `_apply_classify_reads` + dict registration at ~line 3036)
- Modify: `backend/app/pipelines/node_types.py` (`EXCLUDED_LAUNCHES` entry)
- Test: `backend/tests/queue/test_kraken_handlers.py` (facts-assembly unit test)
- Test: `backend/tests/pipelines/test_node_types.py` (full `TestExhaustiveness`)

**Interfaces:**
- Consumes: `build_kraken2_command`, `build_bracken_command`, `parse_kraken_report`, `parse_bracken_output`, `top_taxa`, `organism_mismatch` (Tasks 3–5); `db_present`, `KRAKEN_DBS` (Task 2); `launch_kraken_db_download` (Task 6); `_resolve_input`, `_prepare_workdir`, `_failure` from `queue/pipeline_handlers.py`.
- Produces:
  - `pipeline_service.launch_classify_reads(*, object_id: PydanticObjectId, db_key: str, owner: str, mate_object_id: PydanticObjectId | None = None) -> Job`
  - handler `classify_reads` returning `{"object_id", "job_id", "facts": {"taxonomy": ..., "taxonomy_mismatch"?: ...}}`
  - applier merging `facts` onto the object (the `_apply_annotate_genome` shape)

- [ ] **Step 1: Write the failing test for facts assembly**

The handler's parse-to-facts step is a pure function; test it directly:

```python
def test_build_classification_facts_with_mismatch():
    kraken_rows = [
        {"pct": 5.0, "clade_reads": 500, "direct_reads": 500,
         "rank": "U", "taxid": 0, "name": "unclassified"},
        {"pct": 94.0, "clade_reads": 9400, "direct_reads": 9000,
         "rank": "S", "taxid": 1280, "name": "Staphylococcus aureus"},
    ]
    facts = kraken_handlers.build_classification_facts(
        kraken_rows=kraken_rows,
        bracken_rows=[],
        metadata_organism="Escherichia coli",
        db_key="standard-8",
        bracken_note=None,
    )
    tax = facts["taxonomy"]
    assert tax["db_key"] == "standard-8"
    assert tax["bracken_used"] is False
    assert tax["taxa"][0]["name"] == "Staphylococcus aureus"
    assert facts["taxonomy_mismatch"]["claimed"] == "Escherichia coli"


def test_build_classification_facts_records_bracken_skip():
    kraken_rows = [
        {"pct": 1.0, "clade_reads": 100, "direct_reads": 100,
         "rank": "S", "taxid": 562, "name": "Escherichia coli"},
    ]
    facts = kraken_handlers.build_classification_facts(
        kraken_rows=kraken_rows,
        bracken_rows=[],
        metadata_organism=None,
        db_key="viral",
        bracken_note="bracken exited 1",
    )
    assert facts["taxonomy"]["bracken_skipped"] == "bracken exited 1"
    assert "taxonomy_mismatch" not in facts
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/queue/test_kraken_handlers.py -q`
Expected: new tests FAIL with AttributeError

- [ ] **Step 3: Implement `build_classification_facts` and the handler**

In `kraken_handlers.py`:

```python
def build_classification_facts(
    *,
    kraken_rows: list[dict],
    bracken_rows: list[dict],
    metadata_organism: str | None,
    db_key: str,
    bracken_note: str | None,
) -> dict:
    """The facts payload for one classification run (spec K2-H2).

    `taxonomy` always; `taxonomy_mismatch` only when the check fires --
    its absence is itself the "metadata agrees or is absent" claim.
    """
    from app.pipelines import kraken_runner

    taxonomy = kraken_runner.top_taxa(kraken_rows, bracken_rows)
    taxonomy["db_key"] = db_key
    if bracken_note:
        taxonomy["bracken_skipped"] = bracken_note

    facts: dict = {"taxonomy": taxonomy}
    mismatch = kraken_runner.organism_mismatch(metadata_organism, kraken_rows)
    if mismatch is not None:
        facts["taxonomy_mismatch"] = mismatch
    return facts
```

Then the handler, following `annotate_genome`'s structure
(`assembly_qc_handlers.py:1314-1440` — read it first):

```python
_CLASSIFY_LEASE_SECONDS = 2 * 3600


@handler(
    "classify_reads",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # mem_mb here is the floor; launch_classify_reads overrides per
    # database from the registry (spec K2-C3).
    resources=JobResources(cpu=4, mem_mb=9216, io=IoClass.HEAVY),
    max_attempts=1,
)
def classify_reads(ctx: JobContext) -> dict:
    """Classify one read set against a Kraken2 database, refine with Bracken.

    Kraken2 failure fails the run; Bracken failure or an unusable
    distribution is recorded in the facts and the run succeeds with
    Kraken2-only results (spec K2-H1).  Reports are copied to
    `qc_reports/<object_id>/kraken2/` -- the same shelf the QUAST and fastp
    reports use -- non-fatally.
    """
    from app.pipelines import kraken_runner, tools
    from app.pipelines.kraken_db_registry import KRAKEN_DBS, db_present
    from app.queue.pipeline_handlers import _failure, _prepare_workdir, _resolve_input

    kraken_tool = tools.require(tools.kraken2())

    db_key = (ctx.payload.get("db_key") or "").strip()
    if db_key not in KRAKEN_DBS or not db_present(db_key):
        raise PermanentError(
            f"kraken database {db_key!r} is not on disk -- the download "
            "dependency should have run first"
        )
    db_dir = settings.kraken_dbs_dir / db_key

    work = _prepare_workdir(ctx, "classify")
    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    reads = _resolve_input(ctx.payload, "reads")
    mate = None
    if ctx.payload.get("mate_sha256") or ctx.payload.get("mate_path"):
        mate = _resolve_input(ctx.payload, "mate")

    report = work / "kraken2_report.txt"
    bracken_out = work / "bracken_species.tsv"

    ctx.progress(phase="classifying", pct=None, message="running Kraken2")
    ctx.extend_lease(_CLASSIFY_LEASE_SECONDS)

    cmd = kraken_runner.build_kraken2_command(
        kraken2_path=kraken_tool.path,
        db_dir=db_dir,
        reads=reads,
        mate=mate,
        report=report,
        output=Path("/dev/null"),
        threads=max(1, int(ctx.payload.get("threads") or 4)),
        gzipped=reads.suffix == ".gz",
    )
    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "kraken2")

    kraken_rows = kraken_runner.parse_kraken_report(
        report.read_text() if report.exists() else ""
    )

    # -- Bracken: non-fatal refinement --------------------------------
    bracken_rows: list[dict] = []
    bracken_note: str | None = None
    bracken_tool = tools.bracken()
    if not bracken_tool.available:
        bracken_note = "bracken is not installed"
    else:
        ctx.progress(phase="abundance", pct=None, message="running Bracken")
        read_len = _nearest_bracken_read_len(ctx.payload.get("mean_read_length"))
        bcmd = kraken_runner.build_bracken_command(
            bracken_path=bracken_tool.path,
            db_dir=db_dir,
            report=report,
            output=bracken_out,
            read_len=read_len,
        )
        bcode = run_subprocess(ctx, bcmd, log_path=str(log_path))
        if bcode != 0:
            bracken_note = f"bracken exited {bcode}"
            log.warning("bracken_failed", job_id=ctx.job_id, code=bcode)
        else:
            bracken_rows = kraken_runner.parse_bracken_output(
                bracken_out.read_text() if bracken_out.exists() else ""
            )

    facts = build_classification_facts(
        kraken_rows=kraken_rows,
        bracken_rows=bracken_rows,
        metadata_organism=ctx.payload.get("organism"),
        db_key=db_key,
        bracken_note=bracken_note,
    )

    _copy_kraken_reports(ctx, report, bracken_out)

    ctx.progress(phase="done", pct=1.0, message="classification complete")
    log.info(
        "classification_finished",
        job_id=ctx.job_id,
        taxa=len(facts["taxonomy"]["taxa"]),
        mismatch="taxonomy_mismatch" in facts,
    )
    return {
        "object_id": ctx.payload.get("object_id"),
        "job_id": ctx.job_id,
        "facts": facts,
    }


def _nearest_bracken_read_len(mean: object) -> int:
    """Bracken only accepts lengths its distributions were built for:
    50..300 in steps of 50 on the pre-built databases.  Default 100
    (spec K2-R3)."""
    try:
        value = float(mean)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 100
    return min((50, 100, 150, 200, 250, 300), key=lambda s: abs(s - value))


def _copy_kraken_reports(ctx: JobContext, report: Path, bracken_out: Path) -> None:
    """Copy the raw reports where the QC-report endpoint serves them.

    Non-fatal, the `_copy_report` posture in assembly_qc_handlers: a run
    that produced real facts must not fail over an artifact copy.
    """
    object_id = ctx.payload.get("object_id")
    if not object_id:
        return
    dest = settings.qc_reports_dir / str(object_id) / "kraken2"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        if report.exists():
            shutil.copyfile(report, dest / "kraken2_report.txt")
        if bracken_out.exists():
            shutil.copyfile(bracken_out, dest / "bracken_species.tsv")
    except OSError:
        log.warning("kraken_report_copy_failed", job_id=ctx.job_id)
```

Add the needed imports to `kraken_handlers.py` (`shutil` is already there
from Task 6; add `Path`, `run_subprocess` from `app.queue.executor`).
Also add a unit test for `_nearest_bracken_read_len` (None → 100, 140 →
150, 500 → 300).

- [ ] **Step 4: Implement the launcher**

In `pipeline_service.py`, after `launch_kraken_db_download`. Read
`launch_annotate_genome` (~line 5200) first and mirror its
object-resolution steps:

```python
async def launch_classify_reads(
    *,
    object_id: PydanticObjectId,
    db_key: str,
    owner: str,
    mate_object_id: PydanticObjectId | None = None,
    resource_override: bool = False,
) -> Job:
    """Queue Kraken2 classification for one FASTQ read set.

    Facts-only, no PipelineRun -- the launch_annotate_genome shape.  When
    the chosen database is not on disk, the download job is enqueued
    (deduped) and this job chains behind it via depends_on, the same shape
    launch_completeness uses for a missing lineage (spec K2-C2).  Memory is
    declared from the registry's known load size, never the fitted model
    (spec K2-C3).
    """
    from app.pipelines.kraken_db_registry import KRAKEN_DBS, db_present
    from app.queue import queue
    from app.services import object_service

    spec = KRAKEN_DBS.get(db_key)
    if spec is None:
        raise ValidationError(
            f"Unknown Kraken2 database {db_key!r}", details={"db_key": db_key}
        )

    refuse_if_over_budget(
        declared_mb=spec.mem_mb,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    tools.require(tools.kraken2())

    obj = await object_service.get_object(object_id, owner=owner)
    if obj.format.kind is not FormatKind.FASTQ:
        raise ValidationError(
            "Classification runs on FASTQ reads",
            details={"object_id": str(obj.id), "format": obj.format.kind.value},
        )

    digest, path = await _resolve_readable(obj)
    if not digest and not path:
        raise ValidationError(
            f"{obj.name!r} has no stored content yet (status={obj.status.value})",
            details={"object_id": str(obj.id)},
        )

    payload: dict = {
        "object_id": str(obj.id),
        "db_key": db_key,
        "organism": (obj.metadata or {}).get("organism"),
        "mean_read_length": (obj.facts or {}).get("mean_read_length"),
        "threads": 4,
    }
    if digest:
        payload["reads_sha256"] = digest
    if path:
        payload["reads_path"] = str(path)

    if mate_object_id is not None:
        mate = await object_service.get_object(mate_object_id, owner=owner)
        m_digest, m_path = await _resolve_readable(mate)
        if m_digest:
            payload["mate_sha256"] = m_digest
        if m_path:
            payload["mate_path"] = str(m_path)

    depends_on: list[PydanticObjectId] = []
    if not db_present(db_key):
        download = await launch_kraken_db_download(db_key=db_key, owner=owner)
        depends_on.append(download.id)

    return await queue.enqueue(
        "classify_reads",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=4, mem_mb=spec.mem_mb, io=IoClass.HEAVY),
        max_attempts=1,
        depends_on=depends_on,
    )
```

Check the exact keyword `queue.enqueue` uses for dependencies (read
`launch_completeness` ~line 4875 for the chaining call it makes) and the
exact fact key for mean read length (grep `obj.facts` for what the QC stats
applier stores — use the real key, or omit the field if none exists and let
the handler default to 100). Verify `_resolve_readable` is the right
resolver by reading its use in `launch_annotate_genome`.

- [ ] **Step 5: Applier + EXCLUDED_LAUNCHES**

In `results.py`, next to `_apply_annotate_genome` (~line 2175), a near-copy:

```python
async def _apply_classify_reads(result: dict, *, owner: str) -> None:
    """Record classification facts on the reads object they describe.

    Near-copy of ``_apply_annotate_genome``: read-only, no files to
    ingest.  A prior ``taxonomy_mismatch`` is cleared when the new run has
    none -- reclassifying against a better database must be able to
    retract the accusation, not merely restate it.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("classification_object_missing", object_id=object_id)
        return

    merged = {**obj.facts, **facts}
    if "taxonomy_mismatch" not in facts:
        merged.pop("taxonomy_mismatch", None)

    await obj.set(
        {
            DataObject.facts: merged,
            DataObject.updated_at: datetime.now(UTC),
        }
    )
    log.info(
        "classification_applied",
        object_id=object_id,
        taxa=len((facts.get("taxonomy") or {}).get("taxa") or []),
        mismatch="taxonomy_mismatch" in facts,
    )
```

Register it in the applier dict (~line 3036): `"classify_reads":
_apply_classify_reads,`.

In `node_types.py`'s `EXCLUDED_LAUNCHES`, beside `launch_annotate_genome`:

```python
        # Read-only classification -- taxonomy facts on an existing reads
        # object, same class as gc_tracks and annotate_genome.  The db
        # download it may chain is the download_kraken_db node type.
        "pipeline_service.launch_classify_reads",
```

- [ ] **Step 6: Run the tests**

Run: `./backend/run-worktree-tests.sh tests/queue/test_kraken_handlers.py tests/pipelines/test_node_types.py -q`
Expected: PASS, including the full `TestExhaustiveness` class.

- [ ] **Step 7: Commit**

```bash
git add backend/app/queue/kraken_handlers.py backend/app/queue/results.py backend/app/services/pipeline_service.py backend/app/pipelines/node_types.py backend/tests/queue/test_kraken_handlers.py
git commit -m "feat(pipelines): classify_reads job with taxonomy facts and mismatch check"
```

---

### Task 8: API routes

**Files:**
- Modify: `backend/app/api/v1/pipelines.py` (two routes, next to `/annotate-genome` ~line 1847)
- Test: `backend/tests/api/` — follow the file organization the existing pipeline-route tests use (find the test file covering `/pipelines/annotate-genome` and put these beside it)

**Interfaces:**
- Consumes: `launch_classify_reads`, `launch_kraken_db_download` (Tasks 6–7), `KRAKEN_DBS`, `db_present` (Task 2)
- Produces:
  - `POST /pipelines/classify-reads` body `{object_id, db_key, mate_object_id?}` → `JobOut`, 201
  - `GET /pipelines/kraken-dbs` → `[{key, label, description, download_bytes, present}]` — what the dialog renders

- [ ] **Step 1: Write the failing tests**

Read the existing tests for `/pipelines/annotate-genome` first and copy
their fixture/client conventions exactly. The two behaviors to pin:

```python
async def test_kraken_dbs_lists_registry_with_presence(client):
    resp = await client.get("/api/v1/pipelines/kraken-dbs")
    assert resp.status_code == 200
    rows = resp.json()
    assert {r["key"] for r in rows} == {"standard-8", "pluspf-8", "viral"}
    for r in rows:
        assert set(r) >= {"key", "label", "description", "download_bytes", "present"}
        assert r["present"] is False  # nothing downloaded in the test env


async def test_classify_reads_rejects_unknown_db(client, fastq_object):
    resp = await client.post(
        "/api/v1/pipelines/classify-reads",
        json={"object_id": str(fastq_object.id), "db_key": "nonsense"},
    )
    assert resp.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/api/ -q -k kraken`
Expected: FAIL with 404s

- [ ] **Step 3: Implement**

Next to the annotate-genome route, matching its auth/`owner` conventions
exactly (read it first):

```python
class ClassifyReadsIn(BaseModel):
    object_id: PydanticObjectId
    db_key: str
    mate_object_id: PydanticObjectId | None = None


@router.post("/classify-reads", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_classify_reads_route(body: ClassifyReadsIn, ...):  # match neighbors' deps
    job = await pipeline_service.launch_classify_reads(
        object_id=body.object_id,
        db_key=body.db_key,
        mate_object_id=body.mate_object_id,
        owner=owner,
    )
    return JobOut.from_job(job)  # match the neighbors' serialization


@router.get("/kraken-dbs")
async def list_kraken_dbs():
    """The database choices for the classify dialog, with disk presence --
    presence is what flips the dialog's download warning (spec K2-F1)."""
    from app.pipelines.kraken_db_registry import KRAKEN_DBS, db_present

    return [
        {
            "key": s.key,
            "label": s.label,
            "description": s.description,
            "download_bytes": s.download_bytes,
            "present": db_present(s.key),
        }
        for s in KRAKEN_DBS.values()
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/api/ -q -k kraken`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/pipelines.py backend/tests/api/
git commit -m "feat(api): classify-reads launch and kraken database listing routes"
```

---

### Task 9: Suggestion card

**Files:**
- Modify: `backend/app/services/suggestion_service.py` (builder + registration in the builders list ~line 2051)
- Test: `backend/tests/services/test_suggestion_service.py`

**Interfaces:**
- Consumes: `tools.kraken2()` (Task 1)
- Produces: `build_classify_reads_card(obj) -> SuggestionCard | None`, kind `"classify_reads"`, category `"CLASSIFY_READS"`, launch endpoint `/pipelines/classify-reads`

- [ ] **Step 1: Write the failing tests**

Follow the file's existing fixture style (hand-built objects). Both
directions, per CLAUDE.md — and note the trap: the image ships kraken2, so
the available-direction test passes vacuously; the unavailable-direction
test is the one that fails when the seam breaks. Patch the *probe function*
`tools.kraken2` (a plain module function looked up at call time in the
builder — not captured in a frozen spec, so unlike `spec_for` there is no
indirection trap here, but verify the builder calls `tools.kraken2()` at
runtime rather than importing the result):

```python
def test_classify_card_offered_for_fastq(fastq_obj):
    card = suggestion_service.build_classify_reads_card(fastq_obj)
    assert card is not None
    assert card.kind == "classify_reads"
    assert card.status is CardStatus.AVAILABLE
    assert card.launch["endpoint"] == "/pipelines/classify-reads"
    assert card.launch["body"] == {"object_id": str(fastq_obj.id)}


def test_classify_card_absent_for_fasta(fasta_obj):
    assert suggestion_service.build_classify_reads_card(fasta_obj) is None


def test_classify_card_flips_unavailable_when_probe_off(fastq_obj, monkeypatch):
    from app.pipelines.tools import Tool

    monkeypatch.setattr(
        suggestion_service.tools, "kraken2",
        lambda: Tool(name="kraken2", path="", available=False, error="not found"),
    )
    card = suggestion_service.build_classify_reads_card(fastq_obj)
    assert card is not None
    assert card.status is CardStatus.UNAVAILABLE
```

(Match the `Tool` constructor to its real fields — read the dataclass in
`tools.py` first.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q -k classify`
Expected: FAIL with AttributeError

- [ ] **Step 3: Implement**

Next to `build_annotate_genome_card`:

```python
def build_classify_reads_card(obj) -> SuggestionCard | None:
    """Taxonomic classification: what species these reads actually contain.

    Deliberately positioned against the read-quality QC card: that one
    reports adapter and duplication levels, this one answers identity --
    verifying the labeled organism and catching cross-species
    contamination (spec K2-S1).  Availability tracks the binary probe
    only; the database's absence changes the launch dialog's copy, never
    the card state (spec K2-S2).
    """
    if obj.format.kind is not FormatKind.FASTQ:
        return None

    title = "Identify organisms"
    description = (
        "Classify reads by species with Kraken2 to verify the sample's "
        "organism and detect cross-species contamination -- a different "
        "question from read-quality QC's adapter and duplication checks."
    )

    kraken_tool = tools.kraken2()
    if not kraken_tool.available:
        return SuggestionCard(
            kind="classify_reads",
            category="CLASSIFY_READS",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=kraken_tool.error or "Kraken2 is unavailable.",
        )

    return SuggestionCard(
        kind="classify_reads",
        category="CLASSIFY_READS",
        title=title,
        description=description,
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/classify-reads",
            # db_key is added by the frontend dialog; the body here is the
            # part the card knows.
            "body": {"object_id": str(obj.id)},
        },
    )
```

Register in the builders list (~line 2051):

```python
    ("classify_reads", lambda obj, ctx: build_classify_reads_card(obj)),
```

If the `category` string is validated anywhere (grep for how
`"ANNOTATE_GENOME"` is consumed — a frontend enum, an ordering list), add
`"CLASSIFY_READS"` there too.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q`
Expected: PASS (whole file — the registration can break neighbors)

- [ ] **Step 5: Real-database check**

Per CLAUDE.md: from the main checkout, run the rule against a real project's
objects (`docker compose exec api python -c "..."` iterating real FASTQ
objects through `build_classify_reads_card`) and confirm the card appears on
real reads and not on references. Record the result in the eventual PR body.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat(pipelines): identify-organisms suggestion card for fastq reads"
```

---

### Task 10: Frontend — launch dialog

**Files:**
- Create: `frontend/src/components/KrakenDbDialog.tsx`
- Modify: wherever suggestion-card kinds map to dialogs (find how kind `"completeness"` opens `CompletenessDialog.tsx` — grep `CompletenessDialog` usages — and register `"classify_reads"` → `KrakenDbDialog` the same way)

**Interfaces:**
- Consumes: `GET /pipelines/kraken-dbs`, `POST /pipelines/classify-reads` (Task 8); the card's `launch.body.object_id`
- Produces: a dialog that POSTs `{object_id, db_key}` on confirm

- [ ] **Step 1: Read `CompletenessDialog.tsx` in full**

It is the direct precedent: a card-triggered dialog with a picker that
augments the card's launch body. Match its styling, state handling, and API
client usage exactly — this repo's frontend conventions beat anything
below, which is a content sketch, not a style guide.

- [ ] **Step 2: Implement the dialog**

Content requirements (spec K2-F1):

- Fetch `/pipelines/kraken-dbs` on open.
- Radio list: label, human-readable size (`download_bytes` formatted GB/MB),
  description; `standard-8` preselected.
- When the selected entry has `present: false`, show a plainly-worded line:
  "This database isn't downloaded yet — the first run fetches ~7.5 GB
  before classifying." (size formatted from `download_bytes`). No such
  line when present. A multi-GB download never starts without that
  sentence having been on screen.
- Confirm button POSTs `{object_id, db_key}` to `/pipelines/classify-reads`
  and surfaces the queued job the way the other card launches do.

- [ ] **Step 3: Verify manually**

From the worktree: `./ops/worktree-up.sh`, open `localhost:5273`, open a
project with FASTQ reads, confirm: card appears with the identity-vs-quality
copy; dialog lists three databases with sizes; the download warning shows
for absent databases. (Don't run the 7.5 GB download to test the dialog —
the warning text is the thing under test.)

- [ ] **Step 4: Commit**

```bash
git add frontend/src
git commit -m "feat(ui): kraken database picker dialog with download warning"
```

---

### Task 11: Frontend — taxonomy results panel

**Files:**
- Create: `frontend/src/components/TaxonomyFacts.tsx`
- Modify: the object detail view that renders fact panels (find where `AssemblyFacts.tsx` is mounted and conditionally rendered on its facts; mount `TaxonomyFacts` the same way, gated on `obj.facts.taxonomy`)

**Interfaces:**
- Consumes: `obj.facts.taxonomy` = `{taxa: [{name, rank, taxid, pct}], unclassified_pct, db_key, bracken_used, bracken_skipped?}`; `obj.facts.taxonomy_mismatch` = `{claimed, dominant: [{name, pct}]}`

- [ ] **Step 1: Read `AssemblyFacts.tsx` for the panel conventions**

- [ ] **Step 2: Implement the panel**

Content requirements (spec K2-F2):

- Table of taxa: name (italic for species), rank, percent — sorted as
  stored (already descending).
- An explicit "Unclassified" row showing `unclassified_pct` — zero shown
  as zero, never omitted.
- Footer: database label + `bracken_used` ("abundances refined with
  Bracken" / the `bracken_skipped` note verbatim when present).
- When `taxonomy_mismatch` exists, a warning banner above the table:
  "Metadata says *{claimed}*; reads classify as {dominant[0].pct}%
  *{dominant[0].name}*." with the remaining dominant taxa listed after.

- [ ] **Step 3: Verify manually**

Needs a completed run; if no database is downloaded yet, use the `viral`
database (~0.6 GB) against a small FASTQ for a fast end-to-end pass —
this doubles as Task 12's smoke test setup. Confirm table, unclassified
row, footer, and (by temporarily setting a wrong organism on the object's
metadata and re-running) the mismatch banner.

- [ ] **Step 4: Commit**

```bash
git add frontend/src
git commit -m "feat(ui): taxonomy panel with organism mismatch warning"
```

---

### Task 12: End-to-end verification and close-out

**Files:**
- Modify: `docs/superpowers/specs/2026-08-18-kraken2-classification-design.md` (K2-C1/K2-C4 amendment, if not already committed)

- [ ] **Step 1: Full backend suite from the worktree**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: green at the established count. (Remember the shared-Mongo trap:
this script brings its own replica set; do not run two suites at once.)

- [ ] **Step 2: End-to-end on the worktree stack**

With the worktree stack up: pick a small real FASTQ object, launch
classification with the `viral` database from the UI, and confirm the whole
chain: download job runs first (progress visible), classify chains behind
it, facts land, panel renders, reports exist under
`qc_reports/<object_id>/kraken2/`. Then re-launch on the same database and
confirm no second download happens (dedup + presence check).

- [ ] **Step 3: Spec amendment**

Confirm the spec's K2-C1/K2-C4 were amended to match the shipped shape
(no PipelineRun; `EXCLUDED_LAUNCHES`; `download_kraken_db` as the only new
NodeTypeSpec). If the amendment commit hasn't happened yet, make it now:

```bash
git add docs/superpowers/specs/2026-08-18-kraken2-classification-design.md
git commit -m "docs(pipelines): align kraken2 spec with facts-only launcher precedent"
```

- [ ] **Step 4: Push, PR, merge**

Follow CLAUDE.md's merge workflow exactly: rebase on `origin/main`, verify
the diff survived, push, `gh pr create --base main` with a title written
for the changelog (e.g. `feat(pipelines): identify organisms in reads with
kraken2 and bracken`), labels `type:feature` + `area:pipelines`, body
carrying the why and `Closes #625`. Poll `gh pr checks` to completion, fix
what CI finds, merge with `--rebase --delete-branch`, then bring down the
worktree stack (`./ops/worktree-up.sh --down`) and remove the worktree.

- [ ] **Step 5: Update issue #625**

Comment with what shipped and where; the `Closes #625` handles the close on
merge. Note any deltas from the spec (there will be at least the K2-C1
amendment) — the delta is the most valuable sentence in the record.
