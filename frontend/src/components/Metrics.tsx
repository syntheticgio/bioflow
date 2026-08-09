import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { JobTypeMetrics, MetricSummary, MetricsStats } from "../api/types";
import { formatBytes, formatDuration } from "../lib/format";
import { FileHeadlineStats } from "./FileHeadline";
import type { Stat } from "./FileHeadline";

/**
 * The Reference → Metrics page: what BioFlow's computations have cost.
 *
 * One row per job type, read from the computation records every finished job
 * leaves behind. Two facts about the numbers shape the page:
 *
 *  * Durations, memory, input sizes and read counts summarize the most recent
 *    *successful* runs of each type (the same window the predictive models
 *    fit, so a failure can never drag a median into looking like a fast,
 *    cheap run). Run counts, by contrast, cover every recorded run --
 *    failures included, because a metrics page that hid them would be a
 *    status page for a rosier app.
 *  * Any number can be absent: memory below the 60s sampling floor, read
 *    counts before pipelines started recording them, everything for a job
 *    type nobody has run yet. Absence renders as an em-dash, never as 0 --
 *    a null is the lack of a measurement, not a measurement of nothing.
 */

const DASH = "—";

/** A summary's median as text, or the dash when there is no measurement. */
function med(s: MetricSummary | undefined, f: (n: number) => string): string {
  return s && s.median != null ? f(s.median) : DASH;
}

/** A summary's p90 as text, or the dash when there is no measurement. */
function p90(s: MetricSummary | undefined, f: (n: number) => string): string {
  return s && s.p90 != null ? f(s.p90) : DASH;
}

function totalRuns(row: JobTypeMetrics): number {
  return Object.values(row.outcomes).reduce((a, b) => a + (b ?? 0), 0);
}

/** The binary a type's runs used most often, "name version". */
function toolName(row: JobTypeMetrics): string {
  const top = row.tools[0];
  if (!top || !top.name) return DASH;
  return top.version ? `${top.name} ${top.version}` : top.name;
}

export function Metrics() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["jobs", "metrics"],
    queryFn: api.metrics,
  });

  if (isLoading) {
    return (
      <div className="help-page">
        <p className="help-intro">Loading…</p>
      </div>
    );
  }
  if (isError || !data) {
    return (
      <div className="help-page">
        <p className="help-intro">Couldn't load metrics.</p>
      </div>
    );
  }
  return <MetricsBody data={data} />;
}

function MetricsBody({ data }: { data: MetricsStats }) {
  const { totals, types, min_samples, resource_floor_ms } = data;

  const recorded = Object.values(totals).reduce((a, b) => a + (b ?? 0), 0);
  const succeeded = totals.succeeded ?? 0;
  const failed = totals.failed ?? 0;

  const stats: Stat[] = [
    { label: "Recorded runs", value: recorded.toLocaleString(), lead: true },
    { label: "Successful", value: succeeded.toLocaleString() },
    { label: "Failed", value: failed.toLocaleString() },
    { label: "Job types", value: types.length.toLocaleString() },
  ];

  const rows = [...types].sort(
    (a, b) => totalRuns(b) - totalRuns(a) || a.job_type.localeCompare(b.job_type),
  );

  return (
    <div className="help-page">
      <h1>Metrics</h1>
      <p className="help-intro">
        What BioFlow's computations have cost — how long they took, how much
        memory they used, how big the inputs were — recorded from every run.
      </p>

      <section className="help-section">
        <h2>Overview</h2>
        <FileHeadlineStats stats={stats} />
        <p>
          Duration, memory, input-size and read-count numbers describe the most
          recent successful runs of each job type (at most {min_samples} each);
          run counts cover every recorded run, failures included. Memory is
          only sampled for runs of {formatDuration(resource_floor_ms)} or more
          — a shorter run has no peak to report, so it shows as {DASH}, not
          zero.
        </p>
      </section>

      <section className="help-section">
        <h2>By job type</h2>
        <table className="help-table">
          <thead>
            <tr>
              <th style={{ textAlign: "left" }}>Job type</th>
              <th>Runs</th>
              <th>Median duration</th>
              <th>P90 duration</th>
              <th>Median input</th>
              <th>Median peak memory</th>
              <th>Median reads</th>
              <th style={{ textAlign: "left" }}>Most-used tool</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.job_type}>
                <td className="mono" style={{ textAlign: "left" }}>
                  {row.job_type}
                </td>
                <td>
                  {(row.outcomes.succeeded ?? 0).toLocaleString()}
                  {(row.outcomes.failed ?? 0) > 0 && (
                    <span className="stat-note">
                      {" "}
                      +{(row.outcomes.failed ?? 0).toLocaleString()} failed
                    </span>
                  )}
                </td>
                <td className="mono">{med(row.duration_ms, formatDuration)}</td>
                <td className="mono">{p90(row.duration_ms, formatDuration)}</td>
                <td className="mono">{med(row.input_bytes, formatBytes)}</td>
                <td className="mono">{med(row.peak_rss_bytes, formatBytes)}</td>
                <td className="mono">
                  {med(row.read_count, (n) => n.toLocaleString())}
                </td>
                <td style={{ textAlign: "left" }}>{toolName(row)}</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={8} style={{ color: "var(--text-secondary)" }}>
                  No runs recorded yet — this page fills in as jobs complete.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}
