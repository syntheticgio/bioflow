# Chromosome Strip and NCBI Sequence Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a chromosome strip beside Base Composition in the reference Quality tab, with NCBI's Sequence Viewer opening in a modal for a selected chromosome.

**Architecture:** A pure classifier (`lib/chromosomes.ts`) sorts a reference's `facts` into one of four buckets and ranks its sequences by length. A presentational SVG strip renders the top 24 as proportional bars with an overflow `<select>`. Clicking a bar opens a modal that lazily injects NCBI's `sviewer.js` — the app's only runtime outbound dependency, loaded on demand so nothing else depends on NCBI being reachable.

**Tech Stack:** React 18, TypeScript, Vitest (no jsdom — logic tests only), inline SVG (no charting library), NCBI Sequence Viewer embedding API.

**Spec:** `docs/superpowers/specs/2026-07-30-ncbi-chromosome-viewer-design.md`

---

## Conventions verified against this repo

Read these before starting; they are facts checked against the running stack, not assumptions.

- **Tests run in the `web` container**, from the repo root (never a worktree):
  `docker compose exec -T web npx vitest run src/lib/chromosomes.test.ts`
  A bare host `npx vitest` is not the supported path.
- **`vitest` has no jsdom/testing-library.** There are zero `.test.tsx` files and none are expected. Only `lib/*.ts` gets unit tests; components are verified manually at localhost:5173.
- **Existing test idiom** is `frontend/src/lib/readQuality.test.ts`: `import { describe, expect, it } from "vitest"`, plain fixture builders, no mocking framework.
- **Modal markup** is `.modal-backdrop` > `.modal` > `<h2>` + `.modal-body`, with `onClick={onClose}` on the backdrop and `onClick={(e) => e.stopPropagation()}` on the modal. See `frontend/src/components/AlignDialog.tsx:196`.
- **CSS custom properties** available: `--border`, `--text`, `--text-dim`, `--text-faint`, `--accent`, `--accent-dim`, `--panel`. Both a dark default and a light override exist, so never hardcode a hex.
- **`.qc-charts` is already** `grid-template-columns: repeat(auto-fit, minmax(320px, 1fr))` (`frontend/src/styles.css:2517`). A second `.qc-chart` child needs **no grid CSS change**.
- **After changing frontend source**, `vite dev` hot-reloads — no rebuild. (Only `worker` needs `docker compose restart worker`, and this plan does not touch it.)

## File structure

| File | Responsibility |
|---|---|
| `frontend/src/lib/chromosomes.ts` (create) | Classify a reference's facts into a tagged union; rank bars by length. Pure, no React. |
| `frontend/src/lib/chromosomes.test.ts` (create) | Vitest cases built from real object shapes. |
| `frontend/src/components/ChromosomeStrip.tsx` (create) | SVG bars, overflow select, degraded-state messages. |
| `frontend/src/components/SequenceViewerModal.tsx` (create) | Lazy `sviewer.js` loader, viewer mount, escape hatch, load states. |
| `frontend/src/lib/format.ts` (modify) | Add a nucleotide-accession entry to `ACCESSION_LINKS`. |
| `frontend/src/components/DetailPanel.tsx` (modify) | Render the strip as a second `.qc-chart` when `isReference`. |
| `frontend/src/styles.css` (modify) | Bar, overflow and modal-sizing rules. |

Task order builds bottom-up: pure logic first (fully tested), then presentation, then the outbound dependency, then wiring. Each task ends at a commit and leaves the app working.

---

### Task 1: Classifier types and the two trivial buckets

**Files:**
- Create: `frontend/src/lib/chromosomes.ts`
- Create: `frontend/src/lib/chromosomes.test.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/lib/chromosomes.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { classifyChromosomes } from "./chromosomes";

describe("classifyChromosomes", () => {
  // genomic.gff: parsed, but carries no sequence names at all.
  it("returns nothing when there are no sequence facts", () => {
    expect(classifyChromosomes({}).kind).toBe("nothing");
    expect(classifyChromosomes({ sequence_names: [] }).kind).toBe("nothing");
  });

  // The real GCA_000146045.2_R64 and one of the two GCF_000002445.2 objects:
  // ingested before sequence_lengths existed, so names are known but no
  // lengths are. Re-running QC is what fixes this, so say so.
  it("returns needs-qc when names are known but lengths are not", () => {
    const view = classifyChromosomes({
      sequence_names: ["BK006935.2", "BK006936.2"],
      sequence_count: 16,
    });
    expect(view.kind).toBe("needs-qc");
  });

  it("returns needs-qc when sequence_lengths is present but empty", () => {
    const view = classifyChromosomes({
      sequence_names: ["BK006935.2"],
      sequence_lengths: {},
    });
    expect(view.kind).toBe("needs-qc");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run from the repo root (`/Users/syntheticgio/Programming/local-bio-pipeliner`):

```bash
docker compose exec -T web npx vitest run src/lib/chromosomes.test.ts
```

Expected: FAIL — `Failed to resolve import "./chromosomes"`.

- [ ] **Step 3: Write minimal implementation**

Create `frontend/src/lib/chromosomes.ts`:

```ts
/**
 * What a reference FASTA's sequences can be shown as.
 *
 * A reference is not automatically a set of chromosomes: the same project can
 * hold a 17-sequence genome, a `cds_from_genomic.fna` with 8,769 coding
 * records, and a `protein.faa`. Drawing chromosome bars for the latter two
 * would be the same category error the Actions-tab suggestion rules once made
 * by treating every FASTA as an alignable reference.
 *
 * A tagged union rather than a nullable result so the caller renders per case
 * and cannot silently drop one.
 */
export type ChromosomeView =
  | { kind: "drawable"; bars: Bar[]; overflow: Bar[]; linkable: boolean }
  /** Names parsed, lengths never measured -- an object ingested before
   *  `sequence_lengths` was added. Re-running QC populates it. */
  | { kind: "needs-qc" }
  | { kind: "not-chromosomal"; reason: string }
  | { kind: "nothing" };

export interface Bar {
  name: string;
  length: number;
}

export function classifyChromosomes(
  facts: Record<string, unknown>,
): ChromosomeView {
  const names = Array.isArray(facts.sequence_names)
    ? (facts.sequence_names as string[])
    : [];
  const lengths =
    facts.sequence_lengths && typeof facts.sequence_lengths === "object"
      ? (facts.sequence_lengths as Record<string, number>)
      : {};
  const lengthCount = Object.keys(lengths).length;

  if (!names.length && !lengthCount) return { kind: "nothing" };
  if (!lengthCount) return { kind: "needs-qc" };

  return { kind: "nothing" };
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec -T web npx vitest run src/lib/chromosomes.test.ts
```

Expected: PASS — 3 tests.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/chromosomes.ts frontend/src/lib/chromosomes.test.ts
git commit -m "feat: classify references with no lengths as needing QC"
```

---

### Task 2: Reject files that are not chromosome-shaped

The shape test: fewer than 5 sequences of at least 100 kb means this is not a chromosome set. `cds_from_genomic.fna` (8,769 records, longest ~15 kb) and `protein.faa` both fail on it, and neither can be rejected by count alone.

**Files:**
- Modify: `frontend/src/lib/chromosomes.ts`
- Modify: `frontend/src/lib/chromosomes.test.ts`

- [ ] **Step 1: Write the failing test**

Append inside the `describe` block in `frontend/src/lib/chromosomes.test.ts`:

```ts
  /** N sequences of `len` bases, named by the given pattern. */
  function lengths(n: number, len: number, name: (i: number) => string) {
    const out: Record<string, number> = {};
    for (let i = 0; i < n; i++) out[name(i)] = len;
    return out;
  }

  // cds_from_genomic.fna, as it really is: 8,769 coding records whose names
  // are `lcl|` local identifiers NCBI cannot resolve.
  it("rejects a CDS file as not chromosomal", () => {
    const view = classifyChromosomes({
      sequence_count: 8769,
      sequence_names: ["lcl|NC_008409.1_cds_XP_001218755.1_1"],
      sequence_lengths: lengths(
        8769,
        1400,
        (i) => `lcl|NC_008409.1_cds_XP_${i}_1`,
      ),
    });
    expect(view.kind).toBe("not-chromosomal");
    if (view.kind === "not-chromosomal") {
      expect(view.reason).toContain("8,769");
    }
  });

  // protein.faa: 8,758 XP_ protein accessions. Real accessions, wrong molecule.
  it("rejects a protein file as not chromosomal", () => {
    const view = classifyChromosomes({
      sequence_count: 8758,
      sequence_names: ["XP_001218755.1"],
      sequence_lengths: lengths(8758, 450, (i) => `XP_00121${i}.1`),
    });
    expect(view.kind).toBe("not-chromosomal");
  });

  // A plasmid-only or single-contig file: real DNA, too few chromosome-scale
  // sequences to be a chromosome set.
  it("rejects a file with too few chromosome-scale sequences", () => {
    const view = classifyChromosomes({
      sequence_count: 3,
      sequence_names: ["NC_000001.1", "NC_000002.1", "NC_000003.1"],
      sequence_lengths: {
        "NC_000001.1": 500_000,
        "NC_000002.1": 4_000,
        "NC_000003.1": 3_000,
      },
    });
    expect(view.kind).toBe("not-chromosomal");
  });
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec -T web npx vitest run src/lib/chromosomes.test.ts
```

Expected: FAIL — 3 failures, each `expected 'nothing' to be 'not-chromosomal'`.

- [ ] **Step 3: Write minimal implementation**

In `frontend/src/lib/chromosomes.ts`, add the constants above `classifyChromosomes` and replace the final `return { kind: "nothing" }`:

```ts
/** Below this, a sequence is not a chromosome or a large scaffold. */
const CHROMOSOME_SCALE_BP = 100_000;

/** Fewer chromosome-scale sequences than this and the file is something else
 *  -- coding sequences, proteins, a lone plasmid. */
const MIN_CHROMOSOME_SCALE = 5;
```

```ts
  const entries: Bar[] = Object.entries(lengths).map(([name, length]) => ({
    name,
    length: Number(length) || 0,
  }));
  const bigEnough = entries.filter((e) => e.length >= CHROMOSOME_SCALE_BP);

  if (bigEnough.length < MIN_CHROMOSOME_SCALE) {
    return { kind: "not-chromosomal", reason: describeNonChromosomal(entries) };
  }

  return { kind: "nothing" };
```

Add below `classifyChromosomes`:

```ts
/**
 * Why this file is not a chromosome set, in terms of what it actually holds.
 *
 * "None over 100 kb" is the useful half of the message: it tells the user the
 * file is short records, without claiming to know whether they are CDS,
 * proteins or something else.
 */
function describeNonChromosomal(entries: Bar[]): string {
  const count = entries.length.toLocaleString();
  const longest = entries.reduce((m, e) => Math.max(m, e.length), 0);
  if (longest < CHROMOSOME_SCALE_BP) {
    return `${count} sequences, none over 100 kb — this looks like coding sequences or proteins, not chromosomes.`;
  }
  return `${count} sequences, too few of them chromosome-scale to draw a chromosome map.`;
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec -T web npx vitest run src/lib/chromosomes.test.ts
```

Expected: PASS — 6 tests.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/chromosomes.ts frontend/src/lib/chromosomes.test.ts
git commit -m "feat: reject CDS and protein files as not chromosomal"
```

---

### Task 3: Rank bars and split the overflow

**Files:**
- Modify: `frontend/src/lib/chromosomes.ts`
- Modify: `frontend/src/lib/chromosomes.test.ts`

- [ ] **Step 1: Write the failing test**

Append inside the `describe` block:

```ts
  /** The real GCF_000146045.2_R64 yeast genome: 17 sequences, 16 nuclear
   *  chromosomes plus the 85 kb mitochondrion. */
  const YEAST_LENGTHS: Record<string, number> = {
    "NC_001133.9": 230218,
    "NC_001134.8": 813184,
    "NC_001135.5": 316620,
    "NC_001136.10": 1531933,
    "NC_001137.3": 576874,
    "NC_001138.5": 270161,
    "NC_001139.9": 1090940,
    "NC_001140.6": 562643,
    "NC_001141.2": 439888,
    "NC_001142.9": 745751,
    "NC_001143.9": 666816,
    "NC_001144.5": 1078177,
    "NC_001145.3": 924431,
    "NC_001146.8": 784333,
    "NC_001147.6": 1091291,
    "NC_001148.4": 948066,
    "NC_001224.1": 85779,
  };

  it("ranks bars longest first", () => {
    const view = classifyChromosomes({
      sequence_count: 17,
      sequence_names: Object.keys(YEAST_LENGTHS),
      sequence_lengths: YEAST_LENGTHS,
    });
    expect(view.kind).toBe("drawable");
    if (view.kind !== "drawable") return;
    expect(view.bars[0]).toEqual({ name: "NC_001136.10", length: 1531933 });
    expect(view.bars).toHaveLength(17);
    expect(view.overflow).toHaveLength(0);
  });

  // The 100 kb rule decides whether to draw at all; it must never drop a
  // sequence from a file that passed. Yeast's mitochondrion is 85 kb and
  // still belongs on the strip.
  it("keeps sub-100kb sequences as bars once the file qualifies", () => {
    const view = classifyChromosomes({
      sequence_names: Object.keys(YEAST_LENGTHS),
      sequence_lengths: YEAST_LENGTHS,
    });
    if (view.kind !== "drawable") throw new Error("expected drawable");
    expect(view.bars.map((b) => b.name)).toContain("NC_001224.1");
  });

  // A human-like assembly: 24 primary chromosomes plus 200 unplaced
  // scaffolds. The bars must be the 24 biggest, with the rest reachable
  // rather than discarded.
  it("caps bars at 24 and puts the rest in overflow", () => {
    const many: Record<string, number> = {};
    for (let i = 0; i < 24; i++) many[`NC_0000${i}.1`] = 50_000_000 - i * 1000;
    for (let i = 0; i < 200; i++) many[`NW_0001${i}.1`] = 120_000;

    const view = classifyChromosomes({
      sequence_names: Object.keys(many),
      sequence_lengths: many,
    });
    if (view.kind !== "drawable") throw new Error("expected drawable");
    expect(view.bars).toHaveLength(24);
    expect(view.overflow).toHaveLength(200);
    expect(view.bars.every((b) => b.name.startsWith("NC_"))).toBe(true);
  });
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec -T web npx vitest run src/lib/chromosomes.test.ts
```

Expected: FAIL — 3 failures, `expected 'nothing' to be 'drawable'`.

- [ ] **Step 3: Write minimal implementation**

In `frontend/src/lib/chromosomes.ts`, add near the other constants:

```ts
/** Bars drawn before the rest move to the overflow picker. Chosen so a human
 *  assembly shows its 24 primary chromosomes and yeast shows all 17. */
const MAX_BARS = 24;
```

Replace the final `return { kind: "nothing" }` in `classifyChromosomes`:

```ts
  // Ranked by length, not file order: chromosome numbers cannot be recovered
  // from an accession like NC_001133.9 without an NCBI lookup this design
  // does without, and ranking is what makes the top-24 cut meaningful.
  const ranked = [...entries].sort((a, b) => b.length - a.length);

  return {
    kind: "drawable",
    bars: ranked.slice(0, MAX_BARS),
    overflow: ranked.slice(MAX_BARS),
    linkable: false,
  };
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec -T web npx vitest run src/lib/chromosomes.test.ts
```

Expected: PASS — 9 tests.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/chromosomes.ts frontend/src/lib/chromosomes.test.ts
git commit -m "feat: rank chromosome bars by length with overflow"
```

---

### Task 4: Decide whether bars can link to NCBI

`linkable` is a separate axis from drawability: a local assembly with arbitrary contig names still deserves its strip, just with inert bars. Note `lcl|NC_008409.1_cds_...` **contains** a valid accession, so the pattern must be anchored to the whole string.

**Files:**
- Modify: `frontend/src/lib/chromosomes.ts`
- Modify: `frontend/src/lib/chromosomes.test.ts`

- [ ] **Step 1: Write the failing test**

Append inside the `describe` block:

```ts
  it("marks NCBI RefSeq accessions as linkable", () => {
    const view = classifyChromosomes({
      sequence_names: Object.keys(YEAST_LENGTHS),
      sequence_lengths: YEAST_LENGTHS,
    });
    if (view.kind !== "drawable") throw new Error("expected drawable");
    expect(view.linkable).toBe(true);
  });

  // The local-assembly path. Nothing in the live database exercises this, so
  // it is the branch most likely to break unnoticed.
  it("draws a local assembly but marks it unlinkable", () => {
    const local: Record<string, number> = {};
    for (let i = 1; i <= 8; i++) local[`contig_${i}`] = 900_000 - i * 1000;

    const view = classifyChromosomes({
      sequence_names: Object.keys(local),
      sequence_lengths: local,
    });
    expect(view.kind).toBe("drawable");
    if (view.kind !== "drawable") return;
    expect(view.linkable).toBe(false);
    expect(view.bars).toHaveLength(8);
  });

  // `lcl|NC_008409.1_cds_...` embeds a real accession. An unanchored test
  // would call it linkable and feed NCBI an id it cannot resolve.
  it("does not treat an embedded accession as linkable", () => {
    expect(isNcbiNucleotideAccession("lcl|NC_008409.1_cds_XP_846376.1_2")).toBe(
      false,
    );
    expect(isNcbiNucleotideAccession("NC_001133.9")).toBe(true);
    expect(isNcbiNucleotideAccession("BK006935.2")).toBe(true);
    // A protein accession is resolvable at NCBI but is not a chromosome.
    expect(isNcbiNucleotideAccession("XP_001218755.1")).toBe(false);
  });
```

Add `isNcbiNucleotideAccession` to the import at the top of the file:

```ts
import { classifyChromosomes, isNcbiNucleotideAccession } from "./chromosomes";
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec -T web npx vitest run src/lib/chromosomes.test.ts
```

Expected: FAIL — `isNcbiNucleotideAccession is not a function`.

- [ ] **Step 3: Write minimal implementation**

In `frontend/src/lib/chromosomes.ts`, add above `classifyChromosomes`:

```ts
/**
 * Whether a sequence name is an accession NCBI's Sequence Viewer can resolve
 * as a nucleotide record.
 *
 * Anchored deliberately: `lcl|NC_008409.1_cds_XP_846376.1_2` contains a real
 * accession but is a local identifier NCBI cannot resolve, and an unanchored
 * match would hand the viewer an id it rejects. `XP_`/`NP_` are excluded on
 * purpose -- they resolve, but as proteins, which is not what a chromosome
 * bar claims to be.
 */
const NUCLEOTIDE_ACCESSION =
  /^(?:(?:NC|NZ|NT|NW|AC)_\d+\.\d+|[A-Z]{2}\d{6}\.\d+|[A-Z]{4}\d{8,}\.\d+)$/;

export function isNcbiNucleotideAccession(name: string): boolean {
  return NUCLEOTIDE_ACCESSION.test(name.trim());
}
```

Then replace `linkable: false` in the `drawable` return:

```ts
    // One resolvable name is enough: a genome can carry an unplaced scaffold
    // with a local name without that making the chromosomes unlinkable. Each
    // bar is re-checked individually at render time.
    linkable: ranked.some((b) => isNcbiNucleotideAccession(b.name)),
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec -T web npx vitest run src/lib/chromosomes.test.ts
```

Expected: PASS — 12 tests.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/chromosomes.ts frontend/src/lib/chromosomes.test.ts
git commit -m "feat: detect NCBI-resolvable nucleotide accessions"
```

---

### Task 5: Add the nucleotide accession URL

`accessionUrl()` keys off a *field name* (`assembly_accession`, `sra_accession`) and has no entry for a bare per-chromosome accession, so the modal cannot reuse it as-is.

**Files:**
- Modify: `frontend/src/lib/format.ts:118-121`

- [ ] **Step 1: Read the existing shape**

Open `frontend/src/lib/format.ts` and find `ACCESSION_LINKS`. Each entry has `pattern`, `url`, and `label`; `accessionUrl(key, value)` returns `null` when the key is unknown or the pattern does not match.

- [ ] **Step 2: Add the entry**

Add to the `ACCESSION_LINKS` object, alongside `assembly_accession`:

```ts
  // A single chromosome or scaffold record, for the Sequence Viewer's
  // "View at NCBI" escape hatch. Separate from assembly_accession, which
  // points at a whole genome's Datasets page.
  nucleotide_accession: {
    pattern: /^[A-Z]{2}_?\d+(\.\d+)?$|^[A-Z]{4}\d{8,}(\.\d+)?$/i,
    url: (v) => `https://www.ncbi.nlm.nih.gov/nuccore/${v}`,
    label: "Sequence",
  },
```

- [ ] **Step 3: Verify it type-checks**

```bash
docker compose exec -T web npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/format.ts
git commit -m "feat: add a nucleotide accession link target"
```

---

### Task 6: The chromosome strip component

No test step: this repo has no jsdom or testing-library, and `.test.tsx` files do not exist here. Verification is Task 9, in the browser.

**Files:**
- Create: `frontend/src/components/ChromosomeStrip.tsx`

- [ ] **Step 1: Write the component**

Create `frontend/src/components/ChromosomeStrip.tsx`:

```tsx
import { useState } from "react";
import {
  classifyChromosomes,
  isNcbiNucleotideAccession,
} from "../lib/chromosomes";
import { SequenceViewerModal } from "./SequenceViewerModal";

/** Tallest bar, in px. The shortest is floored so a mitochondrion stays
 *  visible next to a 1.5 Mb chromosome rather than collapsing to a line. */
const MAX_BAR_H = 72;
const MIN_BAR_H = 8;
const BAR_W = 11;
const BAR_GAP = 7;

/**
 * A reference's sequences as proportional bars, in the empty second column of
 * the Quality tab's chart grid.
 *
 * Drawn entirely from `facts.sequence_lengths`, which ingest already stores --
 * no NCBI call is made to render this. Only the per-chromosome viewer behind a
 * click reaches out to NCBI.
 */
export function ChromosomeStrip({ facts }: { facts: Record<string, unknown> }) {
  const view = classifyChromosomes(facts);
  const [selected, setSelected] = useState<string | null>(null);

  if (view.kind === "nothing") return null;

  if (view.kind === "needs-qc") {
    return (
      <Framed title="Sequences">
        <div className="chrom-note">
          Sequence lengths weren’t measured for this file. Re-run QC to draw the
          chromosome map.
        </div>
      </Framed>
    );
  }

  if (view.kind === "not-chromosomal") {
    return (
      <Framed title="Sequences">
        <div className="chrom-note">{view.reason}</div>
      </Framed>
    );
  }

  const longest = view.bars[0]?.length || 1;

  return (
    <Framed title="Chromosomes">
      <svg
        className="chrom-strip"
        width={view.bars.length * (BAR_W + BAR_GAP)}
        height={MAX_BAR_H + 18}
        role="list"
        aria-label="Chromosomes in this reference"
      >
        {view.bars.map((bar, i) => {
          const h = Math.max(MIN_BAR_H, (bar.length / longest) * MAX_BAR_H);
          const clickable = view.linkable && isNcbiNucleotideAccession(bar.name);
          return (
            <g
              key={bar.name}
              role="listitem"
              className={clickable ? "chrom-bar is-clickable" : "chrom-bar"}
              onClick={clickable ? () => setSelected(bar.name) : undefined}
            >
              <title>
                {bar.name} · {formatBases(bar.length)}
              </title>
              <rect
                x={i * (BAR_W + BAR_GAP)}
                y={MAX_BAR_H - h}
                width={BAR_W}
                height={h}
                rx={BAR_W / 2}
              />
            </g>
          );
        })}
      </svg>

      {!view.linkable && (
        <div className="chrom-note">
          Sequence names aren’t NCBI accessions, so these can’t be opened at
          NCBI.
        </div>
      )}

      {view.overflow.length > 0 && (
        <select
          className="chrom-overflow"
          value=""
          onChange={(e) => e.target.value && setSelected(e.target.value)}
        >
          <option value="">…and {view.overflow.length} more</option>
          {view.overflow.map((bar) => (
            <option key={bar.name} value={bar.name}>
              {bar.name} · {formatBases(bar.length)}
            </option>
          ))}
        </select>
      )}

      {selected && (
        <SequenceViewerModal
          accession={selected}
          onClose={() => setSelected(null)}
        />
      )}
    </Framed>
  );
}

/** The chart-column wrapper, matching the Base Composition card beside it. */
function Framed({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="qc-chart">
      <div className="section-title">{title}</div>
      {children}
    </div>
  );
}

/** Duplicated from AssemblyFacts rather than shared: the two will drift, and
 *  a bar label wants Mb where a facts row may later want exact digits. */
function formatBases(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} Gb`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} Mb`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} kb`;
  return `${n} bp`;
}
```

- [ ] **Step 2: Verify it type-checks**

This will fail until Task 7 creates the modal — that is expected, and Task 7 closes it.

```bash
docker compose exec -T web npx tsc --noEmit
```

Expected: one error, `Cannot find module './SequenceViewerModal'`.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ChromosomeStrip.tsx
git commit -m "feat: add the chromosome strip component"
```

---

### Task 7: The Sequence Viewer modal

**Files:**
- Create: `frontend/src/components/SequenceViewerModal.tsx`

- [ ] **Step 1: Write the component**

Create `frontend/src/components/SequenceViewerModal.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";
import { accessionUrl } from "../lib/format";

const SVIEWER_SRC = "https://www.ncbi.nlm.nih.gov/projects/sviewer/js/sviewer.js";

/** Long enough for a cold NCBI fetch, short enough that an offline machine
 *  reaches the escape hatch rather than spinning forever. */
const LOAD_TIMEOUT_MS = 15_000;

let sviewerPromise: Promise<void> | null = null;

/**
 * Fetch NCBI's Sequence Viewer script, once per page load.
 *
 * Deliberately not imported at module scope: this is the app's only runtime
 * outbound dependency, and everything else here works with no network. Loading
 * it when the modal first opens keeps an offline machine fully functional
 * right up until someone asks for a chromosome view.
 */
function loadSviewer(): Promise<void> {
  if (sviewerPromise) return sviewerPromise;

  sviewerPromise = new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = SVIEWER_SRC;
    script.async = true;
    const timer = setTimeout(() => {
      // A failed load must not be cached, or a user who reconnects can never
      // retry without reloading the page.
      sviewerPromise = null;
      reject(new Error("timed out"));
    }, LOAD_TIMEOUT_MS);
    script.onload = () => {
      clearTimeout(timer);
      resolve();
    };
    script.onerror = () => {
      clearTimeout(timer);
      sviewerPromise = null;
      reject(new Error("failed to load"));
    };
    document.head.appendChild(script);
  });

  return sviewerPromise;
}

/**
 * NCBI's embedded genome browser for one chromosome.
 *
 * A modal rather than a third column: the viewer needs far more width than the
 * Quality tab's chart grid can give it.
 */
export function SequenceViewerModal({
  accession,
  onClose,
}: {
  accession: string;
  onClose: () => void;
}) {
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const mountRef = useRef<HTMLDivElement>(null);
  const ncbiUrl = accessionUrl("nucleotide_accession", accession);

  useEffect(() => {
    let cancelled = false;
    loadSviewer().then(
      () => !cancelled && setState("ready"),
      () => !cancelled && setState("failed"),
    );
    return () => {
      cancelled = true;
    };
  }, []);

  // Escape closes. A listener on document rather than onKeyDown on the
  // backdrop, which only fires when focus is already inside it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  // The script claims `.SeqViewerApp` nodes when it loads. Injecting the div
  // as raw HTML after that, rather than rendering it through React, keeps
  // React from reconciling a subtree the script owns and mutates.
  useEffect(() => {
    if (state !== "ready" || !mountRef.current) return;
    mountRef.current.innerHTML = "";
    const host = document.createElement("div");
    host.className = "SeqViewerApp";
    host.dataset.id = accession;
    host.dataset.tracks = "[key:gene_model_track]";
    host.dataset.width = "100%";
    mountRef.current.appendChild(host);
  }, [state, accession]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal sviewer-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <h2>
          {accession}
          {ncbiUrl && (
            <a
              className="sviewer-external"
              href={ncbiUrl}
              target="_blank"
              rel="noreferrer"
            >
              View at NCBI ↗
            </a>
          )}
        </h2>

        <div className="modal-body">
          {state === "loading" && (
            <div className="chrom-note">Loading the NCBI Sequence Viewer…</div>
          )}
          {state === "failed" && (
            <div className="error-box">
              Couldn’t load the NCBI Sequence Viewer. It’s fetched from
              ncbi.nlm.nih.gov, so this fails when you’re offline or NCBI is
              unreachable.
              {ncbiUrl && (
                <>
                  {" "}
                  <a href={ncbiUrl} target="_blank" rel="noreferrer">
                    View this sequence at NCBI
                  </a>{" "}
                  instead.
                </>
              )}
            </div>
          )}
          <div ref={mountRef} />
        </div>

        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify it type-checks**

```bash
docker compose exec -T web npx tsc --noEmit
```

Expected: no errors — this also clears Task 6's missing-module error.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/SequenceViewerModal.tsx
git commit -m "feat: add the NCBI Sequence Viewer modal"
```

---

### Task 8: Styles and wiring

**Files:**
- Modify: `frontend/src/styles.css` (append near `.qc-chart`, around line 2527)
- Modify: `frontend/src/components/DetailPanel.tsx:782-802`

- [ ] **Step 1: Add the styles**

Append to `frontend/src/styles.css`:

```css
/* Chromosome strip: bars are proportional to sequence length, drawn from
   facts the ingest already stored. Sits in the second column of .qc-charts,
   which on a reference is otherwise empty -- the quality curve is suppressed
   for a FASTA. */
.chrom-strip {
  display: block;
  max-width: 100%;
  overflow: visible;
}

.chrom-bar rect {
  fill: var(--text-faint);
  transition: fill 0.12s ease;
}

.chrom-bar.is-clickable {
  cursor: pointer;
}

.chrom-bar.is-clickable:hover rect {
  fill: var(--accent);
}

.chrom-note {
  color: var(--text-faint);
  font-size: 11px;
  margin-top: 8px;
  line-height: 1.45;
}

.chrom-overflow {
  margin-top: 10px;
  max-width: 100%;
}

/* The reason this is a modal at all: the viewer is unusable in a column. */
.sviewer-modal {
  width: min(1100px, 92vw);
  max-width: 92vw;
}

.sviewer-external {
  float: right;
  font-size: 12px;
  font-weight: 400;
}
```

- [ ] **Step 2: Wire it into the Quality tab**

In `frontend/src/components/DetailPanel.tsx`, add to the imports near line 25:

```tsx
import { ChromosomeStrip } from "./ChromosomeStrip";
```

Then change the chart-grid condition. Replace:

```tsx
      {(composition || curve) && (
        <div className="qc-charts">
```

with:

```tsx
      {(composition || curve || isReference) && (
        <div className="qc-charts">
```

And add the strip as the last child inside `.qc-charts`, immediately after the `{curve && (...)}` block and before its closing `</div>`:

```tsx
          {/* Second column on a reference, where the quality curve would be
              for reads. Renders nothing when the file has no sequence facts,
              so a GFF sidecar keeps the single-column layout. */}
          {isReference && <ChromosomeStrip facts={obj.facts} />}
```

- [ ] **Step 3: Verify it type-checks**

```bash
docker compose exec -T web npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Run the full test suite**

```bash
docker compose exec -T web npx vitest run
```

Expected: PASS — all files, including the 12 tests in `chromosomes.test.ts`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/styles.css frontend/src/components/DetailPanel.tsx
git commit -m "feat: show the chromosome strip on the reference Quality tab"
```

---

### Task 9: Verify in the browser

`vite dev` hot-reloads, so no rebuild is needed. If the app is not up, run from the repo root — **never from a worktree**, per CLAUDE.md:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose up -d --build api web worker
```

- [ ] **Step 1: The drawable case**

Open localhost:5173, find `GCF_000146045.2_R64_genomic.fna` (`_id` starts `6a6a3416`), open the Quality tab.

Expect: a "Chromosomes" card right of Base Composition; 17 bars, tallest first; hovering shows `NC_001136.10 · 1.5 Mb`; the shortest bar (the 85 kb mitochondrion) is visible, not collapsed; no overflow select.

- [ ] **Step 2: The viewer**

Click the tallest bar. Expect the modal to open, show "Loading the NCBI Sequence Viewer…", then render the gene-model track for `NC_001136.10`. Check "View at NCBI ↗" opens `https://www.ncbi.nlm.nih.gov/nuccore/NC_001136.10` in a new tab. Close with Escape, then reopen and close by clicking the backdrop.

- [ ] **Step 3: The needs-qc case**

Open `GCA_000146045.2_R64_genomic.fna` (`_id` starts `6a6a340f`). Expect a "Sequences" card reading "Sequence lengths weren't measured for this file. Re-run QC to draw the chromosome map." — and no bars.

- [ ] **Step 4: The not-chromosomal case**

Open `cds_from_genomic.fna`. Expect "8,769 sequences, none over 100 kb — this looks like coding sequences or proteins, not chromosomes." Confirm the same for `protein.faa`, and that `genomic.gff` shows no such card at all.

- [ ] **Step 5: The offline path**

In DevTools, set the network to Offline, reload, and click a bar. Expect the failure copy and a working "View this sequence at NCBI" link (it will not load while offline, but must be present and correctly addressed). Restore the network, close the modal, and reopen — it must load, proving a failed load was not cached.

- [ ] **Step 6: Narrow layout**

Narrow the window until `.qc-charts` collapses to one column. Expect the strip to stack under Base Composition with no horizontal overflow.

- [ ] **Step 7: Commit any fixes**

```bash
git add -A && git commit -m "fix: <what the browser pass turned up>"
```

---

## Notes for the implementer

- **Do not add a re-run-QC button** to the strip. `QcTab` already renders `runQcPrompt` above the charts; the `needs-qc` copy points at it deliberately.
- **Do not** add `data-v` coordinate deep-linking, configurable track sets, or the programmatic `SeqView.App.AppNode` API. All three were considered and cut — see the spec's "Features deliberately excluded".
- **The 100 kb threshold is a file-level test only.** If you find yourself filtering individual bars by length, that is a bug: yeast's 85 kb mitochondrion must keep its bar.
- **Run `docker compose` from the repo root**, never from a worktree — the bind mounts are relative and would silently repoint the shared stack.
