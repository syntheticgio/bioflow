import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { RunTable } from "./Metrics";
import { InfoMarker } from "./InfoMarker";

const PAGE = 25;

/**
 * Every recorded run of one job type, paged.
 *
 * The "see more" destination from the Metrics page. Deliberately a plain
 * table rather than a second set of summaries: the medians already live one
 * page back, and what this page adds is the individual rows behind them --
 * failures included, same as the preview tables.
 */
export function MetricsJobType() {
  const { jobType = "" } = useParams();
  const [page, setPage] = useState(0);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["jobs", "metrics", "runs", jobType, page],
    queryFn: () => api.metricsRunsFor(jobType, PAGE, page * PAGE),
    // Without this the table blanks to "Loading…" on every page step, which
    // reads as the data vanishing rather than advancing.
    placeholderData: keepPreviousData,
  });

  return (
    <div className="metrics-page metrics-page-single">
      <div className="metrics-overview">
        <p className="help-intro">
          <Link to="/metrics">← Metrics</Link>
        </p>
        <h1 className="mono">
          {jobType} <InfoMarker metric="ui.metrics_recent_runs" />
        </h1>

        {isLoading && <p className="help-intro">Loading…</p>}
        {isError && <p className="help-intro">Couldn't load runs.</p>}

        {data && (
          <section className="help-section">
            <h2>
              {data.total.toLocaleString()} recorded run
              {data.total === 1 ? "" : "s"}
            </h2>
            <RunTable runs={data.runs} />

            {data.total > PAGE && (
              <div className="run-pager">
                <button
                  className="btn"
                  disabled={page === 0}
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                >
                  ← Newer
                </button>
                <span className="run-pager-at">
                  {(page * PAGE + 1).toLocaleString()}–
                  {Math.min((page + 1) * PAGE, data.total).toLocaleString()} of{" "}
                  {data.total.toLocaleString()}
                </span>
                <button
                  className="btn"
                  disabled={(page + 1) * PAGE >= data.total}
                  onClick={() => setPage((p) => p + 1)}
                >
                  Older →
                </button>
              </div>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
