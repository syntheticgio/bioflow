import { useState } from "react";

interface Props {
  facts: Record<string, unknown>;
}

const MAX_VISIBLE_CONTIGS = 25;

/**
 * Curated facts for a reference assembly.
 *
 * The generic FactsTable dumps every parsed key, which buries the four numbers
 * that actually characterize a genome build. This shows those, and nothing
 * else.
 */
export function AssemblyFacts({ facts }: Props) {
  const [showAllContigs, setShowAllContigs] = useState(false);

  const exactCount = facts.sequence_count as number | undefined;
  const estimatedCount = facts.sequence_count_estimate as number | undefined;
  const isExact = facts.sequence_count_exact === true;
  const totalBases = facts.total_bases as number | undefined;
  const gc = facts.gc_content_percent as number | undefined;
  const sampledBases = facts.stats_sampled_bases as number | undefined;
  const names = Array.isArray(facts.sequence_names)
    ? (facts.sequence_names as string[])
    : [];
  const namesTruncated = facts.sequence_names_truncated === true;

  const count = isExact ? exactCount : estimatedCount;
  const hasAnything =
    count !== undefined || totalBases !== undefined || gc !== undefined;

  const ncbiTotal = facts.ncbi_total_length as number | undefined;
  // Sequences, not contigs: a FASTA's records are scaffolds, and the two
  // counts differ sharply for a chromosome-level assembly (12 vs 50 for
  // GCF_000002445.2). Comparing against contigs would invent a divergence.
  const ncbiSequences = facts.ncbi_sequence_count as number | undefined;
  const ncbiGc = facts.ncbi_gc_percent as number | undefined;
  const ncbiName = facts.ncbi_assembly_name as string | undefined;
  const assemblyError = facts.assembly_error as string | undefined;
  const hasNcbi =
    ncbiTotal !== undefined || ncbiSequences !== undefined || ncbiGc !== undefined;

  // A file named for a full assembly that holds one chromosome is a real and
  // easily-missed problem. Compare only when both sides are known.
  const countDiverges =
    count !== undefined && ncbiSequences !== undefined && count !== ncbiSequences;
  const lengthDiverges =
    totalBases !== undefined &&
    ncbiTotal !== undefined &&
    Math.abs(totalBases - ncbiTotal) / ncbiTotal > 0.01;
  const diverges = countDiverges || lengthDiverges;

  if (!hasAnything && !hasNcbi && !assemblyError) {
    return (
      <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
        No assembly facts extracted yet.
      </div>
    );
  }

  return (
    <div>
      {/* Suppressed entirely when nothing was measured, so a file that failed
          to parse shows the published block alone rather than an empty list. */}
      {hasAnything && (
      <dl className="kv">
        {count !== undefined && (
          <>
            <dt>Sequences</dt>
            <dd>
              {isExact ? count.toLocaleString() : `~${count.toLocaleString()}`}
              {!isExact && (
                <span style={{ color: "var(--text-faint)" }}> (estimated)</span>
              )}
            </dd>
          </>
        )}
        {totalBases !== undefined && (
          <>
            <dt>Total bases</dt>
            <dd>{formatBases(totalBases)}</dd>
          </>
        )}
        {gc !== undefined && (
          <>
            {/* Labelled as sampled because fasta_stats caps at 50M bases read
                from the start of the file -- on a large genome that is chr1,
                not a representative sample. See docs/TODO.md. */}
            <dt>GC content (sampled)</dt>
            <dd>
              {gc}%
              {sampledBases !== undefined && (
                <span style={{ color: "var(--text-faint)" }}>
                  {" "}
                  from {formatBases(sampledBases)} sampled
                </span>
              )}
            </dd>
          </>
        )}
      </dl>
      )}

      {names.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div
            style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 6 }}
          >
            Sequences
          </div>
          <div
            className="mono"
            style={{
              fontSize: 12,
              display: "flex",
              flexWrap: "wrap",
              gap: "4px 10px",
            }}
          >
            {/* Indexed key: a malformed or concatenated FASTA can repeat a
                contig name, and duplicate keys would misrender on toggle. */}
            {(showAllContigs ? names : names.slice(0, MAX_VISIBLE_CONTIGS)).map(
              (n, i) => (
                <span key={`${i}-${n}`}>{n}</span>
              ),
            )}
          </div>
          {names.length > MAX_VISIBLE_CONTIGS && (
            <button
              type="button"
              className="btn"
              style={{ marginTop: 8 }}
              onClick={() => setShowAllContigs(!showAllContigs)}
            >
              {showAllContigs ? "Show fewer" : `Show all ${names.length}`}
            </button>
          )}
          {namesTruncated && (
            <div
              style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}
            >
              List truncated during parsing; the assembly has more sequences
              than are recorded here.
            </div>
          )}
        </div>
      )}

      {hasNcbi && (
        <div style={{ marginTop: 14 }}>
          <div
            style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 6 }}
          >
            Published assembly (NCBI){ncbiName ? ` · ${ncbiName}` : ""}
          </div>
          <dl className="kv">
            {ncbiSequences !== undefined && (
              <>
                <dt>Sequences</dt>
                <dd>{ncbiSequences.toLocaleString()}</dd>
              </>
            )}
            {ncbiTotal !== undefined && (
              <>
                <dt>Total bases</dt>
                <dd>{formatBases(ncbiTotal)}</dd>
              </>
            )}
            {ncbiGc !== undefined && (
              <>
                <dt>GC content</dt>
                <dd>{ncbiGc}%</dd>
              </>
            )}
          </dl>
          {diverges && (
            <div className="warn-box" style={{ marginTop: 8 }}>
              This file{" "}
              {count !== undefined && <>has {count.toLocaleString()} sequences</>}
              {count !== undefined && totalBases !== undefined && " "}
              {totalBases !== undefined && <>totalling {formatBases(totalBases)}</>};
              the published assembly has{" "}
              {ncbiSequences !== undefined && (
                <>{ncbiSequences.toLocaleString()} sequences</>
              )}
              {ncbiSequences !== undefined && ncbiTotal !== undefined && " "}
              {ncbiTotal !== undefined && <>totalling {formatBases(ncbiTotal)}</>}. It
              may be a subset, or a different patch level.
            </div>
          )}
        </div>
      )}

      {assemblyError && (
        <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 10 }}>
          NCBI lookup: {assemblyError}
        </div>
      )}
    </div>
  );
}

/** Base counts read better in Gb/Mb than as raw digits. */
function formatBases(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} Gb`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} Mb`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} kb`;
  return `${n} bp`;
}
