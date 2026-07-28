import { api } from "../api/client";
import type { QcFacts } from "../api/types";

/**
 * What QC measured about a file, as it is.
 *
 * Distinct from `TrimReport` in what it can say: a trim reports a before and
 * an after, so its subject is the delta. QC runs with every fastp filter
 * disabled, so there is only one state -- the file as it actually is -- and
 * the useful presentation is the measurements themselves against the
 * thresholds people judge them by.
 *
 * Self-suppressing when the facts are absent, like its neighbours, so a file
 * nobody has run QC on renders nothing rather than an empty section.
 *
 * Deliberately does not repeat the per-base quality and composition curves:
 * `BaseCompositionChart` and `QualityChart` already render those from the same
 * facts blob and are already mounted alongside this in the QC tab.
 */
export function QcReport({
  facts,
  objectId,
}: {
  facts: Record<string, unknown>;
  objectId: string;
}) {
  const qc = facts as QcFacts;
  const measured = qc.qc_before_filtering;
  if (!qc.qc_tool || !measured) return null;

  const fastqcPath = qc.qc_fastqc_report;
  const fastpPath = qc.qc_fastp_report;

  return (
    <div className="section">
      <div className="section-title">Quality control</div>

      <dl className="kv">
        <dt>Reads</dt>
        <dd>{count(measured.total_reads)}</dd>

        <dt>Bases</dt>
        <dd>{count(measured.total_bases)}</dd>

        {measured.read1_mean_length != null && (
          <>
            <dt>Mean length</dt>
            <dd>{count(measured.read1_mean_length)} bp</dd>
          </>
        )}

        <dt>Q20</dt>
        <dd>{quality(measured.q20_rate, 0.9)}</dd>

        <dt>Q30</dt>
        <dd>{quality(measured.q30_rate, 0.8)}</dd>

        <dt>GC</dt>
        <dd>{pct(measured.gc_content)}</dd>

        {qc.qc_duplication_rate != null && (
          <>
            <dt>Duplication</dt>
            {/* Inverted: a high duplication rate is the bad direction, where a
                high Q30 is the good one. */}
            <dd>{quality(qc.qc_duplication_rate, 0.3, { goodWhenLow: true })}</dd>
          </>
        )}

        {qc.qc_insert_size_peak ? (
          <>
            <dt>Insert size</dt>
            <dd>{count(qc.qc_insert_size_peak)} bp (peak)</dd>
          </>
        ) : null}

        {qc.qc_adapters?.read1_sequence && (
          <>
            <dt>Adapter detected</dt>
            <dd className="mono" style={{ fontSize: 11, wordBreak: "break-all" }}>
              {qc.qc_adapters.read1_sequence}
            </dd>
          </>
        )}

        <dt>Tool</dt>
        <dd>
          {qc.qc_tool} {qc.qc_tool_version}
        </dd>

        {(fastqcPath || fastpPath) && (
          <>
            <dt>Reports</dt>
            <dd>
              {/* New tab rather than an inline frame, and noopener besides.
                  These pages embed sequence data verbatim -- FastQC lists
                  overrepresented sequences straight from the reads -- so they
                  are treated as untrusted content and kept out of the
                  application's own document. The server sandboxes them via
                  CSP; this is the second half of that. */}
              {fastqcPath && (
                <a
                  href={api.qcReportUrl(objectId, fastqcPath)}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  FastQC
                </a>
              )}
              {fastqcPath && fastpPath && " · "}
              {fastpPath && (
                <a
                  href={api.qcReportUrl(objectId, fastpPath)}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  fastp
                </a>
              )}
            </dd>
          </>
        )}
      </dl>
    </div>
  );
}

/**
 * A rate, coloured against the threshold below which it is worth a second
 * look. Only the bad direction is flagged: colouring every healthy number
 * green makes the one that is not healthy harder to spot, not easier.
 */
function quality(
  value: number | null | undefined,
  threshold: number,
  { goodWhenLow = false }: { goodWhenLow?: boolean } = {},
): JSX.Element | string {
  if (value == null) return "—";
  const poor = goodWhenLow ? value > threshold : value < threshold;
  return (
    <span style={{ color: poor ? "var(--warn)" : undefined }}>{pct(value)}</span>
  );
}

function count(v: number | null | undefined): string {
  return v == null ? "—" : v.toLocaleString();
}

function pct(v: number | null | undefined): string {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}
