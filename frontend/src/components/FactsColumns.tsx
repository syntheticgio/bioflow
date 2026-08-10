import { useLayoutEffect, useRef, useState } from "react";

// Same selector CSS gives break-inside: avoid -- these are the indivisible
// blocks the browser will pack into columns.
const BLOCK_SELECTOR = ":scope > .facts-group, :scope > .section";

/**
 * The tallest column height a sequential two-column fill would produce.
 *
 * This mirrors what `column-fill: auto` does with break-inside: avoid
 * blocks: walk them in order, packing each into the current column until it
 * would overflow, then start the next column. Simulating it here (rather
 * than picking half the total) is what guarantees the real layout below
 * lands in exactly two columns -- half the total can split a lumpy set of
 * block heights into three (see the note on FactsColumns), because nothing
 * guarantees any prefix of the blocks sums to close to half.
 */
function twoColumnFillHeight(heights: number[]): number {
  const total = heights.reduce((sum, h) => sum + h, 0);
  if (heights.length === 0) return 0;

  // The minimum viable column height is the tallest single block -- no
  // column can be shorter than the block that must fit inside it. Binary
  // search that lower bound up to the full total (one column holding
  // everything) for the smallest height whose greedy fill uses <= 2 columns.
  let lo = Math.max(...heights);
  let hi = total;

  const columnsNeeded = (limit: number) => {
    let columns = 1;
    let used = 0;
    for (const h of heights) {
      if (used > 0 && used + h > limit) {
        columns += 1;
        used = 0;
      }
      used += h;
    }
    return columns;
  };

  while (lo < hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (columnsNeeded(mid) <= 2) {
      hi = mid;
    } else {
      lo = mid + 1;
    }
  }
  return lo;
}

/**
 * Wraps a `.facts-columns` CSS multi-column layout with a measured height, so
 * the browser fills column one before spilling into column two instead of
 * balancing total content between them.
 *
 * CSS multi-column defaults to `column-fill: balance`, which computes an
 * ideal column height to even out total content and starts column two
 * wherever that height lands -- not at the top of the next group. A short
 * group can land alone at the top of column two while a tall one fills all
 * of column one, leaving the *next* group in column two well below its
 * sibling in column one (#133). `column-fill: auto` fixes that by filling
 * sequentially, but only works with a constrained height -- with `height:
 * auto` (the default for a block that isn't told otherwise), the spec falls
 * back to balance again.
 *
 * The naive fix is half the natural single-column height. That is wrong: if
 * no prefix of the break-inside: avoid blocks sums close to half, greedy
 * fill against a half-height target can overflow a *third* column rather
 * than stopping at two -- CSS multicol makes room for overflow by adding
 * columns outside the box, not by clipping or growing one column tall. This
 * measures each block individually and computes the tallest column a real
 * two-column greedy fill would produce (see twoColumnFillHeight), so the
 * height handed to `column-fill: auto` always resolves to exactly two
 * columns.
 */
export function FactsColumns({ children }: { children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState<number>();

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;

    const measure = () => {
      // Block heights read the same regardless of column-count, since each
      // block is break-inside: avoid and column width doesn't change block
      // height in this layout (each block is a fixed-width card). Clearing
      // the custom property first avoids feeding back a stale height from
      // the previous measurement into this one.
      el.style.removeProperty("--facts-columns-height");
      const blocks = el.querySelectorAll<HTMLElement>(BLOCK_SELECTOR);
      // getBoundingClientRect() is the border box -- it excludes the block's
      // own margin, but margin-bottom (.facts-group, .section) is exactly
      // what reserves the vertical gap the next block in the same column
      // sits in. Leaving it out understates how much room each block really
      // takes in the flow, which understated the computed height enough to
      // spill a third column even though the greedy simulation "fit" in two.
      const heights = [...blocks].map((b) => {
        const cs = getComputedStyle(b);
        return (
          b.getBoundingClientRect().height +
          parseFloat(cs.marginTop) +
          parseFloat(cs.marginBottom)
        );
      });
      setHeight(twoColumnFillHeight(heights));
    };

    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [children]);

  return (
    <div
      ref={ref}
      className="facts-columns"
      style={
        height != null
          ? ({ "--facts-columns-height": `${height}px` } as React.CSSProperties)
          : undefined
      }
    >
      {children}
    </div>
  );
}
