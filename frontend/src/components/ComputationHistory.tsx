import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ComputationRecord } from "../api/types";
import { formatBytes, formatDate } from "../lib/format";

const DASH = "—";

function machineLabel(r: ComputationRecord): string {
  const parts: string[] = [];
  if (r.machine_cpu_model) parts.push(r.machine_cpu_model);
  if (r.machine_logical_cores) parts.push(`${r.machine_logical_cores} cores`);
  return parts.length > 0 ? parts.join(", ") : DASH;
}

function toolLabel(r: ComputationRecord): string {
  if (!r.tool) return DASH;
  return r.tool_version ? `${r.tool} ${r.tool_version}` : r.tool;
}

/**
 * One row's worth of what a run cost and what it used. Every resource field
 * is null for a run under the 60s sampling floor -- rendered as an em-dash,
 * never `0`, since the absence of a measurement is not a measurement of zero.
 */
function ComputationRow({ record }: { record: ComputationRecord }) {
  return (
    <tr>
      <td style={{ textAlign: "left" }}>{formatDate(record.finished_at)}</td>
      <td className="mono" style={{ textAlign: "left" }}>
        {record.job_type}
      </td>
      <td style={{ textAlign: "left" }}>{toolLabel(record)}</td>
      <td>{(record.duration_ms / 1000).toFixed(1)}s</td>
      <td>{record.threads ?? DASH}</td>
      <td>{record.peak_rss_bytes != null ? formatBytes(record.peak_rss_bytes) : DASH}</td>
      <td style={{ textAlign: "left" }}>{machineLabel(record)}</td>
      <td style={{ textAlign: "left" }}>
        <span className={`badge ${record.outcome}`}>{record.outcome}</span>
      </td>
    </tr>
  );
}

function ProducedBySummary({ record }: { record: ComputationRecord }) {
  return (
    <div className="section">
      <div className="section-title">How this file was made</div>
      <dl className="kv">
        <dt>Tool</dt>
        <dd>{toolLabel(record)}</dd>
        <dt>Finished</dt>
        <dd>{formatDate(record.finished_at)}</dd>
        <dt>Duration</dt>
        <dd>{(record.duration_ms / 1000).toFixed(1)}s</dd>
        <dt>Threads</dt>
        <dd>{record.threads ?? DASH}</dd>
        <dt>Peak memory</dt>
        <dd>{record.peak_rss_bytes != null ? formatBytes(record.peak_rss_bytes) : DASH}</dd>
        <dt>Machine</dt>
        <dd>{machineLabel(record)}</dd>
        <dt>Outcome</dt>
        <dd>
          <span className={`badge ${record.outcome}`}>{record.outcome}</span>
        </dd>
      </dl>
    </div>
  );
}

/**
 * Per-object computation provenance: what made this file, and every run
 * that has used it since -- failures included, since a failed run is the
 * most informative record here.
 *
 * The empty state is the default state, not an edge case: on 2026-08-05,
 * nearly every real object in this app has zero JobRunTiming rows, because
 * the collection only started recording `object_id`/`job_id` on 2026-08-03.
 * `producedByJob` set with `producedBy` null means the run that made this
 * file happened before that date -- distinct from "nothing ever ran", and
 * worth saying so rather than implying the file has no history at all.
 */
export function ComputationHistory({ objectId }: { objectId: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["object-computations", objectId],
    queryFn: () => api.getObjectComputations(objectId),
  });

  if (isLoading) {
    return <div className="section-note">Loading computation history…</div>;
  }

  if (isError) {
    return <div className="error-box">Could not load computation history.</div>;
  }

  if (!data) return null;

  const hasRecords = data.records.length > 0;
  const predatesRecording = !data.produced_by && data.produced_by_job != null;

  return (
    <div>
      {data.produced_by && <ProducedBySummary record={data.produced_by} />}

      <div className="section">
        <div className="section-title">Runs on this file</div>

        {!hasRecords && !predatesRecording && (
          <div className="section-note">
            No computations have been recorded for this file.
          </div>
        )}

        {!hasRecords && predatesRecording && (
          <div className="section-note">
            Computation records began on 2026-08-03. The run that produced
            this file happened before then, so it was not recorded — this is
            not the same as nothing having run.
          </div>
        )}

        {hasRecords && (
          <table className="trim-table">
            <thead>
              <tr>
                <th style={{ textAlign: "left" }}>When</th>
                <th style={{ textAlign: "left" }}>Job</th>
                <th style={{ textAlign: "left" }}>Tool</th>
                <th>Duration</th>
                <th>Threads</th>
                <th>Peak RSS</th>
                <th style={{ textAlign: "left" }}>Machine</th>
                <th style={{ textAlign: "left" }}>Outcome</th>
              </tr>
            </thead>
            <tbody>
              {data.records.map((r, i) => (
                // finished_at is not unique across rows in the same second in
                // theory, so index the key by position within this response
                // rather than by timestamp.
                <ComputationRow key={`${r.job_id ?? i}-${r.finished_at}`} record={r} />
              ))}
            </tbody>
          </table>
        )}

        {data.has_more && (
          <div className="section-note" style={{ marginTop: 8 }}>
            More records exist than are shown here.
          </div>
        )}
      </div>
    </div>
  );
}
