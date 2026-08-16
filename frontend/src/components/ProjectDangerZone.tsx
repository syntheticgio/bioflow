import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { DeletionPreview } from "../api/types";
import { assertDeletionPreview } from "../api/validators";
import { formatBytes } from "../lib/format";
import { notify } from "../stores/messageStore";

/** "3 sub-projects, 47 files (2.1 GB), and 12 pipeline runs".
 *
 *  Zero-valued clauses are dropped, so an empty project produces an empty
 *  string and the caller falls back to the bare "Delete X?" wording. */
function describeContents(p: DeletionPreview): string {
  const parts: string[] = [];
  if (p.child_project_count > 0) {
    parts.push(
      `${p.child_project_count} sub-project${p.child_project_count === 1 ? "" : "s"}`,
    );
  }
  if (p.object_count > 0) {
    parts.push(
      `${p.object_count} file${p.object_count === 1 ? "" : "s"} (${formatBytes(p.total_bytes)})`,
    );
  }
  if (p.run_count > 0) {
    parts.push(`${p.run_count} pipeline run${p.run_count === 1 ? "" : "s"}`);
  }
  if (parts.length === 0) return "";
  if (parts.length === 1) return parts[0];
  return `${parts.slice(0, -1).join(", ")}, and ${parts[parts.length - 1]}`;
}

export function ProjectDangerZone({
  projectId,
  projectName,
}: {
  projectId: string;
  projectName: string;
}) {
  const [confirming, setConfirming] = useState(false);
  const qc = useQueryClient();
  const navigate = useNavigate();

  // Only fetched once the user asks, so browsing a project costs no extra
  // request. staleTime: 0 means re-opening the confirm dialog always
  // refetches, so an active job that ended in the meantime won't leave a
  // stale block in place. gcTime is left at its default: gcTime: 0 combined
  // with React.StrictMode's mount-unmount-remount cycle evicts the query the
  // instant it briefly has zero observers, which restarts the fetch and can
  // prevent an error from ever settling into view.
  const preview = useQuery({
    queryKey: ["project", projectId, "deletion-preview"],
    queryFn: async () => {
      const data = await api.deletionPreview(projectId);
      assertDeletionPreview(data);
      return data;
    },
    enabled: confirming,
    staleTime: 0,
  });

  const remove = useMutation({
    mutationFn: () => api.deleteProject(projectId, true),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.removeQueries({ queryKey: ["project", projectId] });
      notify.success(
        `Deleted ${projectName} — disk space is reclaimed by the next cleanup pass (File → Clean up storage now).`,
      );
      // The selected project no longer exists, so the panel must not keep
      // pointing at it.
      navigate("/");
    },
    onError: (e: Error) => {
      // A job may have started between preview and confirm. Re-fetching turns
      // the generic failure back into the specific "still active" message.
      preview.refetch();
      notify.error(e.message);
    },
  });

  if (!confirming) {
    return (
      <div className="section">
        <div className="section-title">Delete</div>
        <button
          type="button"
          className="btn danger"
          onClick={() => setConfirming(true)}
        >
          Delete project
        </button>
        <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}>
          Removes this project, everything inside it, and its pipeline history.
        </div>
      </div>
    );
  }

  const cancel = (
    <button
      type="button"
      className="btn"
      onClick={() => setConfirming(false)}
      disabled={remove.isPending}
    >
      Cancel
    </button>
  );

  return (
    <div className="section">
      <div className="section-title">Delete</div>
      <div className="error-box" style={{ marginBottom: 0 }}>
        {preview.isError ? (
          <>
            <div style={{ marginBottom: 8 }}>
              Couldn't check this project's contents: {preview.error.message}
            </div>
            <div style={{ display: "flex", gap: 8 }}>{cancel}</div>
          </>
        ) : !preview.data ? (
          // Covers both the in-flight fetch and the brief window where the
          // query has just been enabled but hasn't reported isLoading yet
          // (React Query v5's isLoading is isPending && isFetching, so it can
          // read false for a tick before data arrives). Guarding on the data
          // itself, not the loading flag, is what actually prevents the
          // child_project_count crash below.
          <>
            <div style={{ marginBottom: 8 }}>Checking what this would delete…</div>
            <div style={{ display: "flex", gap: 8 }}>{cancel}</div>
          </>
        ) : preview.data?.blocked ? (
          <>
            <div style={{ marginBottom: 8 }}>
              Can't delete yet — {preview.data.active_jobs.length} job
              {preview.data.active_jobs.length === 1 ? " is" : "s are"} still
              active in this project.
              <div style={{ fontSize: 11, marginTop: 4 }}>
                {preview.data.active_jobs
                  .map((j) => `${j.job_type} — ${j.state}`)
                  .join(", ")}
              </div>
              <div style={{ marginTop: 6 }}>
                Wait for them to finish, or cancel them, then try again.
              </div>
            </div>
            <div style={{ display: "flex", gap: 8 }}>{cancel}</div>
          </>
        ) : (
          <>
            <div style={{ marginBottom: 8 }}>
              Delete <strong>{projectName}</strong>
              {describeContents(preview.data!)
                ? `, including ${describeContents(preview.data!)}`
                : ""}
              ? This cannot be undone.
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button
                type="button"
                className="btn danger"
                onClick={() => remove.mutate()}
                disabled={remove.isPending}
              >
                {remove.isPending ? "Deleting…" : "Yes, delete"}
              </button>
              {cancel}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
