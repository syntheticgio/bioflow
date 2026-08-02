import type { AlignmentFacts, BamStatsSummary } from "../api/types";

/**
 * What an alignment actually produced.
 *
 * The four numbers a person checks before trusting a BAM: how much of the data
 * aligned at all, how much aligned as proper pairs, and how much was duplicate.
 * Read from `samtools flagstat` when this app produced the BAM (during
 * indexing, when the file was already being traversed) -- but an imported BAM
 * has no flagstat facts at all, so mapped/unmapped totals fall back to the
 * Results job's genome-wide summary, which every BAM gets once Results has
 * run. Properly-paired and duplicate rates are flagstat-only: bam_stats does
 * not currently compute them, so they are simply absent on an imported BAM
 * rather than approximated.
 */
export function AlignmentReport({ facts }: { facts: Record<string, unknown> }) {
  const f = facts as AlignmentFacts;
  const summary = facts.bam_stats_summary as BamStatsSummary | undefined;

  const totalReads = f.total_reads ?? summaryTotal(summary);
  if (totalReads == null) return null;

  const mappedReads = f.mapped_reads ?? summary?.mapped_reads;
  const mappedPct =
    f.mapped_pct ?? (mappedReads != null ? round1(100 * (mappedReads / totalReads)) : undefined);

  const paired = (f.properly_paired_reads ?? 0) > 0;

  return (
    <div className="section">
      <div className="section-title">Alignment</div>

      <table className="trim-table">
        <tbody>
          <Row label="Reads" value={count(totalReads)} />
          <Row
            label="Mapped"
            value={count(mappedReads)}
            pct={mappedPct}
            // Below this a run is usually wrong rather than merely poor: the
            // wrong reference, the wrong preset for long reads, or untrimmed
            // adapter. Worth flagging rather than leaving as a number to
            // interpret.
            warn={mappedPct != null && mappedPct < 70}
          />
          {paired && (
            <Row
              label="Properly paired"
              value={count(f.properly_paired_reads)}
              pct={f.properly_paired_pct}
              warn={f.properly_paired_pct != null && f.properly_paired_pct < 80}
            />
          )}
          {/* STAR only. Its MAPQ codes carry this directly, and it is the
              number a mean MAPQ was standing in for on that scale -- see
              lib/mapq for why the mean itself is not shown. */}
          {f.uniquely_mapped_percent != null && (
            <Row
              label="Uniquely mapped"
              value="—"
              pct={f.uniquely_mapped_percent}
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

function summaryTotal(summary: BamStatsSummary | undefined): number | undefined {
  if (!summary) return undefined;
  return summary.mapped_reads + summary.unmapped_reads;
}

function round1(n: number): number {
  return Math.round(n * 10) / 10;
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
