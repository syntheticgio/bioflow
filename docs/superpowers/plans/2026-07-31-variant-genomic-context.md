# Variant Genomic Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Context" button to each variant row that opens the already-embedded NCBI Sequence Viewer at that variant's position with a labeled marker.

**Architecture:** Two pure helpers (`focusWindow`, `markerLabel`) go in `frontend/src/lib/chromosomes.ts` beside the existing `isNcbiNucleotideAccession`, unit-tested with vitest. `SequenceViewerModal` gains one optional `focus` prop that appends NCBI's `v=` and `mk=` parameters to the load string it already builds — when the prop is absent the string is byte-identical to today's, so `ChromosomeStrip` is unaffected by construction. `VariantTable` adds a gated column and holds the selected variant in state. No backend, pipeline, or API change.

**Tech Stack:** React 18 + TypeScript, vitest (`frontend/src/lib/*.test.ts`), NCBI Sequence Viewer embedding API (already loaded by `SequenceViewerModal`).

**Spec:** `docs/superpowers/specs/2026-07-31-variant-genomic-context-design.md`

**Run tests with:** `cd frontend && npx vitest run` — the frontend suite runs on the host, unlike the backend suite which needs the `api` container.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `frontend/src/lib/chromosomes.ts` | Modify | Add `focusWindow` + `markerLabel` — pure coordinate/string logic, no NCBI lifecycle |
| `frontend/src/lib/chromosomes.test.ts` | Modify | Unit tests for both helpers |
| `frontend/src/components/SequenceViewerModal.tsx` | Modify | Optional `focus` prop → `v=`/`mk=` params |
| `frontend/src/styles.css` | Modify | One rule for the position in the viewer heading |
| `frontend/src/components/VariantTable.tsx` | Modify | Gated `Context` column + modal state |

---

### Task 1: `focusWindow` helper

**Files:**
- Modify: `frontend/src/lib/chromosomes.ts`
- Test: `frontend/src/lib/chromosomes.test.ts`

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/lib/chromosomes.test.ts`. Add `focusWindow` to the existing import from `./chromosomes` at the top of the file.

```ts
describe("focusWindow", () => {
  // A 10 kb virus: 1% is 100 bases, so the 2 kb floor takes over -- a 4 kb
  // span rather than a 200 b one that would show nothing around the variant.
  it("applies the floor on a small viral genome", () => {
    expect(focusWindow(5_000, 10_000)).toEqual([3_000, 7_000]);
  });

  // Smaller than one full window: clamping at both ends yields the whole
  // sequence, which is the right answer for a tiny genome.
  it("shows the whole sequence when it is shorter than the window", () => {
    expect(focusWindow(1_500, 3_000)).toEqual([1, 3_000]);
  });

  // A 250 Mb plant chromosome: 1% is 2.5 Mb, so the 200 kb ceiling applies
  // and the view stays readable instead of becoming a smear.
  it("applies the ceiling on a large chromosome", () => {
    expect(focusWindow(100_000_000, 250_000_000)).toEqual([
      99_800_000, 100_200_000,
    ]);
  });

  // A 5 Mb bacterial genome: 1% is 50 kb, between floor and ceiling.
  it("uses one percent of length between the bounds", () => {
    expect(focusWindow(2_500_000, 5_000_000)).toEqual([2_450_000, 2_550_000]);
  });

  it("clamps to the start of the sequence", () => {
    expect(focusWindow(100, 5_000_000)).toEqual([1, 50_100]);
  });

  it("clamps to the end of the sequence", () => {
    expect(focusWindow(4_999_900, 5_000_000)).toEqual([4_949_900, 5_000_000]);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run src/lib/chromosomes.test.ts`
Expected: FAIL — `focusWindow is not exported by ./chromosomes` (or `is not a function`).

- [ ] **Step 3: Implement `focusWindow`**

Add to `frontend/src/lib/chromosomes.ts`, directly below `isNcbiNucleotideAccession`:

```ts
/** Fraction of a sequence's length shown to each side of a focused position. */
const FOCUS_FRACTION = 0.01;
/** Smallest half-window, so a short viral genome does not zoom to a near-empty
 *  view. */
const FOCUS_MIN_HALF = 2_000;
/** Largest half-window, so a plant chromosome does not become an unreadable
 *  smear. */
const FOCUS_MAX_HALF = 200_000;

/**
 * The visible range to show around a focused position, as [start, end], 1-based
 * and inclusive.
 *
 * Scaled rather than fixed because references here run from viruses to plants
 * -- four orders of magnitude. A constant flank that frames a gene in a plant
 * genome is the entire genome of a virus, and one that suits a virus crops a
 * plant gene to a fragment.
 *
 * The fraction and both bounds are judgment, not measurement: they are starting
 * points chosen to degrade sensibly at both ends of that range. Tune them if
 * they read wrong in practice.
 */
export function focusWindow(
  position: number,
  sequenceLength: number,
): [number, number] {
  const half = Math.min(
    FOCUS_MAX_HALF,
    Math.max(FOCUS_MIN_HALF, Math.round(sequenceLength * FOCUS_FRACTION)),
  );
  return [
    Math.max(1, position - half),
    Math.min(sequenceLength, position + half),
  ];
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/lib/chromosomes.test.ts`
Expected: PASS — all 5 new `focusWindow` tests green, existing `classifyChromosomes` tests still green.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/chromosomes.ts frontend/src/lib/chromosomes.test.ts
git commit -m "feat: add focusWindow helper for variant view ranges"
```

---

### Task 2: `markerLabel` helper

**Files:**
- Modify: `frontend/src/lib/chromosomes.ts`
- Test: `frontend/src/lib/chromosomes.test.ts`

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/lib/chromosomes.test.ts`. Add `markerLabel` to the existing import from `./chromosomes`.

```ts
describe("markerLabel", () => {
  it("formats a simple SNV", () => {
    expect(markerLabel("G", "A")).toBe("G-to-A");
  });

  // `|` separates fields inside NCBI's mk parameter, so an allele containing
  // one would silently corrupt the marker spec.
  it("strips characters that would break the mk parameter", () => {
    expect(markerLabel("<DEL>", "A|B")).toBe("DEL-to-AB");
  });

  // Indel alleles run to kilobases; the marker label is not where that belongs.
  it("truncates long indel alleles", () => {
    expect(markerLabel("A".repeat(40), "T")).toBe(`${"A".repeat(12)}-to-T`);
  });

  it("falls back when sanitising empties both alleles", () => {
    expect(markerLabel("|", "*")).toBe("variant");
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run src/lib/chromosomes.test.ts`
Expected: FAIL — `markerLabel is not exported by ./chromosomes`.

- [ ] **Step 3: Implement `markerLabel`**

Add to `frontend/src/lib/chromosomes.ts`, below `focusWindow`:

```ts
/** Longest allele fragment kept in a marker label. */
const LABEL_ALLELE_MAX = 12;

/**
 * A marker name for one variant, safe to interpolate into NCBI's `mk`
 * parameter.
 *
 * NCBI warns that special characters in marker names "must be escaped
 * properly", and `|` is the separator between position, name and colour within
 * `mk` -- so an allele carrying one would corrupt the spec rather than just
 * look wrong. VCF also permits symbolic alleles such as `<DEL>` and `*`.
 * Rather than escape a moving target, reduce the label to plain ASCII.
 */
export function markerLabel(ref: string, alt: string): string {
  const clean = (allele: string) =>
    allele.replace(/[^A-Za-z0-9]/g, "").slice(0, LABEL_ALLELE_MAX);
  const r = clean(ref);
  const a = clean(alt);
  if (!r && !a) return "variant";
  return `${r || "?"}-to-${a || "?"}`;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/lib/chromosomes.test.ts`
Expected: PASS — all 4 new `markerLabel` tests green.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/chromosomes.ts frontend/src/lib/chromosomes.test.ts
git commit -m "feat: add markerLabel helper for NCBI mk parameter"
```

---

### Task 3: `focus` prop on the viewer modal

**Files:**
- Modify: `frontend/src/components/SequenceViewerModal.tsx`

No test in this task: the modal drives NCBI's global `SeqView` object and there is no headless component-testing setup in this repo (per CLAUDE.md). The logic worth testing was extracted into Tasks 1–2. Verification here is manual, in Task 5.

- [ ] **Step 1: Add the `focus` type and prop**

In `frontend/src/components/SequenceViewerModal.tsx`, add this interface directly above the `SequenceViewerModal` function (below the `mountSeq` declaration):

```ts
/** A specific position to open on, rather than the whole sequence.
 *
 *  `sequenceLength` is optional because a contig length is not always known;
 *  without it the view range is omitted and NCBI shows the whole sequence,
 *  which is the correct fallback rather than a special case. */
export interface ViewerFocus {
  position: number;
  label: string;
  sequenceLength?: number;
}
```

Then change the component signature from:

```ts
export function SequenceViewerModal({
  accession,
  onClose,
}: {
  accession: string;
  onClose: () => void;
}) {
```

to:

```ts
export function SequenceViewerModal({
  accession,
  focus,
  onClose,
}: {
  accession: string;
  focus?: ViewerFocus;
  onClose: () => void;
}) {
```

- [ ] **Step 2: Build the focus parameters into the load string**

Add the import for `markerLabel`'s siblings at the top of the file — change:

```ts
import { accessionUrl } from "../lib/format";
```

to:

```ts
import { accessionUrl } from "../lib/format";
import { focusWindow } from "../lib/chromosomes";
```

In the load effect, replace this existing `app.load(...)` call:

```ts
    app.load(
      `embedded=true&appname=${VIEWER_APPNAME}` +
        `&id=${encodeURIComponent(accession)}&tracks=[key:gene_model_track]`,
    );
```

with:

```ts
    // Appended only when focused, so the unfocused string stays byte-identical
    // to what ChromosomeStrip has always sent.
    let focusParams = "";
    if (focus) {
      if (focus.sequenceLength != null) {
        const [start, end] = focusWindow(focus.position, focus.sequenceLength);
        focusParams += `&v=${start}:${end}`;
      }
      focusParams += `&mk=${focus.position}|${encodeURIComponent(focus.label)}|ff5555`;
    }

    app.load(
      `embedded=true&appname=${VIEWER_APPNAME}` +
        `&id=${encodeURIComponent(accession)}&tracks=[key:gene_model_track]` +
        focusParams,
    );
```

- [ ] **Step 3: Add the focus fields to the effect dependencies**

So that clicking a second variant reloads the existing instance rather than leaving the first view up. The effect already tears down and rebuilds the host div in its cleanup, so no new teardown logic is needed.

Depend on the *primitive fields*, not on `focus` itself. `VariantTable` builds the `focus` object inline in JSX, so it is a fresh object on every render — depending on its identity would reload the NCBI viewer on every parent re-render, including every keystroke in the table's filter inputs.

Change:

```ts
  }, [state, accession, divId]);
```

to:

```ts
    // Primitives, not `focus`: the parent rebuilds that object literal every
    // render, so depending on its identity would re-load the viewer on every
    // keystroke in the table's filters.
  }, [
    state,
    accession,
    divId,
    focus?.position,
    focus?.label,
    focus?.sequenceLength,
  ]);
```

- [ ] **Step 4: Show the position in the heading when focused**

Change:

```ts
        <h2>
          {accession}
```

to:

```ts
        <h2>
          {accession}
          {focus && (
            <span className="sviewer-position">
              {" "}
              @ {focus.position.toLocaleString()}
            </span>
          )}
```

- [ ] **Step 5: Add the `.sviewer-position` style**

This class does not exist yet. Add it to `frontend/src/styles.css` directly after the existing `.sviewer-external` rule (which ends at line 2877):

```css
/* De-emphasised beside the accession in the viewer heading: the accession
   names the sequence, the position only says where in it we opened. */
.sviewer-position {
  font-size: 12px;
  font-weight: 400;
  color: var(--text-faint);
}
```

- [ ] **Step 6: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/SequenceViewerModal.tsx frontend/src/styles.css
git commit -m "feat: let the sequence viewer open on a focused position"
```

---

### Task 4: `Context` column in the variants table

**Files:**
- Modify: `frontend/src/components/VariantTable.tsx`

- [ ] **Step 1: Add imports and modal state**

At the top of `frontend/src/components/VariantTable.tsx`, add to the existing imports:

```ts
import { isNcbiNucleotideAccession, markerLabel } from "../lib/chromosomes";
import { SequenceViewerModal } from "./SequenceViewerModal";
import type { VariantRow } from "../api/types";
```

Note: `VariantContigRow` is already imported from `../api/types`; add `VariantRow` to that existing import rather than writing a second import line.

Inside the component, alongside the other `useState` calls (near `const [page, setPage] = useState(0);`):

```ts
  // The variant whose genomic context is open, or null for none.
  const [contextRow, setContextRow] = useState<VariantRow | null>(null);
```

- [ ] **Step 2: Build the contig length lookup**

`VariantTable` already receives `contigs: VariantContigRow[]`, and that type already carries `length` — no new plumbing. Add after the state declarations:

```ts
  // Contig -> length, for scaling the viewer's window to the sequence.
  // Memoised so the lookup is not rebuilt on every keystroke in the filters.
  const contigLengths = useMemo(
    () => new Map(contigs.map((c) => [c.contig, c.length])),
    [contigs],
  );
```

`useMemo` is not currently imported in this file. Change the first import line from:

```ts
import { useEffect, useState } from "react";
```

to:

```ts
import { useEffect, useMemo, useState } from "react";
```

- [ ] **Step 3: Add the header cell**

Change:

```tsx
                <th>Genotype</th>
              </tr>
```

to:

```tsx
                <th>Genotype</th>
                <th />
              </tr>
```

- [ ] **Step 4: Add the gated body cell**

Change:

```tsx
                  <td className="mono">{genotypeFor(row.gt, sampleIdx)}</td>
                </tr>
```

to:

```tsx
                  <td className="mono">{genotypeFor(row.gt, sampleIdx)}</td>
                  <td>
                    {/* Variants are called against whatever reference was
                        aligned to, often a local assembly whose contigs have
                        no page at NCBI. No button beats a button that opens a
                        viewer which then fails. */}
                    {isNcbiNucleotideAccession(row.chrom) && (
                      <button
                        type="button"
                        className="btn"
                        style={{ padding: "1px 8px", fontSize: 11 }}
                        onClick={() => setContextRow(row)}
                      >
                        Context
                      </button>
                    )}
                  </td>
                </tr>
```

- [ ] **Step 5: Render the modal**

Change the closing of the component from:

```tsx
        </>
      )}
    </div>
  );
}
```

to:

```tsx
        </>
      )}

      {contextRow && (
        <SequenceViewerModal
          accession={contextRow.chrom}
          focus={{
            position: contextRow.pos,
            label: markerLabel(contextRow.ref, contextRow.alt),
            sequenceLength: contigLengths.get(contextRow.chrom),
          }}
          onClose={() => setContextRow(null)}
        />
      )}
    </div>
  );
}
```

- [ ] **Step 6: Verify it compiles and tests still pass**

Run: `cd frontend && npx tsc --noEmit && npx vitest run`
Expected: no type errors; all tests pass.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/VariantTable.tsx
git commit -m "feat: add genomic context button to variant rows"
```

---

### Task 5: Manual verification

**Files:** none — this is the actual verification step for UI work in this repo (per CLAUDE.md).

- [ ] **Step 1: Rebuild the running stack**

Must run from the main repo root, never a worktree — the Compose bind mounts are relative paths, and running from a worktree silently repoints the shared stack at that branch.

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose up -d --build api web worker
```

- [ ] **Step 2: Confirm the stack is serving the main tree**

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

Expected: no path contains `.claude/worktrees/`. If one does, re-run Step 1 from the main repo root.

- [ ] **Step 3: Check the four behaviours**

At http://localhost:5173, open a project with a called VCF and go to the Variants tab.

1. A variant on an `NC_`/`NZ_` accession shows a **Context** button; clicking it opens the viewer with a red marker on the right base, zoomed to a window rather than the whole sequence.
2. A variant on a local contig (e.g. `contig_47`) shows **no** button.
3. Clicking a second variant's Context moves the view rather than stacking a second viewer.
4. The Quality tab's chromosome strip still opens its whole-chromosome viewer unchanged — no marker, no zoom.

- [ ] **Step 4: Commit any fixes**

If a behaviour is wrong, fix it and commit before proceeding. If all four pass, nothing to commit here.

---

## Notes for the implementer

**No backend work.** Everything needed is already client-side. If you find yourself editing anything under `backend/`, stop — that is a sign of a misread requirement.

**The `v=` fallback is deliberate.** When a contig length is unknown, `v=` is omitted and NCBI shows the whole sequence with the marker still correctly placed. That is the designed degraded path, not a bug to fix.

**Do not add a flanking-window control.** It was considered and cut in the spec: a knob nobody would tune.

**Do not add a disabled state to the gated button.** The cell is empty for non-NCBI contigs on purpose. A disabled control invites a click and then explains why it was pointless.
