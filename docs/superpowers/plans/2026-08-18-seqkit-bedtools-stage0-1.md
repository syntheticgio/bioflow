# seqkit/bedtools Stages 0–1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register bedtools and seqkit as first-class tools (Stage 0) and ship the per-feature coverage card — "which annotated features are poorly covered by reads?" — as the first bedtools-backed pipeline (Stage 1).

**Architecture:** Stage 0 adds probes + `TOOL_META` entries for both tools and installs seqkit in the image. Stage 1 follows the quantify-card chain end to end: pure runner module → queue handler returning a dict → `_apply_*` persister in `queue/results.py` → `launch_*` in `pipeline_service` → API endpoints → suggestion card → frontend results section. Two PRs, one per stage.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor, pytest, React/TypeScript, Docker. bedtools (already in image via apt), seqkit (new, static Go binary from GitHub releases).

**Spec:** `docs/superpowers/specs/2026-08-18-seqkit-bedtools-features-design.md` — stages 2–4 get their own plans after this one's PRs merge; they reuse the runner/card skeleton built here.

## Global Constraints

- Backend tests from this worktree run via `./backend/run-worktree-tests.sh tests/... -q` — never `docker compose exec api` from the worktree (tests main's code silently).
- Every bedtools invocation uses sorted inputs + a genome file + `-sorted` where the subcommand supports it (spec RS-5; OOM shape otherwise).
- Licenses/citations for TOOL_META are **read from each project's repo via `gh api` during the task, never recalled** (spec R0-2).
- Commit subjects are Conventional Commits, lowercase after colon, ≤72 chars; each stage merges as its own PR per CLAUDE.md's merge workflow (rebase on origin/main first, poll `gh pr checks` to completion, `gh pr merge --rebase --delete-branch`).
- New launch functions must be classified in `node_types.py` and the **full** `TestExhaustiveness` class run (spec RS-1). New keys in `queue/results.py`'s applier dict are the hand-maintained-registry trap (spec RS-2) — the handler and its applier land in the same task.
- Suggestion-rule tests must include the "flips to unavailable when the probe is patched off" direction, patching the seam that is actually read at call time (spec RS-3).

---

## Stage 0 — tool registration (PR 1)

### Task 1: Settings paths + probes for bedtools and seqkit

**Files:**
- Modify: `backend/app/config.py` (near `samtools_path`, ~line 132)
- Modify: `backend/app/pipelines/tools.py` (probe section, near `samtools()` ~line 364)
- Test: `backend/tests/pipelines/test_tools.py`

**Interfaces:**
- Produces: `tools.bedtools() -> Tool`, `tools.seqkit() -> Tool` (both `@lru_cache(maxsize=1)`, same contract as `tools.samtools()`); settings fields `bedtools_path`, `seqkit_path`.

- [ ] **Step 1: Read the neighboring probe tests**

Open `backend/tests/pipelines/test_tools.py` and find how existing probes are tested (search for `samtools` or `quast`). Mirror that file's fixture/patching style exactly in the next step — the file's conventions override this plan's sketch if they differ.

- [ ] **Step 2: Write the failing tests**

Add to `backend/tests/pipelines/test_tools.py`, following the file's existing probe-test style:

```python
def test_bedtools_probe_reports_missing_binary(monkeypatch):
    monkeypatch.setattr(settings, "bedtools_path", "/nonexistent/bedtools")
    tools.bedtools.cache_clear()
    tool = tools.bedtools()
    assert tool.name == "bedtools"
    assert tool.available is False


def test_seqkit_probe_reports_missing_binary(monkeypatch):
    monkeypatch.setattr(settings, "seqkit_path", "/nonexistent/seqkit")
    tools.seqkit.cache_clear()
    tool = tools.seqkit()
    assert tool.name == "seqkit"
    assert tool.available is False
```

If the file's existing tests patch differently (e.g. env vars or a fixture that clears all probe caches), copy that mechanism instead.

- [ ] **Step 3: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -q -k "bedtools or seqkit"`
Expected: FAIL — `AttributeError: ... no attribute 'bedtools'`.

- [ ] **Step 4: Implement**

In `backend/app/config.py`, next to `samtools_path`:

```python
    bedtools_path: str = "bedtools"
    seqkit_path: str = "seqkit"
```

In `backend/app/pipelines/tools.py`, next to `samtools()`:

```python
@lru_cache(maxsize=1)
def bedtools() -> Tool:
    # `bedtools --version` prints "bedtools v2.x.y" and exits zero.
    # Installed via apt since the Merqury work (#64); this probe is what
    # finally makes it visible to /help/software and the Actions tab.
    return _probe("bedtools", settings.bedtools_path, ["--version"])


@lru_cache(maxsize=1)
def seqkit() -> Tool:
    # seqkit has no `--version`; `seqkit version` prints "seqkit v2.x.y".
    return _probe("seqkit", settings.seqkit_path, ["version"])
```

Before committing, confirm the probe args against the real binaries: `docker compose exec api bedtools --version` from the **main checkout** (bedtools is already in the running image), and `seqkit version` on any machine with seqkit (or defer to Task 3's image build check). If `_probe` requires exit-zero on the given args and the real output differs, adjust args to match reality, not the plan.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -q`
Expected: PASS (whole file — existing tests must not regress).

- [ ] **Step 6: Commit**

```bash
git add backend/app/config.py backend/app/pipelines/tools.py backend/tests/pipelines/test_tools.py
git commit -m "feat(pipelines): probe bedtools and seqkit as first-class tools"
```

### Task 2: TOOL_META entries with verified bibliography

**Files:**
- Modify: `backend/app/pipelines/tools.py` (`TOOL_META` dict, ~line 1021)
- Test: `backend/tests/pipelines/test_tools.py` (existing `test_every_tool_is_documented`)

**Interfaces:**
- Produces: `TOOL_META["bedtools"]`, `TOOL_META["seqkit"]` entries; consumed by `/help/software` and later stages' cards.

- [ ] **Step 1: Verify licenses and citations from the source**

Run and record the answers (do not trust memory):

```bash
gh api repos/arq5x/bedtools2/license --jq '.license.spdx_id'
gh api repos/shenwei356/seqkit/license --jq '.license.spdx_id'
```

For citations, read each README's "how to cite" section:

```bash
gh api repos/arq5x/bedtools2/readme --jq '.content' | base64 -d | grep -i -A4 cit
gh api repos/shenwei356/seqkit/readme --jq '.content' | base64 -d | grep -i -A4 cit
```

Expected shape (verify, don't assume): bedtools → Quinlan & Hall, Bioinformatics 2010, doi:10.1093/bioinformatics/btq033; seqkit → the README currently asks for the SeqKit2 paper (Shen et al., iMeta 2024). Use whatever the repos actually say today.

- [ ] **Step 2: Confirm the documentation test currently fails for the new tools**

The completeness test only covers tools that appear in the registry the help page reads. Find how `test_every_tool_is_documented` enumerates tools (read it in `backend/tests/pipelines/test_tools.py`) — if Task 1's probes already put bedtools/seqkit in that enumeration, this test is now RED. Run it:

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -q -k documented`
Expected: FAIL naming bedtools/seqkit (if it passes, the enumeration is probe-independent — read `tool_with_meta`/the help endpoint to find the registration point, add the tools there first, and re-run until you have a genuine RED).

- [ ] **Step 3: Add the entries**

In `TOOL_META`, following the `fastp` entry's shape. `usage` describes only what is true *now* (registration); stage 1's PR updates bedtools' usage when the coverage card lands:

```python
    "bedtools": ToolMeta(
        pipelines=(PipelineType.UTILITY,),
        one_liner="Interval arithmetic across BED/GFF/VCF/BAM",
        summary=(
            "The standard toolkit for genome interval arithmetic: "
            "intersect, merge, subtract, and coverage operations across "
            "BED, GFF, VCF, and BAM files. Answers positional questions -- "
            "what overlaps what, and by how much -- that sequence-level "
            "tools cannot."
        ),
        strengths=(
            "One consistent interval model across BED, GFF, VCF, and BAM",
            "Streaming -sorted mode keeps memory flat on large inputs",
            "The de facto standard cited by thousands of pipelines",
        ),
        homepage="https://bedtools.readthedocs.io/",
        repository="https://github.com/arq5x/bedtools2",
        citation="<from Step 1>",
        citation_url="<from Step 1>",
        license="<SPDX from Step 1>",
        usage=(
            "Installed alongside the Merqury k-mer QV scripts, which call "
            "it internally. Direct BioFlow features backed by it are "
            "planned (per-feature coverage first); until one ships, "
            "nothing dispatches to it directly."
        ),
        runnable=False,
    ),
    "seqkit": ToolMeta(
        pipelines=(PipelineType.UTILITY,),
        one_liner="FASTA/FASTQ manipulation toolkit",
        summary=(
            "A general-purpose FASTA/FASTQ toolkit: subsetting sequences "
            "and regions, filtering, deduplication, and format "
            "conversion, as a single static binary."
        ),
        strengths=(
            "Single static binary, no runtime dependencies",
            "Region and name-based subsequence extraction",
            "Handles plain and gzip-compressed input transparently",
        ),
        homepage="https://bioinf.shenwei.me/seqkit/",
        repository="https://github.com/shenwei356/seqkit",
        citation="<from Step 1>",
        citation_url="<from Step 1>",
        license="<SPDX from Step 1>",
        usage=(
            "Installed for the planned region/sequence extraction "
            "feature; nothing dispatches to it yet."
        ),
        runnable=False,
    ),
```

Note `runnable=False` is honest today (no handler branches on either tool) and is exactly what the `ToolMeta.runnable` comment's cutadapt lesson warns about: **stage 1's PR must flip bedtools to `runnable=True`** (Task 8 covers it), and stage 4's plan flips seqkit.

- [ ] **Step 4: Run the documentation test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -q`
Expected: PASS, whole file.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/tools.py
git commit -m "feat(pipelines): document bedtools and seqkit on the software help page"
```

### Task 3: Install seqkit in the image

**Files:**
- Modify: `backend/Dockerfile` (release-binary section; the `TARGETARCH` case blocks around lines 282–310 are the pattern)

**Interfaces:**
- Produces: `/usr/local/bin/seqkit` in the api/worker image, both arches.

- [ ] **Step 1: Pin the release**

```bash
gh api repos/shenwei356/seqkit/releases/latest --jq '.tag_name, (.assets[].name)'
```

Record the version tag and the exact asset names for `linux_amd64` and `linux_arm64` tarballs. Fetch their published checksums if the release ships a checksums file (`.assets[].name` will show it); otherwise download each asset once and record `sha256sum` yourself.

- [ ] **Step 2: Add the install block**

Follow the existing `TARGETARCH` case pattern (read lines 282–310 first and mirror the idiom — ADD vs the fetch tool the Dockerfile actually uses, checksum verification style included):

```dockerfile
# seqkit: single static Go binary, used by the planned region/sequence
# extraction feature (#632). Version pinned; checksums from the release.
ARG SEQKIT_VERSION=<from Step 1>
RUN case "$TARGETARCH" in \
        amd64) SEQKIT_ARCH="linux_amd64"; SEQKIT_SHA256="<from Step 1>" ;; \
        arm64) SEQKIT_ARCH="linux_arm64"; SEQKIT_SHA256="<from Step 1>" ;; \
    esac \
    && <fetch, per the file's existing idiom> \
       "https://github.com/shenwei356/seqkit/releases/download/v${SEQKIT_VERSION}/seqkit_${SEQKIT_ARCH}.tar.gz" \
    && echo "${SEQKIT_SHA256}  seqkit.tar.gz" | sha256sum -c - \
    && tar -xzf seqkit.tar.gz -C /usr/local/bin seqkit \
    && rm seqkit.tar.gz \
    && seqkit version
```

The trailing `seqkit version` makes a broken binary fail the build, not the first user job.

- [ ] **Step 3: Build and verify**

From the worktree:

```bash
./ops/worktree-up.sh
docker exec $(docker ps --format '{{.Names}}' | grep worktree.*api) seqkit version
```

Expected: `seqkit v<pinned version>`. Also confirm `bedtools --version` in the same container while you are there (Task 1's probe args promise it works).

- [ ] **Step 4: Commit**

```bash
git add backend/Dockerfile
git commit -m "feat(ops): install seqkit in the backend image, both arches"
```

### Task 4: Stage 0 PR

- [ ] **Step 1: Full backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: same green count as an unchanged tree (run twice if a number looks off — see CLAUDE.md on concurrent-stack flakiness).

- [ ] **Step 2: Manual help-page check**

With the worktree stack up, open `localhost:5273/help/software` and confirm bedtools and seqkit render with version, license, citation, usage.

- [ ] **Step 3: Open, watch, and merge the PR**

Per CLAUDE.md's merge workflow exactly: `git fetch origin main && git rebase origin/main`, verify the diff (`git diff origin/main...HEAD --stat`), push, `gh pr create --base main --fill` with title `feat(pipelines): register bedtools and seqkit as documented tools`, label `type:feature` + `area:pipelines`, body includes `Refs #632` (not `Closes` — stages remain). Poll `gh pr checks` until every check passes, then `gh pr merge --rebase --delete-branch`. Comment progress on #632.

---

## Stage 1 — per-feature coverage (PR 2)

Model chain to imitate throughout: **quantify** (`build_quantify_card` in `suggestion_service.py:1911`, `launch_quantify` in `pipeline_service.py:4044`, handler in `queue/expression_handlers.py`, applier registered in `queue/results.py:2993`, runner in `pipelines/counts_runner.py`). Read each before implementing its analog.

### Task 5: feature_coverage_runner — pure command builder and parser

**Files:**
- Create: `backend/app/pipelines/feature_coverage_runner.py`
- Test: `backend/tests/pipelines/test_feature_coverage_runner.py`

**Interfaces:**
- Produces:
  - `build_genome_file(fai_path: Path, out_path: Path) -> Path` — writes bedtools' two-column genome file from a `.fai`.
  - `build_command(annotation: Path, bam: Path, genome_file: Path) -> list[str]` — the exact `bedtools coverage` argv.
  - `parse_coverage(stdout_path: Path, annotation_format: str) -> dict` — the report dict.
  - `FEATURE_COLUMNS: tuple[str, ...]` — per-feature keys, used by the API/frontend.

- [ ] **Step 1: Write the failing tests**

```python
from pathlib import Path

from app.pipelines import feature_coverage_runner as fcr


def test_build_command_uses_sorted_streaming(tmp_path):
    cmd = fcr.build_command(
        annotation=tmp_path / "ann.sorted.gff",
        bam=tmp_path / "aln.bam",
        genome_file=tmp_path / "ref.genome",
    )
    assert cmd[0] == "bedtools"
    assert cmd[1] == "coverage"
    assert "-sorted" in cmd
    assert "-g" in cmd
    # -a is the annotation (features reported per-row), -b the BAM
    assert str(tmp_path / "ann.sorted.gff") == cmd[cmd.index("-a") + 1]
    assert str(tmp_path / "aln.bam") == cmd[cmd.index("-b") + 1]


def test_build_genome_file_orders_like_fai(tmp_path):
    fai = tmp_path / "ref.fa.fai"
    fai.write_text("chr2\t500\t6\t60\t61\nchr1\t300\t520\t60\t61\n")
    out = fcr.build_genome_file(fai, tmp_path / "ref.genome")
    assert out.read_text() == "chr2\t500\nchr1\t300\n"


def test_parse_coverage_gff(tmp_path):
    # bedtools coverage appends 4 columns to each annotation row:
    # read count, bases covered, feature length, breadth fraction.
    out = tmp_path / "coverage.tsv"
    out.write_text(
        "chr1\tRefSeq\tgene\t100\t400\t.\t+\t.\tID=gene-abcA;Name=abcA\t"
        "12\t250\t301\t0.8305648\n"
        "chr1\tRefSeq\tgene\t900\t1400\t.\t-\t.\tID=gene-abcB\t"
        "0\t0\t501\t0.0000000\n"
    )
    report = fcr.parse_coverage(out, annotation_format="gff")
    assert report["feature_count"] == 2
    assert report["features_zero_coverage"] == 1
    rows = report["features"]
    assert rows[0]["name"] == "abcB"  # sorted ascending by breadth
    assert rows[0]["breadth"] == 0.0
    assert rows[1] == {
        "name": "abcA",
        "type": "gene",
        "seq_id": "chr1",
        "start": 100,
        "end": 400,
        "strand": "+",
        "read_count": 12,
        "bases_covered": 250,
        "length": 301,
        "breadth": 0.8305648,
    }
    assert 0.0 <= report["median_breadth"] <= 1.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_feature_coverage_runner.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the module**

`backend/app/pipelines/feature_coverage_runner.py`. Docstring style: follow `counts_runner.py`. Core content:

```python
"""Per-feature read coverage: bedtools coverage over an annotation and a BAM.

Pure functions only -- the queue handler owns the subprocess call, mirroring
counts_runner's split. `-sorted` with an explicit genome file is
non-negotiable: without it bedtools loads the whole BAM into memory, which is
the OOM shape job_timings exists to catch (spec RS-5).
"""

from pathlib import Path

FEATURE_COLUMNS: tuple[str, ...] = (
    "name", "type", "seq_id", "start", "end", "strand",
    "read_count", "bases_covered", "length", "breadth",
)

# The unique-feature table stays bounded regardless of annotation size;
# summary numbers always cover every row. Same rationale as spec R3-3.
MAX_FEATURES_IN_REPORT = 10_000


def build_genome_file(fai_path: Path, out_path: Path) -> Path:
    """bedtools' genome file: name<TAB>length, in the .fai's own order.

    Order matters: -sorted requires -a, -b, and -g to agree on contig
    order, and the .fai's order is the reference's own.
    """
    lines = []
    for raw in fai_path.read_text().splitlines():
        if not raw.strip():
            continue
        name, length = raw.split("\t")[:2]
        lines.append(f"{name}\t{length}\n")
    out_path.write_text("".join(lines))
    return out_path


def build_command(annotation: Path, bam: Path, genome_file: Path) -> list[str]:
    return [
        "bedtools", "coverage",
        "-sorted",
        "-g", str(genome_file),
        "-a", str(annotation),
        "-b", str(bam),
    ]


def _gff_name(attributes: str) -> str:
    """Name= wins, then ID=, then the raw attribute string truncated."""
    fields = dict(
        part.split("=", 1) for part in attributes.split(";") if "=" in part
    )
    name = fields.get("Name") or fields.get("ID") or attributes
    return name.removeprefix("gene-")


def parse_coverage(stdout_path: Path, annotation_format: str) -> dict:
    features: list[dict] = []
    zero = 0
    with stdout_path.open() as fh:
        for line in fh:
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 4 or line.startswith("#"):
                continue
            read_count, bases, length, breadth = cols[-4:]
            row = _row_from_annotation_cols(cols[:-4], annotation_format)
            row.update(
                read_count=int(read_count),
                bases_covered=int(bases),
                length=int(length),
                breadth=float(breadth),
            )
            if row["read_count"] == 0:
                zero += 1
            features.append(row)
    features.sort(key=lambda r: (r["breadth"], r["name"]))
    breadths = sorted(r["breadth"] for r in features)
    n = len(breadths)
    median = 0.0 if n == 0 else (
        breadths[n // 2] if n % 2 else (breadths[n // 2 - 1] + breadths[n // 2]) / 2
    )
    return {
        "feature_count": len(features),
        "features_zero_coverage": zero,
        "median_breadth": median,
        "truncated": len(features) > MAX_FEATURES_IN_REPORT,
        "features": features[:MAX_FEATURES_IN_REPORT],
    }


def _row_from_annotation_cols(cols: list[str], fmt: str) -> dict:
    if fmt == "bed":
        name = cols[3] if len(cols) > 3 else f"{cols[0]}:{cols[1]}-{cols[2]}"
        return {
            "name": name, "type": "region", "seq_id": cols[0],
            "start": int(cols[1]), "end": int(cols[2]),
            "strand": cols[5] if len(cols) > 5 else ".",
        }
    # GFF/GTF: 9 columns
    return {
        "name": _gff_name(cols[8]) if len(cols) > 8 else "",
        "type": cols[2], "seq_id": cols[0],
        "start": int(cols[3]), "end": int(cols[4]), "strand": cols[6],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_feature_coverage_runner.py -q`
Expected: PASS.

- [ ] **Step 5: Capture a real fixture and lock the parser to it**

In the worktree stack's api container, run a real `bedtools coverage` on any small BAM+GFF pair (the test-data the repo's e2e harness uses, or a real project's pair). Compare the appended-column layout against the parser's `cols[-4:]` assumption; if the installed bedtools appends differently for GFF input, fix the parser and add the captured lines as a second test case verbatim. This is spec verify-item 4 — do not skip it.

- [ ] **Step 6: Commit**

```bash
git add backend/app/pipelines/feature_coverage_runner.py backend/tests/pipelines/test_feature_coverage_runner.py
git commit -m "feat(pipelines): bedtools coverage command builder and parser"
```

### Task 6: Queue handler + results applier + report dir

**Files:**
- Create: `backend/app/queue/feature_coverage_handlers.py`
- Modify: `backend/app/queue/results.py` (applier dict, ~line 2993, key `"feature_coverage"`)
- Modify: `backend/app/queue/handlers.py` (import for registration side effect — read the file top to see how `expression_handlers` is imported and mirror it)
- Modify: `backend/app/config.py` (add `feature_coverage_dir` property beside `bam_stats_dir`, ~line 374, same "outside objects/" rationale comment)
- Test: `backend/tests/queue/test_feature_coverage_handlers.py` (mirror the structure of the existing quantify/expression handler tests — find them with `grep -rl quantify backend/tests/queue/`)

**Interfaces:**
- Consumes: `feature_coverage_runner.build_genome_file / build_command / parse_coverage` (Task 5).
- Produces: handler `run_feature_coverage(ctx) -> dict` registered as `"feature_coverage"`; report JSON at `settings.feature_coverage_dir / <bam_object_id> / "coverage.json"`; applier `_apply_feature_coverage` merging summary facts (`feature_coverage_median_breadth`, `feature_coverage_zero_features`, `feature_coverage_feature_count`, `feature_coverage_annotation_id`) onto the BAM object.

- [ ] **Step 1: Read the model handler**

Read `backend/app/queue/expression_handlers.py` in full (~its `run_quantify`), plus `results._apply_quantify`. Copy its workdir/prepare/cancel-check/payload idioms exactly; the sketch below shows intent, the model file shows house style.

- [ ] **Step 2: Write the failing handler test**

Test the handler's orchestration with the subprocess seam patched, in the style the model handler's tests use. Minimum cases:

```python
def test_feature_coverage_sorts_annotation_and_streams(...):
    # payload with bam/annotation/reference blob digests resolved to tmp files;
    # patched subprocess records argv: assert a sort step ran and the
    # bedtools argv came from build_command (contains "-sorted" and "-g").

def test_feature_coverage_result_carries_summary_facts(...):
    # patched bedtools output written from the Task 5 fixture; assert the
    # returned dict has report_path, feature_count, features_zero_coverage,
    # median_breadth, and the annotation object id.

def test_apply_feature_coverage_registers_report_and_facts(...):
    # exercise the applier against a stored BAM object; assert facts merged
    # and the report file is where the GET endpoint (Task 8) will look.
```

Write them as real tests against the fixtures the model handler tests use — copy a passing quantify test and reshape it rather than inventing new scaffolding.

- [ ] **Step 3: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/queue/test_feature_coverage_handlers.py -q`
Expected: FAIL — handler module does not exist.

- [ ] **Step 4: Implement handler + applier**

`feature_coverage_handlers.py`, shaped like `expression_handlers.py`:

```python
@handler(
    "feature_coverage",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=1024, io=IoClass.HEAVY),
    # Deterministic failures (bad GFF, unsorted BAM) don't improve with
    # retries; one retry covers transient disk/exec noise.
    max_attempts=2,
)
def run_feature_coverage(ctx: JobContext) -> dict:
    """Per-feature read coverage for one BAM against one annotation.

    Read-only like bam_stats: no derived objects, one JSON report on disk
    plus summary facts merged onto the BAM by _apply_feature_coverage.
    """
```

Body, in order (using the prepare/resolve helpers the model handler uses):
1. Resolve BAM, annotation, and the reference `.fai` from the payload into the workdir.
2. `sort -k1,1 -k4,4n` the GFF (or `-k1,1 -k2,2n` for BED) into `ann.sorted.<ext>` — but **contig order must match the genome file**, so sort with the genome file's contig order: bedtools' documented recipe is sorting the annotation by the same chromosome order as `-g`; implement by generating the genome file first (`build_genome_file`) and passing the annotation through `sort` keyed to that order (the model to follow is whatever the captured fixture in Task 5 Step 5 proved correct — if plain lexical sort disagreed with the `.fai` order there, use `bedtools sort -g ref.genome` instead of GNU sort, which is the tool's own answer to exactly this).
3. Run `build_command(...)` via the ctx's subprocess helper with stdout to `coverage.tsv`, cancellation checks per house style.
4. `report = parse_coverage(...)`; write JSON to `settings.feature_coverage_dir / bam_object_id / "coverage.json"`.
5. Return the summary dict for the applier.

In `results.py`: `_apply_feature_coverage` merging the four facts above, and the dict entry `"feature_coverage": _apply_feature_coverage`. In `config.py`: the `feature_coverage_dir` property copying `bam_stats_dir`'s shape and comment rationale.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/queue/test_feature_coverage_handlers.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/feature_coverage_handlers.py backend/app/queue/results.py backend/app/queue/handlers.py backend/app/config.py backend/tests/queue/test_feature_coverage_handlers.py
git commit -m "feat(queue): feature_coverage handler running bedtools coverage"
```

### Task 7: launch_feature_coverage + node type

**Files:**
- Modify: `backend/app/services/pipeline_service.py` (beside `launch_quantify`, ~line 4044)
- Modify: `backend/app/pipelines/node_types.py` (new `"feature_coverage"` spec beside `"bam_stats"`, ~line 441)
- Test: `backend/tests/services/` (mirror where `launch_quantify`'s tests live — `grep -rl launch_quantify backend/tests/`), and `backend/tests/pipelines/test_node_types.py`

**Interfaces:**
- Consumes: `resolve_annotation(project_id, annotation_id, owner=...)` (existing, used by `launch_quantify`); `refuse_if_over_budget`.
- Produces: `async def launch_feature_coverage(*, bam_id: PydanticObjectId, owner: str, annotation_id: PydanticObjectId | None = None, resource_override: bool = False) -> Job`.

- [ ] **Step 1: Write the failing tests**

Copy the closest existing `launch_quantify` test and reshape; minimum cases:

```python
async def test_launch_feature_coverage_requires_bedtools(...):
    # patch the probe seam off (the seam actually read at call time —
    # tools.require(tools.bedtools()) is patched via tools.bedtools);
    # assert launch raises the tool-missing error.

async def test_launch_feature_coverage_resolves_lone_annotation(...):
    # project with one BAM + one annotation; launch without annotation_id;
    # assert enqueued payload names that annotation's id and blob digest.

async def test_launch_feature_coverage_rejects_non_bam(...):
```

Plus, in `test_node_types.py`, nothing new to write if `TestExhaustiveness` is truly exhaustive — the point of Step 2.

- [ ] **Step 2: Run node_types exhaustiveness to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py -q`
Expected: after adding `launch_feature_coverage` (Step 3) but before the spec entry, `test_every_launch_function_is_classified` FAILS naming it. Do this deliberately: implement Step 3's service function first, run this, see RED, then add the node spec. **Run the whole file, never a single test** (#355/#366).

- [ ] **Step 3: Implement**

`launch_feature_coverage`, shaped like `launch_quantify`: budget refusal first, `tools.require(tools.bedtools())`, fetch + validate BAM (reuse `_check_bam_stats_callable`'s sorted-BAM requirement — coverage `-sorted` needs it just as much), `resolve_annotation`, resolve the reference `.fai` the same way `launch_bam_stats` finds its reference artifacts, enqueue `"feature_coverage"` with `dedup_key=f"feature_coverage:{bam.blob_sha256}:{annotation.blob_sha256}"`.

Then `node_types.py`, copying the `"bam_stats"` spec's shape:

```python
    "feature_coverage": NodeTypeSpec(
        ...,  # every field the bam_stats entry carries, adapted
        launch_name="pipeline_service.launch_feature_coverage",
        launch=_launch_feature_coverage,
    ),
```

with a `_launch_feature_coverage` wrapper beside `_launch_bam_stats` (~line 151) mapping `inputs`/`params` to the keyword signature.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py tests/services/ -q -k "node_types or feature_coverage"`
Expected: PASS, including the full `TestExhaustiveness` class.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/pipelines/node_types.py backend/tests/
git commit -m "feat(services): launch feature coverage as a classified pipeline node"
```

### Task 8: API endpoints + card + TOOL_META usage update

**Files:**
- Modify: `backend/app/api/v1/pipelines.py` (beside `launch_bam_stats` ~line 665 and `get_bam_stats_report` ~line 690)
- Modify: `backend/app/services/suggestion_service.py` (new `build_feature_coverage_card` beside `build_quantify_card` ~line 1911; wire into the fixed card order and `suggestions_for`'s prefetch — annotations are already prefetched)
- Modify: `backend/app/pipelines/tools.py` (bedtools `usage` + `runnable=True`, per Task 2's note)
- Test: `backend/tests/services/test_suggestion_service.py`; API tests beside the existing bam-stats endpoint tests (`grep -rl bam_stats backend/tests/api/`)

**Interfaces:**
- Consumes: `launch_feature_coverage` (Task 7); report JSON location (Task 6).
- Produces: `POST /pipelines/feature-coverage` body `{"bam_id": str, "annotation_id": str | null}` → `JobOut`; `GET /pipelines/feature-coverage/{object_id}/report` → the Task 5 report dict; card `kind="feature_coverage"`, `category="ASSEMBLY_QC"`.

- [ ] **Step 1: Write the failing card tests**

In `test_suggestion_service.py`, following its object-builder helpers:

```python
def test_feature_coverage_card_available_with_bam_and_annotation(...):
    # BAM object + one annotation in prefetch -> AVAILABLE, launch body
    # keys on bam_id like the quantify card.

def test_feature_coverage_card_unavailable_without_annotation(...):
    # reason names the missing half.

def test_feature_coverage_card_unavailable_when_probe_off(...):
    # patch tools.bedtools to return an unavailable Tool; assert the card
    # flips to UNAVAILABLE. This is the direction that fails when the
    # seam breaks (spec RS-3) — the AVAILABLE direction alone proves
    # nothing on an image that ships the tool.

def test_feature_coverage_card_ignores_non_bam(...):
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q -k feature_coverage`
Expected: FAIL — builder does not exist.

- [ ] **Step 3: Implement card, endpoints, usage flip**

Card, mirroring `build_quantify_card` (same annotation-role caveat applies — gate on the prefetched `annotations` list, never `ObjectRole.ANNOTATION`):

```python
def build_feature_coverage_card(obj, annotations) -> SuggestionCard | None:
    """Per-feature read coverage: which annotated features are poorly covered.

    bam_stats answers this genome-wide; annotation_stats never looks at
    reads. This is the positional join of the two, and the first direct
    bedtools consumer (#632, stage 1).
    """
    if obj.format.kind is not FormatKind.BAM:
        return None
    title = "Feature coverage"
    description = (
        "Report read coverage per annotated feature, surfacing the genes "
        "this alignment covers poorly or not at all."
    )
    tool = tools.bedtools()
    if not tool.available:
        return SuggestionCard(
            kind="feature_coverage", category="ASSEMBLY_QC",
            title=title, description=description,
            status=CardStatus.UNAVAILABLE,
            reason=f"{tool.name} is not installed.",
        )
    if not annotations:
        return SuggestionCard(
            kind="feature_coverage", category="ASSEMBLY_QC",
            title=title, description=description,
            status=CardStatus.UNAVAILABLE,
            reason=(
                "This project has no annotation to measure coverage "
                "against. Download one with the assembly, or upload a "
                "GFF/GTF."
            ),
        )
    return SuggestionCard(
        kind="feature_coverage", category="ASSEMBLY_QC",
        title=title, description=description,
        why=(
            "This alignment has an annotation to measure against, so "
            "per-gene coverage gaps are one run away."
        ),
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/feature-coverage",
            # annotation_id omitted: the server resolves it, same as
            # quantify.
            "body": {"bam_id": str(obj.id)},
        },
    )
```

Wire it into the fixed card order (append near the other ASSEMBLY_QC cards; read the ordering comment at the bottom of the file first) passing the prefetched `annotations`.

Endpoints in `api/v1/pipelines.py`, copying the bam-stats pair: a `FeatureCoverageRequest` model (`bam_id`, optional `annotation_id`), POST calling `launch_feature_coverage`, GET reading `settings.feature_coverage_dir / str(object_id) / "coverage.json"` with the same not-found handling as `get_bam_stats_report`.

In `tools.py`, bedtools entry: `runnable=True` and usage rewritten:

```python
        usage=(
            "Backs the Feature coverage card: computes per-feature read "
            "coverage of an alignment against a project annotation. Also "
            "called internally by the Merqury k-mer QV scripts."
        ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py tests/api/ tests/pipelines/test_tools.py -q`
Expected: PASS (tools docs test still green after the usage edit).

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/pipelines.py backend/app/services/suggestion_service.py backend/app/pipelines/tools.py backend/tests/
git commit -m "feat(pipelines): feature coverage card, launch endpoint, and report API"
```

### Task 9: Frontend results section

**Files:**
- Create: `frontend/src/components/FeatureCoverage.tsx`
- Modify: `frontend/src/components/BamResults.tsx` (add the section where the other per-BAM report sections mount)
- Modify: `frontend/src/api/client.ts` + `frontend/src/api/types/` (report fetcher + types)

**Interfaces:**
- Consumes: `GET /pipelines/feature-coverage/{object_id}/report` (Task 8) returning `{feature_count, features_zero_coverage, median_breadth, truncated, features: FeatureCoverageRow[]}` with `FeatureCoverageRow = {name, type, seq_id, start, end, strand, read_count, bases_covered, length, breadth}`.

- [ ] **Step 1: Read the model component**

Read `BamResults.tsx` and one existing table-bearing section it mounts (e.g. `ContigTable.tsx`) — copy fetch/loading/empty-state idioms and table styling from there; no new UI patterns.

- [ ] **Step 2: Implement**

`FeatureCoverage.tsx`: fetch the report for the BAM object; render (a) a summary line — "N features · M at zero coverage · median breadth P%" (+ a truncation note when `truncated`), (b) a sortable table of `features` defaulting to ascending `breadth` (the report arrives pre-sorted; sorting client-side only re-orders), columns Name, Type, Location (`seq_id:start-end`), Reads, Breadth (as a percentage). Absent report (job never run) renders nothing — the card is the affordance, matching how other sections handle their missing reports (confirm against the model component and copy its behavior).

Types in `frontend/src/api/types/` and a fetcher in `client.ts`, both mirroring the bam-stats equivalents found in Step 1.

- [ ] **Step 3: Manual verification (this repo's UI test path)**

With the worktree stack up (`./ops/worktree-up.sh`, UI on 5273): open a project with an aligned BAM and an annotation, confirm the Feature coverage card renders on the BAM, launch it, watch the job complete, and confirm the results section shows the table with zero-coverage features first. There is no headless component test setup in this repo — this manual pass **is** the frontend verification.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/FeatureCoverage.tsx frontend/src/components/BamResults.tsx frontend/src/api/
git commit -m "feat(frontend): per-feature coverage table on BAM results"
```

### Task 10: Real-database spot check + Stage 1 PR

- [ ] **Step 1: Rule check against real objects (spec RS-4)**

From the **main checkout** (this reads the shared stack's real database):

```bash
docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.services import suggestion_service

async def main():
    await connect_to_mongo()
    # For every real BAM object, print the feature_coverage card's status
    # and reason, using the same prefetch suggestions_for does.
    ...
asyncio.run(main())"
```

Flesh the script out against the real model APIs at execution time; the deliverable is a printed line per real BAM showing card status + resolved annotation, eyeballed for the protein.faa-class mistakes (an annotation of the *wrong* assembly being offered, a duplicate annotation counted twice).

- [ ] **Step 2: Full suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: green at the baseline count.

- [ ] **Step 3: Open, watch, and merge the PR**

Same workflow as Task 4 Step 3. Title: `feat(pipelines): per-feature coverage report via bedtools`. Labels `type:feature`, `area:pipelines`. Body: the why (first direct bedtools consumer, the coverage question nothing answered), `Refs #632`. Poll checks to completion, merge `--rebase --delete-branch`.

- [ ] **Step 4: Close out stage bookkeeping**

Comment on #632: stages 0–1 merged (PR links), stages 2–4 remain; next is the stage 2 plan. Bring the worktree stack down (`./ops/worktree-up.sh --down`) if no further task follows immediately.

---

## Self-review record

- Spec coverage: R0-1→Task 1, R0-2→Task 2, R0-3→Task 3, R0-4→Task 4; R1-1/R1-2→Task 8, R1-3→Tasks 5–6, R1-4→Task 5, R1-5→Task 9, R1-6→Task 6 (job_timings automatic via handler registration); RS-1→Task 7, RS-2→Task 6 (applier dict), RS-3→Task 8, RS-4→Task 10, RS-5→Tasks 5–6. Stages 2–4: deliberately deferred to their own plans (spec staging).
- Known unknowns are marked as in-task verification steps (probe args, bedtools column layout for GFF, seqkit asset names/checksums, sort-order recipe), matching the spec's "verify before implementing" list — none are placeholders for design decisions.
