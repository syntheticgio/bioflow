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

  if (!hasAnything) {
    return (
      <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
        No assembly facts extracted yet.
      </div>
    );
  }

  return (
    <div>
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
            {(showAllContigs ? names : names.slice(0, MAX_VISIBLE_CONTIGS)).map(
              (n) => (
                <span key={n}>{n}</span>
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
