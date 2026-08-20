import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, ApiRequestError } from "../api/client";
import { ModalBackdrop } from "./ModalBackdrop";
import { ResourceRefusalCard } from "./ResourceRefusalCard";
import { notify } from "../stores/messageStore";
import type { ResourceRefusalDetails } from "../api/types";

/**
 * Merge a project's per-sample StringTie assemblies into one non-redundant
 * annotation.
 *
 * The second dialog here, after differential expression, whose subject is a
 * *set* rather than one file. A merge asks "which of these do I combine?",
 * which has no single anchoring object, so it is reached from any
 * assembled-transcripts file's Actions tab but always operates across the
 * whole project's assemblies -- there is no per-object "suggestion card" for
 * it (S2 in the design doc). The server refuses a one-input merge (it is a
 * copy), so the list defaults to all assemblies and the launch stays disabled
 * until at least two are checked.
 */
export function MergeTranscriptsDialog({
  projectId,
  onClose,
}: {
  projectId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const { data: defaults, isLoading } = useQuery({
    queryKey: ["pipelines", "merge-transcripts", projectId],
    queryFn: () => api.mergeTranscriptsDefaults(projectId),
  });

  const [selected, setSelected] = useState<Record<string, boolean>>({});

  const assemblies = defaults?.assemblies ?? [];

  // Defaults to all of them: the server treats a one-input merge as a copy,
  // and "I am not sure which to combine" is not a reason to merge nothing.
  const isSelected = (id: string) =>
    selected[id] ?? true;

  const chosen = assemblies.filter((a) => isSelected(a.object_id));

  const tooFew = assemblies.length > 0 && chosen.length < 2;

  // Populated from a 422's `details`, the same reactive path the other
  // dialogs use -- a merge has no client-side estimate to check pre-flight.
  const [refusal, setRefusal] = useState<ResourceRefusalDetails | null>(null);

  const launch = useMutation({
    mutationFn: () =>
      api.launchMergeTranscripts({
        project_id: projectId,
        gtf_object_ids: chosen.map((a) => a.object_id),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Transcript merge started");
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => {
      if (e instanceof ApiRequestError && "refusal" in e.details) {
        setRefusal(e.details as unknown as ResourceRefusalDetails);
        return;
      }
      notify.error(e.message);
    },
  });

  const launchAnyway = useMutation({
    mutationFn: () =>
      api.launchMergeTranscripts({
        project_id: projectId,
        gtf_object_ids: chosen.map((a) => a.object_id),
        resource_override: true,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Launching without the memory check");
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const ready =
    defaults != null &&
    defaults.available &&
    !tooFew &&
    assemblies.length > 0;

  return (
    <ModalBackdrop onClick={onClose}>
      <div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
        <h2>
          Merge transcript assemblies
          <span className="dialog-tool-subtitle"> — StringTie</span>
        </h2>

        <div className="modal-body">
          {defaults != null && !defaults.available && (
            <div className="error-box" style={{ marginBottom: 12 }}>
              StringTie is not installed on this machine.
            </div>
          )}

          {isLoading && <div className="trim-mate-note">Reading assemblies…</div>}

          {defaults != null && assemblies.length === 0 && (
            <div className="warn-box" style={{ marginBottom: 12, fontSize: 12 }}>
              This project has no assembled transcripts yet. Run “Assemble
              transcripts (StringTie)” on each sample’s alignment first.
            </div>
          )}

          {assemblies.length > 0 && (
            <>
              <div className="section-note" style={{ marginBottom: 8 }}>
                Combine these per-sample assemblies into one non-redundant
                annotation — the standard step before quantifying a multi-sample
                RNA-seq experiment against novel transcripts. All are selected by
                default; uncheck any you want to leave out.
              </div>

              <table className="facts-table" style={{ marginBottom: 12 }}>
                <thead>
                  <tr>
                    <th style={{ width: 36 }}>Use</th>
                    <th>Assembly</th>
                    <th style={{ textAlign: "right" }}>Transcripts</th>
                    <th style={{ textAlign: "right" }}>Novel</th>
                  </tr>
                </thead>
                <tbody>
                  {assemblies.map((a) => {
                    const on = isSelected(a.object_id);
                    return (
                      <tr key={a.object_id}>
                        <td>
                          <input
                            type="checkbox"
                            checked={on}
                            aria-label={`Merge ${a.name}`}
                            onChange={(e) =>
                              setSelected((prev) => ({
                                ...prev,
                                [a.object_id]: e.target.checked,
                              }))
                            }
                          />
                        </td>
                        <td title={a.name}>{a.name}</td>
                        <td style={{ textAlign: "right" }}>
                          {a.transcript_count ?? "—"}
                        </td>
                        <td style={{ textAlign: "right" }}>
                          {a.novel_transcript_count ?? "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>

              {tooFew && (
                <div className="warn-box" style={{ marginTop: 10, fontSize: 12 }}>
                  Select at least two assemblies. A one-input merge is just a
                  copy of the input, which the server refuses rather than
                  producing a duplicate object.
                </div>
              )}
            </>
          )}

          {refusal && (
            <ResourceRefusalCard
              estimateMb={refusal.estimate_mb}
              budgetMb={refusal.budget_mb}
              detail={refusal.detail}
              explanation={
                refusal.refusal === "declared"
                  ? `This merge reserves ${(refusal.declared_mb ?? 0).toLocaleString()} MB, ` +
                    `more than the ${refusal.budget_mb.toLocaleString()} MB budget. ` +
                    `Nothing about the run changes that number.`
                  : `This merge needs about ${(refusal.estimate_mb ?? 0).toLocaleString()} MB, ` +
                    `more than the ${refusal.budget_mb.toLocaleString()} MB available.`
              }
              replan={refusal.replan ?? null}
              onCancel={onClose}
              onEdit={() => setRefusal(null)}
              onLaunchAnyway={() => launchAnyway.mutate()}
              launchAnywayPending={launchAnyway.isPending}
              onAcceptReplan={() => setRefusal(null)}
            />
          )}
        </div>

        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={!ready || launch.isPending}
            onClick={() => launch.mutate()}
          >
            {launch.isPending
              ? "Starting…"
              : `Merge ${chosen.length} assembl${chosen.length === 1 ? "y" : "ies"}`}
          </button>
        </div>
      </div>
    </ModalBackdrop>
  );
}
