import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { formatBytes } from "../lib/format";
import { notify } from "../stores/messageStore";

const DEFAULT_THRESHOLD_BYTES = 100 * 1024 * 1024;

/**
 * `created_at` on an export archive is a Unix timestamp in seconds
 * (`Path.stat().st_mtime`), not an ISO string like everything else this app
 * renders -- so it can't go through `formatDate`. This is the same
 * `toLocaleString` shape that gives, just fed a `Date` built from seconds.
 */
function formatArchiveDate(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function ProjectExport({
  projectId,
  projectName,
}: {
  projectId: string;
  projectName: string;
}) {
  const [open, setOpen] = useState(false);
  const [thresholdMb, setThresholdMb] = useState(
    String(DEFAULT_THRESHOLD_BYTES / (1024 * 1024)),
  );
  // Which archive is currently being fetched, so only that row's button
  // shows "Downloading..." rather than every row in the list at once.
  const [downloading, setDownloading] = useState<string | null>(null);

  // Only fetched once the dialog is open, matching ProjectDangerZone's
  // deletion-preview query: browsing a project should not cost a request
  // for a list nobody asked to see yet.
  const exports = useQuery({
    queryKey: ["exports"],
    queryFn: () => api.listExports(),
    enabled: open,
  });

  const create = useMutation({
    mutationFn: () => {
      const trimmed = thresholdMb.trim();
      const mb = trimmed ? Number(trimmed) : NaN;
      const thresholdBytes =
        trimmed && Number.isFinite(mb) && mb >= 0
          ? Math.round(mb * 1024 * 1024)
          : undefined;
      return api.createExport(projectId, thresholdBytes);
    },
    onSuccess: () => {
      notify.success(
        `Export of ${projectName} queued — it will appear in the list below once the job finishes.`,
      );
      setOpen(false);
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const download = async (name: string) => {
    setDownloading(name);
    try {
      const blob = await api.downloadExport(name);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      notify.error(e instanceof Error ? e.message : "Download failed");
    } finally {
      setDownloading(null);
    }
  };

  if (!open) {
    return (
      <div className="section">
        <div className="section-title">Export</div>
        <button type="button" className="btn" onClick={() => setOpen(true)}>
          Export project
        </button>
        <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}>
          Bundles this project's files and metadata into a downloadable archive.
        </div>
      </div>
    );
  }

  return (
    <div className="section">
      <div className="section-title">Export</div>

      <div style={{ marginBottom: 12 }}>
        <label
          htmlFor="export-threshold"
          style={{ display: "block", fontSize: 12, marginBottom: 4 }}
        >
          Include files up to
        </label>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <input
            id="export-threshold"
            type="number"
            min={0}
            step="1"
            value={thresholdMb}
            onChange={(e) => setThresholdMb(e.target.value)}
            disabled={create.isPending}
            style={{ width: 100, padding: "3px 6px" }}
          />
          <span style={{ fontSize: 12, color: "var(--text-faint)" }}>MB</span>
        </div>
        <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 4 }}>
          Larger files are listed in the archive but not included in it.
          Default is {formatBytes(DEFAULT_THRESHOLD_BYTES)}.
        </div>
      </div>

      <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        <button
          type="button"
          className="btn primary"
          onClick={() => create.mutate()}
          disabled={create.isPending}
        >
          {create.isPending ? "Queuing…" : "Start export"}
        </button>
        <button
          type="button"
          className="btn"
          onClick={() => setOpen(false)}
          disabled={create.isPending}
        >
          Close
        </button>
      </div>

      <div className="section-title" style={{ fontSize: 12 }}>
        Past exports
      </div>
      {exports.isLoading ? (
        <div style={{ fontSize: 12, color: "var(--text-faint)" }}>Loading…</div>
      ) : exports.isError ? (
        <div style={{ fontSize: 12, color: "var(--text-faint)" }}>
          Couldn't load exports: {exports.error.message}
        </div>
      ) : !exports.data || exports.data.length === 0 ? (
        <div style={{ fontSize: 12, color: "var(--text-faint)" }}>
          No exports yet. A queued export appears here once it finishes.
        </div>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0, fontSize: 12 }}>
          {exports.data.map((e) => (
            <li
              key={e.name}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 8,
                padding: "4px 0",
                borderBottom: "1px solid var(--border-subtle)",
              }}
            >
              <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                {e.name}
                <span style={{ color: "var(--text-faint)" }}>
                  {" "}
                  — {formatBytes(e.size_bytes)} · {formatArchiveDate(e.created_at)}
                </span>
              </span>
              <button
                type="button"
                className="btn-text"
                onClick={() => download(e.name)}
                disabled={downloading === e.name}
              >
                {downloading === e.name ? "Downloading…" : "Download"}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
