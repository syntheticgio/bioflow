interface Props {
  facts: Record<string, unknown>;
  formatKind: string;
  /** The record's own metadata, for the archive identifiers it carries. */
  metadata?: Record<string, unknown>;
}

/** Which NCBI database each identifier belongs to. */
type ArchiveKind = "sra" | "bioproject" | "biosample";

const ARCHIVE_URL: Record<ArchiveKind, (id: string) => string> = {
  sra: (id) => `https://www.ncbi.nlm.nih.gov/sra/${id}`,
  bioproject: (id) => `https://www.ncbi.nlm.nih.gov/bioproject/${id}`,
  biosample: (id) => `https://www.ncbi.nlm.nih.gov/biosample/${id}`,
};

function ArchiveLink({ id, kind }: { id: string; kind: ArchiveKind }) {
  return (
    <a
      href={ARCHIVE_URL[kind](id)}
      target="_blank"
      rel="noreferrer noopener"
      style={{ color: "var(--accent)" }}
    >
      {id} ↗
    </a>
  );
}

/**
 * SRA provenance: which accession was used, where it came from, and where NCBI
 * disagrees with a value the user set.
 *
 * Conflicts are shown rather than resolved. Public records contain mistakes,
 * and a user who corrected one should not have that correction quietly
 * reverted — so we report the difference and leave the decision to them.
 */
export function SraPanel({ facts, formatKind, metadata = {} }: Props) {
  const accession = facts.sra_accession as string | undefined;
  const source = facts.sra_source as string | undefined;
  const applied = (facts.sra_fields_applied as string[] | undefined) ?? [];
  const conflicts =
    (facts.sra_conflicts as { key: string; yours: unknown; sra: unknown }[] | undefined) ??
    [];
  const error = facts.sra_error as string | undefined;

  const isSequenceFile = formatKind === "fastq" || formatKind === "fasta";
  if (!accession && !error && !isSequenceFile) return null;

  const id = (key: string): string | null => {
    const v = metadata[key];
    return typeof v === "string" && v.trim() ? v.trim() : null;
  };

  // Sample and study share a row: they are the same kind of pointer at
  // different scopes, and one line each would stretch this list past the
  // things worth scanning it for.
  const archiveRows = (
    [
      { label: "SRA experiment", ids: [["sra_experiment", "sra"] as const] },
      {
        label: "SRA sample / study",
        ids: [["sra_sample", "sra"] as const, ["sra_study", "sra"] as const],
      },
      { label: "BioProject", ids: [["bioproject", "bioproject"] as const] },
      { label: "BioSample", ids: [["biosample", "biosample"] as const] },
    ] as const
  )
    .map(({ label, ids }) => ({
      label,
      entries: ids
        .map(([key, kind]) => {
          const value = id(key);
          return value ? { id: value, kind: kind as ArchiveKind } : null;
        })
        .filter((e): e is { id: string; kind: ArchiveKind } => e !== null),
    }))
    // A row with nothing in it says less than no row at all.
    .filter((r) => r.entries.length > 0);

  return (
    <div className="section">
      <div className="section-title">Public archive</div>

      {accession ? (
        <>
          <dl className="kv">
            <dt>SRA run</dt>
            <dd>
              <ArchiveLink id={accession} kind="sra" />
            </dd>

            {/* The archive identifiers the record carries. Read-only here and
                editable under Record → Archive: the same value in two places
                would be two sources of truth for one field, so the editable
                form owns them and this states what they currently are. */}
            {archiveRows.map(({ label, entries }) => (
              <span key={label} style={{ display: "contents" }}>
                <dt>{label}</dt>
                <dd>
                  {entries.map((e, i) => (
                    <span key={e.id}>
                      {i > 0 && " · "}
                      <ArchiveLink id={e.id} kind={e.kind} />
                    </span>
                  ))}
                </dd>
              </span>
            ))}

            <dt>Detected from</dt>
            <dd>
              {source === "metadata" ? "Accession you entered" : "Filename"}
              {applied.length > 0 && (
                <>
                  {" · filled "}
                  {applied.map((k) => (
                    <span key={k} className="chip">
                      {k}
                    </span>
                  ))}
                </>
              )}
            </dd>
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
