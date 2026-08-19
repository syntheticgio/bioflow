# mosdepth Coverage Depth — Implementation Plan

**For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal: Add per-window and per-region read-depth analysis (mosdepth) alongside the existing `bam_stats` global stats.** A completed BAM gains a "Per-window coverage" suggestion card (when mosdepth is installed); launching it runs mosdepth, writes a JSON report, and merges coverage summary facts onto the BAM, reusable by the existing track-viz components.

**Architecture:** A new pure `mosdepth_runner.py` (command-builder + windows-BED generator + parsers), a `SUBPROCESS` handler mirroring `feature_coverage_handlers.py`, a `launch_coverage` in `pipeline_service.py`, two API routes mirroring the feature-coverage routes, a `_apply_coverage` results applier merging facts onto the BAM, and a `build_coverage_card` suggestion rule. mosdepth is registered as `PipelineType.UTILITY` in `tools.py` and installed from Debian trixie.

**Tech Stack:** FastAPI + Beanie/MongoDB backend, pytest; React + TanStack Query frontend, Vite.

**Spec:** `docs/superpowers/specs/2026-08-19-mosdepth-coverage-depth-design.md`

---

## Key context for the implementer

You are working in a single-user local tool. Read `CLAUDE.md` first. Five things that will bite you otherwise:

1. **Run pytest inside the container**, not on the host: `docker compose exec api python -m pytest tests/ -q`, or `backend/run-worktree-tests.sh tests/ -q` from a worktree (private Mongo, per CLAUDE.md). The host venv hits Mongo replica-set errors.
2. **`api` and `web` hot-reload; `worker` does not.** Every task here edits `mosdepth_handlers.py`, so after any handler change run `docker compose restart worker` before re-testing a job — otherwise the worker silently runs the old in-memory code and the fix "doesn't work."
3. **Run `docker compose` from the repo root only**, never a worktree (the stack's bind mounts point at the main checkout).
4. **License and citation must be verified against mosdepth's own repository** at implementation time, not recalled (CLAUDE.md rule). The spec records the expected values (MIT; Pedersen & Quinlan 2018 *Bioinformatics*; github.com/brentp/mosdepth) but they must be confirmed against the source, and the Dockerfile comment must name the exact trixie `mosdepth` version.
5. **The exhaustiveness net is real.** Adding `launch_coverage` without a matching `NodeTypeSpec` in `node_types.py` (a `NODE_TYPES` entry plus a `_launch_*` adapter) fails the node-types exhaustiveness test that compares `launch_function_names()` against `NODE_TYPES`; adding a `TOOL_META` entry missing `homepage`/`citation`/`license`/`usage` fails `test_every_tool_is_documented`; adding a handler module without importing it for side-effects leaves it unregistered. Run the **full** relevant test classes, not just the one test a step names.

### What already exists — do not reimplement

- `app/pipelines/bam_stats_runner.py` — the global-stats runner `launch_coverage` complements, not replaces.
- `app/queue/feature_coverage_handlers.py` — the structural template: `@handler("feature_coverage", mode=HandlerMode.SUBPROCESS, job_class=JobClass.COMPUTE, resources=JobResources(cpu=1, mem_mb=1024, io=IoClass.HEAVY), max_attempts=2)`, returns `{"object_id", "facts", "report_path"}`, no derived objects.
- `app/queue/results.py:_apply_feature_coverage` — merges `facts` onto the BAM object, read-only (lines 1738–1769).
- `app/pipelines/gc_tracks.py` — `WINDOW_COUNT = 500`, `MIN_WINDOW_BASES = 100`, used as `min(WINDOW_COUNT, length // MIN_WINDOW_BASES)` windows per contig in `gc_tracks.windows()` (a contig shorter than `MIN_WINDOW_BASES` yields zero windows). The scheme to reuse in `build_windows_bed`.
- `app/config.py` — `bam_stats_dir` / `feature_coverage_dir` dir properties (derivative, under `bioinfo_home`, outside `objects/`).
- `app/services/pipeline_service.py:launch_feature_coverage` — the launch-function template (eligibility check, `refuse_if_over_budget`, sidecar checks, `enqueue`); signature `launch_feature_coverage(*, bam_id, owner, annotation_id=None, resource_override=False) -> Job` (lines 4466+).
- `app/pipelines/node_types.py` — every `launch_*` in `pipeline_service.py` needs a `_launch_*` adapter + `NodeTypeSpec`; `launch_function_names()` (line 1182) drives exhaustiveness. `_launch_feature_coverage` is at line 163.
- `app/api/v1/pipelines.py` — `POST /feature-coverage` + `GET /feature-coverage/{object_id}/report` (lines 748–792) are the routes to mirror.
- `app/services/suggestion_service.py` — `CARD_BUILDERS` tuple + `build_feature_coverage_card` (the closest analog; available for a BAM + resolves annotation).
- `app/queue/handlers.py` — imports handler modules for `@handler` side-effects (feature_coverage_handlers imported near line 1042).
- `CLAUDE.md` — the new-tool note: a tool needs a `tools.py` probe **and** a `suggestion_service.py` rule **and** a `/help/software` `TOOL_META` entry, or it stays invisible.

---

## File Structure

**Create:**
- `backend/app/pipelines/mosdepth_runner.py` — pure command-builder, windows-BED generator, parsers, summarizer.
- `backend/app/queue/mosdepth_handlers.py` — the `@handler` job.
- `backend/tests/pipelines/test_mosdepth_runner.py` — pure unit tests with fixtures.
- `backend/tests/queue/test_mosdepth_handlers.py` — handler wiring + report parse.
- `backend/tests/services/test_coverage_launch.py` — launch + card both-direction tests.

**Modify:**
- `backend/app/pipelines/tools.py` — `mosdepth()` probe + `TOOL_META["mosdepth"]`.
- `backend/Dockerfile` — add `mosdepth` to the apt block (Stage 0).
- `backend/app/config.py` — `coverage_dir` property.
- `backend/app/queue/results.py` — `_apply_coverage`.
- `backend/app/services/pipeline_service.py` — `launch_coverage`.
- `backend/app/pipelines/node_types.py` — `_launch_coverage` adapter + `NodeTypeSpec`.
- `backend/app/api/v1/pipelines.py` — `POST /pipelines/coverage` + `GET /pipelines/coverage/{object_id}/report`.
- `backend/app/queue/handlers.py` — import `mosdepth_handlers` for side-effects.
- `backend/app/services/suggestion_service.py` — `build_coverage_card` + `CARD_BUILDERS` entry.
- `backend/tests/services/test_suggestion_service.py` — append card tests.
- `frontend/src/components/BamResults.tsx` — render `coverage_*` facts beside bam_stats (Stage 2).
- `CLAUDE.md` — append the new-tool note entry for coverage/mosdepth.

Stages map to PRs: **Stage 0** = Tasks 1; **Stage 1** = Tasks 2–9 (one PR, or split at the handler boundary if preferred — each task is independently green); **Stage 2** = Tasks 10–11.

---

### Task 1: Tool registration (Stage 0)

**Files:** Modify `backend/app/pipelines/tools.py`, `backend/Dockerfile`; Create `backend/tests/pipelines/test_tools.py` addition (or extend existing).

- [ ] **Step 1: Add the `mosdepth()` probe**

Add near the other probes (after `bedtools` at line 813):

```python
def mosdepth() -> Tool:
    """Probe for mosdepth, the per-base/per-window coverage calculator."""
    return _probe("mosdepth", settings.mosdepth_path, ["--version"])
```

Follow the existing `_probe` pattern (cached by `tool_cache`). Confirm `_probe` accepts `(name, configured_path, args)` — it does (bedtools uses exactly this shape at line 813).

- [ ] **Step 2: Add the `TOOL_META` entry**

Append to `TOOL_META` (after `bedtools`, line ~2469), with license/citation **verified against the mosdepth repo at implementation time**:

```python
"mosdepth": ToolMeta(
    pipelines=(PipelineType.UTILITY,),
    one_liner="Fast, pragmatic read-depth calculator",
    summary=(
        "mosdepth computes per-base and per-window read coverage from a BAM "
        "using a fast index-based approach. It answers depth-uniformity and "
        "per-region coverage questions that alignment-wide statistics cannot."
    ),
    strengths=(
        "Per-base, per-window, and per-region depth in one pass",
        "Streams a BAM index; scales to large genomes",
        "MIT-licensed, no heavy dependencies",
    ),
    homepage="https://github.com/brentp/mosdepth",
    citation=(
        "Pedersen BS, Quinlan AR. Mosdepth: quick coverage calculation for "
        "genomes and exomes. Bioinformatics. 2018;34(5):867-868. "
        "doi:10.1093/bioinformatics/btx699"
    ),
    license="MIT",
    usage=(
        "Computes per-window and per-region read depth for the coverage "
        "report served on a BAM object."
    ),
),
```

- [ ] **Step 3: Install in the Dockerfile**

Add `mosdepth` to the `apt-get install` block (lines 95–124), with a build-time version comment matching the existing style ("samtools 1.21"):

```dockerfile
        bedtools \
        mosdepth \   # <-- new; record the trixie version here
```

Verify the exact trixie version at build time and write it in the comment.

- [ ] **Step 4: Run `test_every_tool_is_documented`**

Run: `docker compose exec api python -m pytest tests/pipelines/test_tools.py::TestToolMeta::test_every_tool_is_documented -q`
Expected: PASS (the four bibliographic fields are present).

- [ ] **Step 5: Confirm `/help/software` shows mosdepth**

Build the image (`docker compose up -d --build api`) and open `/help/software`; mosdepth appears with version, license, citation, usage. This is success criterion #1.

- [ ] **Step 6: Commit (Stage 0 PR)**

```bash
git add backend/app/pipelines/tools.py backend/Dockerfile
git commit -m "feat(pipelines): register mosdepth tool and probe"
```

---

### Task 2: The runner module

**Files:** Create `backend/app/pipelines/mosdepth_runner.py`; Create `backend/tests/pipelines/test_mosdepth_runner.py`.

- [ ] **Step 1: Write the failing test for the command builder + windows generator + parsers**

```python
import pytest
from app.pipelines import mosdepth_runner as mr

class TestBuildCommand:
    def test_windowed_mode_emits_by_windows_bed(self, tmp_path):
        bam = tmp_path / "aln.bam"
        windows = tmp_path / "windows.bed"
        cmd = mr.build_command(bam=bam, windows_bed=windows, prefix=tmp_path / "cov")
        assert "--by" in cmd
        assert str(windows) in cmd
        assert str(bam) in cmd
        assert "-t" in cmd and "1" in cmd

    def test_region_mode_emits_by_regions_bed(self, tmp_path):
        bam = tmp_path / "aln.bam"
        regions = tmp_path / "regions.bed"
        cmd = mr.build_command(bam=bam, regions_bed=regions, prefix=tmp_path / "cov")
        assert str(regions) in cmd
        assert "--by" in cmd

class TestBuildWindowsBed:
    def test_tiles_each_contig_like_gc_tracks(self):
        # Mirror gc_tracks.windows(): min(WINDOW_COUNT, length // MIN_WINDOW_BASES)
        # windows per contig, floored so a contig < 100bp yields zero windows.
        lengths = {"chr1": 5_000_000, "tiny": 50}
        beds = mr.build_windows_bed(lengths)
        chr1 = [b for b in beds if b[0] == "chr1"]
        assert len(chr1) == min(mr.WINDOW_COUNT, 5_000_000 // mr.MIN_WINDOW_BASES)
        tiny = [b for b in beds if b[0] == "tiny"]
        assert tiny == []  # < MIN_WINDOW_BASES => no windows (matches gc_tracks)

class TestParsers:
    def test_parse_summary_reads_mean_median_and_breadth(self, tmp_path):
        # Write a captured .mosdepth.summary.txt fixture and assert the dict.
        ...
    def test_parse_regions_reads_per_window_depth(self, tmp_path):
        # Write a captured .regions.bed.gz fixture and assert the dict.
        ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_mosdepth_runner.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'app.pipelines.mosdepth_runner'`

- [ ] **Step 3: Write the runner**

```python
"""mosdepth command-building, windows-BED generation, and output parsing.

Pure module (the quast_runner.py / feature_coverage_runner.py model): no
subprocess here, so every function is unit-testable without the binary.
Windowing reuses gc_tracks' scheme so the output feeds the same track viz.
"""
from app.pipelines import gc_tracks

WINDOW_COUNT = gc_tracks.WINDOW_COUNT          # 500
MIN_WINDOW_BASES = gc_tracks.MIN_WINDOW_BASES  # 100

def build_command(*, bam, windows_bed=None, regions_bed=None, prefix) -> list[str]:
    """mosdepth --by <bed> <prefix> <bam>, single-threaded (-t 1)."""
    assert bool(windows_bed) ^ bool(regions_bed), "exactly one --by source"
    by = windows_bed or regions_bed
    return ["mosdepth", "--by", str(by), "-t", "1",
            "--no-per-base", str(prefix), str(bam)]

def build_windows_bed(contig_lengths: dict[str, int]) -> list[tuple[str, int, int]]:
    """Tile each contig into windows, mirroring gc_tracks.windows().

    window_count = min(WINDOW_COUNT, length // MIN_WINDOW_BASES) per contig,
    so a contig shorter than MIN_WINDOW_BASES yields no windows (matching the
    app's existing track axis). Returns [(chrom, start, end), ...] in the same
    order gc_tracks uses, so a downstream chart can align the depth track to
    the GC track.
    """
    beds = []
    for chrom, length in contig_lengths.items():
        window_count = min(WINDOW_COUNT, length // MIN_WINDOW_BASES)
        if window_count == 0:
            continue
        width = length // window_count
        for start in range(0, length, width):
            beds.append((chrom, start, min(start + width, length)))
    return beds

def parse_summary(path) -> dict: ...   # mean/median depth, % bases >= thresholds
def parse_regions(path) -> dict: ...   # per-window/per-region depth array
def summarize(report: dict) -> dict: ...  # the coverage_* facts
```

`parse_summary`/`parse_regions` read the real column layouts from a captured
fixture (verify the trixie mosdepth `.mosdepth.summary.txt` and `.regions.bed.gz`
headers at implementation time — see Spec "Verify before implementing" #4).

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec api python -m pytest tests/pipelines/test_mosdepth_runner.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/mosdepth_runner.py backend/tests/pipelines/test_mosdepth_runner.py
git commit -m "feat(pipelines): add mosdepth runner (command, windows, parsers)"
```

---

### Task 3: Config dir property

**Files:** Modify `backend/app/config.py`.

- [ ] **Step 1: Add `coverage_dir` mirroring `feature_coverage_dir`**

After `feature_coverage_dir` (around line 405):

```python
@property
def coverage_dir(self) -> Path:
    """Generated mosdepth coverage reports (JSON), outside objects/.

    Derivative and regenerable like the other *_dir properties: a re-run
    replaces it, so it is not part of an object's durable payload.
    """
    return self.bioinfo_home / "coverage"
```

- [ ] **Step 2: Smoke-check the property resolves**

Run: `docker compose exec api python -c "from app.config import settings; print(settings.coverage_dir)"`
Expected: prints `<bioinfo_home>/coverage`.

- [ ] **Step 3: Commit**

```bash
git add backend/app/config.py
git commit -m "feat(config): add coverage_dir for mosdepth reports"
```

---

### Task 4: The handler

**Files:** Create `backend/app/queue/mosdepth_handlers.py`; Modify `backend/app/queue/handlers.py`; Create `backend/tests/queue/test_mosdepth_handlers.py`.

- [ ] **Step 1: Write the failing test**

```python
from unittest.mock import patch
from app.queue import mosdepth_handlers

class TestCoverageHandlerRegistration:
    def test_handler_is_registered(self):
        from app.queue.registry import get_handler
        assert get_handler("coverage") is not None

class TestCoverageHandlerRuns:
    def test_requires_bam_id(self):
        from app.errors import PermanentError
        with pytest.raises(PermanentError):
            mosdepth_handlers.run_coverage(_ctx_with_payload({}))
```

(`_ctx_with_payload` builds a minimal `JobContext` — mirror how
`test_feature_coverage_handlers.py` constructs one.)

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/queue/test_mosdepth_handlers.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'app.queue.mosdepth_handlers'`

- [ ] **Step 3: Write the handler, mirroring `feature_coverage_handlers.py`**

```python
"""coverage: per-window/per-region read depth for one BAM via mosdepth.

Split into runner (pure) + this handler (shells out), like feature_coverage.
"""
from app.config import settings
from app.queue import registry, tools
from app.queue.registry import HandlerMode, JobContext, JobClass, JobResources, IoClass, handler
from app.queue import results

@handler(
    "coverage",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
    max_attempts=2,
)
def run_coverage(ctx: JobContext) -> dict:
    """Per-window read coverage for one BAM.

    Read-only like bam_stats: no derived objects, one JSON report on disk
    plus summary facts merged onto the BAM by `_apply_coverage`.
    """
    mosdepth = tools.require(tools.mosdepth())

    bam_id = ctx.payload.get("bam_id")
    if not bam_id:
        raise PermanentError("coverage requires a 'bam_id'")

    # Resolve the BAM and its contig lengths (stored contigs_tsv facts, or
    # samtools idxstats on the .bai) -- never assume an ordering.
    ...
    windows_bed = settings.coverage_dir / f"{bam_id}.windows.bed"
    windows_bed.write_text("\n".join(
        f"{c}\t{s}\t{e}" for c, s, e in mr.build_windows_bed(contig_lengths)
    ))
    prefix = settings.coverage_dir / f"{bam_id}.mosdepth"
    cmd = mr.build_command(bam=bam_path, windows_bed=windows_bed, prefix=prefix)
    proc = await ctx.run(cmd)  # cancellation-checked subprocess, like feature_coverage
    if proc.returncode != 0:
        raise ToolError("mosdepth", proc.returncode, proc.stderr)

    report = {
        "summary": mr.parse_summary(prefix + ".mosdepth.summary.txt"),
        "windows": mr.parse_regions(prefix + ".regions.bed.gz"),
    }
    report_path = settings.coverage_dir / f"{bam_id}.coverage.json"
    report_path.write_text(json.dumps(report))
    facts = mr.summarize(report)
    return {"object_id": str(bam_id), "facts": facts, "report_path": str(report_path)}

import json  # top of file in practice
```

- [ ] **Step 4: Register the handler for side-effects in `handlers.py`**

Near the feature_coverage import (line ~1042):

```python
from app.queue import mosdepth_handlers  # noqa: F401  (registers @handler)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `docker compose restart worker` (handler changed), then
`docker compose exec api python -m pytest tests/queue/test_mosdepth_handlers.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/mosdepth_handlers.py backend/app/queue/handlers.py backend/tests/queue/test_mosdepth_handlers.py
git commit -m "feat(queue): add coverage handler (mosdepth)"
```

---

### Task 5: Launch function + node-type classification

**Files:** Modify `backend/app/services/pipeline_service.py`, `backend/app/pipelines/node_types.py`; Create `backend/tests/services/test_coverage_launch.py` (launch portion).

- [ ] **Step 1: Write the failing test (exhaustiveness + launch)**

```python
from app.services import pipeline_service as ps
from app.pipelines import node_types

class TestLaunchCoverageClassified:
    def test_node_types_covers_launch_coverage(self):
        assert "pipeline_service.launch_coverage" in node_types.launch_function_names()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_node_types.py -q`
Expected: FAIL (`launch_coverage` not yet classified) — or `ModuleNotFoundError` if the launch fn isn't written yet; write both in Step 3.

- [ ] **Step 3: Add `launch_coverage` to `pipeline_service.py`**

Mirror `launch_feature_coverage` (lines 4466+): eligibility via
`_check_bam_stats_callable`, `refuse_if_over_budget` with
`COVERAGE_MEM_MB = 2048`, resolve the BAM, require a `.bai` sidecar (refuse
with "Index it first" like feature_coverage does), build the payload, `enqueue`.

```python
COVERAGE_MEM_MB = 2048

async def launch_coverage(
    *,
    bam_id: PydanticObjectId,
    owner: str,
    regions_id: PydanticObjectId | None = None,
    resource_override: bool = False,
) -> Job:
    """Queue per-window read coverage for one BAM via mosdepth."""
    from app.queue import queue
    refuse_if_over_budget(
        declared_mb=COVERAGE_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )
    bam = await object_service.get_object(bam_id, owner=owner)
    _check_bam_stats_callable(bam)
    bai = await _sidecar_of_role(bam, SidecarRole.BAI)
    if bai is None:
        raise ValidationError(
            f"{bam.name!r} has no BAM index (.bai). Index it first.",
            details={"bam_id": str(bam.id), "needs": "index_bam"},
        )
    payload = {"bam_id": str(bam_id)}
    if regions_id is not None:
        payload["regions_id"] = str(regions_id)
    return await queue.enqueue("coverage", payload, owner=owner)
```

- [ ] **Step 4: Add the node-type adapter + spec in `node_types.py`**

After `_launch_feature_coverage` (line 168):

```python
async def _launch_coverage(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_coverage(
        bam_id=inputs["alignment"],
        owner=owner,
        regions_id=inputs.get("regions"),
    )
```

And register a `NodeTypeSpec` in the `NODE_TYPES` list (mirror the
`feature_coverage` spec: `run_kind=None`, `inputs=(alignment_port,)`,
`outputs=()`). The `launch_function_names()` exhaustiveness test then passes
with no second hand-written list.

- [ ] **Step 5: Run the node-types exhaustiveness test**

Run: `docker compose exec api python -m pytest tests/pipelines/test_node_types.py -q`
Expected: PASS — the test compares `launch_function_names()` against
`NODE_TYPES`, so `launch_coverage` must have its spec (CLAUDE.md #355/#366 trap).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/pipelines/node_types.py backend/tests/services/test_coverage_launch.py
git commit -m "feat(services): add launch_coverage and node-type classification"
```

---

### Task 6: API endpoints

**Files:** Modify `backend/app/api/v1/pipelines.py`.

- [ ] **Step 1: Add routes mirroring feature-coverage (lines 748–792)**

```python
class CoverageRequest(BaseModel):
    bam_id: PydanticObjectId
    regions_id: PydanticObjectId | None = None

@router.post("/coverage", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_coverage_endpoint(body: CoverageRequest, request: Request):
    owner = request.state.owner
    job = await pipeline_service.launch_coverage(
        bam_id=body.bam_id, owner=owner, regions_id=body.regions_id,
    )
    return job

@router.get("/coverage/{object_id}/report")
async def coverage_report(object_id: str, request: Request):
    """Serve the stored mosdepth JSON report for a BAM."""
    ...
```

Mirror the feature-coverage report route's owner/scope checks and 404 handling.

- [ ] **Step 2: Run the route tests**

Add route tests to `backend/tests/api/test_pipelines.py` (POST returns 201
with a job; GET report returns the stored JSON). Run:
`docker compose exec api python -m pytest tests/api/test_pipelines.py -k coverage -q`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add backend/app/api/v1/pipelines.py backend/tests/api/test_pipelines.py
git commit -m "feat(api): add /pipelines/coverage routes"
```

---

### Task 7: Results applier

**Files:** Modify `backend/app/queue/results.py`.

- [ ] **Step 1: Add `_apply_coverage` mirroring `_apply_feature_coverage` (line 1738)**

```python
async def _apply_coverage(result: dict, *, owner: str) -> None:
    """Record a coverage computation's numbers on the BAM it described.

    Read-only like bam_stats: no files to ingest, just facts merged onto
    the object.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return
    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("coverage_object_missing", object_id=object_id)
        return
    await obj.set({
        DataObject.facts: {**obj.facts, **facts},
        DataObject.updated_at: datetime.now(UTC),
    })
    log.info("coverage_applied", object_id=object_id,
             mean_depth=facts.get("coverage_mean_depth"))
```

- [ ] **Step 2: Wire it into the applier dispatch**

Find where `_apply_feature_coverage` is mapped to its job name (the
`APPLIERS` / `results_for` dispatch) and add `"coverage": _apply_coverage`
alongside it. (Confirm the exact dispatch key by grepping for
`feature_coverage` in `results.py`.)

- [ ] **Step 3: Run the applier test**

Add a test in `backend/tests/queue/test_results.py` that `_apply_coverage`
merges `coverage_*` facts onto a BAM and leaves other facts intact. Run:
`docker compose exec api python -m pytest tests/queue/test_results.py -k coverage -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/app/queue/results.py backend/tests/queue/test_results.py
git commit -m "feat(queue): apply coverage facts to the BAM"
```

---

### Task 8: Suggestion card

**Files:** Modify `backend/app/services/suggestion_service.py`; Modify `backend/tests/services/test_suggestion_service.py`.

- [ ] **Step 1: Write both-direction tests**

```python
from unittest.mock import patch
from app.services.suggestion_service import build_coverage_card

class _FakeTool:
    def __init__(self, available): self.available = available; self.version = "0.3.9"

class TestCoverageCard:
    def test_not_offered_for_a_non_bam(self):
        assert build_coverage_card(_fake_obj(kind=FormatKind.FASTQ)) is None

    def test_available_for_a_bam_when_mosdepth_installed(self):
        with patch("app.services.suggestion_service.tools.mosdepth",
                   return_value=_FakeTool(True)):
            card = build_coverage_card(_fake_obj(kind=FormatKind.BAM))
        assert card is not None
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/coverage"
        assert card.launch["body"]["bam_id"] == "abc123"
        assert "depth" in card.title.lower()

    def test_unavailable_when_mosdepth_not_installed(self):
        with patch("app.services.suggestion_service.tools.mosdepth",
                   return_value=_FakeTool(False)):
            card = build_coverage_card(_fake_obj(kind=FormatKind.BAM))
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None
        assert "mosdepth" in card.reason

    def test_flips_to_unavailable_when_probe_patched_off(self):
        # The direction that catches a broken seam: patch the *probe*, not the
        # frozen-at-import function object (CLAUDE.md trap).
        with patch("app.services.suggestion_service.tools.mosdepth",
                   return_value=_FakeTool(False)):
            card = build_coverage_card(_fake_obj(kind=FormatKind.BAM))
        assert card.status is CardStatus.UNAVAILABLE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/services/test_suggestion_service.py -k coverage -q`
Expected: FAIL, `ImportError: cannot import name 'build_coverage_card'`

- [ ] **Step 3: Write `build_coverage_card`**

```python
def build_coverage_card(obj) -> SuggestionCard | None:
    """Per-window read coverage via mosdepth.

    Available for any completed BAM (no annotation required, unlike
    feature_coverage). Distinct from bam_stats: bam_stats reports
    alignment-wide stats; this reports depth across the genome.
    """
    if obj.format.kind is not FormatKind.BAM:
        return None
    mosdepth = tools.mosdepth()
    if not mosdepth.available:
        return SuggestionCard(
            kind="coverage", category="ASSEMBLY_QC",
            title="Per-window coverage -- mosdepth",
            description="Read depth across the genome, in uniform windows.",
            why="Complements bam_stats' alignment-wide stats with depth uniformity.",
            status=CardStatus.UNAVAILABLE,
            reason="mosdepth is not installed.",
        )
    return SuggestionCard(
        kind="coverage", category="ASSEMBLY_QC",
        title="Per-window coverage -- mosdepth",
        description="Read depth across the genome, in uniform windows.",
        why="Complements bam_stats' alignment-wide stats with depth uniformity.",
        status=CardStatus.AVAILABLE,
        launch={"endpoint": "/pipelines/coverage",
                "body": {"bam_id": str(obj.id)}},
    )
```

Add `("coverage", build_coverage_card)` to `CARD_BUILDERS`.

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec api python -m pytest tests/services/test_suggestion_service.py -k coverage -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat(services): add coverage suggestion card"
```

---

### Task 9: Real-database spot check (RS-4)

- [ ] **Step 1: Against a real aligned BAM, confirm the card and the job**

```bash
docker compose exec api python -c "
from app.services.suggestion_service import build_coverage_card
from app.services import object_service
# pick a real completed BAM in your test project
obj = <a real BAM DataObject>
card = build_coverage_card(obj)
print(card.status, card.launch)
"
```

Expected: `available` with a `/pipelines/coverage` launch body — not a
false `unavailable` from a hand-built fact the unit tests fed. This is the
protein.faa/duplicate-assembly lesson: fixtures that already look right hide a
rule that misreads real objects.

- [ ] **Step 2: Launch end-to-end and confirm facts land**

```bash
docker compose exec api python -c "
import asyncio
from app.services import pipeline_service
job = asyncio.run(pipeline_service.launch_coverage(bam_id=<real_bam_id>, owner=<owner>))
print(job.id)
"
```

Then wait for the job, `docker compose restart worker` if you changed the
handler since the stack started, and confirm `coverage_mean_depth` etc. appear
on the BAM's facts (DB check, not just the green suite).

- [ ] **Step 3: Manual UI verification**

`docker compose up -d --build api web worker`, open the BAM's detail panel;
the coverage card is present in the Actions tab, and (after Stage 2) its
facts render in BamResults. UI verification is manual — there is no headless
component-test setup in this repo.

---

### Task 10: BED-region mode (Stage 2)

**Files:** Modify `backend/app/pipelines/mosdepth_runner.py`, `backend/app/queue/mosdepth_handlers.py`, `backend/app/services/suggestion_service.py`, `backend/app/services/pipeline_service.py`.

- [ ] **Step 1: Region mode in the runner**

`build_command` already accepts `regions_bed`; add `parse_regions` handling so
a regions BED yields per-region depth and `summarize` records
`coverage_mode="regions"`. Unit-test the region path against a captured
`.regions.bed.gz` from a BED run.

- [ ] **Step 2: Card offers region mode when a regions object resolves**

In `build_coverage_card`, resolve a regions `DataObject` of the same reference
(via the same `resolve_reference` machinery `feature_coverage` uses). When
present, the launch body includes `regions_id`; the card's `why` names the
mode ("depth over your selected regions").

- [ ] **Step 3: `launch_coverage` accepts `regions_id`**

Already in the Task 5 signature; confirm the handler reads
`ctx.payload["regions_id"]` and passes it as `regions_bed` to `build_command`
instead of the generated windows BED.

- [ ] **Step 4: Tests**

Extend `test_mosdepth_runner.py` (region parse) and `test_coverage_launch.py`
(card offers region mode when a regions object is present, windowed otherwise).
Run both suites.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/mosdepth_runner.py backend/app/queue/mosdepth_handlers.py backend/app/services/suggestion_service.py backend/app/services/pipeline_service.py backend/tests
git commit -m "feat(coverage): add BED-region mode"
```

---

### Task 11: Track-style frontend viz (Stage 2)

**Files:** Modify `frontend/src/components/BamResults.tsx`; optionally `frontend/src/components/CoverageChart.tsx`.

- [ ] **Step 1: Render coverage facts in BamResults**

Read `coverage_mean_depth` / `coverage_median_depth` / `coverage_bases_ge_*`
from the BAM's facts and show them beside the existing `bam_stats` panel, so
the two are visible together and clearly distinct (success criterion #3).

- [ ] **Step 2: Depth track**

Store the per-window depth in a `BirdsEyeCoverageChart`-compatible shape (or a
dedicated `CoverageChart` panel) keyed by contig, reusing the `gc_tracks`
windowing so the axis matches existing GC tracks. Confirm the chart's expected
fact/array shape first (Spec "Verify before implementing" #5).

- [ ] **Step 3: Manual UI verification**

Against the worktree stack on 5273 (or the main stack on 5173), confirm the
coverage card launches, the job completes, and the depth track renders. UI
verification is manual.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/BamResults.tsx frontend/src/components/CoverageChart.tsx
git commit -m "feat(frontend): render mosdepth coverage facts and depth track"
```

---

## Acceptance criteria mapping

| Criterion | Satisfied by |
|---|---|
| Passes `test_every_tool_is_documented` | Task 1 (R0-2) |
| Visible on `/help/software` | Task 1 (R0-4) |
| Runs end-to-end on a real BAM | Tasks 2–9, verified in Task 9 |
| Suggestion card available for any completed BAM | Task 8, distinct from bam_stats by title/why |
| Clearly distinguished from bam_stats | Task 8 card copy + Task 11 side-by-side render |

## Cross-cutting checks (run before each stage's PR merges)

- RS-1: full `TestExhaustiveness` class passes (Task 5).
- RS-2: any new report/sidecar role present in its registry with the
  exhaustiveness test updated.
- RS-3: suggestion rule tested in both directions, including "flips to
  unavailable when the probe is patched off" (Task 8).
- RS-4: real-database spot check, not only fixtures (Task 9).
- RS-5: handler reads contig lengths from a resolved source, writes the
  windows BED atomically before launch (Task 4).

## Out of scope

- Replacing or folding into `bam_stats`.
- Exporting mosdepth's raw `.per-base.bed.gz` as a downloadable artifact (the
  report endpoint serves parsed data).
- Multi-BAM / cross-sample coverage comparisons.
- Auto-running mosdepth from the align thread (it is a user-launched card).
