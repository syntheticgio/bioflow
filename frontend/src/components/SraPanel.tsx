interface Props {
  facts: Record<string, unknown>;
  formatKind: string;
}

/**
 * SRA provenance: which accession was used, where it came from, and where NCBI
 * disagrees with a value the user set.
 *
 * Conflicts are shown rather than resolved. Public records contain mistakes,
 * and a user who corrected one should not have that correction quietly
 * reverted — so we report the difference and leave the decision to them.
 */
export function SraPanel({ facts, formatKind }: Props) {
  const accession = facts.sra_accession as string | undefined;
  const source = facts.sra_source as string | undefined;
  const applied = (facts.sra_fields_applied as string[] | undefined) ?? [];
  const conflicts =
    (facts.sra_conflicts as { key: string; yours: unknown; sra: unknown }[] | undefined) ??
    [];
  const error = facts.sra_error as string | undefined;

  const isSequenceFile = formatKind === "fastq" || formatKind === "fasta";
  if (!accession && !error && !isSequenceFile) return null;

  return (
    <div className="section">
      <div className="section-title">SRA / public archive</div>

      {accession ? (
        <>
          <dl className="kv">
            <dt>Accession</dt>
            <dd>
              <a
                href={`https://www.ncbi.nlm.nih.gov/sra/${accession}`}
                target="_blank"
                rel="noreferrer noopener"
                style={{ color: "var(--accent)" }}
              >
                {accession} ↗
              </a>
            </dd>
            <dt>Detected from</dt>
            <dd>
              {source === "metadata" ? "accession you entered" : "filename"}
            </dd>
            {applied.length > 0 && (
              <>
                <dt>Fields filled</dt>
                <dd>
                  {applied.map((k) => (
                    <span key={k} className="chip">
                      {k}
                    </span>
                  ))}
                </dd>
              </>
            )}
          </dl>

          {conflicts.length > 0 && (
            <div className="warn-box" style={{ marginTop: 10 }}>
              <div style={{ marginBottom: 6 }}>
                <strong>NCBI disagrees with {conflicts.length} field(s).</strong> Your
                values were kept.
              </div>
              {conflicts.map((c) => (
                <div key={c.key} style={{ fontSize: 11, marginTop: 3 }}>
                  <span className="mono">{c.key}</span>: yours{" "}
                  <strong>{String(c.yours)}</strong> · SRA{" "}
                  <strong>{String(c.sra)}</strong>
                </div>
              ))}
            </div>
          )}
        </>
      ) : error ? (
        <div className="warn-box">{error}</div>
      ) : (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          No SRA accession detected in the filename. If this file came from SRA,
          enter its run accession under <strong>Archive → SRA run</strong> and click{" "}
          <strong>re-ingest</strong> to pull metadata from NCBI.
        </div>
      )}
    </div>
  );
}
