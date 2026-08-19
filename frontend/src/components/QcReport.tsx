import { api } from "../api/client";
import { InfoMarker } from "./InfoMarker";
import { LongReadDistributions } from "./LongReadCharts";
import type { QcFacts } from "../api/types";
import type { JSX } from "react";

/**
 * A report name that opens the same new-tab, noopener, CSP-sandboxed link
 * whether the click lands on the text or the trailing ↗ -- the icon is only
 * there so the row reads as "leaves the app" at a glance, not a second,
 * different destination. See the untrusted-content comment where this is
 * used: these pages embed raw sequence data, so they stay out of an iframe
 * in the app's own document.
 */
function ReportLink({
  href,
  children,
}: {
  href: string;
  children: React.ReactNode;
}) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="report-link"
    >
      {children}
      <span className="report-link-icon" aria-hidden="true">
        ↗
      </span>
    </a>
  );
}

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
 * Two shapes of facts land here, chosen by which tool actually ran rather
 * than by platform: fastp/FastQC for short reads, NanoPlot for long reads
 * (Nanopore/PacBio) -- see `run_qc`'s platform dispatch on the backend. They
 * measure different things (a per-base curve makes no sense for a file whose
 * reads run from 200bp to 100kb), so this renders a different block for
 * each rather than forcing NanoPlot's numbers through fastp's layout.
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
  if (!qc.qc_tool) return null;

  if (qc.qc_before_filtering) {
    return <ShortReadQcReport qc={qc} objectId={objectId} />;
  }
  if (qc.qc_read_length_n50 != null) {
    return <LongReadQcReport qc={qc} objectId={objectId} />;
  }
  return null;
}

function ShortReadQcReport({
  qc,
  objectId,
}: {
  qc: QcFacts;
  objectId: string;
}) {
  const measured = qc.qc_before_filtering!;
  const fastqcPath = qc.qc_fastqc_report;
  const fastpPath = qc.qc_fastp_report;

  return (
    <div className="section">
      <div className="section-title">Quality control</div>

      <dl className="kv">
        <dt>
          Reads
          <InfoMarker metric="ui.qc_total_reads" />
        </dt>
        <dd>{count(measured.total_reads)}</dd>

        <dt>
          Bases
          <InfoMarker metric="ui.qc_total_bases" />
        </dt>
        <dd>{count(measured.total_bases)}</dd>

        {measured.read1_mean_length != null && (
          <>
            <dt>
              Mean length
              <InfoMarker metric="ui.qc_mean_length" />
            </dt>
            <dd>{count(measured.read1_mean_length)} bp</dd>
          </>
        )}

        <dt>
          Q20
          <InfoMarker metric="ui.qc_q20" />
        </dt>
        <dd>{quality(measured.q20_rate, 0.9)}</dd>

        <dt>
          Q30
          <InfoMarker metric="ui.qc_q30" />
        </dt>
        <dd>{quality(measured.q30_rate, 0.8)}</dd>

        <dt>
          GC
          <InfoMarker metric="ui.qc_gc" />
        </dt>
        <dd>{pct(measured.gc_content)}</dd>

        {/* The whole-file scan's number wins over fastp's when it exists:
            fastp reports duplication from its own sampled estimate, while
            `qc_percent_unique` comes from a full pass with FastQC's
            frozen-dictionary correction applied. fastp's value stays in the
            facts for provenance -- it is a real measurement -- but showing
            both would put two methods' answers side by side, disagreeing, on
            the same screen. */}
        {(qc.qc_percent_unique != null || qc.qc_duplication_rate != null) && (
          <>
            <dt>
              Duplication
              <InfoMarker metric="ui.qc_duplication" />
            </dt>
            {/* Inverted: a high duplication rate is the bad direction, where a
                high Q30 is the good one. */}
            <dd>
              {quality(
                qc.qc_percent_unique != null
                  ? 1 - qc.qc_percent_unique / 100
                  : qc.qc_duplication_rate,
                0.3,
                { goodWhenLow: true },
              )}
            </dd>
          </>
        )}

        {qc.qc_insert_size_peak ? (
          <>
            <dt>
              Insert size
              <InfoMarker metric="ui.qc_insert_size_peak" />
            </dt>
            <dd>{count(qc.qc_insert_size_peak)} bp (peak)</dd>
          </>
        ) : null}

        {qc.qc_adapters?.read1_sequence && (
          <>
            <dt>
              Adapter detected
              <InfoMarker metric="ui.qc_adapter" />
            </dt>
            <dd
              className="mono"
              style={{ fontSize: 11, wordBreak: "break-all" }}
            >
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
                <ReportLink href={api.qcReportUrl(objectId, fastqcPath)}>
                  FastQC
                </ReportLink>
              )}
              {fastqcPath && fastpPath && " · "}
              {fastpPath && (
                <ReportLink href={api.qcReportUrl(objectId, fastpPath)}>
                  fastp
                </ReportLink>
              )}
            </dd>
          </>
        )}
      </dl>
    </div>
  );
}

/**
 * N50 leads, the way Q30 leads the short-read block: it is the number a
 * long-read run is actually judged by, not an also-ran statistic.
 */
function LongReadQcReport({ qc, objectId }: { qc: QcFacts; objectId: string }) {
  const nanoplotPath = qc.qc_nanoplot_report;

  return (
    <div className="section">
      <div className="section-title">Quality control</div>

      <dl className="kv">
        <dt>
          N50
          <InfoMarker metric="ui.qc_read_length_n50" />
        </dt>
        <dd>{count(qc.qc_read_length_n50)} bp</dd>

        <dt>
          Reads
          <InfoMarker metric="ui.qc_total_reads" />
        </dt>
        <dd>{count(qc.qc_total_reads)}</dd>

        <dt>
          Bases
          <InfoMarker metric="ui.qc_total_bases" />
        </dt>
        <dd>{count(qc.qc_total_bases)}</dd>

        {qc.qc_mean_read_length != null && (
          <>
            <dt>
              Mean length
              <InfoMarker metric="ui.qc_mean_length" />
            </dt>
            <dd>{count(qc.qc_mean_read_length)} bp</dd>
          </>
        )}

        {qc.qc_median_read_length != null && (
          <>
            <dt>
              Median length
              <InfoMarker metric="ui.qc_median_read_length" />
            </dt>
            <dd>{count(qc.qc_median_read_length)} bp</dd>
          </>
        )}

        {/* The spread matters as much as the averages for long reads: a wide
            one is the normal shape of a Nanopore run, not a fault. */}
        {qc.qc_read_length_stdev != null && (
          <>
            <dt>
              Length std. dev.
              <InfoMarker metric="ui.qc_read_length_stdev" />
            </dt>
            <dd>{count(qc.qc_read_length_stdev)} bp</dd>
          </>
        )}

        {qc.qc_mean_quality != null && (
          <>
            <dt>
              Mean quality
              <InfoMarker metric="ui.qc_mean_quality" />
            </dt>
            <dd>Q{count(qc.qc_mean_quality)}</dd>
          </>
        )}

        {qc.qc_median_quality != null && (
          <>
            <dt>
              Median quality
              <InfoMarker metric="ui.qc_median_quality" />
            </dt>
            <dd>Q{count(qc.qc_median_quality)}</dd>
          </>
        )}

        {qc.qc_read_chemistry && (
          <>
            <dt>
              Chemistry
              <InfoMarker metric="ui.qc_read_chemistry" />
            </dt>
            <dd>
              {qc.qc_read_chemistry}
              {qc.qc_read_chemistry_reason && (
                <span style={{ color: "var(--text-dim)" }}>
                  {" "}
                  — {qc.qc_read_chemistry_reason}
                </span>
              )}
            </dd>
          </>
        )}

        {qc.qc_platform && (
          <>
            <dt>
              Platform
              <InfoMarker metric="ui.qc_platform" />
            </dt>
            <dd>{qc.qc_platform}</dd>
          </>
        )}

        {/* Only worth a row when it is not the ordinary outcome -- "ok" on
            every file is a row that never says anything. */}
        {qc.qc_status && qc.qc_status !== "ok" && (
          <>
            <dt>
              Status
              <InfoMarker metric="ui.qc_status" />
            </dt>
            <dd>{qc.qc_status}</dd>
          </>
        )}

        <dt>Tool</dt>
        <dd>
          {qc.qc_tool} {qc.qc_tool_version}
        </dd>

        {nanoplotPath && (
          <>
            <dt>Reports</dt>
            <dd>
              {/* Same untrusted-content treatment as the fastp/FastQC links:
                  new tab, noopener, CSP-sandboxed on the server. */}
              <ReportLink href={api.qcReportUrl(objectId, nanoplotPath)}>
                NanoPlot
              </ReportLink>
            </dd>
          </>
        )}
      </dl>

      {/* The distributions behind the scalars above -- N50 is one number off
          the length histogram, and the mean quality is one number off the
          density grid. Inside this section rather than a sibling so the
          numbers and the shapes they summarise stay together; renders
          nothing at all for a file QC'd before those facts existed. */}
      <LongReadDistributions qc={qc} />
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
    <span style={{ color: poor ? "var(--warn)" : undefined }}>
      {pct(value)}
    </span>
  );
}

function count(v: number | null | undefined): string {
  return v == null ? "—" : v.toLocaleString();
}

function pct(v: number | null | undefined): string {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}
