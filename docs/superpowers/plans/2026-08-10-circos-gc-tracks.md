# Circos GC Tracks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Draw a Circos-style circular plot of a polished genome — contigs around the perimeter, GC content and GC skew as concentric rings — so compositional anomalies and a bacterial chromosome's origin of replication are visible.

**Architecture:** A new pure scanner walks a FASTA once and produces per-window GC and GC-skew tracks; a new thread-mode QC job runs it and merges one `gc_tracks` fact onto the assembly; a new hand-rolled SVG component draws the rings. No new tool dependency and no new npm dependency.

**Tech Stack:** Python 3 / pytest (backend), React 18 + TypeScript, hand-rolled SVG (frontend).

Implements [#151](https://github.com/syntheticgio/bioflow/issues/151). Spec: [`docs/superpowers/specs/2026-08-10-circos-gc-tracks-design.md`](../specs/2026-08-10-circos-gc-tracks-design.md).

---

## Background an engineer needs before starting

**The existing GC code cannot be extended to do this, and reusing it is the main trap.** `sequence_stats.fasta_stats` (`backend/app/storage/sequence_stats.py:195`) computes whole-assembly GC, and it is a **sampler**: `max_bases: int = 50_000_000`, and for uncompressed files over that budget it seeks to ~2,000 disjoint blocks spread across the file (`_fasta_sample_strided`, `:286`), discarding a partial line after each seek. It cannot produce contiguous ordered windows and does not know where in a contig its bytes came from.

GC skew is cumulative and order-dependent — its sign flip around the chromosome is the entire diagnostic — so a sampled subset of windows in unknown positions has no trend to read. **Write a new scanner. Do not add counters to the sampler, and do not add them to `_parse_fasta`'s loop either**, which is subject to a 256MB truncation limit (`parsers.py:40`, `:578`). Both shortcuts produce correct-looking windows for small test genomes and biased or truncated tracks for exactly the multi-chromosome genomes a Circos plot is for.

**Window sizing is a fixed count, not a fixed width.** 500 windows per contig, floored at 100 bp minimum window width. A fixed base width is unbounded — a 3 Gb genome at 10 kb windows is 300,000 windows against Mongo's 16 MB document cap, while a 5 Mb bacterial genome gets only 500. A fixed count renders identically at every genome size, which is what a radial plot needs since every ring is drawn at the same angular resolution regardless of genome length.

**Read-only QC jobs in this repo create no run record.** `launch_completeness` (`pipeline_service.py:3633`) enqueues directly with no `create_run` and no `link_job`. Follow it. Do not add a `RunKind` or `RunJobRole` member.

**This is a `THREAD` handler, which carries a specific trap.** It runs pure Python with no subprocess, so `HandlerMode.THREAD`. Thread handlers run in a worker-pool thread with no event loop, and `asyncio.run()` is the obvious-looking way to reach an async helper — but this process's Mongo client is a module-level `AsyncIOMotorClient` bound to the loop `connect_to_mongo()` ran on, and a second loop makes Motor raise "attached to a different loop" the instant a query touches it. Use `app.db.client.run_from_thread` if any async path is needed. Every unit test stays green under this bug because the tests mock the seam.

**How this repo tests.** Backend tests run in a container. **From a worktree you must use `./backend/run-worktree-tests.sh`** — a bare `docker compose exec api python -m pytest` silently tests `main`'s code instead of yours. There is no frontend component-testing setup, so frontend verification is manual in the browser.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/pipelines/gc_tracks.py` | **New.** `compute_gc_tracks`. Pure, streaming, cancellable. |
| `backend/tests/pipelines/test_gc_tracks.py` | **New.** Unit tests. |
| `backend/app/queue/assembly_qc_handlers.py` | Add the `analyze_gc_tracks` handler. |
| `backend/app/services/pipeline_service.py` | Add `launch_gc_tracks`, modelled on `launch_completeness`. |
| `backend/app/services/suggestion_service.py` | Add the suggestion card. |
| `backend/tests/services/test_suggestion_service.py` | Add the rule's test case. |
| `frontend/src/components/CircosPlot.tsx` | **New.** Pure presentational SVG, ring-descriptor driven. |
| `frontend/src/components/AssemblyFacts.tsx` | Render `<CircosPlot>` when the fact is present. |

---

## Task 1: The scanner

**Files:**
- Create: `backend/app/pipelines/gc_tracks.py`
- Test: `backend/tests/pipelines/test_gc_tracks.py`

- [ ] **Step 1: Write the failing tests**

The skew-sign and lowercase tests are the two that catch silent, plausible-looking wrongness.

```python
def test_gc_percent_per_window(tmp_path):
    p = tmp_path / "a.fasta"
    p.write_text(">c1\n" + ("GC" * 5000) + "\n")   # 100% GC
    out = gc_tracks.compute_gc_tracks(p, Compression.NONE)
    gc = out["contigs"][0]["gc"]
    assert all(v == 100.0 for v in gc if v is not None)


def test_skew_sign_flips_where_composition_flips(tmp_path):
    """(G-C)/(G+C) changing sign is the whole diagnostic -- it locates the
    origin of replication. A G/C transposition in the formula still produces a
    plausible-looking ring, so assert the sign, not just the magnitude."""
    p = tmp_path / "a.fasta"
    p.write_text(">c1\n" + ("G" * 50_000) + ("C" * 50_000) + "\n")
    out = gc_tracks.compute_gc_tracks(p, Compression.NONE)
    skew = [v for v in out["contigs"][0]["skew"] if v is not None]
    assert skew[0] > 0            # G-rich half
    assert skew[-1] < 0           # C-rich half


def test_all_n_window_is_null_not_zero(tmp_path):
    """Zero GC and 'no sequence here' are different facts. A gap plotted as 0%
    draws a cliff that reads as a real compositional feature."""
    p = tmp_path / "a.fasta"
    p.write_text(">c1\n" + ("N" * 100_000) + "\n")
    out = gc_tracks.compute_gc_tracks(p, Compression.NONE)
    assert all(v is None for v in out["contigs"][0]["gc"])


def test_lowercase_sequence_counts(tmp_path):
    """Soft-masked FASTA is common. A scanner that forgets case silently
    halves GC on a masked genome."""
    p = tmp_path / "a.fasta"
    p.write_text(">c1\n" + ("gc" * 5000) + "\n")
    out = gc_tracks.compute_gc_tracks(p, Compression.NONE)
    gc = [v for v in out["contigs"][0]["gc"] if v is not None]
    assert gc and all(v == 100.0 for v in gc)


def test_short_contig_gets_fewer_windows_not_tiny_ones(tmp_path):
    """A 2kb plasmid divided 500 ways is 4bp per window, where skew is noise."""
    p = tmp_path / "a.fasta"
    p.write_text(">small\n" + ("ACGT" * 500) + "\n")   # 2000 bp
    out = gc_tracks.compute_gc_tracks(p, Compression.NONE)
    c = out["contigs"][0]
    assert len(c["gc"]) == 20                 # 2000 // 100
    assert c["window_bases"] >= gc_tracks.MIN_WINDOW_BASES


def test_keeps_longest_contigs_and_flags_partial(tmp_path):
    p = tmp_path / "a.fasta"
    body = "".join(f">c{i}\n" + "ACGT" * 250 + "\n" for i in range(60))
    body += ">longest\n" + "ACGT" * 5000 + "\n"
    p.write_text(body)
    out = gc_tracks.compute_gc_tracks(p, Compression.NONE)
    assert len(out["contigs"]) == 50
    assert out["gc_tracks_partial"] is True
    assert any(c["name"] == "longest" for c in out["contigs"])


def test_cancel_propagates(tmp_path):
    p = tmp_path / "a.fasta"
    p.write_text(">c1\n" + ("ACGT" * 100_000) + "\n")
    ev = threading.Event()
    ev.set()
    with pytest.raises(JobCancelled):
        gc_tracks.compute_gc_tracks(p, Compression.NONE, cancel_event=ev)
```

- [ ] **Step 2: Implement**

Constants: `WINDOW_COUNT = 500`, `MIN_WINDOW_BASES = 100`, `FINE_BINS = 10_000`, and reuse `parsers.MAX_STORED_CONTIGS` (50) rather than adding a second number for the same idea.

Signature mirrors `sequence_stats.fasta_stats`: `compute_gc_tracks(path, compression, *, cancel_event=None) -> dict`, returning `{}` on an unreadable file rather than raising (`sequence_stats.py:239` is the precedent), but re-raising `JobCancelled`.

**Window width depends on total contig length, which is unknown until the contig ends.** Accumulate into `FINE_BINS` fine bins per contig, then aggregate down to the final window count at commit. Do not make a second pass over the file to learn lengths — that is the larger cost on a multi-GB genome.

Per contig, final window count is `min(WINDOW_COUNT, length // MIN_WINDOW_BASES)`.

Count `G`/`g`/`C`/`c` and `A`/`a`/`T`/`t` separately per bin. A window whose `G+C+A+T` is zero stores `None` for both tracks. Round to 2 dp on write — full float precision triples stored size to encode noise below what a ring can render.

Keep only the longest `MAX_STORED_CONTIGS` contigs; set `gc_tracks_partial` when any were dropped.

Check `cancel_event` every 100,000 lines, matching `sequence_stats.py:280`. This is the longest pure-Python loop in the codebase and an uncancellable one blocks a worker slot for the length of a genome.

- [ ] **Step 3: Run the tests**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_gc_tracks.py -q
```

---

## Task 2: The handler

**Files:**
- Modify: `backend/app/queue/assembly_qc_handlers.py`

- [ ] **Step 1: Add `analyze_gc_tracks`**

```python
@handler(
    "analyze_gc_tracks",
    mode=HandlerMode.THREAD,
    job_class=JobClass.COMPUTE,
    # One core: the scan is single-threaded. Modest memory: fine bins for one
    # contig at a time. Heavy IO: it reads the entire file, unlike the sampler
    # in sequence_stats which spends a fixed byte budget.
    resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
    max_attempts=1,
)
def analyze_gc_tracks(ctx: JobContext) -> dict:
```

Re-read the THREAD-handler trap in the background section before writing this: **no `asyncio.run()`**.

Return `{"facts": {"gc_tracks": result}}` when the scan produced contigs; return facts unchanged when it returned `{}` rather than writing an empty track.

Extend the lease — a full scan of a large genome is not a short job.

- [ ] **Step 2: Restart the worker**

```bash
docker compose restart worker
```

`worker` does not hot-reload. Skipping this runs the old in-memory code and reads as "the fix didn't work."

---

## Task 3: The launcher

**Files:**
- Modify: `backend/app/services/pipeline_service.py`

- [ ] **Step 1: Add `launch_gc_tracks`**

Copy `launch_completeness` (`:3633`) for shape — single input, `_resolve_readable`, direct `queue.enqueue` with **no `create_run` and no `link_job`**.

`dedup_key=f"gc_tracks:{obj.id}"`. No tool check: there is no external tool.

- [ ] **Step 2: Wire the API route**

Same router as the other assembly-QC launchers.

---

## Task 4: The suggestion rule

**Files:**
- Modify: `backend/app/services/suggestion_service.py`
- Test: `backend/tests/services/test_suggestion_service.py`

- [ ] **Step 1: Add the card and its test**

Offer GC track analysis for reference-role FASTA objects. Gate on contig count where already known — offering a Circos plot for a 200,000-contig draft offers a run whose output will not render.

Same two traps as every other rule here: `protein.faa` is FASTA but has no meaningful GC skew, and duplicate assemblies must be deduplicated by digest.

- [ ] **Step 2: Check against the real database**

```bash
docker compose exec api python -c "..."
```

---

## Task 5: The chart

**Files:**
- Create: `frontend/src/components/CircosPlot.tsx`
- Modify: `frontend/src/components/AssemblyFacts.tsx`

- [ ] **Step 1: Build the component**

Hand-rolled SVG, **no library**. This does not repeat #150's cytoscape departure: that needed a computed force-directed layout, while every element here sits at `(radius, angle)` where angle is a known fraction of a known total length. Trigonometry over fully determined positions is exactly what this repo's hand-rolled charts already cover.

Three rings, outermost first:

- **Contig arcs**, sized proportionally to length, small angular gap between them, labels only on arcs wide enough to carry one.
- **GC content**, filled area against the assembly's **mean GC as baseline**. Deviation from the mean is the signal — a region far off the mean is a candidate horizontal transfer or contaminant contig. Sequential colour scale (one hue, varying lightness).
- **GC skew**, **diverging** fill: positive and negative in different hues against a zero baseline. A single-colour fill hides the sign flip, which is the whole diagnostic.

**Break the line at `null` windows** rather than interpolating or drawing zero.

**Render only when the tracked contig count is legible** — 24 or fewer. Above that the perimeter is a picket fence of unlabelled slivers, which is not a smaller version of the visualization; render nothing, matching how `<NxChart>` simply omits NGx when genome size is absent.

**Accept rings as a list of descriptors rather than hard-coding two.** #177 (repeat density) and #179 (gene density) each add one against the windowing scheme this plan fixes, and both should be a data change rather than a component rewrite.

Both themes must hold up — light and dark.

- [ ] **Step 2: Wire it in**

Read `facts.gc_tracks` in `AssemblyFacts.tsx`, render `<CircosPlot>` alongside `<NxChart>` and `<BuscoChart>` when present.

- [ ] **Step 3: Verify in the browser**

```bash
./ops/worktree-up.sh
```

UI on localhost:5273. **Verify against a real bacterial genome** — E. coli or B. subtilis, where the origin of replication is a documented fact. A synthetic fixture confirms the code draws what it was given, not that what it was given is biologically right. The skew sign flip should land at the known origin.

---

## Task 6: Close out

- [ ] Run the full suite: `./backend/run-worktree-tests.sh tests/ -q`. Read the count, not the exit code.
- [ ] Commit as `feat(pipelines): add GC content and GC skew rings for finished genomes`. Keep scanner, handler, and frontend separable.
- [ ] Push and open a PR against `main` with `Closes #151`. Label `type:feature`, `area:backend`, `area:frontend`, `area:pipelines`.
- [ ] **Do not merge.** The end state is an open PR.
- [ ] Note in the PR that #177 and #179 are unblocked by this landing, since both add rings to `CircosPlot` and windows to this scheme.
