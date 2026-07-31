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
