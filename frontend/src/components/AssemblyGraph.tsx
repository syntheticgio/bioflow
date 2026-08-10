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

import { useEffect, useMemo, useRef } from "react";
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

  // A caller that builds `segments`/`links` inline (e.g. mapping over
  // fetched data on every render) hands us a new array identity each time
  // with identical content. Array identity is not a reliable signal that
  // the graph changed, and rebuilding cytoscape -- including the
  // 400-iteration cose layout -- is expensive, so the effect below keys off
  // this cheap length-based signature instead of the arrays themselves.
  const signature = useMemo(
    () => `${segments.length}:${links.length}`,
    [segments, links],
  );

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
            color: "#888",
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
    // Gated on the cheap `signature` above rather than `segments`/`links`
    // directly; see the comment at the signature's definition.
  }, [signature]);

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
