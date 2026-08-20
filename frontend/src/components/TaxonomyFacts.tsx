import type { TaxonomyFactsData, TaxonomyMismatchData } from "../api/types";

/**
 * What Kraken2/Bracken found when classifying a reads file: the abundance
 * table plus, when the reads disagree with the metadata's stated organism,
 * a warning banner above it.
 *
 * Self-suppressing when the facts are absent, like its neighbours (`QcReport`,
 * `TrimReport`) -- a file nobody has classified renders nothing rather than
 * an empty section.
 */
export function TaxonomyFacts({ facts }: { facts: Record<string, unknown> }) {
  const taxonomy = facts.taxonomy as TaxonomyFactsData | undefined;
  if (!taxonomy || !Array.isArray(taxonomy.taxa)) return null;

  const mismatch = facts.taxonomy_mismatch as TaxonomyMismatchData | undefined;
  const binLabel = facts.bin_taxon_label as string | undefined;
  const binFraction = facts.bin_taxon_fraction as number | undefined;
  const unclassifiedFraction = facts.bin_unclassified_fraction as number | undefined;

  return (
    <div className="section">
      <div className="section-title">Classification</div>

      {mismatch && <MismatchBanner mismatch={mismatch} />}

      {binLabel && (
        <div className="section-note" style={{ marginBottom: 8, fontSize: 13 }}>
          Dominant taxon:{" "}
          <strong>
            {binLabel === "mixed" || binLabel === "unclassified" ? (
              binLabel
            ) : (
              <em>{binLabel}</em>
            )}
          </strong>
          {typeof binFraction === "number" && ` (${pct(binFraction * 100)})`}
          {typeof unclassifiedFraction === "number" &&
            ` · ${pct(unclassifiedFraction * 100)} unclassified`}
        </div>
      )}

      <table className="trim-table">
        <thead>
          <tr>
            <th>Taxon</th>
            <th>Rank</th>
            <th>%</th>
          </tr>
        </thead>
        <tbody>
          {taxonomy.taxa.map((t) => (
            <tr key={`${t.taxid}-${t.rank}`}>
              <td>{t.rank === "S" ? <em>{t.name}</em> : t.name}</td>
              <td>{t.rank}</td>
              <td>{pct(t.pct)}</td>
            </tr>
          ))}
          {/* Shown even at true zero -- an omitted row would read as "not
              measured" rather than "measured, and nothing was
              unclassified". */}
          <tr>
            <td style={{ color: "var(--text-faint)" }}>Unclassified</td>
            <td />
            <td>{pct(taxonomy.unclassified_pct)}</td>
          </tr>
        </tbody>
      </table>

      <div className="section-note">
        {taxonomy.db_key} ·{" "}
        {taxonomy.bracken_used
          ? "abundances refined with Bracken"
          : (taxonomy.bracken_skipped ?? "Bracken did not run")}
      </div>
    </div>
  );
}

function MismatchBanner({ mismatch }: { mismatch: TaxonomyMismatchData }) {
  const [first, ...rest] = mismatch.dominant;
  if (!first) return null;

  return (
    <div className="warn-box" style={{ marginBottom: 8 }}>
      Metadata says <em>{mismatch.claimed}</em>; reads classify as{" "}
      {pct(first.pct)} <em>{first.name}</em>
      {rest.length > 0 && (
        <>
          {" "}
          (also{" "}
          {rest.map((r, i) => (
            <span key={r.name}>
              {i > 0 && ", "}
              {pct(r.pct)} <em>{r.name}</em>
            </span>
          ))}
          )
        </>
      )}
      .
    </div>
  );
}

function pct(v: number): string {
  return `${v.toFixed(1)}%`;
}
