import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { assertDeletionPreview } from "../api/validators";
import { describeContents } from "../lib/deletionSummary";
import { notify } from "../stores/messageStore";
import { ModalBackdrop } from "./ModalBackdrop";

interface Props {
  projectId: string;
  projectName: string;
  onClose: () => void;
  /** Runs after a successful delete, before the modal closes. The project
   *  list needs nothing here, but a caller currently *viewing* the deleted
   *  project has to navigate away from a route that no longer resolves. */
  onDeleted?: () => void;
}

/**
 * The confirm step for deleting a project from the project list's row action.
 *
 * Deleting always cascades -- a project with contents used to return a 409
 * telling the user to "pass cascade=true", which is API wording aimed at a
 * caller, not an answer a person can act on. But cascading silently from a
 * one-click row action is the opposite failure: the row shows a file count
 * and nothing about sub-projects or pipeline runs, so the scale of what is
 * about to be destroyed is invisible at the point of clicking. This dialog
 * fetches the same deletion preview the detail panel's danger zone uses and
 * names the whole subtree before asking.
 */
export function DeleteProjectModal({
  projectId,
  projectName,
  onClose,
  onDeleted,
}: Props) {
  const qc = useQueryClient();

  // staleTime: 0 so re-opening always refetches -- an active job that ended
  // in the meantime must not leave a stale block in place. gcTime is left at
  // its default deliberately; see ProjectDangerZone for why gcTime: 0 breaks
  // under StrictMode's mount-unmount-remount cycle.
  const preview = useQuery({
    queryKey: ["project", projectId, "deletion-preview"],
    queryFn: async () => {
      const data = await api.deletionPreview(projectId);
      assertDeletionPreview(data);
      return data;
    },
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
      onDeleted?.();
      onClose();
    },
    onError: (e: Error) => {
      // A job may have started between preview and confirm. Re-fetching turns
      // the generic failure back into the specific "still active" message.
      preview.refetch();
      notify.error(e.message);
    },
  });

  const cancel = (
    <button
      type="button"
      className="btn"
      onClick={onClose}
      disabled={remove.isPending}
    >
      Cancel
    </button>
  );

  return (
    <ModalBackdrop
      onClick={onClose}
      onKeyDown={(e) => e.key === "Escape" && onClose()}
    >
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>Delete project</h2>

        {preview.isError ? (
          <>
            <p>Couldn't check this project's contents: {preview.error.message}</p>
            <div className="modal-actions">{cancel}</div>
          </>
        ) : !preview.data ? (
          // Guarding on the data itself rather than the loading flag: React
          // Query v5's isLoading can read false for a tick before data
          // arrives, which would crash the counts below.
          <>
            <p>Checking what this would delete…</p>
            <div className="modal-actions">{cancel}</div>
          </>
        ) : preview.data.blocked ? (
          <>
            <p>
              Can't delete yet — {preview.data.active_jobs.length} job
              {preview.data.active_jobs.length === 1 ? " is" : "s are"} still
              active in this project.
            </p>
            <p style={{ color: "var(--text-faint)", fontSize: 11 }}>
              {preview.data.active_jobs
                .map((j) => `${j.job_type} — ${j.state}`)
                .join(", ")}
            </p>
            <p>Wait for them to finish, or cancel them, then try again.</p>
            <div className="modal-actions">{cancel}</div>
          </>
        ) : (
          <>
            <p>
              Delete <strong>{projectName}</strong>
              {describeContents(preview.data)
                ? `, including ${describeContents(preview.data)}`
                : ""}
              ? This cannot be undone.
            </p>
            <div className="modal-actions">
              {cancel}
              <button
                type="button"
                className="btn danger"
                onClick={() => remove.mutate()}
                disabled={remove.isPending}
              >
                {remove.isPending ? "Deleting…" : "Yes, delete"}
              </button>
            </div>
          </>
        )}
      </div>
    </ModalBackdrop>
  );
}
