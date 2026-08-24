import { useState } from "react";
import { InfoMarker } from "./InfoMarker";
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

/** Room under the bars for the accession-tail labels. */
const LABEL_BAND_H = 18;

/**
 * The part of an accession worth printing under an 11px-wide bar.
 *
 * A full `NC_000001.11` needs roughly 70px against an 18px pitch, so the
 * shared prefix and the version suffix are dropped and the significant digits
 * kept: `NC_000001.11` renders as `1`, `NC_001133.9` as `1133`. The `<title>`
 * and the bar's accessible name still carry the accession in full, so nothing
 * is only available in the abbreviation.
 */
function accessionTail(name: string): string {
  const bare = name.trim().replace(/\.\d+$/, "");
  const m = /^[A-Z]{2}_?0*(\d+)$/.exec(bare);
  if (m) return m[1];
  // Not an accession we can shorten safely -- show the tail rather than a
  // misleading fragment of the front, since names that collide usually do so
  // at the front (scaffold_1, scaffold_2).
  return bare.length > 6 ? `…${bare.slice(-5)}` : bare;
}

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
          Sequence lengths weren’t measured for this file. Re-ingest it to draw
          the chromosome map — the Computations panel has the button.
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
        height={MAX_BAR_H + LABEL_BAND_H}
        role="group"
        aria-label="Chromosomes in this reference"
      >
        {view.bars.map((bar, i) => {
          const h = Math.max(MIN_BAR_H, (bar.length / longest) * MAX_BAR_H);
          const clickable =
            view.linkable && isNcbiNucleotideAccession(bar.name);
          const described = bar.label
            ? `${bar.label} · ${bar.name} · ${formatBases(bar.length)}`
            : `${bar.name} · ${formatBases(bar.length)}`;
          const caption = bar.label ?? accessionTail(bar.name);
          const x = i * (BAR_W + BAR_GAP);
          return (
            <g
              key={bar.name}
              // Only the clickable bars are controls. A bar with no viewer
              // behind it stays plain graphics: unfocusable, and not
              // announced as a button it would do nothing to activate.
              role={clickable ? "button" : undefined}
              tabIndex={clickable ? 0 : undefined}
              aria-label={clickable ? described : undefined}
              className={clickable ? "chrom-bar is-clickable" : "chrom-bar"}
              onClick={clickable ? () => setSelected(bar.name) : undefined}
              onKeyDown={
                clickable
                  ? (e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        // Space would otherwise scroll the panel.
                        e.preventDefault();
                        setSelected(bar.name);
                      }
                    }
                  : undefined
              }
            >
              <title>{described}</title>
              <rect
                x={x}
                y={MAX_BAR_H - h}
                width={BAR_W}
                height={h}
                rx={BAR_W / 2}
              />
              <text
                className="chrom-bar-label"
                x={x + BAR_W / 2}
                y={MAX_BAR_H + 12}
                textAnchor="middle"
              >
                {caption}
              </text>
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
      {view.linkable && (
        <div className="chrom-note">
          Click a chromosome to open it in NCBI's sequence viewer.
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
              {bar.label ? `${bar.label} · ` : ""}
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
      <div className="section-title">
        {title}
        {/* Keyed on the card rather than on `title`, which varies with what
            the file actually holds (chromosomes, scaffolds, contigs). The
            explanation is the same in all three cases. */}
        <InfoMarker metric="ui.chart_chromosome_strip" />
      </div>
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
