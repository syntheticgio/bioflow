import type { TrimReport as Report } from "../api/types";

/**
 * What trimming actually did to a file.
 *
 * fastp reports the same measurements before and after in one pass, so this is
 * a genuine comparison rather than two separate runs -- which is why the
 * default path needs no FastQC. The point is the delta: whether reads were
 * lost, whether quality improved, how much adapter was there.
 */
export function TrimReport({ facts }: { facts: Record<string, unknown> }) {
  const report = facts.trim_report as Report | undefined;
  if (!report?.before || !report?.after) return null;

  const { before, after } = report;
  const readsLost = delta(before.total_reads, after.total_reads);
  const outputs = Array.isArray(facts.trim_outputs)
    ? (facts.trim_outputs as string[])
    : [];

  return (
    <div className="section">
      <div className="section-title">Trimming</div>

      <table className="trim-table">
        <thead>
          <tr>
            <th />
            <th>Before</th>
            <th>After</th>
            <th>Change</th>
          </tr>
        </thead>
        <tbody>
          <Row
            label="Reads"
            before={before.total_reads}
            after={after.total_reads}
            format={count}
          />
          <Row
            label="Bases"
            before={before.total_bases}
            after={after.total_bases}
            format={count}
          />
          <Row
            label="Mean length"
            before={before.read1_mean_length}
            after={after.read1_mean_length}
            format={(v) => `${count(v)} bp`}
          />
          <Row
            label="Q20"
            before={before.q20_rate}
            after={after.q20_rate}
            format={pct}
            // Higher is better, so an increase is the good direction.
            goodWhenUp
          />
          <Row
            label="Q30"
            before={before.q30_rate}
            after={after.q30_rate}
            format={pct}
            goodWhenUp
          />
          <Row
            label="GC"
            before={before.gc_content}
            after={after.gc_content}
            format={pct}
            neutral
          />
        </tbody>
      </table>

      <dl className="kv" style={{ marginTop: 10 }}>
        {report.adapters?.trimmed_reads != null && (
          <>
            <dt>Adapters trimmed</dt>
            <dd>
              {count(report.adapters.trimmed_reads)} reads
              {report.adapters.trimmed_bases != null && (
                <span style={{ color: "var(--text-faint)" }}>
                  {" "}
                  ({count(report.adapters.trimmed_bases)} bases)
                </span>
              )}
            </dd>
          </>
        )}

        {report.adapters?.read1_sequence && (
          <>
            <dt>Adapter found</dt>
            <dd className="mono" style={{ fontSize: 11, wordBreak: "break-all" }}>
              {report.adapters.read1_sequence}
            </dd>
          </>
        )}

        {readsLost != null && readsLost !== 0 && (
          <>
            <dt>Reads discarded</dt>
            <dd>
              {count(Math.abs(readsLost))}
              {discardBreakdown(report)}
            </dd>
          </>
        )}

        {report.duplication_rate != null && (
          <>
            <dt>Duplication</dt>
            <dd>{pct(report.duplication_rate)}</dd>
          </>
        )}

        {report.insert_size_peak ? (
          <>
            <dt>Insert size</dt>
            <dd>{count(report.insert_size_peak)} bp (peak)</dd>
          </>
        ) : null}

        <dt>Tool</dt>
        <dd>
          {report.tool} {report.tool_version}
        </dd>

        {outputs.length > 0 && (
          <>
            <dt>Produced</dt>
            <dd>
              {outputs.length} trimmed {outputs.length === 1 ? "file" : "files"}
            </dd>
          </>
        )}
      </dl>
    </div>
  );
}

function Row({
  label,
  before,
  after,
  format,
  goodWhenUp,
  neutral,
}: {
  label: string;
  before: number | null;
  after: number | null;
  format: (v: number | null) => string;
  goodWhenUp?: boolean;
  neutral?: boolean;
}) {
  if (before == null && after == null) return null;
  const d = delta(before, after);

  let colour: string | undefined;
  if (!neutral && d != null && d !== 0 && before) {
    const improved = goodWhenUp ? d > 0 : d < 0;
    // Only flag a change worth noticing. Losing 0.1% of reads to quality
    // filtering is the tool working, not a problem.
    const relative = Math.abs(d / before);
    if (relative > 0.02) colour = improved ? "var(--success)" : "var(--warn)";
  }

  return (
    <tr>
      <th>{label}</th>
      <td>{format(before)}</td>
      <td>{format(after)}</td>
      <td style={{ color: colour ?? "var(--text-faint)" }}>
        {d == null || d === 0 ? "—" : formatDelta(d, before, format)}
      </td>
    </tr>
  );
}

function delta(before: number | null, after: number | null): number | null {
  if (before == null || after == null) return null;
  return after - before;
}

function formatDelta(
  d: number,
  before: number | null,
  format: (v: number | null) => string,
): string {
  const sign = d > 0 ? "+" : "";
  if (before) {
    const relative = (d / before) * 100;
    // A percentage is the readable form for a big absolute change; a raw
    // count is clearer for a small one.
    if (Math.abs(relative) >= 0.1) return `${sign}${relative.toFixed(1)}%`;
  }
  return `${sign}${format(d)}`;
}

/** Why reads went away, when fastp said. */
function discardBreakdown(report: Report): string {
  const f = report.filtering;
  const parts: string[] = [];
  if (f.low_quality_reads) parts.push(`${count(f.low_quality_reads)} low quality`);
  if (f.too_short_reads) parts.push(`${count(f.too_short_reads)} too short`);
  if (f.too_many_n_reads) parts.push(`${count(f.too_many_n_reads)} too many N`);
  return parts.length ? ` — ${parts.join(", ")}` : "";
}

function count(v: number | null): string {
  return v == null ? "—" : v.toLocaleString();
}

function pct(v: number | null): string {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}
