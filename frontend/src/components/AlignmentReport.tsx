import type { AlignmentFacts } from "../api/types";

/**
 * What an alignment actually produced.
 *
 * The four numbers a person checks before trusting a BAM: how much of the data
 * aligned at all, how much aligned as proper pairs, and how much was duplicate.
 * Read from `samtools flagstat` during indexing, when the file was already
 * being traversed.
 */
export function AlignmentReport({ facts }: { facts: Record<string, unknown> }) {
  const f = facts as AlignmentFacts;
  if (f.total_reads == null) return null;

  const paired = (f.properly_paired_reads ?? 0) > 0;

  return (
    <div className="section">
      <div className="section-title">Alignment</div>

      <table className="trim-table">
        <tbody>
          <Row label="Reads" value={count(f.total_reads)} />
          <Row
            label="Mapped"
            value={count(f.mapped_reads)}
            pct={f.mapped_pct}
            // Below this a run is usually wrong rather than merely poor: the
            // wrong reference, the wrong preset for long reads, or untrimmed
            // adapter. Worth flagging rather than leaving as a number to
            // interpret.
            warn={f.mapped_pct != null && f.mapped_pct < 70}
          />
          {paired && (
            <Row
              label="Properly paired"
              value={count(f.properly_paired_reads)}
              pct={f.properly_paired_pct}
              warn={f.properly_paired_pct != null && f.properly_paired_pct < 80}
            />
          )}
          {f.duplicate_reads != null && f.duplicate_reads > 0 && (
            <Row
              label="Duplicates"
              value={count(f.duplicate_reads)}
              pct={f.duplicate_pct}
            />
          )}
        </tbody>
      </table>

      {f.aligned_by && (
        <div className="align-provenance">
          {f.aligned_by}
          {f.aligner_version ? ` ${f.aligner_version}` : ""}
        </div>
      )}
    </div>
  );
}

function Row({
  label,
  value,
  pct,
  warn,
}: {
  label: string;
  value: string;
  pct?: number;
  warn?: boolean;
}) {
  return (
    <tr>
      <th>{label}</th>
      <td>{value}</td>
      <td className={warn ? "align-warn" : undefined}>
        {pct != null ? `${pct}%` : ""}
      </td>
    </tr>
  );
}

function count(n: number | undefined): string {
  return n == null ? "—" : n.toLocaleString();
}
