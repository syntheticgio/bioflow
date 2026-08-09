import { useEffect, useMemo, useRef, useState } from "react";
import type { TileMatrix } from "../api/types";

/** Above this many cells, one <rect> per cell is too many DOM nodes and the
 *  chart draws to a canvas instead. A NovaSeq matrix is 200,000+; the SVG
 *  path is for MiSeq-scale runs where crisp vector cells are nicer. */
const CANVAS_THRESHOLD = 20_000;

type Scale = "absolute" | "relative";

/**
 * Mean quality by flow-cell tile and read position.
 *
 * Rows are tiles in ascending order, which is physical order -- Illumina
 * encodes surface, swath, and position in the tile number, so a smudge
 * covering adjacent tiles reads as one band rather than scattered rows.
 * Sorting by "worst first" would find the bad tile faster and destroy exactly
 * the spatial structure this chart exists to show; `qc_tile_worst` in the
 * facts does that job instead.
 */
export function TileQualityChart({ data }: { data: TileMatrix }) {
  const [scale, setScale] = useState<Scale>("absolute");
  const [hover, setHover] = useState<{ tile: number; pos: number; q: number } | null>(null);

  const rows = data.tiles.length;
  const cols = data.positions;
  const cells = rows * cols;

  // Per-position mean across tiles: the baseline the relative scale compares
  // each cell against. Nulls are skipped rather than counted as zero.
  const positionMeans = useMemo(() => {
    const means: number[] = [];
    for (let p = 0; p < cols; p++) {
      let sum = 0;
      let n = 0;
      for (let t = 0; t < rows; t++) {
        const v = data.matrix[t]?.[p];
        if (v != null) {
          sum += v;
          n += 1;
        }
      }
      means.push(n ? sum / n : 0);
    }
    return means;
  }, [data, rows, cols]);

  const colorFor = useMemo(() => {
    return (q: number | null, p: number): string => {
      if (q == null) return "transparent";
      if (scale === "absolute") return absoluteColor(q);
      return relativeColor(q - positionMeans[p]);
    };
  }, [scale, positionMeans]);

  return (
    <div>
      <div className="tile-scale-toggle">
        <button
          className={scale === "absolute" ? "active" : ""}
          onClick={() => setScale("absolute")}
        >
          Absolute
        </button>
        <button
          className={scale === "relative" ? "active" : ""}
          onClick={() => setScale("relative")}
        >
          Relative
        </button>
      </div>

      {cells > CANVAS_THRESHOLD ? (
        <TileCanvas data={data} colorFor={colorFor} onHover={setHover} />
      ) : (
        <TileSvg data={data} colorFor={colorFor} onHover={setHover} />
      )}

      <div className="tile-readout">
        {hover
          ? `Tile ${hover.tile} · position ${hover.pos} · Q${hover.q.toFixed(1)}`
          : `${rows.toLocaleString()} tiles · ${cols} positions · 1 in ${data.sample_rate} reads sampled`}
      </div>

      {scale === "relative" && (
        /* Load-bearing caption. This scale shows deviation from each
           position's mean, so a dip that hits every tile equally -- a
           fluidics stumble at one cycle -- produces no deviation anywhere and
           renders as a clean plot. Without this line that reads as "nothing
           wrong". */
        <div className="tile-note">
          Showing each tile’s deviation from the average at that position. A dip
          affecting every tile equally will not appear here — check the absolute
          scale for that.
        </div>
      )}

      {data.truncated && (
        <div className="tile-note">
          Showing the first {rows.toLocaleString()} tiles; this file has more.
        </div>
      )}
    </div>
  );
}

/** Absolute Phred on the same thresholds QualityChart uses, so a colour means
 *  the same thing on both charts. */
function absoluteColor(q: number): string {
  if (q >= 30) {
    const t = Math.min((q - 30) / 8, 1);
    return mix([31, 90, 45], [63, 185, 80], t);
  }
  if (q >= 20) return mix([248, 81, 73], [210, 153, 34], (q - 20) / 10);
  return mix([90, 20, 18], [248, 81, 73], Math.max(q / 20, 0));
}

/** Deviation from the position mean. At or above it is a flat cool tone --
 *  deliberately unshowy, so the eye goes only to what is below. */
function relativeColor(delta: number): string {
  if (delta >= -0.5) {
    return mix([30, 42, 58], [74, 158, 255], Math.min(Math.max(delta, 0) / 3, 1) * 0.45);
  }
  return mix([30, 42, 58], [248, 81, 73], Math.min(-delta / 10, 1));
}

function mix(a: number[], b: number[], t: number): string {
  const c = (i: number) => Math.round(a[i] + (b[i] - a[i]) * t);
  return `rgb(${c(0)},${c(1)},${c(2)})`;
}

type CellRenderer = {
  data: TileMatrix;
  colorFor: (q: number | null, p: number) => string;
  onHover: (h: { tile: number; pos: number; q: number } | null) => void;
};

const PAD = { left: 42, bottom: 26, top: 4 };
const PLOT_W = 460;
const MAX_PLOT_H = 320;

/** Row height in pixels, and how many tiles share a row when they outnumber
 *  the pixels available. Binning is unavoidable on a big flow cell -- 1408
 *  rows do not fit in 320px -- so it is done explicitly here rather than left
 *  to the browser's image smoothing. */
function layout(rows: number) {
  const rowH = Math.max(1, Math.min(9, Math.floor(MAX_PLOT_H / rows)));
  const tilesPerRow = Math.max(1, Math.ceil(rows / Math.floor(MAX_PLOT_H / rowH)));
  const drawnRows = Math.ceil(rows / tilesPerRow);
  return { rowH, tilesPerRow, drawnRows, plotH: drawnRows * rowH };
}

function TileSvg({ data, colorFor, onHover }: CellRenderer) {
  const rows = data.tiles.length;
  const cols = data.positions;
  const { rowH, tilesPerRow, plotH } = layout(rows);
  const cellW = PLOT_W / cols;

  const rects: JSX.Element[] = [];
  for (let t = 0; t < rows; t++) {
    const drawnRow = Math.floor(t / tilesPerRow);
    for (let p = 0; p < cols; p++) {
      const q = data.matrix[t]?.[p] ?? null;
      rects.push(
        <rect
          key={`${t}-${p}`}
          x={PAD.left + p * cellW}
          y={PAD.top + drawnRow * rowH}
          width={Math.ceil(cellW)}
          height={rowH}
          fill={colorFor(q, p)}
          onMouseEnter={() =>
            q != null && onHover({ tile: data.tiles[t], pos: p + 1, q })
          }
        />,
      );
    }
  }

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${PAD.left + PLOT_W + 6} ${PAD.top + plotH + PAD.bottom}`}
      style={{ maxWidth: PAD.left + PLOT_W + 6, display: "block" }}
      onMouseLeave={() => onHover(null)}
    >
      {rects}
      <Axes data={data} plotH={plotH} rowH={rowH} tilesPerRow={tilesPerRow} />
    </svg>
  );
}

function TileCanvas({ data, colorFor, onHover }: CellRenderer) {
  const ref = useRef<HTMLCanvasElement>(null);
  const rows = data.tiles.length;
  const cols = data.positions;
  const { rowH, tilesPerRow, drawnRows, plotH } = layout(rows);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = PLOT_W * dpr;
    canvas.height = plotH * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, PLOT_W, plotH);

    const cellW = PLOT_W / cols;
    // One pass, painting each source tile into its binned row. Later tiles in
    // a bin overpaint earlier ones; with a bin of a handful of adjacent tiles
    // that is visually indistinguishable from averaging them, and it keeps
    // this to a single loop over the matrix.
    for (let t = 0; t < rows; t++) {
      const y = PAD.top + Math.floor(t / tilesPerRow) * rowH;
      for (let p = 0; p < cols; p++) {
        const q = data.matrix[t]?.[p] ?? null;
        if (q == null) continue;
        ctx.fillStyle = colorFor(q, p);
        ctx.fillRect(p * cellW, y - PAD.top, Math.ceil(cellW), rowH);
      }
    }
  }, [data, colorFor, rows, cols, rowH, tilesPerRow, plotH]);

  function handleMove(e: React.MouseEvent<HTMLCanvasElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    const p = Math.floor(((e.clientX - rect.left) / rect.width) * cols);
    const drawnRow = Math.floor(((e.clientY - rect.top) / rect.height) * drawnRows);
    // Read from the unbinned data so the tooltip names a tile that exists,
    // even when its row is several tiles wide. Picks the LAST tile in the
    // bin, not the first: the paint loop below writes tiles in ascending
    // order with later ones overpainting earlier ones at the same pixel row,
    // so the last tile is whichever one is actually visible. Picking the
    // first tile here would report a different tile -- and a different Q
    // value -- than what's on screen. If the paint loop's write order ever
    // changes, this must change with it.
    const t = Math.min(drawnRow * tilesPerRow + tilesPerRow - 1, rows - 1);
    const q = data.matrix[t]?.[p];
    if (q == null) {
      onHover(null);
      return;
    }
    onHover({ tile: data.tiles[t], pos: p + 1, q });
  }

  return (
    <div style={{ position: "relative", paddingLeft: PAD.left }}>
      <canvas
        ref={ref}
        style={{ width: PLOT_W, height: plotH, display: "block" }}
        onMouseMove={handleMove}
        onMouseLeave={() => onHover(null)}
      />
    </div>
  );
}

function Axes({
  data,
  plotH,
  rowH,
  tilesPerRow,
}: {
  data: TileMatrix;
  plotH: number;
  rowH: number;
  tilesPerRow: number;
}) {
  const cols = data.positions;
  const step = Math.max(1, Math.round(cols / 5));
  const ticks: JSX.Element[] = [];

  for (let p = 0; p < cols; p += step) {
    ticks.push(
      <text
        key={`x${p}`}
        x={PAD.left + (p / cols) * PLOT_W}
        y={PAD.top + plotH + 14}
        fontSize="9"
        fill="var(--text-faint)"
        textAnchor="middle"
      >
        {p + 1}
      </text>,
    );
  }

  const rowStep = Math.max(1, Math.round(data.tiles.length / tilesPerRow / 4));
  for (let r = 0; r * tilesPerRow < data.tiles.length; r += rowStep) {
    ticks.push(
      <text
        key={`y${r}`}
        x={PAD.left - 6}
        y={PAD.top + r * rowH + 7}
        fontSize="9"
        fill="var(--text-faint)"
        textAnchor="end"
      >
        {data.tiles[r * tilesPerRow]}
      </text>,
    );
  }

  return (
    <>
      {ticks}
      <text
        x={PAD.left + PLOT_W / 2}
        y={PAD.top + plotH + PAD.bottom - 2}
        fontSize="10"
        fill="var(--text-dim)"
        textAnchor="middle"
      >
        Position in read (bp)
      </text>
    </>
  );
}
