# Synteny Dot Plot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compare a draft assembly against a reference genome with minimap2 and draw the result as a synteny dot plot on the Reference Quality tab, so breaks, inversions, and translocations are visible at a glance.

**Architecture:** A new runner builds a minimap2 command and parses its PAF output into a bounded segment list; a new read-only QC handler runs it and merges one `synteny_alignment` fact onto the draft assembly; a new hand-rolled SVG component draws it. No new tool dependency and no new npm dependency — minimap2 is already installed and already used for genome-to-genome alignment by RagTag.

**Tech Stack:** Python 3 / pytest (backend), React 18 + TypeScript, hand-rolled SVG (frontend).

Implements [#149](https://github.com/syntheticgio/bioflow/issues/149). Spec: [`docs/superpowers/specs/2026-08-10-synteny-dot-plot-design.md`](../specs/2026-08-10-synteny-dot-plot-design.md).

---

## Background an engineer needs before starting

**PAF, precisely.** Tab-separated, 12 mandatory columns then a variable number of `tag:type:value` fields. Parse by index into the fixed prefix and ignore the tail. The columns this needs, 0-based:

| Index | Meaning |
|---|---|
| 0 | query (assembly contig) name |
| 1 | query length |
| 2 | query start |
| 3 | query end |
| 4 | strand, `+` or `-` |
| 5 | target (reference contig) name |
| 6 | target length |
| 7 | target start |
| 8 | target end |

Coordinates are 0-based half-open on both axes — already what an SVG wants, so no conversion.

**`--secondary=no` is load-bearing, not decoration.** minimap2 emits secondary alignments by default. On a repeat-rich genome every repeat copy hits every other copy, and those render as an off-diagonal scatter indistinguishable from a real translocation. Omitting this flag produces a plot that looks like it found something.

**Divergence already exists — do not invent a second vocabulary.** `ragtag_runner.py:31` defines `Divergence.SAME_SPECIES` / `SAME_GENUS` / `DISTANT`, and `_mm2_preset` (`ragtag_runner.py:47`) maps them to `-x asm5` / `-x asm10` / `-x asm20`. Import and reuse both. It is already a user-facing choice on the scaffold dialog.

**Read-only QC jobs in this repo create no run record.** Both `launch_completeness` (`pipeline_service.py:3633`) and `launch_misassembly_qc` (`:4180`) enqueue directly with no `create_run` and no `link_job`, because they produce facts on an existing object rather than a new object. Follow them. Do **not** add a `RunKind` or `RunJobRole` member — that is the pattern for pipelines that produce artifacts.

**How this repo tests.** Backend tests run in a container. **From a worktree you must use `./backend/run-worktree-tests.sh`** — a bare `docker compose exec api python -m pytest` silently tests `main`'s code instead of yours, because the `api` container bind-mounts the main checkout. There is no frontend component-testing setup (no jsdom, zero `.test.tsx`), so frontend verification is manual in the browser.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/pipelines/synteny_runner.py` | **New.** `build_synteny_command` + `parse_paf`. Both pure. |
| `backend/tests/pipelines/test_synteny_runner.py` | **New.** Unit tests for both. |
| `backend/app/queue/assembly_qc_handlers.py` | Add the `analyze_synteny` handler. |
| `backend/app/services/pipeline_service.py` | Add `launch_synteny`, modelled on `launch_misassembly_qc`. |
| `backend/app/services/suggestion_service.py` | Add the suggestion card. |
| `backend/tests/services/test_suggestion_service.py` | Add the rule's test case. |
| `frontend/src/components/SyntenyPlot.tsx` | **New.** Pure presentational SVG. |
| `frontend/src/components/AssemblyFacts.tsx` | Render `<SyntenyPlot>` when the fact is present. |

---

## Task 1: The runner

**Files:**
- Create: `backend/app/pipelines/synteny_runner.py`
- Test: `backend/tests/pipelines/test_synteny_runner.py`

- [ ] **Step 1: Write the failing tests**

Cover the command builder and the parser. The filtering and cap tests are the ones that matter — they encode decisions that are silently wrong if reversed.

```python
MIN = synteny_runner.MIN_SEGMENT_LENGTH
MAX = synteny_runner.MAX_SYNTENY_SEGMENTS


def _paf(query, qs, qe, strand, target, ts, te, qlen=100_000, tlen=100_000):
    return "\t".join(
        [query, str(qlen), str(qs), str(qe), strand,
         target, str(tlen), str(ts), str(te), "500", "500", "60"]
    )


def test_command_uses_preset_for_divergence():
    cmd = synteny_runner.build_synteny_command(
        minimap2_path="/usr/bin/minimap2",
        reference=Path("/ref.fa"),
        draft=Path("/draft.fa"),
        divergence=ragtag_runner.Divergence.SAME_GENUS,
        threads=4,
    )
    assert "-x" in cmd and "asm10" in cmd


def test_command_suppresses_secondary_alignments():
    """Secondary alignments render as off-diagonal scatter that reads as a
    translocation. Without this flag the plot invents findings."""
    cmd = synteny_runner.build_synteny_command(
        minimap2_path="/usr/bin/minimap2",
        reference=Path("/ref.fa"),
        draft=Path("/draft.fa"),
        divergence=ragtag_runner.Divergence.SAME_SPECIES,
        threads=4,
    )
    assert "--secondary=no" in cmd


def test_parses_strand_both_ways():
    text = "\n".join([
        _paf("c1", 0, 5000, "+", "chrI", 0, 5000),
        _paf("c2", 0, 5000, "-", "chrI", 9000, 14000),
    ])
    out = synteny_runner.parse_paf(text)
    assert [s[6] for s in out["segments"]] == ["+", "-"]


def test_ignores_trailing_tag_fields():
    line = _paf("c1", 0, 5000, "+", "chrI", 0, 5000) + "\tNM:i:12\ttp:A:P\tcm:i:100"
    out = synteny_runner.parse_paf(line)
    assert len(out["segments"]) == 1


def test_drops_blocks_below_minimum_length():
    text = "\n".join([
        _paf("c1", 0, MIN - 1, "+", "chrI", 0, MIN - 1),
        _paf("c1", 0, MIN + 1, "+", "chrI", 0, MIN + 1),
    ])
    out = synteny_runner.parse_paf(text)
    assert len(out["segments"]) == 1


def test_skips_malformed_lines_without_raising():
    text = "not\tenough\tcolumns\n" + _paf("c1", 0, 5000, "+", "chrI", 0, 5000)
    out = synteny_runner.parse_paf(text)
    assert len(out["segments"]) == 1


def test_cap_keeps_the_longest_not_the_first():
    """PAF is emitted in query order. Keeping the first N would keep everything
    from the first contigs and nothing from the rest -- a positional bias that
    renders as 'the genome aligns only at one end', which looks like a real
    finding. A count-only assertion passes under that bug, so assert on which
    segments survive."""
    short = [_paf(f"c{i}", 0, MIN + 10, "+", "chrI", i * 100, i * 100 + MIN + 10)
             for i in range(MAX)]
    long_one = _paf("zLast", 0, 900_000, "+", "chrI", 0, 900_000)
    out = synteny_runner.parse_paf("\n".join(short + [long_one]))

    assert len(out["segments"]) == MAX
    assert out["synteny_segments_partial"] is True
    assert any(s[3] == "zLast" for s in out["segments"])


def test_no_partial_flag_when_under_cap():
    out = synteny_runner.parse_paf(_paf("c1", 0, 5000, "+", "chrI", 0, 5000))
    assert "synteny_segments_partial" not in out


def test_collects_axis_lengths_from_records():
    """Axes must span the full genome even where nothing aligned -- an
    unaligned chromosome is a finding, and an axis scaled to the data alone
    crops it out of existence."""
    out = synteny_runner.parse_paf(
        _paf("c1", 0, 5000, "+", "chrI", 0, 5000, qlen=812430, tlen=230218)
    )
    assert out["target_lengths"]["chrI"] == 230218
    assert out["query_lengths"]["c1"] == 812430
```

- [ ] **Step 2: Implement**

`MIN_SEGMENT_LENGTH = 1000`, `MAX_SYNTENY_SEGMENTS = 10_000` as module constants.

`build_synteny_command(*, minimap2_path, reference, draft, divergence, threads) -> list[str]` returns `[minimap2_path, *_mm2_preset(divergence).split(), "--secondary=no", "-t", str(threads), str(reference), str(draft)]`. Note `_mm2_preset` returns `"-x asm5"` as one string, so split it.

`parse_paf(text) -> dict` returns `{"segments": [...], "target_lengths": {...}, "query_lengths": {...}}` plus `synteny_segments_partial: True` when capped. Each segment is the positional array `[target_name, target_start, target_end, query_name, query_start, query_end, strand]`.

Filter on target-axis length (`target_end - target_start`). Collect axis lengths from **every** record read, before the cap is applied — an axis is about the genome, not about which segments survived. When over the cap, sort by target-axis length descending and keep the first `MAX_SYNTENY_SEGMENTS`.

- [ ] **Step 3: Run the tests**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_synteny_runner.py -q
```

---

## Task 2: The handler

**Files:**
- Modify: `backend/app/queue/assembly_qc_handlers.py`

- [ ] **Step 1: Add `analyze_synteny`**

Follow `assess_misassemblies` in the same file for structure: resolve inputs, build the command, run it, parse, return `{"facts": {...}}`.

```python
@handler(
    "analyze_synteny",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.LIGHT),
    # One attempt: deterministic tool on deterministic input, so a retry fails
    # the same way twice. Same reasoning assess_completeness gives above.
    max_attempts=1,
)
def analyze_synteny(ctx: JobContext) -> dict:
```

minimap2 writes PAF to **stdout**, unlike QUAST which writes report files — capture it rather than reading an output path. Redirect to a file in the job workdir and read it back rather than buffering a large stdout in memory.

The fact is a single nested dict under one key:

```python
facts = {"synteny_alignment": {
    "reference_object_id": ctx.payload.get("reference_object_id"),
    "reference_name": ctx.payload.get("reference_name"),
    "divergence": divergence,
    **parsed,
}}
```

Extend the lease as the other long QC handlers do — a whole-genome alignment against a large reference is not a short job.

- [ ] **Step 2: Restart the worker**

`worker` does not hot-reload. After editing a queue handler:

```bash
docker compose restart worker
```

Skipping this makes the job run the old in-memory code and reads as "the handler isn't picking up my change."

---

## Task 3: The launcher

**Files:**
- Modify: `backend/app/services/pipeline_service.py`

- [ ] **Step 1: Add `launch_synteny`**

Copy `launch_misassembly_qc` (`:4180`) and adapt. Keep all four of its guards:

1. reference resolution, with the "name the one to use" error when several exist
2. reject draft-equals-reference
3. reject draft and reference in different projects
4. no `create_run`, no `link_job` — enqueue directly

`tools.require(tools.minimap2())` for the tool check. `dedup_key=f"analyze_synteny:{draft.id}:{reference.id}"`. `divergence` defaults to `ragtag_runner.Divergence.SAME_SPECIES`, matching `launch_scaffold:4107`.

- [ ] **Step 2: Wire the API route**

Follow whatever route `launch_misassembly_qc` is exposed on, in the same router.

---

## Task 4: The suggestion rule

**Files:**
- Modify: `backend/app/services/suggestion_service.py`
- Test: `backend/tests/services/test_suggestion_service.py`

- [ ] **Step 1: Add the card and its test**

Offer synteny analysis when the project holds a draft assembly and at least one alignable reference-role FASTA.

Two traps this repo has already been bitten by, both of which made a project with one usable reference refuse to align:

- **`protein.faa` and `cds_from_genomic.fna` are FASTA but are not alignable references.** A rule keying on format alone counts them.
- **The same assembly stored twice counts as two**, producing a spurious "several references, name one" ambiguity. Deduplicate by digest.

Test the direction that fails when the seam breaks: assert the card is **unavailable** when the probe is patched off. The image ships minimap2 as installed, so an "available" assertion passes whether or not the patch worked.

- [ ] **Step 2: Check against the real database**

Unit tests here feed hand-built objects that already look the way the rules expect. Verify against real objects:

```bash
docker compose exec api python -c "..."
```

---

## Task 5: The chart

**Files:**
- Create: `frontend/src/components/SyntenyPlot.tsx`
- Modify: `frontend/src/components/AssemblyFacts.tsx`

- [ ] **Step 1: Build the component**

Hand-rolled SVG, no dependency — every segment's position is given by its coordinates, so there is no layout to compute. This is not #150's cytoscape case.

Four things that are load-bearing rather than cosmetic:

- **Facet by contig on both axes**, with thin separator lines between bands. On a single concatenated axis a contig boundary is indistinguishable from a real break in alignment.
- **Order Y (assembly contigs) by median target position**, not by name or length. Name order is arbitrary with respect to the reference and turns a perfectly collinear assembly into scattered disconnected bands — the plot's central signal destroyed by sort order alone.
- **Colour by strand.** At whole-genome zoom a short inverted segment's slope is unreadable but its colour is not.
- **Scale axes from `target_lengths` / `query_lengths`**, not from the segments' extent, so an unaligned chromosome still occupies its span.

`aria-label` describing the comparison, matching `BuscoChart.tsx` and `NxChart.tsx`.

- [ ] **Step 2: Wire it in**

In `AssemblyFacts.tsx`, read `facts.synteny_alignment` and render `<SyntenyPlot>` beside `<NxChart>` when present. When absent render nothing — no empty state, no disabled control, matching how NGx degrades when genome size is missing.

- [ ] **Step 3: Verify in the browser**

```bash
./ops/worktree-up.sh
```

UI on localhost:5273. **Construct a case with a known inversion** and confirm the reversed segment renders on the opposite diagonal. A clean assembly is a straight line whether or not strand is handled correctly, so a plausible-looking plot on good data proves very little.

---

## Task 6: Close out

- [ ] Run the full suite: `./backend/run-worktree-tests.sh tests/ -q`. Read the count, not the exit code.
- [ ] Commit as `feat(pipelines): compare an assembly to a reference as a synteny dot plot`. Keep the runner, handler, and frontend as separable commits.
- [ ] Push and open a PR against `main` with `Closes #149`. Label it `type:feature`, `area:backend`, `area:frontend`, `area:pipelines` — `.github/release.yml` categorizes by label, and an unlabelled PR lands under "Other changes".
- [ ] **Do not merge.** The end state is an open PR.
