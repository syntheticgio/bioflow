import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type {
  JobRun,
  JobTypeMetrics,
  MetricSummary,
  MetricsStats,
} from "../api/types";
import { formatBytes, formatDuration } from "../lib/format";
import { FileHeadlineStats } from "./FileHeadline";
import type { Stat } from "./FileHeadline";
import { InfoMarker } from "./InfoMarker";

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

/** A run's tool as "name version", or the dash when unrecorded. */
function runTool(run: JobRun): string {
  if (!run.tool) return DASH;
  return run.tool_version ? `${run.tool} ${run.tool_version}` : run.tool;
}

/** An optional measurement as text, or the dash. Never renders 0 for null. */
function opt<T>(v: T | null, f: (x: T) => string): string {
  return v == null ? DASH : f(v);
}

/**
 * One job type's runs, newest first.
 *
 * Failures are listed alongside successes, which makes this table
 * deliberately inconsistent with the medians in the left column -- those read
 * successful runs only, so a failure cannot make a job type look fast and
 * cheap. Both are correct for their own question, and the outcome column is
 * what keeps the difference legible.
 */
export function RunTable({ runs }: { runs: JobRun[] }) {
  if (runs.length === 0) {
    return <p className="run-table-empty">No runs recorded yet.</p>;
  }
  return (
    <table className="help-table run-table">
      <thead>
        <tr>
          <th>Finished</th>
          <th>Outcome</th>
          <th>Duration</th>
          <th>Input</th>
          <th>Peak memory</th>
          <th>Tool</th>
        </tr>
      </thead>
      <tbody>
        {/* Keyed by position, not by job_id or finished_at. Both look like
            better keys and neither is unique here: job_id is null on rows
            recorded before it was stored, and batch-recorded runs share a
            finished_at to the second (eight ingest_headers rows sit on one
            timestamp in a real library). The list is a plain ordered window
            that is only ever replaced wholesale, never reordered or edited
            in place, so the index is stable for as long as a row lives. */}
        {runs.map((run, i) => (
          <tr key={i}>
            <td className="mono">
              {opt(run.finished_at, (s) => new Date(s).toLocaleString())}
            </td>
            <td>
              <span className={`run-outcome run-outcome-${run.outcome}`}>
                {run.outcome}
              </span>
            </td>
            <td className="mono">{formatDuration(run.duration_ms)}</td>
            <td className="mono">{formatBytes(run.input_bytes)}</td>
            <td className="mono">{opt(run.peak_rss_bytes, formatBytes)}</td>
            <td>{runTool(run)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * The right column: one table per job type, most recent runs first.
 *
 * Every type is drawn on load rather than behind a selection, so the column
 * answers "what has each job type been doing" without a click. One request
 * serves them all -- see the endpoint's own note on why.
 */
function RecentRunsColumn() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["jobs", "metrics", "runs"],
    queryFn: api.metricsRuns,
  });

  if (isLoading) return <p className="help-intro">Loading runs…</p>;
  if (isError || !data) {
    return <p className="help-intro">Couldn't load runs.</p>;
  }

  const types = Object.keys(data.by_type).sort(
    (a, b) =>
      data.by_type[b].total - data.by_type[a].total || a.localeCompare(b),
  );

  return (
    <section className="help-section">
      <h2>
        Recent runs <InfoMarker metric="ui.metrics_recent_runs" />
      </h2>
      {types.length === 0 && (
        <p className="run-table-empty">
          No runs recorded yet — this fills in as jobs complete.
        </p>
      )}
      {types.map((jobType) => {
        const entry = data.by_type[jobType];
        return (
          <div className="run-group" key={jobType}>
            <div className="run-group-head">
              <h3 className="mono">{jobType}</h3>
              {entry.total > entry.runs.length && (
                <Link className="run-see-more" to={`/metrics/${jobType}`}>
                  See all {entry.total.toLocaleString()} →
                </Link>
              )}
            </div>
            <RunTable runs={entry.runs} />
          </div>
        );
      })}
    </section>
  );
}

export function Metrics() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["jobs", "metrics"],
    queryFn: api.metrics,
  });

  if (isLoading) {
    return (
      <div className="metrics-page metrics-page-single">
        <p className="help-intro">Loading…</p>
      </div>
    );
  }
  if (isError || !data) {
    return (
      <div className="metrics-page metrics-page-single">
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
    <div className="metrics-page">
      <div className="metrics-overview">
        <h1>
          Metrics <InfoMarker metric="ui.metrics_overview" />
        </h1>
      <p className="help-intro">
        What BioFlow's computations have cost — how long they took, how much
        memory they used, how big the inputs were — recorded from every run.
      </p>

      <section className="help-section">
        <h2>
          Overview <InfoMarker metric="ui.metrics_overview" />
        </h2>
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
        <h2>
          By job type <InfoMarker metric="ui.metrics_estimates" />
        </h2>
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

      <div className="metrics-runs">
        <RecentRunsColumn />
      </div>
    </div>
  );
}
