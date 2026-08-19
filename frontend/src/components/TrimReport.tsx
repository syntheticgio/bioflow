import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { TrimReport as Report } from "../api/types";
import { InfoMarker } from "./InfoMarker";
import { QualityOverlayChart } from "./SequenceCharts";

/**
 * What trimming actually did to a file.
 *
 * fastp reports the same measurements before and after in one pass, so this is
 * a genuine comparison rather than two separate runs -- which is why the
 * default path needs no FastQC. The point is the delta: whether reads were
 * lost, whether quality improved, how much adapter was there -- and, from
 * the overlaid per-cycle curves, *where* along the read it happened.
 */
export function TrimReport({
  facts,
  projectId,
}: {
  facts: Record<string, unknown>;
  /** Resolves output ids to names, so the produced files can be linked. */
  projectId?: string;
}) {
  const [, setParams] = useSearchParams();

  const outputs = Array.isArray(facts.trim_outputs)
    ? (facts.trim_outputs as string[])
    : [];

  // Already fetched for the explorer, so naming the outputs costs no extra
  // round trip. Hooks run before the early return below.
  const { data: siblings = [] } = useQuery({
    queryKey: ["objects", projectId],
    queryFn: () => api.listObjects(projectId!),
    enabled: !!projectId && outputs.length > 0,
  });

  const report = facts.trim_report as Report | undefined;
  if (!report?.before || !report?.after) return null;

  const { before, after } = report;
  const readsLost = delta(before.total_reads, after.total_reads);
  const overlay = report.quality_overlay;

  const outputFiles = outputs
    .map((id) => siblings.find((s) => s.id === id))
    .filter((o): o is NonNullable<typeof o> => !!o);

  // Tool and what it produced, as one line under the heading: they are the
  // caption for the comparison rather than two more rows to read.
  const caption = [
    `${report.tool}${report.tool_version ? ` ${report.tool_version}` : ""}`,
    outputs.length > 0
      ? `produced ${outputs.length} trimmed ${outputs.length === 1 ? "file" : "files"}`
      : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="section">
      <div className="section-title">Trimming</div>
      <div className="section-note">{caption}</div>

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

      </dl>

      {/* Where trimming acted, rather than only how much it removed. Absent
          on files trimmed before the curves were persisted (#639), which is
          why this is conditional rather than a chart that would render empty
          for them. */}
      {overlay && overlay.length > 0 && (
        <div className="trim-chart">
          <div className="section-title">
            Quality per position
            <InfoMarker metric="ui.chart_trim_quality_overlay" />
          </div>
          <QualityOverlayChart curve={overlay} />
        </div>
      )}

      {/* The outcome, as sentences under the table: what the comparison cost
          and where the result went. Both are conclusions drawn from the rows
          above, which is why they read as prose rather than more rows. */}
      {(readsLost != null && readsLost !== 0) || outputFiles.length > 0 ? (
        <div className="trim-outcome">
          {readsLost != null && readsLost !== 0 && (
            <div>
              {count(Math.abs(readsLost))} reads discarded
              {discardBreakdown(report, Math.abs(readsLost))}.
            </div>
          )}

          {outputFiles.length > 0 && (
            <div>
              Output:{" "}
              {outputFiles.map((o, i) => (
                <span key={o.id}>
                  {i > 0 && ", "}
                  {/* Selects the output in this same panel, the way the
                      derived-files list does -- staying put beats navigating
                      away from the comparison you were reading. */}
                  <button
                    type="button"
                    className="btn-text"
                    onClick={() => {
                      setParams(
                        (p) => {
                          const next = new URLSearchParams(p);
                          next.set("sel", `object:${o.id}`);
                          return next;
                        },
                        { replace: true },
                      );
                    }}
                  >
                    {o.name}
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>
      ) : null}
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

/**
 * Why reads went away, when the tool said, as a clause continuing "N reads
 * discarded".
 *
 * When one reason accounts for the whole loss it is stated as such -- "all of
 * them too short" says more than repeating the number the sentence just gave.
 */
function discardBreakdown(report: Report, discarded: number): string {
  const f = report.filtering;
  const reasons: { n: number; label: string }[] = [];
  if (f.low_quality_reads) reasons.push({ n: f.low_quality_reads, label: "low quality" });
  if (f.too_short_reads) reasons.push({ n: f.too_short_reads, label: "too short" });
  if (f.too_many_n_reads) reasons.push({ n: f.too_many_n_reads, label: "too many N" });
  if (reasons.length === 0) return "";

  if (reasons.length === 1 && reasons[0].n === discarded) {
    return `, all of them ${reasons[0].label}`;
  }
  return `, ${reasons.map((r) => `${count(r.n)} ${r.label}`).join(", ")}`;
}

function count(v: number | null): string {
  return v == null ? "—" : v.toLocaleString();
}

function pct(v: number | null): string {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}
