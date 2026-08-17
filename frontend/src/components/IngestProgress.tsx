import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import { formatDuration } from "../lib/format";
import { computeTimingLabel, estimateSubtext } from "../lib/jobTiming";

/**
 * Estimated progress for a running ingest.
 *
 * There is no honest a-priori percentage: ingest phases have very different
 * throughput, so a byte-progress bar sprints to 90% then stalls. Instead the
 * bar is driven by elapsed time against a duration predicted from previous
 * runs of the same job type -- `pct_estimated`, computed server-side by
 * timing_service.pct_estimated() from the same model `timing_estimate`
 * exposes, so this component no longer reimplements that arithmetic
 * client-side (elapsed/predicted, capped below 100%).
 *
 * Until enough runs have been recorded, no bar is shown at all -- it says how
 * many more are needed. A confidently wrong progress bar is worse than none.
 */
export function IngestProgress({ objectId }: { objectId: string }) {
  const [now, setNow] = useState(Date.now());

  const { data: jobs } = useQuery({
    queryKey: ["jobs", "object", objectId],
    queryFn: () => api.listJobs({ limit: 20 }),
    refetchInterval: 2000,
  });

  const active = jobs?.find(
    (j) =>
      j.object_id === objectId &&
      (j.state === "running" || j.state === "queued" || j.state === "pending"),
  );

  const { data: detail } = useQuery({
    queryKey: ["job", active?.id],
    queryFn: () => api.getJob(active!.id),
    enabled: !!active,
    refetchInterval: 2000,
  });

  // Local ticker so the bar advances smoothly between the 2s polls.
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(t);
  }, [active]);

  if (!active) return null;

  const estimate = detail?.timing_estimate;
  const startedAt = active.timing.started_at
    ? new Date(active.timing.started_at).getTime()
    : null;
  const elapsedMs = startedAt ? Math.max(0, now - startedAt) : 0;

  // The handler's own phase reporting is more accurate than any prediction, so
  // prefer it when the job publishes real progress. pct_estimated is already
  // null server-side whenever a real pct exists (see pct_estimated's
  // docstring), so hasReported only needs to check the measured value.
  const reportedPct = active.progress?.pct ?? 0;
  const hasReported = reportedPct > 0;

  let pct: number | null = null;
  let isEstimated = false;
  let label = active.progress?.phase || active.state;

  if (hasReported) {
    pct = Math.min(100, reportedPct * 100);
  } else if (detail?.pct_estimated != null) {
    pct = detail.pct_estimated * 100;
    const timing = computeTimingLabel(elapsedMs, reportedPct, estimate);
    isEstimated = timing.isEstimated;
    if (timing.label) label = timing.label;
  }

  return (
    <div
      style={{
        padding: "8px 10px",
        borderRadius: "var(--radius)",
        background: "var(--bg-elevated)",
        marginBottom: 12,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 12,
          marginBottom: 5,
        }}
      >
        <span>
          <span className="spinner" style={{ marginRight: 6 }} />
          {active.type.replace(/_/g, " ")}
        </span>
        <span style={{ color: "var(--text-faint)" }}>{label}</span>
      </div>

      {pct != null ? (
        <div className="progress">
          <div
            className={`progress-bar${isEstimated ? " estimated" : ""}`}
            style={{ width: `${pct}%` }}
          />
        </div>
      ) : (
        // Indeterminate: we genuinely do not know, and say so.
        <div className="progress">
          <div
            className="progress-bar"
            style={{ width: "100%", opacity: 0.25 }}
          />
        </div>
      )}

      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 4 }}>
        {formatDuration(elapsedMs)} elapsed
        {estimateSubtext(estimate) && (
          <> · {estimateSubtext(estimate)}</>
        )}
      </div>
    </div>
  );
}
