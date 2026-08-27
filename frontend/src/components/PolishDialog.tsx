import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, ApiRequestError } from "../api/client";
import { notify } from "../stores/messageStore";
import type { DataObject, ResourceRefusalDetails } from "../api/types";
import { NodeSelector } from "./NodeSelector";
import { ResourceRefusalCard } from "./ResourceRefusalCard";
import { ModalBackdrop } from "./ModalBackdrop";

/**
 * Launch Medaka polishing against one assembly, with the optional
 * `--bacteria` model opt-in.
 *
 * `--bacteria` is an opt-in from this dialog, not a card decision -- matching
 * iVar's primer scheme and completeness's lineage override. The card offers
 * the tool; it does not guess that this assembly is a bacterial isolate. The
 * dialog notes that ONT labels the bacterial-methylation model a research
 * release with minimal support, so the opt-in is informed. The safe default
 * (off) ships regardless of whether the user ever opens this dialog.
 */
export function PolishDialog({
  object,
  prefill,
  onClose,
}: {
  object: DataObject;
  onClose: () => void;
  /**
   * A suggestion card's launch body, when the dialog was opened by "Adjust…".
   *
   * Seeds the fields the card already decided (draft and reads), so the dialog
   * opens on that card's run rather than on generic defaults.
   */
  prefill?: Record<string, unknown> | null;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const [bacteria, setBacteria] = useState<boolean>(
    () => (prefill?.bacteria as boolean) ?? false,
  );
  const [targetNode, setTargetNode] = useState("");
  // Populated from a 422's `details`, the same reactive path CompletenessDialog
  // uses -- a polish run can be refused for exceeding the memory budget, and
  // the user gets the "launch anyway" escape rather than a dead-end error.
  const [refusal, setRefusal] = useState<ResourceRefusalDetails | null>(null);

  const launch = useMutation({
    mutationFn: () =>
      api.launchSuggestion(
        "/pipelines/polish-long",
        { ...(prefill ?? {}), bacteria },
        targetNode || undefined,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Long-read polishing started");
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
      api.launchSuggestion(
        "/pipelines/polish-long",
        { ...(prefill ?? {}), bacteria, resource_override: true },
        targetNode || undefined,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Launching without the memory check");
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  return (
    <ModalBackdrop onClick={onClose}>
      <div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
        <h2>
          Polish assembly (long reads)
          <span className="dialog-tool-subtitle"> — medaka</span>
        </h2>

        <div className="modal-body">
          <div className="trim-inputs">
            <div className="trim-file">{object.name}</div>
          </div>

          <div className="trim-fields">
            <label className="trim-check">
              <input
                type="checkbox"
                checked={bacteria}
                onChange={(e) => setBacteria(e.target.checked)}
              />
              <span>
                Use ONT&rsquo;s bacterial-methylation model (
                <code>--bacteria</code>)
              </span>
            </label>
            <small>
              ONT labels this model a research release with minimal support.
              Leave it off unless you are polishing a bacterial or archaeal
              isolate and want methylation-aware consensus calling.
            </small>
          </div>

          {refusal && (
            <ResourceRefusalCard
              estimateMb={refusal.estimate_mb}
              budgetMb={refusal.budget_mb}
              detail={refusal.detail}
              explanation={
                refusal.refusal === "declared"
                  ? `This polish run reserves ${(refusal.declared_mb ?? 0).toLocaleString()} MB, ` +
                    `more than the ${refusal.budget_mb.toLocaleString()} MB budget. ` +
                    `Nothing about the run changes that number.`
                  : `This polish run needs about ${(refusal.estimate_mb ?? 0).toLocaleString()} MB, ` +
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

          <div className="trim-mate-note" style={{ marginTop: 12 }}>
            Runs as its own job, separate from assembly. You can close this
            window; it runs in the background.
          </div>
        </div>

        <NodeSelector value={targetNode} onChange={setTargetNode} fullWidth />

        <div className="modal-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            disabled={launch.isPending}
            onClick={() => launch.mutate()}
          >
            {launch.isPending ? "Starting…" : "Polish assembly"}
          </button>
        </div>
      </div>
    </ModalBackdrop>
  );
}
