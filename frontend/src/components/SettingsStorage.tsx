import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import { SettingsNav } from "./SettingsNav";

type DriftEntry = {
  category: string;
  path: string;
  object_id: string | null;
  digest: string | null;
  size_bytes: number;
};

type DriftReport = {
  swept_at: string;
  skipped: boolean;
  skip_reason: string | null;
  counts: Record<string, number>;
  entries: DriftEntry[];
  reclaimable_bytes: number;
};

/**
 * What each category means, in the user's terms rather than the schema's.
 * The label answers "what happened"; the hint answers "what should I do".
 */
const CATEGORIES: { key: string; label: string; hint: string }[] = [
  {
    key: "orphaned_file",
    label: "Files with no record",
    hint: "Disk space used by files nothing points at. Safe to ignore; not yet reclaimable here.",
  },
  {
    key: "stalled_ingest",
    label: "Unfinished ingests",
    hint: "An upload or import started and never completed. Re-importing the file is the fix.",
  },
  {
    key: "missing_blob",
    label: "Records with no file",
    hint: "These will fail when a pipeline tries to read them. Re-import the source file.",
  },
  {
    key: "missing_report_dir",
    label: "Missing results",
    hint: "The object offers a Results tab whose data is gone. Re-run the analysis to restore it.",
  },
];

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${units[unit]}`;
}

export function SettingsStorage() {
  const report = useQuery({
    queryKey: ["maintenance", "drift"],
    queryFn: () => api.get<DriftReport>("/maintenance/drift"),
  });

  if (report.isLoading) {
    return (
      <div className="settings-page">
        <SettingsNav />
        <div>Loading…</div>
      </div>
    );
  }
  if (report.isError || !report.data) {
    return (
      <div className="settings-page">
        <SettingsNav />
        <div>Could not load the storage report.</div>
      </div>
    );
  }

  const data = report.data;
  const total = Object.values(data.counts).reduce((a, b) => a + b, 0);

  return (
    <div className="settings-page">
      <SettingsNav />
      <h1>Settings · Storage</h1>
      <p className="settings-hint">
        A read-only check that the database and the files on disk still agree.
        Nothing here deletes anything.
      </p>

      {data.skipped ? (
        <p className="settings-hint settings-hint-warn">
          The last check did not run: {data.skip_reason ?? "storage unavailable"}
        </p>
      ) : (
        <>
          <p className="settings-hint">
            Last checked {new Date(data.swept_at).toLocaleString()}
          </p>

          {total === 0 ? (
            <p>No drift found. The database and the filesystem agree.</p>
          ) : (
            <>
              <p>
                {formatBytes(data.reclaimable_bytes)} in files that are no longer
                referenced.
              </p>
              <dl className="drift-categories">
                {CATEGORIES.map((c) => (
                  <div key={c.key}>
                    <dt>
                      {c.label}: {data.counts[c.key] ?? 0}
                    </dt>
                    <dd className="settings-hint">{c.hint}</dd>
                  </div>
                ))}
              </dl>
            </>
          )}
        </>
      )}
    </div>
  );
}
