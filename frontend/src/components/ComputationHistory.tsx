import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { ComputationRecord } from "../api/types";
import { formatBytes, formatDate } from "../lib/format";

const DASH = "—";

// How many rows show before the reader asks for more, and how many each
// click reveals. Five covers what anyone needs at a glance -- this table is
// scanned for "did the last run work", not read end to end. Matches the
// +N more pattern in FactsTable.
const INITIAL = 5;
const PAGE = 20;

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
 * The machine line under the table.
 *
 * The Machine column is gone from the rows because on a single-user install
 * every row says the same thing, and a column of identical values is spent
 * width. When rows genuinely differ the footer lists what ran where, and each
 * row's Threads cell carries its own machine as a tooltip.
 *
 * That tooltip is deliberately low-discoverability -- it needs a hover, so it
 * is invisible on touch. It is debugging information, and the footer sentence
 * is the part a reader is meant to notice.
 */
function machineFooter(records: ComputationRecord[]): string | null {
  const seen = [...new Set(records.map(machineLabel))].filter((m) => m !== DASH);
  if (seen.length === 0) return null;
  if (seen.length === 1) return `All ran on ${seen[0]}.`;
  const last = seen[seen.length - 1];
  // Semicolons, not commas, between machines: a label is itself
  // comma-separated ("aarch64, 24 cores"), so a comma-joined list of three or
  // more reads as one long run of alternating machines and core counts.
  return `Ran on a combination of ${seen.slice(0, -1).join("; ")} and ${last}.`;
}

/**
 * One row's worth of what a run cost and what it used. Every resource field
 * is null for a run under the 60s sampling floor -- rendered as an em-dash,
 * never `0`, since the absence of a measurement is not a measurement of zero.
 */
function ComputationRow({
  record,
  showMachineHint,
}: {
  record: ComputationRecord;
  showMachineHint: boolean;
}) {
  return (
    <tr>
      <td style={{ textAlign: "left" }}>{formatDate(record.finished_at)}</td>
      <td className="mono" style={{ textAlign: "left" }}>
        {record.job_type}
      </td>
      <td style={{ textAlign: "left" }}>{toolLabel(record)}</td>
      <td>{(record.duration_ms / 1000).toFixed(1)}s</td>
      <td title={showMachineHint ? machineLabel(record) : undefined}>
        {record.threads ?? DASH}
      </td>
      <td>{record.peak_rss_bytes != null ? formatBytes(record.peak_rss_bytes) : DASH}</td>
      <td style={{ textAlign: "left" }}>
        <span className={`badge ${record.outcome}`}>{record.outcome}</span>
      </td>
    </tr>
  );
}

/**
 * Every run that has touched this file -- failures included, since a failed
 * run is the most informative record here.
 *
 * How the file was made is no longer summarized separately: it is the last
 * row of the lineage list above, which says the same thing in the place a
 * reader is already looking.
 *
 * The empty state is the default state, not an edge case: on 2026-08-05,
 * nearly every real object in this app has zero JobRunTiming rows, because
 * the collection only started recording `object_id`/`job_id` on 2026-08-03.
 * `producedByJob` set with `producedBy` null means the run that made this
 * file happened before that date -- distinct from "nothing ever ran", and
 * worth saying so rather than implying the file has no history at all.
 */
export function ComputationHistory({ objectId }: { objectId: string }) {
  const [visible, setVisible] = useState(INITIAL);

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

  const shown = data.records.slice(0, visible);
  const remaining = data.records.length - shown.length;
  // The tooltip only earns its place when there is something to disambiguate.
  const machines = new Set(data.records.map(machineLabel));
  const mixedMachines = machines.size > 1;
  const footer = machineFooter(data.records);

  return (
    <div className="section" style={{ marginTop: 30 }}>
      <div className="section-title">Runs on this file</div>

      {!hasRecords && !predatesRecording && (
        <div className="section-note">
          No computations have been recorded for this file.
        </div>
      )}

      {!hasRecords && predatesRecording && (
        <div className="section-note">
          Computation records began on 2026-08-03. The run that produced this
          file happened before then, so it was not recorded — this is not the
          same as nothing having run.
        </div>
      )}

      {hasRecords && (
        <>
          <div className="section-note">
            {data.records.length} recorded {data.records.length === 1 ? "run" : "runs"}, newest first.
          </div>
          <table className="trim-table runs-table">
            <thead>
              <tr>
                <th style={{ textAlign: "left" }}>When</th>
                <th style={{ textAlign: "left" }}>Job</th>
                <th style={{ textAlign: "left" }}>Tool</th>
                <th>Duration</th>
                <th>Threads</th>
                <th>Peak RSS</th>
                <th style={{ textAlign: "left" }}>Outcome</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r, i) => (
                // finished_at is not unique across rows in the same second in
                // theory, so index the key by position within this response
                // rather than by timestamp.
                <ComputationRow
                  key={`${r.job_id ?? i}-${r.finished_at}`}
                  record={r}
                  showMachineHint={mixedMachines}
                />
              ))}
            </tbody>
          </table>

          <div className="runs-footer">
            {remaining > 0 && (
              <button
                type="button"
                className="btn-text"
                onClick={() => setVisible(visible + PAGE)}
              >
                +{remaining} more
              </button>
            )}
            {remaining <= 0 && data.records.length > INITIAL && (
              <button
                type="button"
                className="btn-text"
                onClick={() => setVisible(INITIAL)}
              >
                Show less
              </button>
            )}
            {footer && <span className="section-note">{footer}</span>}
          </div>
        </>
      )}

      {data.has_more && remaining <= 0 && (
        <div className="section-note" style={{ marginTop: 8 }}>
          More records exist than are shown here.
        </div>
      )}
    </div>
  );
}
