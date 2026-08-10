# Assembly Graph (GFA) Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render an assembler's `.gfa` graph as an interactive node-and-edge diagram, so tangles and bubbles — the visual signature of unresolved repeats — are visible.

**Architecture:** The GFA parser already walks S and L lines and keeps only counts. It gains topology output (segments with lengths, links with orientation), capped by segment count with a partial flag. The frontend adds cytoscape.js and renders with its `cose` force-directed layout.

**Tech Stack:** Python 3 / pytest (backend); React 18 + TypeScript with **cytoscape.js** (new dependency — MIT, zero runtime dependencies, 137KB gzipped).

Implements [#150](https://github.com/syntheticgio/bioflow/issues/150). Spec: [`docs/superpowers/specs/2026-08-10-reference-visualizations-design.md`](../specs/2026-08-10-reference-visualizations-design.md).

---

## Background an engineer needs before starting

**What a GFA is.** A tab-separated assembly-graph format. Two line types matter here:

- `S <name> <sequence> [tags...]` — a segment (a contig). The sequence may be `*`, meaning "not stored"; the length then comes from an `LN:i:<n>` tag. Flye writes both.
- `L <from> <from_orient> <to> <to_orient> <overlap>` — a link between two segments. Orientations are `+` or `-` and say which strand/end joins, which is why they must be kept: a graph read without orientation misrepresents which end of a segment connects to which.

**What the visualization is for.** A resolved assembly is a few tidy paths. A repeat-riddled one has tangles ("hairballs") and bubbles. That shape tells the user the fix is long-read data, not another parameter sweep — which is actionable in a way `gfa_segment_count: 4213` is not.

**The two premises in issue #150 that are wrong.** Fix your mental model before starting:

1. The issue says the GFA is "ingested as an opaque blob" and asks for a parser. **A parser already exists** — `_parse_gfa` at `backend/app/storage/parsers.py:582`. It already handles S/L lines, the `LN:i:` tag, the `*` placeholder, a byte budget, and a `gfa_counts_partial` flag. Your job is to *retain the topology it already reads and throws away*.
2. The issue implies the frontend has no graph rendering. `WorkflowCanvas.tsx` draws an interactive node-edge SVG canvas in 1,090 lines — but its node positions are **user-placed and persisted** (`NodePosition`). An assembly graph arrives with no positions. *Computing a layout* is the new capability, and it is why this needs a library.

**Why a new dependency, given this repo's convention.** `SequenceCharts.tsx` says "the smallest chart dependency would outweigh the entire rest of the bundle," and the frontend runs on six runtime dependencies. Measured: cytoscape 3.34.0 is MIT, **zero runtime dependencies**, 435KB minified, **137KB gzipped**. The alternative was a hand-rolled force layout capped at a couple of thousand segments — which fails exactly on the large fragmented graphs where the hairball is the finding. **You must update that comment in the same commit** (Task 3, Step 4); this repo has been bitten before by a comment that silently went false.

**Testing.** Backend tests run via `./backend/run-worktree-tests.sh` from this worktree — never `docker compose exec api`, which silently tests `main`'s code. There is no frontend component-testing setup, so the viewer is verified in the browser against a real Flye graph.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/storage/parsers.py` | Modify `_parse_gfa` (line 582) to retain topology; add `MAX_GRAPH_SEGMENTS`. |
| `backend/tests/storage/test_parsers.py` | **New `TestGfaParsing` class** — there is currently zero GFA coverage. |
| `frontend/package.json` | Add `cytoscape` and `@types/cytoscape`. |
| `frontend/src/components/AssemblyGraph.tsx` | **New.** Owns cytoscape setup, styling, and lifecycle. |
| `frontend/src/components/DetailPanel.tsx` | Render `<AssemblyGraph>` when GFA topology facts are present. |
| `frontend/src/components/SequenceCharts.tsx` | Update the now-false no-dependency comment. |

---

## Task 1: Retain graph topology in the parser

**Files:**
- Modify: `backend/app/storage/parsers.py:582-637` (`_parse_gfa`)
- Test: `backend/tests/storage/test_parsers.py` (new class at end of file)

- [ ] **Step 1: Write the failing tests**

There is **no existing GFA test coverage** — add a new class at the end of `backend/tests/storage/test_parsers.py`. Match the file's existing style (`tmp_path`, `parsers.parse`, assert on facts).

```python
class TestGfaParsing:
    """Segment/link counts and graph topology.

    The counts predate this class; the topology is what the assembly-graph
    viewer draws. Both come from one pass over the file.
    """

    def test_counts_segments_and_links(self, tmp_path):
        p = tmp_path / "g.gfa"
        p.write_text(
            "S\ts1\tACGTACGT\n"
            "S\ts2\tACGT\n"
            "L\ts1\t+\ts2\t+\t0M\n"
        )
        facts = parsers.parse(p, FormatKind.GFA, Compression.NONE)
        assert facts["gfa_segment_count"] == 2
        assert facts["gfa_link_count"] == 1
        assert facts["gfa_total_length"] == 12

    def test_segments_carry_id_and_length(self, tmp_path):
        p = tmp_path / "g.gfa"
        p.write_text("S\ts1\tACGTACGT\nS\ts2\tACGT\n")
        facts = parsers.parse(p, FormatKind.GFA, Compression.NONE)
        assert facts["gfa_segments"] == [["s1", 8], ["s2", 4]]

    def test_length_comes_from_the_ln_tag_when_sequence_is_absent(self, tmp_path):
        """A GFA may carry `*` instead of the sequence. Reading that as a
        zero-length contig would make a valid graph look empty."""
        p = tmp_path / "g.gfa"
        p.write_text("S\ts1\t*\tLN:i:5000\n")
        facts = parsers.parse(p, FormatKind.GFA, Compression.NONE)
        assert facts["gfa_segments"] == [["s1", 5000]]
        assert facts["gfa_total_length"] == 5000

    def test_links_keep_both_orientations(self, tmp_path):
        """Orientation says which end of a segment joins which. A graph read
        without it misrepresents the topology it is drawn from."""
        p = tmp_path / "g.gfa"
        p.write_text(
            "S\ts1\tACGT\n"
            "S\ts2\tACGT\n"
            "L\ts1\t+\ts2\t-\t0M\n"
        )
        facts = parsers.parse(p, FormatKind.GFA, Compression.NONE)
        assert facts["gfa_links"] == [["s1", "+", "s2", "-"]]

    def test_a_bubble_keeps_all_four_links(self, tmp_path):
        # s1 -> {s2, s3} -> s4: the classic unresolved-haplotype shape.
        p = tmp_path / "g.gfa"
        p.write_text(
            "S\ts1\tACGT\nS\ts2\tACGT\nS\ts3\tACGT\nS\ts4\tACGT\n"
            "L\ts1\t+\ts2\t+\t0M\n"
            "L\ts1\t+\ts3\t+\t0M\n"
            "L\ts2\t+\ts4\t+\t0M\n"
            "L\ts3\t+\ts4\t+\t0M\n"
        )
        facts = parsers.parse(p, FormatKind.GFA, Compression.NONE)
        assert facts["gfa_link_count"] == 4
        assert len(facts["gfa_links"]) == 4

    def test_oversized_graph_keeps_counts_but_drops_topology(self, tmp_path, monkeypatch):
        """The counts stay exact -- they are cheap and already correct, and a
        graph too large to draw is still a graph worth counting."""
        monkeypatch.setattr(parsers, "MAX_GRAPH_SEGMENTS", 3)
        p = tmp_path / "g.gfa"
        p.write_text("".join(f"S\ts{i}\tACGT\n" for i in range(10)))
        facts = parsers.parse(p, FormatKind.GFA, Compression.NONE)
        assert facts["gfa_segment_count"] == 10
        assert facts["gfa_topology_partial"] is True
        assert "gfa_segments" not in facts
        assert "gfa_links" not in facts

    def test_graph_within_the_cap_is_not_flagged_partial(self, tmp_path):
        p = tmp_path / "g.gfa"
        p.write_text("S\ts1\tACGT\n")
        facts = parsers.parse(p, FormatKind.GFA, Compression.NONE)
        assert "gfa_topology_partial" not in facts

    def test_empty_graph_has_no_facts(self, tmp_path):
        p = tmp_path / "g.gfa"
        p.write_text("")
        facts = parsers.parse(p, FormatKind.GFA, Compression.NONE)
        assert "gfa_segment_count" not in facts
        assert "gfa_segments" not in facts
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/storage/test_parsers.py::TestGfaParsing -v
```

Expected: the count tests PASS (that behaviour exists), the topology tests FAIL with `KeyError: 'gfa_segments'`, and `test_oversized_graph_keeps_counts_but_drops_topology` FAILS on `monkeypatch.setattr` with `AttributeError` because `MAX_GRAPH_SEGMENTS` does not exist yet.

- [ ] **Step 3: Implement topology retention**

Add the constant beside `MAX_STORED_CONTIGS` (line 29) in `backend/app/storage/parsers.py`:

```python
# Above this many segments a graph is not drawable -- a force-directed layout
# of it is a hairball whatever the renderer, and the fact document would carry
# megabytes nothing reads. The counts are still exact above the cap; only the
# topology is dropped.
MAX_GRAPH_SEGMENTS = 5000
```

Replace `_parse_gfa` (lines 582-637) with:

```python
def _parse_gfa(path: Path, compression: Compression, cancel) -> dict:
    """Segment and link counts, plus the topology the graph viewer draws.

    The two numbers that say what the graph is: how many pieces, and how
    tangled. A graph with as many links as segments is a resolved assembly; one
    with far more is where the contigs came from a repeat-riddled region.

    Segment lengths come from the `LN:i:` tag when present and the sequence
    field otherwise -- Flye writes both, but a GFA is allowed to carry `*` in
    place of the sequence, and reading that as a zero-length contig would make
    a valid graph look empty.

    Topology is kept only up to `MAX_GRAPH_SEGMENTS`; past that the counts
    remain exact and `gfa_topology_partial` says the node and edge lists were
    dropped. Link orientation is retained because it says which end of a
    segment joins which, and a graph drawn without it is a different graph.
    """
    facts: dict = {}
    segments = 0
    links = 0
    total_length = 0
    read_bytes = 0
    truncated = False
    # Same budget and reasoning as the FASTA path: a graph for a fragmented
    # draft is large, and the counts are worth more than exactness on a file
    # nobody will read to the end.
    limit = 256 * 1024 * 1024

    segment_list: list[list] = []
    link_list: list[list] = []
    over_cap = False

    with _open_text(path, compression) as fh:
        for i, line in enumerate(fh):
            read_bytes += len(line)
            cols = line.rstrip("\n").split("\t")
            if cols[0] == "S":
                segments += 1
                length = None
                for col in cols[3:]:
                    if col.startswith("LN:i:"):
                        try:
                            length = int(col[5:])
                        except ValueError:
                            length = None
                        break
                if length is None and len(cols) > 2 and cols[2] != "*":
                    length = len(cols[2])
                total_length += length or 0
                if segments > MAX_GRAPH_SEGMENTS:
                    # Stop accumulating, but keep counting: the counts are the
                    # facts that survive for an undrawable graph.
                    over_cap = True
                    segment_list = []
                    link_list = []
                elif not over_cap and len(cols) > 1:
                    segment_list.append([cols[1], length or 0])
            elif cols[0] == "L":
                links += 1
                if not over_cap and len(cols) >= 5:
                    link_list.append([cols[1], cols[2], cols[3], cols[4]])
            if i % 5000 == 0:
                _check(cancel)
            if compression is Compression.NONE and read_bytes > limit:
                truncated = True
                break

    if segments:
        facts["gfa_segment_count"] = segments
        facts["gfa_link_count"] = links
        if total_length:
            facts["gfa_total_length"] = total_length
        if truncated:
            facts["gfa_counts_partial"] = True
        if over_cap:
            facts["gfa_topology_partial"] = True
        else:
            facts["gfa_segments"] = segment_list
            facts["gfa_links"] = link_list
    return facts
```

Note the ordering subtlety: `over_cap` is set when the segment count *exceeds* the cap, and both lists are cleared at that moment so a graph that crosses the cap does not leave a truncated prefix behind. Links seen before the cap was crossed are discarded with them — a partial edge list over a partial node list would draw a graph that does not exist.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/storage/test_parsers.py::TestGfaParsing -v
```

Expected: all 8 PASS.

- [ ] **Step 5: Run the full storage suite**

```bash
./backend/run-worktree-tests.sh tests/storage/ -q
```

Expected: all pass. Read the count.

- [ ] **Step 6: Commit**

```bash
git add backend/app/storage/parsers.py backend/tests/storage/test_parsers.py
git commit -m "feat(backend): retain assembly graph topology, not just counts

_parse_gfa already walked S and L lines and kept only the counts. It now
also emits gfa_segments and gfa_links, which is what the graph viewer
draws. Link orientation is kept: it says which end of a segment joins
which, and a graph drawn without it is a different graph.

Capped at MAX_GRAPH_SEGMENTS, past which the counts stay exact and
gfa_topology_partial says the lists were dropped. Both lists are cleared
together when the cap is crossed -- a partial edge list over a partial
node list would draw a graph that does not exist.

Adds the first GFA parser tests; there were none."
```

---

## Task 2: Add cytoscape.js

**Files:**
- Modify: `frontend/package.json`

- [ ] **Step 1: Install**

```bash
cd frontend && npm install cytoscape && npm install --save-dev @types/cytoscape
```

- [ ] **Step 2: Verify what landed**

```bash
cd frontend && npm ls cytoscape && node -e "console.log(require('cytoscape/package.json').license)"
```

Expected: a single `cytoscape@3.x` with no nested dependency tree, and `MIT`. If npm reports transitive dependencies, stop — the spec's justification rests on there being none, and something has changed upstream since it was written.

- [ ] **Step 3: Commit**

```bash
git add frontend/package.json frontend/package-lock.json
git commit -m "chore(frontend): add cytoscape for assembly graph layout

MIT, zero runtime dependencies, 137KB gzipped. Taken deliberately against
this frontend's no-chart-library convention: the alternative was a
hand-rolled force layout capped at a few thousand segments, which fails
exactly on the fragmented graphs where the hairball is the finding."
```

---

## Task 3: The AssemblyGraph component

**Files:**
- Create: `frontend/src/components/AssemblyGraph.tsx`
- Modify: `frontend/src/components/SequenceCharts.tsx:3-9` (the module docstring)

- [ ] **Step 1: Write the component**

Create `frontend/src/components/AssemblyGraph.tsx`:

```tsx
/**
 * The assembler's raw assembly graph.
 *
 * Nodes are segments (contigs), edges are the links between them. The shape
 * is the point: a resolved assembly is a few tidy paths, while tangles and
 * bubbles mark repeats the assembler could not resolve. That tells the user
 * the fix is long-read data rather than another parameter sweep.
 *
 * cytoscape rather than hand-rolled SVG, unlike every other chart here. The
 * others draw fixed shapes from data that is already positioned; this one has
 * to *compute* a layout, which `WorkflowCanvas` never does -- its node
 * positions are user-placed and stored.
 */

import { useEffect, useRef } from "react";
import cytoscape from "cytoscape";

interface Props {
  /** `[id, length]` per segment, from `gfa_segments`. */
  segments: [string, number][];
  /** `[from, fromOrient, to, toOrient]`, from `gfa_links`. */
  links: [string, string, string, string][];
}

/** Node diameter in px, scaled by segment length against the largest one. */
function radiusFor(length: number, max: number): number {
  if (max <= 0) return 12;
  // Square-root scaling so area, not diameter, tracks length -- a 10x longer
  // contig drawn 10x wider swamps the canvas and hides the topology.
  return 8 + Math.sqrt(length / max) * 34;
}

export function AssemblyGraph({ segments, links }: Props) {
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!box.current || segments.length === 0) return;

    const maxLen = Math.max(...segments.map(([, length]) => length), 1);
    const known = new Set(segments.map(([id]) => id));

    const cy = cytoscape({
      container: box.current,
      elements: [
        ...segments.map(([id, length]) => ({
          data: { id, length, size: radiusFor(length, maxLen) },
        })),
        // A link naming a segment outside the node set would make cytoscape
        // throw and take the whole panel down. Skipping is right rather than
        // defensive: past the topology cap the lists are dropped together,
        // so a dangling reference means a malformed file, and one bad edge
        // should not cost the user the other 4,000 good ones.
        ...links
          .filter(([from, , to]) => known.has(from) && known.has(to))
          .map(([from, fo, to, to_o], i) => ({
            data: {
              id: `e${i}`,
              source: from,
              target: to,
              orient: `${fo}/${to_o}`,
            },
          })),
      ],
      style: [
        {
          selector: "node",
          style: {
            "background-color": "#2e7d32",
            width: "data(size)",
            height: "data(size)",
            label: "data(id)",
            "font-size": 8,
            color: "var(--text-faint)",
            "text-valign": "center",
            "text-halign": "center",
            "min-zoomed-font-size": 8,
          },
        },
        {
          selector: "edge",
          style: {
            width: 1.5,
            "line-color": "#888",
            "curve-style": "bezier",
          },
        },
      ],
      layout: {
        name: "cose",
        // Bounded rather than run-to-convergence: a few thousand segments
        // will otherwise pin a tab for many seconds, and the shape a reader
        // needs is legible well before the layout settles.
        numIter: 400,
        animate: false,
      },
      // Fit on load, then let the user drive.
      minZoom: 0.1,
      maxZoom: 4,
    });

    return () => cy.destroy();
  }, [segments, links]);

  if (segments.length === 0) return null;

  return (
    <div style={{ marginTop: 12 }}>
      <div style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 6 }}>
        Assembly graph · {segments.length.toLocaleString()} segments ·{" "}
        {links.length.toLocaleString()} links · drag to pan, scroll to zoom
      </div>
      <div
        ref={box}
        style={{
          width: "100%",
          height: 380,
          border: "1px solid var(--border)",
          borderRadius: 4,
        }}
      />
    </div>
  );
}
```

- [ ] **Step 2: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors. If cytoscape's style types reject the `var(--text-faint)` colour string, replace that one value with a literal `"#888"` — cytoscape renders to canvas, not the DOM, so CSS custom properties do not resolve there.

- [ ] **Step 3: Update the now-false comment in SequenceCharts.tsx**

`frontend/src/components/SequenceCharts.tsx` lines 3-9 currently read:

```tsx
/**
 * Base-composition pie and per-position quality curve.
 *
 * Hand-rolled SVG rather than a charting library: these are two fixed,
 * simple shapes, and the smallest chart dependency would outweigh the entire
 * rest of the bundle.
 */
```

Replace with:

```tsx
/**
 * Base-composition pie and per-position quality curve.
 *
 * Hand-rolled SVG rather than a charting library: these are two fixed,
 * simple shapes, and a charting dependency to draw them would outweigh what
 * it replaced.
 *
 * That reasoning covers every chart here except `AssemblyGraph`, which needs
 * a *computed* graph layout rather than a fixed shape and takes cytoscape for
 * it. The line is whether the layout is already known: it is for a pie, a
 * curve, and a stacked bar, and it is not for a few thousand assembly-graph
 * segments with no positions.
 */
```

This is not optional tidying. Left alone the old comment is false the moment cytoscape lands, and this repo has already paid for that: `ToolMeta.runnable`'s comment cited cutadapt and Trimmomatic as undispatched years after `trim_reads` grew its three-way dispatch, and nothing caught it, because a comment cannot fail.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/AssemblyGraph.tsx frontend/src/components/SequenceCharts.tsx
git commit -m "feat(frontend): add assembly graph viewer component

Node area scales with segment length (square-root, so a 10x longer contig
does not swamp the canvas). Layout is capped at 400 cose iterations: the
shape a reader needs is legible well before convergence, and running to
settle pins the tab on a few thousand segments.

Updates SequenceCharts' no-dependency comment in the same commit rather
than leaving it to go quietly false."
```

---

## Task 4: Render the graph in the object detail panel

**Files:**
- Modify: `frontend/src/components/DetailPanel.tsx` (import near line 23; render in the non-reference branch near line 1170)

- [ ] **Step 1: Import**

Beside the existing `import { AssemblyFacts } from "./AssemblyFacts";` at line 23:

```tsx
import { AssemblyGraph } from "./AssemblyGraph";
```

- [ ] **Step 2: Read the facts and render**

A GFA is its own object with `role` other than `reference`, so it takes the **else** branch of the `isReference` ternary at roughly line 1167. Inside that branch's `<div className="facts-columns">`, before the existing `{hasFacts && <FactsTable facts={obj.facts} columns />}`, add:

```tsx
          {/* An assembly graph object. Rendered above the fact table because
              the shape is the finding and the counts merely quantify it.
              Absent for a graph past the topology cap, where the parser keeps
              gfa_topology_partial and the counts alone. */}
          {Array.isArray(obj.facts.gfa_segments) &&
            Array.isArray(obj.facts.gfa_links) && (
              <AssemblyGraph
                segments={obj.facts.gfa_segments as [string, number][]}
                links={obj.facts.gfa_links as [string, string, string, string][]}
              />
            )}
```

Confirm the branch structure before editing — the ternary and its `facts-columns` div should be where the earlier read left them:

```bash
grep -n "isReference ? (\|facts-columns\|FactsTable facts={obj.facts} columns" frontend/src/components/DetailPanel.tsx | head
```

- [ ] **Step 3: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Verify in the browser against a real graph**

A hand-written 4-segment fixture will not tell you whether this works — the question is whether a real Flye graph is legible. Start this worktree's stack:

```bash
./ops/worktree-up.sh
```

Find an existing GFA object, or produce one by running an assembly:

```bash
docker compose -p bioflow-worktree exec api python -c "
import asyncio
from app.db.client import connect_to_mongo, get_db
async def main():
    await connect_to_mongo()
    db = get_db()
    async for o in db.data_objects.find({'facts.gfa_segment_count': {'\$exists': True}}).limit(10):
        f = o['facts']
        print(o['_id'], o.get('name'), f.get('gfa_segment_count'), 'topology:', 'gfa_segments' in f)
asyncio.run(main())
"
```

**Note:** objects ingested before Task 1 have counts but no topology — their facts were written by the old parser. Re-ingest one, or run a fresh assembly, to get a graph with `gfa_segments`.

Open http://localhost:5273, select the GFA object, and check:

1. The graph renders and settles within a couple of seconds.
2. Pan (drag) and zoom (scroll) work.
3. Node sizes visibly differ, tracking segment length.
4. A graph past the cap shows the fact table with `gfa_topology_partial` and no canvas — not a broken or empty box.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/DetailPanel.tsx
git commit -m "feat(frontend): show the assembly graph on a GFA object

Above the fact table: the shape is the finding, and the segment and link
counts only quantify it."
```

---

## Task 5: Close out the issue

- [ ] **Step 1: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: all pass. Read the printed count. Exit code 137 is the host running out of memory from concurrent stacks, not a test failure.

- [ ] **Step 2: Build the frontend to confirm the new dependency bundles**

```bash
cd frontend && npm run build
```

Expected: a clean build. Note the reported bundle size — cytoscape should add roughly 137KB gzipped. A much larger jump means it bundled something unexpected and is worth investigating before merge.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --title "feat(frontend): add assembly graph (GFA) viewer" --body "$(cat <<'EOF'
Renders the assembler's raw graph as an interactive node-and-edge diagram,
from the #146 visualization epic.

Two things about #150's framing turned out to be wrong, and both made the
work different:

- The GFA was never an opaque blob. `_parse_gfa` already walked S and L
  lines and kept only the counts, so the backend change is retaining the
  topology it already read, not writing a parser.
- The frontend does draw graphs (`WorkflowCanvas`), but has never *laid one
  out* — its positions are user-placed and stored. Computing a layout is
  the new capability, and it is why this takes a dependency.

Adds cytoscape.js: MIT, zero runtime dependencies, 137KB gzipped. This is a
deliberate departure from the no-chart-library convention stated in
`SequenceCharts.tsx`, and that comment is updated here rather than left to
go quietly false. The alternative was a hand-rolled force layout capped at
a few thousand segments, which fails exactly on the fragmented graphs where
the hairball is the finding.

Topology is capped at 5,000 segments; past that the counts stay exact and
`gfa_topology_partial` records that the lists were dropped.

Adds the first GFA parser tests — there were none. Verified in the browser
against a real Flye graph at localhost:5273.

Closes #150
EOF
)"
```

Label the PR `type:feature`, `area:backend`, `area:frontend`.

- [ ] **Step 4: Report the PR URL and stop**

Do not merge. The user reviews and merges.
