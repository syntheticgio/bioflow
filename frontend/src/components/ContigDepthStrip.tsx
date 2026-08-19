import type { ContigCoverage } from "../api/types";
import { InfoMarker } from "./InfoMarker";
import { baselineDepth, depthRatio, formatRatio, shadeFor } from "../lib/depthShade";

/** Matches ChromosomeStrip's geometry so the two read as the same object seen
 *  two ways -- a reference's sequences, and those sequences carrying depth. */
const MAX_BAR_H = 72;
const MIN_BAR_H = 8;
const BAR_W = 11;
const BAR_GAP = 7;
const LABEL_BAND_H = 18;

/** Past this the bars are too narrow to compare and the caption band is a
 *  smear. `bam_stats_contigs_top` is already capped at 50 upstream; this is
 *  the readability cap on top of that. */
const MAX_BARS = 24;

/**
 * The alignment's contigs as proportional bars, shaded by depth relative to
 * the genome mean.
 *
 * This is `BirdsEyeCoverageChart`'s data asked a different question. That
 * chart lays the whole reference end to end on one axis, which answers "where
 * are the gaps" but makes chromosome-to-chromosome comparison a matter of
 * tracking position along a strip. Here each contig is its own bar, so the
 * comparisons that matter clinically are adjacent: a chromosome at half the
 * depth of its neighbours (aneuploidy, or a sex chromosome at the expected
 * dosage), an arm at zero (a dropout or a reference/sample mismatch), one at
 * double depth (a duplication).
 *
 * **Everything here comes from the BAM, nothing from the reference object.**
 * `bam_stats_contigs_top` carries name, length, and mean depth together, all
 * three derived from one `samtools coverage` pass, so the lengths are the
 * BAM header's own -- the same table the depths were measured against. There
 * is no join to a reference FASTA and therefore no contig-name mismatch to
 * detect: the strip cannot be half-painted, because a contig without a depth
 * is not a contig this data knows about. That is why this lives on the
 * alignment's Results tab rather than being a shading option on
 * `ChromosomeStrip`, which reads a reference's `sequence_lengths` and would
 * need exactly that fragile join to paint anything.
 *
 * The baseline is computed here from the contigs rather than taken from
 * `bam_stats_summary.mean_depth` -- see `baselineDepth` for why using the
 * summary's mean makes this chart report a dropout on an ordinary sample.
 */
export function ContigDepthStrip({
  contigs,
  totalContigs,
}: {
  contigs: ContigCoverage[];
  totalContigs?: number;
}) {
  if (!contigs?.length) return null;

  // Over every contig the fact carries, not just the drawn ones: a baseline
  // computed from the longest 24 would shift with the display cap.
  const baseline = baselineDepth(contigs);

  // Ranked by length rather than by the mapped-read order the fact arrives
  // in: a strip is read left to right as descending size, and a depth
  // anomaly is only legible against neighbours of comparable length.
  const ranked = [...contigs].sort((a, b) => b.length - a.length);
  const shown = ranked.slice(0, MAX_BARS);
  const longest = shown[0]?.length || 1;

  const hidden = (totalContigs ?? ranked.length) - shown.length;

  return (
    <div className="qc-chart">
      <div className="section-title">
        Depth by chromosome
        <InfoMarker metric="ui.chart_contig_depth_strip" />
      </div>

      <svg
        className="chrom-strip"
        width={shown.length * (BAR_W + BAR_GAP)}
        height={MAX_BAR_H + LABEL_BAND_H}
        role="group"
        aria-label="Contigs in this alignment, shaded by depth"
      >
        {shown.map((c, i) => {
          const h = Math.max(MIN_BAR_H, (c.length / longest) * MAX_BAR_H);
          const ratio = depthRatio(c.mean_depth, baseline);
          const shade = shadeFor(ratio);
          const x = i * (BAR_W + BAR_GAP);
          const described =
            ratio == null
              ? `${c.contig} · ${c.mean_depth.toFixed(1)}×`
              : `${c.contig} · ${c.mean_depth.toFixed(1)}× · ${formatRatio(ratio)}`;
          return (
            <g key={c.contig} className={`depth-bar is-${shade.kind}`}>
              <title>{described}</title>
              <rect
                x={x}
                y={MAX_BAR_H - h}
                width={BAR_W}
                height={h}
                rx={BAR_W / 2}
                // Intensity carries the magnitude, the class carries the
                // direction, so the two colours stay themeable in CSS rather
                // than being hardcoded here.
                opacity={shade.kind === "neutral" || shade.kind === "unknown" ? 1 : 0.35 + 0.65 * shade.t}
              />
              <text
                className="chrom-bar-label"
                x={x + BAR_W / 2}
                y={MAX_BAR_H + 12}
                textAnchor="middle"
              >
                {shortName(c.contig)}
              </text>
            </g>
          );
        })}
      </svg>

      <DepthLegend baseline={baseline} />

      {hidden > 0 && (
        <div className="chrom-note">
          Longest {shown.length} of {(totalContigs ?? ranked.length).toLocaleString()} contigs.
          The full table is below.
        </div>
      )}

      {baseline == null ? (
        // Not a blank strip and not a mismatch message -- the bars are real,
        // there is simply no depth to read them against. Bar height is
        // sequence length either way, so the strip still says something.
        <div className="chrom-note">
          Nothing aligned to these contigs, so there is no typical depth to
          shade against. Bar height is sequence length.
        </div>
      ) : null}
    </div>
  );
}

/** The scale, stated rather than implied: a diverging shade means nothing
 *  without knowing which end is which and where it saturates. */
function DepthLegend({ baseline }: { baseline: number | null }) {
  if (baseline == null) return null;
  return (
    <div className="depth-legend">
      <span className="depth-swatch is-low" aria-hidden="true" />
      <span>≤0.5×</span>
      <span className="depth-swatch is-neutral" aria-hidden="true" />
      <span>{baseline.toFixed(1)}× typical</span>
      <span className="depth-swatch is-high" aria-hidden="true" />
      <span>≥2×</span>
    </div>
  );
}

/** Contig names run from `1` to `NC_000001.11` to a 40-character scaffold
 *  ID against an 18px pitch. Kept to the tail rather than the front: names
 *  that collide usually do so at the front (`scaffold_1`, `scaffold_2`). */
function shortName(name: string): string {
  const bare = name.trim().replace(/\.\d+$/, "");
  const m = /^[A-Za-z]{2}_?0*(\d+)$/.exec(bare);
  if (m) return m[1];
  const stripped = bare.replace(/^chr/i, "");
  if (stripped.length <= 5) return stripped;
  return `…${bare.slice(-4)}`;
}
