import { useParams } from "react-router-dom";
import { formatBytes } from "../lib/format";
import { useUploads } from "../hooks/useUploads";
import { useUploadStore } from "../stores/uploadStore";

export function UploadTray() {
  const { projectId } = useParams();
  const { items, clearFinished } = useUploadStore();
  const { resumeUpload } = useUploads(projectId);

  if (items.length === 0) return null;

  const active = items.filter((i) => i.status === "uploading").length;

  return (
    <div className="tray">
      <div className="tray-header">
        <span>{active > 0 ? `Uploading ${active} file(s)` : "Uploads"}</span>
        <button
          type="button"
          className="icon-btn"
          style={{ marginLeft: "auto" }}
          title="Clear finished"
          onClick={clearFinished}
        >
          ×
        </button>
      </div>

      <div className="tray-body">
        {items.map((item) => {
          const pct = item.size > 0 ? Math.round((item.loaded / item.size) * 100) : 0;
          const barClass =
            item.status === "done"
              ? "done"
              : item.status === "error" || item.status === "cancelled"
                ? "error"
                : "";
          // Only a transfer that got a session can pick up where it left off.
          const resumable =
            (item.status === "error" || item.status === "cancelled") && !!item.file;

          return (
            <div className="tray-item" key={item.id}>
              <div className="tray-item-name" title={item.filename}>
                {item.filename}
              </div>

              <div className="progress">
                <div
                  className={`progress-bar ${barClass}`}
                  style={{ width: `${item.status === "done" ? 100 : pct}%` }}
                />
              </div>

              <div className="tray-item-meta">
                <span>
                  {item.status === "error"
                    ? item.error
                    : item.status === "done"
                      ? item.phase === "deduplicated"
                        ? "Already stored (deduplicated)"
                        : "Complete"
                      : item.status === "cancelled"
                        ? "Cancelled"
                        : item.phase === "assembling"
                          ? "Assembling on server…"
                          : item.phase === "preparing"
                            ? "Preparing…"
                            : `${formatBytes(item.loaded)} / ${formatBytes(item.size)}`}
                </span>

                <span style={{ display: "flex", gap: 8 }}>
                  {resumable && (
                    <button
                      type="button"
                      onClick={() => void resumeUpload(item.id)}
                      style={{ color: "var(--accent)", fontSize: 11 }}
                      title="Resume from the last chunk the server received"
                    >
                      resume
                    </button>
                  )}
                  {item.status === "uploading" && (
                    <button
                      type="button"
                      onClick={() => item.controller?.abort()}
                      style={{ color: "var(--text-faint)", fontSize: 11 }}
                    >
                      cancel
                    </button>
                  )}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
