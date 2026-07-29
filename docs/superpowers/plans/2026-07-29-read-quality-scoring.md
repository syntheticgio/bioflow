# Read Quality Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Grade every read file 1–5 in plain English (Excellent → Unsuitable) and show that grade as a colored badge on the file's list icon, in the list row subtitle, and in the detail panel header, with a Help page explaining the derivation.

**Architecture:** One pure function in `frontend/src/lib/readQuality.ts` turns a `DataObject` into a tier, a word, a basis string, caveats, and an assembled tooltip. Every surface calls that one function, so thresholds live in exactly one place. Frontend-only: `facts` already rides on the base `DataObject` type, so list rows have the data — no API, schema, or backend change, and no re-running QC on existing files.

**Tech Stack:** TypeScript, React 18, react-router-dom 6, Vitest 2 (already installed, zero test files so far), plain CSS with existing theme variables.

---

## Critical environment notes

Read these before Task 1. They will save you an hour.

**Tests run inside the `web` container, not on the host.** This worktree has no
`node_modules` (verified: `ls node_modules/.bin/vitest` fails). The container
has Vitest 2.1.9 installed. Every test command in this plan is therefore:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose exec -T web npx vitest run src/lib/readQuality.test.ts
```

**Always run `docker compose` from the main repo root**, never from this
worktree — per CLAUDE.md, running it from a worktree silently repoints the
shared stack. The `cd` above is mandatory, not decorative.

**The container bind-mounts the MAIN repo's `frontend/src`, not this
worktree.** Verified via `docker inspect biopipe-web-1`. So Vitest in the
container runs *main's* code, and it will not see files you create in this
worktree until the branch merges. This is expected and must NOT be "fixed" by
repointing the stack.

This means the TDD loop needs the test file visible to the container. Use this
one-liner, which runs Vitest against this worktree's source by mounting it into
a throwaway container that reuses the web image's `node_modules`:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose run --rm --no-deps -v "$(git -C .claude/worktrees/read-quality-scoring-3a4b60 rev-parse --show-toplevel)/frontend/src:/srv/src" web npx vitest run src/lib/readQuality.test.ts
```

Define it once as a shell alias at the start of your session:

```bash
alias wtest='cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose run --rm --no-deps -v /Users/syntheticgio/Programming/local-bio-pipeliner/.claude/worktrees/read-quality-scoring-3a4b60/frontend/src:/srv/src web npx vitest run'
```

Then every test step is just `wtest src/lib/readQuality.test.ts`.

**Vitest exits code 1 with "No test files found"** when the glob matches
nothing. That is a *setup* failure, not a red test. A genuine red test names the
failing assertion.

**Typecheck** is `npm run lint` (which is `tsc --noEmit`) and has the same
no-`node_modules` problem. Run it in the container the same way, or rely on
`vite dev`'s own errors in the browser.

**UI verification is manual at localhost:5173**, per CLAUDE.md — there is no
jsdom or component-testing setup and none is added here. But note the bind-mount
finding above: **port 5173 serves main, not this worktree.** To see these
changes in a browser before merge, either merge to main first, or start a
throwaway stack on another port with its own project name:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose -p biopipe-readqual up -d --build web
```

Do not repoint the shared `biopipe` stack at this worktree.

---

## File Structure

**Create:**

- `frontend/src/lib/readQuality.ts` — the scoring function and its thresholds.
  Pure, no React, no DOM. The single source of truth for grading.
- `frontend/src/lib/readQuality.test.ts` — unit tests. The repo's first test file.
- `frontend/src/components/QualityBadge.tsx` — the colored dot for the list icon.
- `frontend/src/components/HelpCalculations.tsx` — the `/help/calculations` page.

**Modify:**

- `frontend/src/components/ProjectExplorer.tsx` — badge on the icon (line 387),
  word in the row subtitle (line 396).
- `frontend/src/components/DetailPanel.tsx` — word in `.detail-subtitle` (line 468).
- `frontend/src/components/Header.tsx` — real Help dropdown (replaces the inert
  placeholder button at line 8/45).
- `frontend/src/App.tsx` — the `/help/calculations` route (line 54) and the
  `singleColumn` full-width check (line 44).
- `frontend/src/styles.css` — badge, dropdown, and help-page styles.

Why `QualityBadge` is its own file: it is used in the explorer now and is the
obvious thing to reuse in search results later. Keeping it separate from
`ProjectExplorer.tsx` (already 400+ lines) avoids growing that file further.

---

## Task 1: The scoring module

**Files:**
- Create: `frontend/src/lib/readQuality.ts`
- Test: `frontend/src/lib/readQuality.test.ts`

This is the whole design. Get it right and the rest is wiring.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/lib/readQuality.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { readQuality } from "./readQuality";
import type { DataObject } from "../api/types";

/** A ready FASTQ with the given facts/metadata. Only the fields readQuality
 *  reads are set; the rest satisfy the type. */
function fastq(
  facts: Record<string, unknown>,
  metadata: Record<string, unknown> = {},
): DataObject {
  return {
    id: "1",
    project_id: "p",
    name: "reads_1.fastq",
    size: 2_100_000_000,
    status: "ready",
    blob_sha256: "abc",
    format: { kind: "fastq" },
    facts,
    metadata,
    tags: [],
    role: null,
    derived_from: [],
    produced_by_job: null,
    mate_object_id: null,
    sidecar_of: null,
    sidecar_role: null,
  } as unknown as DataObject;
}

/** The real facts from DRR1066343_1.fastq, trimmed to what scoring reads. */
const EXAMPLE_FACTS = {
  mean_quality: 38.0,
  min_position_quality: 30.54,
  gc_content_percent: 30.93,
  base_composition: [
    { base: "A", count: 10384579, percent: 34.615 },
    { base: "C", count: 4630603, percent: 15.435 },
    { base: "G", count: 4648787, percent: 15.496 },
    { base: "T", count: 10335189, percent: 34.451 },
    { base: "N", count: 842, percent: 0.003 },
  ],
  qc_before_filtering: { q30_rate: 0.92134, q20_rate: 0.969812 },
  qc_duplication_rate: 0.652221,
};

describe("readQuality", () => {
  it("scores the example file Good (4/5): Excellent Q30, demoted for duplication", () => {
    const q = readQuality(fastq(EXAMPLE_FACTS));
    expect(q).not.toBeNull();
    expect(q!.tier).toBe(4);
    expect(q!.word).toBe("Good");
    expect(q!.basis).toBe("Q30 92.1%");
    expect(q!.caveats.join(" ")).toContain("65% duplication");
  });

  it("does not demote for duplication when the assay expects it", () => {
    const q = readQuality(fastq(EXAMPLE_FACTS, { assay: "RNA-seq" }));
    expect(q!.tier).toBe(5);
    expect(q!.word).toBe("Excellent");
    expect(q!.caveats).toEqual([]);
  });

  it("demotes for duplication when the assay is WGS", () => {
    const q = readQuality(fastq(EXAMPLE_FACTS, { assay: "WGS" }));
    expect(q!.tier).toBe(4);
  });

  it("falls back to mean_quality when fastp has not run", () => {
    const q = readQuality(
      fastq({ mean_quality: 38.0, min_position_quality: 30.54 }),
    );
    expect(q!.tier).toBe(5);
    expect(q!.basis).toBe("mean Q38.0");
  });

  it("demotes a clean average that hides a collapsed tail", () => {
    const q = readQuality(
      fastq({ mean_quality: 38.0, min_position_quality: 12.0 }),
    );
    expect(q!.tier).toBe(4);
    expect(q!.caveats.join(" ")).toContain("drops to Q12");
  });

  it("demotes for a high N rate", () => {
    const q = readQuality(
      fastq({
        mean_quality: 38.0,
        min_position_quality: 30.0,
        base_composition: [{ base: "N", count: 100, percent: 5.0 }],
      }),
    );
    expect(q!.tier).toBe(4);
    expect(q!.caveats.join(" ")).toContain("5% ambiguous");
  });

  it("floors at 1 rather than going to zero", () => {
    const q = readQuality(
      fastq({
        qc_before_filtering: { q30_rate: 0.4 },
        mean_quality: 15,
        min_position_quality: 5,
        base_composition: [{ base: "N", count: 100, percent: 5.0 }],
      }),
    );
    expect(q!.tier).toBe(1);
    expect(q!.word).toBe("Unsuitable");
  });

  it("never reports GC as a caveat", () => {
    const q = readQuality(fastq(EXAMPLE_FACTS));
    expect(q!.caveats.join(" ").toLowerCase()).not.toContain("gc");
  });

  it("assembles a tooltip with the word, score and basis", () => {
    const q = readQuality(fastq(EXAMPLE_FACTS));
    expect(q!.tooltip).toContain("Good (4/5)");
    expect(q!.tooltip).toContain("Q30 92.1%");
    expect(q!.tooltip).toContain("Assay");
  });

  it("omits the assay hint once the assay is known", () => {
    const q = readQuality(fastq(EXAMPLE_FACTS, { assay: "WGS" }));
    expect(q!.tooltip).not.toContain("Set Assay");
  });

  it("returns null for the sixth state", () => {
    // Not a read file.
    const bam = { ...fastq(EXAMPLE_FACTS), format: { kind: "bam" } };
    expect(readQuality(bam as unknown as DataObject)).toBeNull();
    // A FASTQ with no quality facts yet.
    expect(readQuality(fastq({}))).toBeNull();
    // Still ingesting.
    const pending = { ...fastq(EXAMPLE_FACTS), status: "ingesting" };
    expect(readQuality(pending as unknown as DataObject)).toBeNull();
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
wtest src/lib/readQuality.test.ts
```

Expected: FAIL — `Failed to resolve import "./readQuality"`. If instead you see
"No test files found", the bind-mount in your `wtest` alias is wrong; fix that
before continuing.

- [ ] **Step 3: Write the implementation**

Create `frontend/src/lib/readQuality.ts`:

```ts
import type { DataObject } from "../api/types";

/**
 * A 1-5 read quality grade with the reasoning behind it.
 *
 * Base quality drives the tier; composition problems demote it. GC never
 * does -- the organism's expected GC is unknown, so 31% is only "wrong" if
 * you assume human.
 */
export interface ReadQuality {
  tier: 1 | 2 | 3 | 4 | 5;
  word: "Excellent" | "Good" | "Fair" | "Poor" | "Unsuitable";
  /** What produced the tier, e.g. "Q30 92.1%". Shown in the tooltip. */
  basis: string;
  /** Demotion reasons, already human-readable. Empty when nothing demoted. */
  caveats: string[];
  /** Assembled hover text for every surface. */
  tooltip: string;
}

const WORDS = {
  5: "Excellent",
  4: "Good",
  3: "Fair",
  2: "Poor",
  1: "Unsuitable",
} as const;

/** Illumina conventions: Q30 is the industry yardstick. */
const Q30_TIERS: [number, 1 | 2 | 3 | 4 | 5][] = [
  [0.9, 5],
  [0.8, 4],
  [0.7, 3],
  [0.55, 2],
];

/** Used only when fastp has not run and all we have is ingest's mean. */
const MEAN_Q_TIERS: [number, 1 | 2 | 3 | 4 | 5][] = [
  [36, 5],
  [32, 4],
  [28, 3],
  [22, 2],
];

/**
 * Assays where PCR or amplification makes high duplication expected rather
 * than a defect. Values match the vocabulary in backend/app/metadata/sra.py.
 */
const HIGH_DUP_EXPECTED = new Set([
  "RNA-seq",
  "Amplicon",
  "Targeted panel",
  "ChIP-seq",
  "ATAC-seq",
]);

const DUP_LIMIT = 0.5;
const N_LIMIT = 1.0; // percent
const TAIL_COLLAPSE_Q = 20;
const HEALTHY_MEAN_Q = 30;

function tierFrom(
  value: number,
  table: [number, 1 | 2 | 3 | 4 | 5][],
): 1 | 2 | 3 | 4 | 5 {
  for (const [threshold, tier] of table) {
    if (value >= threshold) return tier;
  }
  return 1;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** The N percentage from ingest's base_composition, if it is there. */
function nPercent(facts: Record<string, unknown>): number | null {
  const comp = facts.base_composition;
  if (!Array.isArray(comp)) return null;
  for (const entry of comp) {
    if (entry && typeof entry === "object" && (entry as { base?: string }).base === "N") {
      return num((entry as { percent?: unknown }).percent);
    }
  }
  return null;
}

/**
 * Grade a read file, or null when there is nothing honest to say.
 *
 * Null (the "sixth state") covers three cases that all mean "no grade":
 * the object is not a read file, it is still ingesting, or its quality facts
 * are missing. Rendering nothing reads as "not applicable"; a word like
 * "Unknown" would imply we measured and failed.
 */
export function readQuality(obj: DataObject): ReadQuality | null {
  if (obj.format?.kind !== "fastq") return null;
  if (obj.status !== "ready") return null;

  const facts = (obj.facts ?? {}) as Record<string, unknown>;
  const before = (facts.qc_before_filtering ?? {}) as Record<string, unknown>;

  const q30 = num(before.q30_rate);
  const meanQ = num(facts.mean_quality);

  // Prefer fastp's Q30 -- it is the whole file, where mean_quality is a
  // 200k-read sample and a coarser signal.
  let tier: 1 | 2 | 3 | 4 | 5;
  let basis: string;
  if (q30 !== null) {
    tier = tierFrom(q30, Q30_TIERS);
    basis = `Q30 ${(q30 * 100).toFixed(1)}%`;
  } else if (meanQ !== null) {
    tier = tierFrom(meanQ, MEAN_Q_TIERS);
    basis = `mean Q${meanQ.toFixed(1)}`;
  } else {
    return null;
  }

  const caveats: string[] = [];
  const assay = typeof obj.metadata?.assay === "string" ? obj.metadata.assay : null;

  // Duplication. Suppressed entirely when the assay explains it, because
  // penalising an amplicon library for amplifying is noise, not signal.
  const dup = num(facts.qc_duplication_rate);
  if (dup !== null && dup > DUP_LIMIT && !(assay && HIGH_DUP_EXPECTED.has(assay))) {
    tier = Math.max(1, tier - 1) as 1 | 2 | 3 | 4 | 5;
    caveats.push(
      `${Math.round(dup * 100)}% duplication; normal for amplicon or RNA-seq.`,
    );
  }

  // Ambiguous bases. Assay-independent: no library design wants N.
  const n = nPercent(facts);
  if (n !== null && n > N_LIMIT) {
    tier = Math.max(1, tier - 1) as 1 | 2 | 3 | 4 | 5;
    caveats.push(`${+n.toFixed(2)}% ambiguous (N) bases.`);
  }

  // A healthy average can hide cycles that collapsed at the read's end,
  // which is exactly what trimming fixes -- so it is worth surfacing.
  const minPos = num(facts.min_position_quality);
  if (minPos !== null && minPos < TAIL_COLLAPSE_Q && (meanQ ?? 0) >= HEALTHY_MEAN_Q) {
    tier = Math.max(1, tier - 1) as 1 | 2 | 3 | 4 | 5;
    caveats.push(
      `Quality drops to Q${minPos.toFixed(0)} at some cycles; consider trimming.`,
    );
  }

  const word = WORDS[tier];
  const lines = [`${word} (${tier}/5) — ${basis}`, ...caveats];
  // Only worth suggesting while it would change something: the hint exists to
  // explain a duplication demotion the user can legitimately lift.
  if (!assay && dup !== null && dup > DUP_LIMIT) {
    lines.push("Set Assay under Metadata to refine this score.");
  }

  return { tier, word, basis, caveats, tooltip: lines.join("\n") };
}

/** Tier -> CSS class for the badge. Colors live in styles.css. */
export function qualityClass(tier: 1 | 2 | 3 | 4 | 5): string {
  return `q-badge q-badge-${tier}`;
}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
wtest src/lib/readQuality.test.ts
```

Expected: PASS, 11 passed.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/readQuality.ts frontend/src/lib/readQuality.test.ts
git commit -m "feat: assay-aware 1-5 read quality scoring"
```

---

## Task 2: The badge component and its styles

**Files:**
- Create: `frontend/src/components/QualityBadge.tsx`
- Modify: `frontend/src/styles.css` (append)

No test: this is presentational, and the repo has no component-testing setup.
The scoring it displays is already covered by Task 1.

- [ ] **Step 1: Create the component**

Create `frontend/src/components/QualityBadge.tsx`:

```tsx
import { qualityClass, type ReadQuality } from "../lib/readQuality";

/**
 * The quality grade as a dot on a file's icon.
 *
 * Tiers 5/4 and 3/2 differ only by shade, so color is deliberately never the
 * only signal: the word sits beside it in the row, and the full tooltip --
 * including the numeric score -- hangs off the badge itself, so hovering the
 * icon alone answers "how good is this file?". That is also what keeps the
 * badge meaningful for colorblind users.
 */
export function QualityBadge({ quality }: { quality: ReadQuality }) {
  return (
    <span
      className={qualityClass(quality.tier)}
      title={quality.tooltip}
      aria-label={`Read quality: ${quality.word}, ${quality.tier} of 5`}
    />
  );
}
```

- [ ] **Step 2: Add the styles**

Append to `frontend/src/styles.css`:

```css
/* Read quality badge -------------------------------------------------- */

/* The icon is the positioning context so the dot can sit on its corner. */
.row-icon {
  position: relative;
}

.q-badge {
  position: absolute;
  right: -1px;
  bottom: -1px;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  /* Against the row background, so the dot reads as separate from the glyph
     rather than merging with it on hover. */
  box-shadow: 0 0 0 1.5px var(--bg-panel);
}

.row.selected .q-badge,
.row:hover .q-badge {
  box-shadow: 0 0 0 1.5px var(--bg-hover);
}

/* Green -> amber -> red. Existing theme vars, so light mode tracks. */
.q-badge-5 {
  background: var(--success);
}

/* Tier 4 is still good; dimmed rather than a different hue. */
.q-badge-4 {
  background: var(--success);
  opacity: 0.65;
}

.q-badge-3 {
  background: var(--warn);
}

/* Deepened rather than a new hue: still "caution", further along. */
.q-badge-2 {
  background: color-mix(in srgb, var(--warn) 75%, var(--error));
}

.q-badge-1 {
  background: var(--error);
}
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/QualityBadge.tsx frontend/src/styles.css
git commit -m "feat: quality badge component and tier colors"
```

---

## Task 3: Wire the explorer list

**Files:**
- Modify: `frontend/src/components/ProjectExplorer.tsx`

- [ ] **Step 1: Add the imports**

`ProjectExplorer.tsx` line 5 currently reads:

```tsx
import { formatBytes, formatKindLabel } from "../lib/format";
```

Add after it:

```tsx
import { readQuality } from "../lib/readQuality";
import { QualityBadge } from "./QualityBadge";
```

- [ ] **Step 2: Compute the grade and render both surfaces**

Replace the file row block (currently lines 381–403, starting
`categoryFiles.map((o: DataObject) => (`) with:

```tsx
                  categoryFiles.map((o: DataObject) => {
                    const quality = readQuality(o);
                    return (
                    <div
                      key={o.id}
                      className={`row ${sel === `object:${o.id}` ? "selected" : ""}`}
                      onClick={() => select(`object:${o.id}`)}
                    >
                      <span className="row-icon">
                        {o.status !== "ready"
                          ? "⏳"
                          : o.role === "reference"
                            ? "📗"
                            : "📄"}
                        {quality && <QualityBadge quality={quality} />}
                      </span>
                      <div className="row-main">
                        <div className="row-name">{o.name}</div>
                        <div className="row-sub">
                          <span>{formatBytes(o.size)}</span>
                          {o.format.kind !== "unknown" && (
                            <span>{formatKindLabel(o.format.kind)}</span>
                          )}
                          {/* After size and format, matching the detail
                              panel's ordering. */}
                          {quality && (
                            <span title={quality.tooltip}>{quality.word}</span>
                          )}
                          {o.status !== "ready" && <span>{o.status}</span>}
                        </div>
                      </div>
```

- [ ] **Step 3: Close the new arrow-function body**

Because Step 2 changed the `map` callback from an expression (`(o) => (`) to a
block (`(o) => {`), its closing must change too. Find the end of that row block
— the `))` that closed the map, immediately after the delete `</button>` and its
`</div>` — and change:

```tsx
                    </div>
                  ))}
```

to:

```tsx
                    </div>
                    );
                  })}
```

- [ ] **Step 4: Typecheck**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose run --rm --no-deps -v /Users/syntheticgio/Programming/local-bio-pipeliner/.claude/worktrees/read-quality-scoring-3a4b60/frontend/src:/srv/src web npx tsc --noEmit
```

Expected: no output (success). A JSX "unexpected token" error means Step 3's
brace/paren balance is off.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ProjectExplorer.tsx
git commit -m "feat: show read quality in the file listing"
```

---

## Task 4: Wire the detail panel header

**Files:**
- Modify: `frontend/src/components/DetailPanel.tsx`

- [ ] **Step 1: Add the import**

After the existing `../lib/format` import block near line 12, add:

```tsx
import { readQuality } from "../lib/readQuality";
```

- [ ] **Step 2: Compute the grade**

Immediately after the `species` declaration (currently line 378–380, ending
`typeof organism === "string" && organism.trim() ? organism.trim() : null;`),
add:

```tsx
  // Same function the explorer rows use, so the word here and the word there
  // can never disagree.
  const quality = readQuality(obj);
```

- [ ] **Step 3: Render it in the subtitle**

In `.detail-subtitle` (line 468), after the `{species && (...)}` block and
before the closing `</div>`, add:

```tsx
          {/* Last in the line: it is a judgement about the file rather than
              an identifying property of it, and it carries the caveats. */}
          {quality && (
            <>
              {" · "}
              <span title={quality.tooltip} style={{ cursor: "help" }}>
                {quality.word}
              </span>
            </>
          )}
```

- [ ] **Step 4: Typecheck**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose run --rm --no-deps -v /Users/syntheticgio/Programming/local-bio-pipeliner/.claude/worktrees/read-quality-scoring-3a4b60/frontend/src:/srv/src web npx tsc --noEmit
```

Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/DetailPanel.tsx
git commit -m "feat: show read quality in the detail panel header"
```

---

## Task 5: The Help > BioFlow Calculations page

**Files:**
- Create: `frontend/src/components/HelpCalculations.tsx`
- Modify: `frontend/src/styles.css` (append)

Built before the menu that links to it, so the route is real when Task 6 wires
it up.

- [ ] **Step 1: Create the page**

Create `frontend/src/components/HelpCalculations.tsx`:

```tsx
/**
 * How BioFlow's derived numbers are computed.
 *
 * One section per topic. Read Quality Score is the first; the structure exists
 * so the next derived number is one more <section>, not a new page.
 */
export function HelpCalculations() {
  return (
    <div className="help-page">
      <h1>BioFlow Calculations</h1>
      <p className="help-intro">
        How the numbers BioFlow derives are computed, and what they do and do
        not tell you.
      </p>

      <section className="help-section">
        <h2>Read Quality Score</h2>
        <p>
          Every read file gets a 1–5 grade. Base quality sets the grade;
          specific problems can lower it. The grade appears as a colored dot on
          the file's icon, as a word in the file list, and in the detail panel
          header — hover any of them for the score and its reasoning.
        </p>

        <h3>Base quality sets the tier</h3>
        <p>
          When QC has run, the grade comes from <strong>Q30</strong> — the
          fraction of bases with a 99.9%-or-better confidence call, the standard
          Illumina yardstick. Thresholds follow Illumina convention:
        </p>
        <table className="help-table">
          <thead>
            <tr>
              <th>Grade</th>
              <th>Word</th>
              <th>Q30</th>
              <th>Without QC: mean quality</th>
            </tr>
          </thead>
          <tbody>
            <tr><td>5</td><td>Excellent</td><td>≥ 90%</td><td>≥ Q36</td></tr>
            <tr><td>4</td><td>Good</td><td>≥ 80%</td><td>≥ Q32</td></tr>
            <tr><td>3</td><td>Fair</td><td>≥ 70%</td><td>≥ Q28</td></tr>
            <tr><td>2</td><td>Poor</td><td>≥ 55%</td><td>≥ Q22</td></tr>
            <tr><td>1</td><td>Unsuitable</td><td>&lt; 55%</td><td>&lt; Q22</td></tr>
          </tbody>
        </table>
        <p>
          Before QC runs, the grade uses the mean quality measured from a
          200,000-read sample at ingest. That is a coarser signal, so the
          tooltip says which one it used.
        </p>

        <h3>What lowers the grade</h3>
        <p>Each of these drops the grade by one, never below 1:</p>
        <ul>
          <li>
            <strong>Duplication above 50%</strong> — but only when the assay is
            unset, WGS, or WES. See the caveat below.
          </li>
          <li>
            <strong>More than 1% ambiguous (N) bases</strong> — no library
            design wants these, so this always counts.
          </li>
          <li>
            <strong>Collapsed cycles</strong> — some read position averages
            below Q20 while the overall mean is Q30 or better. A healthy
            average can hide a bad read tail, which trimming fixes.
          </li>
        </ul>

        <h3>The duplication caveat</h3>
        <p>
          High duplication means something different depending on how the
          library was made. For whole-genome or exome sequencing it suggests
          over-amplification of too little input. For{" "}
          <strong>RNA-seq, amplicon, targeted panel, ChIP-seq, and ATAC-seq</strong>{" "}
          it is expected — those methods amplify on purpose, and abundant
          transcripts or enriched regions are genuinely sequenced many times.
        </p>
        <p>
          So the duplication penalty is skipped for those assays. When the assay
          is not recorded, the penalty is applied, because unlabeled data is
          most often whole-genome. If a file is RNA-seq or amplicon and looks
          unfairly marked down, set <strong>Assay</strong> under the file's
          Metadata tab and the grade will account for it.
        </p>

        <h3>What GC content does not do</h3>
        <p>
          GC content is reported but never changes the grade. Expected GC is a
          property of the organism — roughly 41% for human, under 20% for{" "}
          <em>Plasmodium</em> — so without knowing the source, an unusual GC is
          not evidence of a problem.
        </p>

        <h3>When no grade is shown</h3>
        <p>
          Files with no grade are either not read files (alignments, references,
          indexes), still being ingested, or missing quality measurements. An
          empty space means the question does not apply — not that the file
          failed.
        </p>
      </section>
    </div>
  );
}
```

- [ ] **Step 2: Add the styles**

Append to `frontend/src/styles.css`:

```css
/* Help pages ---------------------------------------------------------- */

.help-page {
  padding: 24px 32px;
  max-width: 760px;
  overflow-y: auto;
  color: var(--text);
}

.help-page h1 {
  font-size: 20px;
  margin: 0 0 4px;
}

.help-intro {
  color: var(--text-dim);
  margin: 0 0 24px;
}

.help-section {
  border-top: 1px solid var(--border);
  padding-top: 20px;
}

.help-section h2 {
  font-size: 16px;
  margin: 0 0 12px;
}

.help-section h3 {
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-dim);
  margin: 24px 0 8px;
}

.help-section p,
.help-section li {
  line-height: 1.6;
  color: var(--text);
}

.help-section ul {
  padding-left: 20px;
}

.help-section li {
  margin-bottom: 6px;
}

.help-table {
  border-collapse: collapse;
  width: 100%;
  margin: 12px 0;
  font-size: 13px;
}

.help-table th,
.help-table td {
  text-align: left;
  padding: 6px 10px;
  border-bottom: 1px solid var(--border);
}

.help-table th {
  color: var(--text-dim);
  font-weight: 600;
}
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/HelpCalculations.tsx frontend/src/styles.css
git commit -m "feat: BioFlow Calculations help page"
```

---

## Task 6: The route

**Files:**
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Add the import**

Alongside the other component imports in `App.tsx`, add:

```tsx
import { HelpCalculations } from "./components/HelpCalculations";
```

- [ ] **Step 2: Widen the full-width check**

`App.tsx` line 44 currently reads:

```tsx
  const singleColumn = useLocation().pathname === "/activity";
```

The help page is prose with no file to select beside it, so it needs the same
full-width treatment. Replace with:

```tsx
  // Both are single full-width views with no left-hand tree to sit beside:
  // /activity is one long list, and the help pages are prose.
  const pathname = useLocation().pathname;
  const singleColumn = pathname === "/activity" || pathname.startsWith("/help/");
```

- [ ] **Step 3: Add the route**

In the `<Routes>` block (line 54), after the `/activity` route, add:

```tsx
          <Route path="/help/calculations" element={<HelpCalculations />} />
```

- [ ] **Step 4: Typecheck**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose run --rm --no-deps -v /Users/syntheticgio/Programming/local-bio-pipeliner/.claude/worktrees/read-quality-scoring-3a4b60/frontend/src:/srv/src web npx tsc --noEmit
```

Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/App.tsx
git commit -m "feat: route for the calculations help page"
```

---

## Task 7: The Help dropdown

**Files:**
- Modify: `frontend/src/components/Header.tsx`

`File`, `View`, and `Help` are currently inert placeholder buttons
(`Header.tsx:8`) with no dropdown machinery anywhere in the app. This builds the
first real one. `File` and `View` stay placeholders.

- [ ] **Step 1: Replace the header**

Rewrite `frontend/src/components/Header.tsx` in full:

```tsx
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { formatBytes } from "../lib/format";
import { LoadIndicator } from "./LoadIndicator";

/** Still awaiting real actions. Help is implemented separately below. */
const MENUS = ["File", "View"];

/** Destinations that exist. Without these, /search and /activity are
 *  reachable only by typing the URL. */
const LINKS: { to: string; label: string; title: string }[] = [
  { to: "/search", label: "Search", title: "Search files by metadata" },
  { to: "/activity", label: "Activity", title: "Running and queued jobs" },
];

/** Help menu contents. One entry today; the shape is the point. */
const HELP_ITEMS: { to: string; label: string }[] = [
  { to: "/help/calculations", label: "BioFlow Calculations" },
];

export function Header() {
  const { data } = useQuery({
    queryKey: ["system", "stats"],
    queryFn: api.systemStats,
    refetchInterval: 15000,
  });

  const navigate = useNavigate();
  const [helpOpen, setHelpOpen] = useState(false);
  const helpRef = useRef<HTMLDivElement>(null);

  // A menu that only closes by re-clicking its button feels broken, so handle
  // the two things people actually do: click elsewhere, or press Escape.
  useEffect(() => {
    if (!helpOpen) return;

    function onPointerDown(e: MouseEvent) {
      if (helpRef.current && !helpRef.current.contains(e.target as Node)) {
        setHelpOpen(false);
      }
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setHelpOpen(false);
    }

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [helpOpen]);

  return (
    <header className="header">
      {/* The brand is the conventional way back to the file explorer, and it
          is the only one from a full-width view like /activity. */}
      <Link to="/" className="header-brand" title="Back to projects">
        <span className="header-logo">B</span>
        <span>BioFlow</span>
      </Link>

      <nav className="header-menu">
        {LINKS.map((l) => (
          <NavLink
            key={l.to}
            to={l.to}
            title={l.title}
            className={({ isActive }) => (isActive ? "active" : undefined)}
          >
            {l.label}
          </NavLink>
        ))}
        {MENUS.map((m) => (
          <button key={m} type="button" title={`${m} menu (not yet implemented)`}>
            {m}
          </button>
        ))}

        <div className="header-dropdown" ref={helpRef}>
          <button
            type="button"
            aria-haspopup="menu"
            aria-expanded={helpOpen}
            onClick={() => setHelpOpen((v) => !v)}
          >
            Help
          </button>
          {helpOpen && (
            <div className="header-dropdown-menu" role="menu">
              {HELP_ITEMS.map((item) => (
                <button
                  key={item.to}
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setHelpOpen(false);
                    navigate(item.to);
                  }}
                >
                  {item.label}
                </button>
              ))}
            </div>
          )}
        </div>
      </nav>

      <div className="header-right">
        <LoadIndicator />
        {/* Library size rather than free space: under Docker Desktop the
            container cannot see the external drive's real capacity, and a
            confidently wrong "192 GB free" is worse than not saying. This we
            can count exactly. */}
        {data && (
          <div
            className="load-indicator"
            title={`${data.counts.objects} files at ${data.storage.path}`}
          >
            <span>{formatBytes(data.storage.library_bytes)} stored</span>
          </div>
        )}
      </div>
    </header>
  );
}
```

- [ ] **Step 2: Add the dropdown styles**

Append to `frontend/src/styles.css`:

```css
/* Header dropdown ----------------------------------------------------- */

.header-dropdown {
  position: relative;
}

.header-dropdown-menu {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  z-index: 50;
  min-width: 200px;
  padding: 4px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: 0 8px 24px rgb(0 0 0 / 35%);
}

.header-dropdown-menu button {
  display: block;
  width: 100%;
  padding: 7px 10px;
  text-align: left;
  font-size: 13px;
  color: var(--text);
  border-radius: 4px;
}

.header-dropdown-menu button:hover {
  background: var(--bg-hover);
}
```

- [ ] **Step 3: Typecheck**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose run --rm --no-deps -v /Users/syntheticgio/Programming/local-bio-pipeliner/.claude/worktrees/read-quality-scoring-3a4b60/frontend/src:/srv/src web npx tsc --noEmit
```

Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/Header.tsx frontend/src/styles.css
git commit -m "feat: Help dropdown linking to BioFlow Calculations"
```

---

## Task 8: Verify in a browser

The only step that can catch a wrong color or a clipped dropdown. Per CLAUDE.md
there is no component-test substitute for this.

- [ ] **Step 1: Re-run the unit tests**

```bash
wtest src/lib/readQuality.test.ts
```

Expected: PASS, 11 passed.

- [ ] **Step 2: Start a throwaway stack on this worktree**

The shared `biopipe` stack serves main, so it will not show these changes.
Start a separate one with its own project name:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner/.claude/worktrees/read-quality-scoring-3a4b60 && docker compose -p biopipe-readqual up -d --build web
```

Find the published port:

```bash
docker compose -p biopipe-readqual port web 5173
```

- [ ] **Step 3: Check the explorer**

Open the project containing `DRR1066343_1.fastq`. Confirm:

- A dot sits on the corner of that file's 📄 icon.
- Its row subtitle reads `<size> · FASTQ · Good`.
- Hovering the dot alone shows `Good (4/5) — Q30 92.1%`, the duplication
  caveat, and the "Set Assay under Metadata" line.
- BAMs, the reference FASTA, and index sidecars have no dot and no word.

- [ ] **Step 4: Check the detail panel**

Select that file. Confirm `Good` appears at the end of the subtitle line after
the organism, above the QC/Metadata/Actions tabs, with the same tooltip.

- [ ] **Step 5: Check both themes**

Toggle your OS between light and dark. Confirm the dot stays visible against
the row in both, including while the row is selected and hovered.

- [ ] **Step 6: Check the Help menu**

Click **Help**. Confirm the dropdown opens, closes on Escape, closes on an
outside click, and that **BioFlow Calculations** navigates to a full-width page
whose threshold table matches Task 1's constants.

- [ ] **Step 7: Tear down**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner/.claude/worktrees/read-quality-scoring-3a4b60 && docker compose -p biopipe-readqual down
```

- [ ] **Step 8: Commit any fixes**

Only if steps 3–6 required changes:

```bash
git add -A && git commit -m "fix: read quality display corrections from manual verification"
```

---

## Done when

- `wtest src/lib/readQuality.test.ts` passes all 11 cases.
- `DRR1066343_1.fastq` reads **Good** in the list and the detail panel, and its
  icon carries a dimmed-green dot whose tooltip gives `Good (4/5) — Q30 92.1%`.
- Non-read and still-ingesting files show no badge and no word.
- **Help → BioFlow Calculations** reaches a page documenting the thresholds,
  the three demotions, the duplication caveat, and the no-grade state.
- `tsc --noEmit` is clean.
