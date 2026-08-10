# Nx/NGx Contiguity Curve Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an Nx/NGx contiguity curve to the Reference Quality tab, showing contig length (log Y) against percentage of the assembly (X).

**Architecture:** The FASTA parser already computes N50/N90/L50/auN from a full uncapped list of contig lengths and then discards that list. This adds one more output to the same function — a 100-point downsampled curve stored as `sequence_nx_curve` — and a hand-rolled SVG component that renders it. NGx is derived on the frontend from the same curve plus `assembly_genome_size`, which reaches the frontend already because assembly provenance is merged into the facts dict.

**Tech Stack:** Python 3 / pytest (backend), React 18 + TypeScript, hand-rolled SVG (frontend). No new dependencies.

Implements [#147](https://github.com/syntheticgio/bioflow/issues/147). Spec: [`docs/superpowers/specs/2026-08-10-reference-visualizations-design.md`](../specs/2026-08-10-reference-visualizations-design.md).

---

## Background an engineer needs before starting

**The curve, defined precisely.** Sort contig lengths descending. For x in 1..100, walk the sorted list accumulating length; the Nx value at x is the length of the contig at which the running total first reaches or exceeds x% of the assembly's total length. N50 is exactly the x=50 point, which is why this generalizes code that already exists.

**Why 100 points and not the raw list.** A fragmented draft assembly can have hundreds of thousands of contigs. These facts are stored in MongoDB documents, which are capped at 16MB. A hundred points is a fixed, small cost whether the assembly has 8 contigs or 500,000.

**The truncation rule you must not break.** `_parse_fasta` has two branches: a complete parse and a truncated one (the file exceeded a byte budget). Contiguity facts are emitted **only** on the complete branch, and `test_truncated_parse_omits_contiguity_entirely` enforces it. The reason is in the code comment: a curve from the first 256MB of a large draft is not an approximation of the real curve, it is a different curve over a biased population — the file's leading records, not its longest. Because you will add the curve *inside* `_contiguity_stats`, which is only called on the complete branch, this is inherited for free. Do not add a separate emission point elsewhere.

**Where genome size lives, and why NGx is conditional.** BioFlow has no genome-size estimate for an arbitrary FASTA. It exists only as an assembly *parameter*, written into provenance by `assembly_provenance()` (`backend/app/queue/results.py:1316`) and merged into the object's facts at `results.py:1354` — so the frontend reads `facts.assembly_genome_size` exactly like any other fact. An assembly BioFlow produced from reads with a size supplied has one; an uploaded FASTA or NCBI download does not.

**How this repo tests.** Backend tests run in a container. **From this worktree you must use `./backend/run-worktree-tests.sh`** — a bare `docker compose exec api python -m pytest` would silently test `main`'s code instead of yours, because the `api` container bind-mounts the main checkout. There is no frontend component-testing setup (no jsdom, zero `.test.tsx`), so frontend verification is manual in the browser.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/storage/parsers.py` | Modify `_contiguity_stats` (line 427) to also emit `sequence_nx_curve`. |
| `backend/tests/storage/test_parsers.py` | Add cases to the existing `TestFastaContiguity` class (line 440). |
| `frontend/src/components/NxChart.tsx` | **New.** Pure presentational SVG chart. Takes the curve and an optional genome size; owns axis scaling and the NGx derivation. |
| `frontend/src/components/AssemblyFacts.tsx` | Read the new fact, render `<NxChart>` below the existing contiguity `<dl>`. |

`NxChart` is its own file rather than more lines in `AssemblyFacts.tsx`, which is already 855 lines — the same call `BuscoChart.tsx` represents.

---

## Task 1: Emit the Nx curve from the parser

**Files:**
- Modify: `backend/app/storage/parsers.py:427-461` (`_contiguity_stats`)
- Test: `backend/tests/storage/test_parsers.py` (add to `TestFastaContiguity`, line 440)

- [ ] **Step 1: Write the failing tests**

Add these to the **existing** `TestFastaContiguity` class in `backend/tests/storage/test_parsers.py`. Append them after `test_empty_file_has_no_contiguity_facts` (the last method in the class). Keep the class's existing style: build a FASTA with `tmp_path`, call `parsers.parse`, assert on facts.

```python
    def test_nx_curve_has_one_hundred_points(self, tmp_path):
        p = tmp_path / "ref.fasta"
        p.write_text(
            ">a\n" + "A" * 100 + "\n"
            ">b\n" + "A" * 80 + "\n"
            ">c\n" + "A" * 60 + "\n"
            ">d\n" + "A" * 40 + "\n"
            ">e\n" + "A" * 20 + "\n"
        )
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        curve = facts["sequence_nx_curve"]
        assert len(curve) == 100
        assert curve[0][0] == 1
        assert curve[-1][0] == 100

    def test_nx_curve_at_fifty_equals_n50(self, tmp_path):
        """The curve generalizes N50 rather than recomputing it differently:
        the x=50 point and sequence_n50 must never disagree."""
        p = tmp_path / "ref.fasta"
        p.write_text(
            ">a\n" + "A" * 100 + "\n"
            ">b\n" + "A" * 80 + "\n"
            ">c\n" + "A" * 60 + "\n"
            ">d\n" + "A" * 40 + "\n"
            ">e\n" + "A" * 20 + "\n"
        )
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        at_fifty = dict(facts["sequence_nx_curve"])[50]
        assert at_fifty == facts["sequence_n50"] == 80

    def test_nx_curve_at_ninety_equals_n90(self, tmp_path):
        p = tmp_path / "ref.fasta"
        p.write_text(
            ">a\n" + "A" * 100 + "\n"
            ">b\n" + "A" * 80 + "\n"
            ">c\n" + "A" * 60 + "\n"
            ">d\n" + "A" * 40 + "\n"
            ">e\n" + "A" * 20 + "\n"
        )
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        at_ninety = dict(facts["sequence_nx_curve"])[90]
        assert at_ninety == facts["sequence_n90"] == 40

    def test_single_contig_curve_is_flat(self, tmp_path):
        p = tmp_path / "one.fasta"
        p.write_text(">only\n" + "A" * 500 + "\n")
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        assert {length for _, length in facts["sequence_nx_curve"]} == {500}

    def test_uniform_contigs_give_a_flat_curve(self, tmp_path):
        p = tmp_path / "ref.fasta"
        p.write_text("".join(f">c{i}\n" + "A" * 50 + "\n" for i in range(4)))
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        assert {length for _, length in facts["sequence_nx_curve"]} == {50}

    def test_dominant_contig_curve_drops_sharply(self, tmp_path):
        """One 900bp contig and ten 10bp ones: the curve holds 900 until the
        big contig's own share of the total is exhausted, then falls to 10.
        This shape is the whole point of the visualization."""
        p = tmp_path / "ref.fasta"
        p.write_text(
            ">big\n" + "A" * 900 + "\n"
            + "".join(f">s{i}\n" + "A" * 10 + "\n" for i in range(10))
        )
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        curve = dict(facts["sequence_nx_curve"])
        assert curve[50] == 900
        assert curve[90] == 900
        assert curve[100] == 10

    def test_curve_is_monotonically_non_increasing(self, tmp_path):
        p = tmp_path / "ref.fasta"
        p.write_text(
            "".join(f">c{i}\n" + "A" * (100 - i * 7) + "\n" for i in range(12))
        )
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        lengths = [length for _, length in facts["sequence_nx_curve"]]
        assert lengths == sorted(lengths, reverse=True)

    def test_empty_file_has_no_nx_curve(self, tmp_path):
        p = tmp_path / "empty.fasta"
        p.write_text("")
        facts = parsers.parse(p, FormatKind.FASTA, Compression.NONE)
        assert "sequence_nx_curve" not in facts
```

Now extend the **existing** truncation test so the curve is covered by the guard that already protects N50. Find `test_truncated_parse_omits_contiguity_entirely` (line 526) and add `"sequence_nx_curve"` to its tuple of keys:

```python
        for key in (
            "sequence_n50",
            "sequence_n90",
            "sequence_l50",
            "sequence_auN",
            "sequence_gap_count",
            "sequence_gap_bases",
            "sequence_nx_curve",
        ):
            assert key not in facts
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/storage/test_parsers.py::TestFastaContiguity -v
```

Expected: the eight new tests FAIL with `KeyError: 'sequence_nx_curve'` (and the truncation test still passes, since an absent key trivially satisfies `not in`). The other pre-existing tests in the class must still pass.

- [ ] **Step 3: Implement the curve**

In `backend/app/storage/parsers.py`, replace the body of `_contiguity_stats` (currently lines 427-461) with the version below. The N50/N90/L50/auN logic is unchanged — you are adding the `NX_CURVE_POINTS` constant, the `sequence_nx_curve` block, and the docstring paragraph.

Add the constant near the top of the module, beside `MAX_STORED_CONTIGS` (line 29):

```python
# The Nx curve is stored at fixed resolution rather than as raw contig
# lengths: a fragmented draft is hundreds of thousands of contigs, fact
# documents live in Mongo, and Mongo caps a document at 16MB. A hundred
# points costs the same for an 8-contig finished genome and a 500,000-contig
# draft, and it is exactly what the chart draws.
NX_CURVE_POINTS = 100
```

Then the function:

```python
def _contiguity_stats(lengths: list[int], gap_bases: int, gap_count: int) -> dict:
    """N50/N90/L50/auN, the Nx curve, and gap counts, over every record's
    true length.

    Genuinely absent from anywhere else now that `assembly_runner._n50` is
    gone: Flye's own table gave an N50 for its own output, and nothing gave
    one for an uploaded assembly. This is the one contiguity fact set that
    applies to any FASTA regardless of what produced it.

    Takes the full length list rather than the capped `sequence_lengths`
    dict, deliberately -- N50 over 50 stored contigs of a 40,000-contig draft
    is not an approximate N50, it is a different number computed from the
    wrong population.

    `sequence_nx_curve` is the continuous form of the same walk: N50 is its
    x=50 point and N90 its x=90 point, computed here in one pass so the three
    can never disagree the way two separate implementations eventually would.
    """
    if not lengths:
        return {}
    ordered = sorted(lengths, reverse=True)
    total = sum(ordered)
    facts: dict = {}
    for label, fraction in (("n50", 0.5), ("n90", 0.9)):
        threshold = total * fraction
        running = 0
        for i, length in enumerate(ordered):
            running += length
            if running >= threshold:
                facts[f"sequence_{label}"] = length
                if label == "n50":
                    facts["sequence_l50"] = i + 1
                break

    # One pass for all 100 thresholds: walk the sorted lengths once, and
    # every time the running total crosses the next percentage boundary,
    # record the contig that carried it there. A per-point rescan would be
    # 100 walks of a list that can hold half a million entries.
    curve: list[list[int]] = []
    running = 0
    x = 1
    for length in ordered:
        running += length
        while x <= NX_CURVE_POINTS and running >= total * x / NX_CURVE_POINTS:
            curve.append([x, length])
            x += 1
        if x > NX_CURVE_POINTS:
            break
    # Floating-point comparison can leave the final point unreached when the
    # running total lands a hair under the threshold it should have met
    # exactly. The last contig is the answer for any remaining x by
    # definition, so fill rather than emit a short curve.
    while x <= NX_CURVE_POINTS:
        curve.append([x, ordered[-1]])
        x += 1
    facts["sequence_nx_curve"] = curve

    # auN: the area under the Nx curve, treating each base as weighted by the
    # length of the contig it sits in. Unlike N50 it does not jump
    # discontinuously when one contig crosses the halfway point, which is why
    # two assemblies can share an N50 and still differ here.
    facts["sequence_auN"] = round(sum(length * length for length in ordered) / total, 1)
    facts["sequence_gap_count"] = gap_count
    facts["sequence_gap_bases"] = gap_bases
    return facts
```

Points are `[x, length]` lists rather than tuples because these facts are serialized to BSON and back; a tuple would round-trip as a list anyway, and writing it as a list keeps the Python and the stored shape identical.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/storage/test_parsers.py::TestFastaContiguity -v
```

Expected: all PASS, including the pre-existing N50/N90/auN/gap tests.

- [ ] **Step 5: Run the full storage suite for regressions**

```bash
./backend/run-worktree-tests.sh tests/storage/ -q
```

Expected: all pass. Read the count, not just the exit code.

- [ ] **Step 6: Commit**

```bash
git add backend/app/storage/parsers.py backend/tests/storage/test_parsers.py
git commit -m "feat(backend): store the Nx contiguity curve alongside N50

A hundred (percent, length) points computed in the same walk that already
produces N50 and N90, so the three cannot disagree. Downsampled rather
than raw: a fragmented draft is hundreds of thousands of contigs against
Mongo's 16MB document cap.

Emitted from inside _contiguity_stats, so it inherits the existing rule
that a truncated parse gets no contiguity facts at all -- a curve over a
file's leading records is a different curve, not an approximate one."
```

---

## Task 2: The NxChart component

**Files:**
- Create: `frontend/src/components/NxChart.tsx`

This task creates the component in isolation; Task 3 wires it in. There is no component-testing setup in this repo, so correctness here is verified in the browser in Task 3.

- [ ] **Step 1: Write the component**

Create `frontend/src/components/NxChart.tsx`:

```tsx
/**
 * Nx and NGx contiguity curves.
 *
 * Hand-rolled SVG for the same reason the other charts here are: this is a
 * fixed, simple shape, and the smallest charting dependency would outweigh
 * the entire rest of the bundle.
 *
 * The Y axis is logarithmic. On a linear axis every real assembly renders as
 * a cliff pinned against the axis -- contig lengths in an assembly span
 * several orders of magnitude, which is exactly what the reader needs to see.
 */

interface Props {
  /** [percent, length] pairs at x = 1..100, from `sequence_nx_curve`. */
  curve: [number, number][];
  /**
   * The assembly's own total length, from `total_bases`.
   *
   * Passed in rather than derived: the curve holds one length per
   * percentile, which is not enough to recover the sum, and NGx needs the
   * real total to scale against expected genome size.
   */
  totalBases: number;
  /** Expected genome size, when known. Enables the NGx curve. */
  genomeSize?: number;
}

const W = 320;
const H = 180;
const PAD_L = 46;
const PAD_R = 10;
const PAD_T = 12;
const PAD_B = 30;
const PLOT_W = W - PAD_L - PAD_R;
const PLOT_H = H - PAD_T - PAD_B;

const NX_COLOR = "#2e7d32";
const NGX_COLOR = "#f9a825";

/** Compact base-count label for axis ticks: 4500000 -> "4.5 Mb". */
function tick(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} Gb`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} Mb`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} kb`;
  return `${n} b`;
}

/**
 * NGx: the same walk as Nx, but against expected genome size instead of the
 * assembly's own total.
 *
 * The curve deliberately stops early when the assembly is shorter than the
 * genome is expected to be -- the cumulative length never reaches 100% of
 * that size, and a curve that ends at x=78 is the visualization saying 22%
 * of the expected genome is not in this file. Extending it to the axis would
 * erase the finding.
 */
function ngxPoints(
  curve: [number, number][],
  assemblyTotal: number,
  genomeSize: number,
): [number, number][] {
  const scale = assemblyTotal / genomeSize;
  const out: [number, number][] = [];
  let lastX = 0;
  for (const [x, length] of curve) {
    const gx = Math.round(x * scale);
    // Rounding collapses several source points onto one x when the assembly
    // is much shorter than the genome. Keeping only the first of each run
    // preserves a strictly increasing path; without this the line doubles
    // back on itself and renders as a scribble.
    if (gx >= 1 && gx <= 100 && gx > lastX) {
      out.push([gx, length]);
      lastX = gx;
    }
  }
  return out;
}

export function NxChart({ curve, totalBases, genomeSize }: Props) {
  if (!curve || curve.length === 0) return null;

  const lengths = curve.map(([, length]) => length).filter((n) => n > 0);
  if (lengths.length === 0) return null;

  const maxLen = Math.max(...lengths);
  const minLen = Math.min(...lengths);
  // Guard a degenerate log domain: a uniform assembly has max === min.
  const hi = Math.log10(maxLen);
  const lo = Math.log10(Math.max(1, minLen));
  const span = hi - lo || 1;

  const px = (x: number) => PAD_L + (x / 100) * PLOT_W;
  const py = (length: number) =>
    PAD_T + PLOT_H - ((Math.log10(Math.max(1, length)) - lo) / span) * PLOT_H;

  const path = (pts: [number, number][]) =>
    pts.map(([x, l], i) => `${i === 0 ? "M" : "L"}${px(x)},${py(l)}`).join(" ");

  const ngx =
    genomeSize !== undefined && genomeSize > 0 && totalBases > 0
      ? ngxPoints(curve, totalBases, genomeSize)
      : [];

  const aria =
    `Contiguity curve: N50 ${tick(
      curve.find(([x]) => x === 50)?.[1] ?? maxLen,
    )}, longest ${tick(maxLen)}, shortest ${tick(minLen)}` +
    (ngx.length > 0 ? ", with NGx against expected genome size" : "");

  return (
    <div style={{ marginTop: 10 }}>
      <svg width={W} height={H} role="img" aria-label={aria}>
        {/* axes */}
        <line
          x1={PAD_L}
          y1={PAD_T + PLOT_H}
          x2={PAD_L + PLOT_W}
          y2={PAD_T + PLOT_H}
          stroke="var(--border)"
        />
        <line
          x1={PAD_L}
          y1={PAD_T}
          x2={PAD_L}
          y2={PAD_T + PLOT_H}
          stroke="var(--border)"
        />
        {/* Y ticks at the log extremes and midpoint */}
        {[maxLen, Math.round(10 ** ((hi + lo) / 2)), minLen].map((v, i) => (
          <g key={i}>
            <text
              x={PAD_L - 4}
              y={py(v) + 3}
              fontSize={9}
              textAnchor="end"
              fill="var(--text-faint)"
            >
              {tick(v)}
            </text>
          </g>
        ))}
        {/* X ticks */}
        {[0, 25, 50, 75, 100].map((x) => (
          <text
            key={x}
            x={px(x)}
            y={PAD_T + PLOT_H + 12}
            fontSize={9}
            textAnchor="middle"
            fill="var(--text-faint)"
          >
            {x}
          </text>
        ))}
        <text
          x={PAD_L + PLOT_W / 2}
          y={H - 2}
          fontSize={9}
          textAnchor="middle"
          fill="var(--text-faint)"
        >
          % of assembly
        </text>
        {/* curves */}
        <path d={path(curve)} fill="none" stroke={NX_COLOR} strokeWidth={1.75} />
        {ngx.length > 0 && (
          <path
            d={path(ngx)}
            fill="none"
            stroke={NGX_COLOR}
            strokeWidth={1.75}
            strokeDasharray="5 3"
          />
        )}
        {/* legend, only meaningful when there are two lines to tell apart */}
        {ngx.length > 0 && (
          <g>
            <rect x={PAD_L + 6} y={PAD_T + 2} width={9} height={3} fill={NX_COLOR} />
            <text x={PAD_L + 19} y={PAD_T + 6} fontSize={9} fill="var(--text-faint)">
              Nx
            </text>
            <rect x={PAD_L + 44} y={PAD_T + 2} width={9} height={3} fill={NGX_COLOR} />
            <text x={PAD_L + 57} y={PAD_T + 6} fontSize={9} fill="var(--text-faint)">
              NGx
            </text>
          </g>
        )}
      </svg>
    </div>
  );
}
```

- [ ] **Step 2: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/NxChart.tsx
git commit -m "feat(frontend): add Nx/NGx contiguity curve component

Log Y axis: contig lengths span orders of magnitude, and on a linear axis
every real assembly is a cliff against the axis.

NGx is drawn on the same chart rather than behind a toggle -- the gap
between the two curves is the diagnostic. It ends early when the assembly
is shorter than the genome should be, which is the missing-sequence
signal and is deliberately not clamped to the axis."
```

---

## Task 3: Wire the chart into the Reference Quality tab

**Files:**
- Modify: `frontend/src/components/AssemblyFacts.tsx` (import at line 5; fact reads near line 85; render near line 300)

- [ ] **Step 1: Import the component**

`AssemblyFacts.tsx` line 5 already imports `BuscoChart`. Add below it:

```tsx
import { NxChart } from "./NxChart";
```

- [ ] **Step 2: Read the new facts**

Find the contiguity block at line 83-90, which currently ends with `const hasContiguity = n50 !== undefined;`. Add two reads immediately before that line:

```tsx
  const nxCurve = facts.sequence_nx_curve as [number, number][] | undefined;
  // Expected genome size, present only on assemblies BioFlow produced from
  // reads with a size supplied -- it arrives here because assembly
  // provenance is merged into the facts document (queue/results.py:1354),
  // not because the FASTA said anything about it. An uploaded or
  // NCBI-downloaded assembly has none, and the chart drops to Nx alone.
  const genomeSize = facts.assembly_genome_size as number | undefined;
```

- [ ] **Step 3: Render the chart**

Find the end of the contiguity `<dl>` — the `gapCount` block at roughly line 294-300:

```tsx
        {gapCount !== undefined && gapCount > 0 && (
          <>
            <dt>Gaps</dt>
            <dd>{gapCount.toLocaleString()}</dd>
          </>
        )}
```

That `<dl>` continues with GC content and other rows, so do **not** insert the chart inside it — an `<svg>` is not valid `<dl>` content. The `<dl>` closes at **line 327**. Insert immediately after that closing `</dl>`:

```tsx
      {nxCurve !== undefined && totalBases !== undefined && (
        <NxChart curve={nxCurve} totalBases={totalBases} genomeSize={genomeSize} />
      )}
```

`totalBases` is already read at line 46 (`facts.total_bases`) — no new binding needed. Confirm the line numbers still hold before editing:

```bash
grep -n "const totalBases\|</dl>" frontend/src/components/AssemblyFacts.tsx | head -3
```

- [ ] **Step 4: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 5: Verify in the browser**

Start this worktree's own stack — **not** plain `docker compose`, which would repoint the main 5173 instance at this worktree:

```bash
./ops/worktree-up.sh
```

Open http://localhost:5273, go to a project with an assembly, and open an assembly object's Reference Quality tab. Check **both** states:

1. **An uploaded or NCBI-downloaded FASTA** — one solid green curve, no legend, no empty-state text.
2. **An assembly BioFlow produced from reads with a genome size** — two curves, the NGx one dashed and amber, with a legend.

For state 2, if no such object exists in the copied database, run an assembly from the Actions tab with a genome size supplied, or confirm the fact is present with:

```bash
docker compose -p bioflow-worktree exec api python -c "
import asyncio
from app.db.client import connect_to_mongo, get_db
async def main():
    await connect_to_mongo()
    db = get_db()
    async for o in db.data_objects.find({'facts.assembly_genome_size': {'\$exists': True}}).limit(5):
        print(o['_id'], o.get('name'), o['facts'].get('assembly_genome_size'))
asyncio.run(main())
"
```

Confirm the curve reads correctly: a finished genome is nearly flat, a fragmented draft falls away steeply. If every assembly looks like a vertical cliff, the Y axis is not actually on a log scale — check `py()`.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/AssemblyFacts.tsx
git commit -m "feat(frontend): show the Nx/NGx curve on the Reference Quality tab

NGx appears only when an expected genome size exists, which is only for
assemblies BioFlow built from reads with one supplied. Uploaded and
NCBI-downloaded assemblies get the Nx curve alone, with no empty state --
Nx is always computable, so there is nothing missing to report."
```

---

## Task 4: Close out the issue

- [ ] **Step 1: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: all pass. Read the printed count. If the run dies with exit code 137, that is the host running out of memory from concurrent stacks, not a test failure — stop other stacks and re-run.

- [ ] **Step 2: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --title "feat(frontend): add Nx/NGx contiguity curve to Reference Quality" --body "$(cat <<'EOF'
Adds the Nx/NGx contiguity curve from the #146 visualization epic.

The parser already computed N50/N90/L50/auN from a full uncapped list of
contig lengths and then discarded the list. This stores a 100-point
downsampled curve from that same walk, so the curve's x=50 point and
`sequence_n50` cannot disagree.

Downsampled rather than raw because a fragmented draft is hundreds of
thousands of contigs and these facts live in Mongo documents capped at
16MB. The cost is that a future visualization wanting a different derived
statistic needs a reparse rather than a recomputation; nothing currently
does.

NGx is available only where an expected genome size exists -- it is an
assembly *parameter*, so BioFlow-produced assemblies have one and uploaded
or NCBI-downloaded FASTAs do not. Both curves share one chart because the
gap between them is the diagnostic, and the chart degrades to Nx alone
rather than showing an empty state.

Verified in the browser at localhost:5273 in both states.

Closes #147
EOF
)"
```

Label the PR `type:feature`, `area:backend`, `area:frontend` — `.github/release.yml` categorizes release notes by label, and an unlabelled PR lands under "Other changes".

- [ ] **Step 3: Report the PR URL and stop**

Do not merge. The user reviews and merges.
