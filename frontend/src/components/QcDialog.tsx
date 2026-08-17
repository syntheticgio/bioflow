import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { ModalBackdrop } from "./ModalBackdrop";
import { NodeSelector } from "./NodeSelector";
import { notify } from "../stores/messageStore";

/**
 * A confirmation dialog for launching QC on a file.
 *
 * QC takes no parameters, but the user should confirm before it runs.
 */
export function QcDialog({
  objectId,
  projectId,
  onClose,
}: {
  objectId: string;
  projectId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [targetNode, setTargetNode] = useState("");

  const runQC = useMutation({
    mutationFn: () => api.launchQC(objectId, targetNode || undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["suggestions", objectId] });
      notify.info("QC queued");
      onClose();
    },
    onError: (e: Error) => notify.error(e.message),
  });

  return (
    <ModalBackdrop onClick={onClose}>
      <div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
        <h2>Run QC</h2>
        <div className="modal-body">
          <p style={{ marginBottom: 12 }}>
            Run quality control on this file to detect read chemistry, adapter
            content, and quality distribution. This information is needed before
            alignment and other pipeline steps.
          </p>
          <NodeSelector value={targetNode} onChange={setTargetNode} />
        </div>
        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn primary"
            onClick={() => runQC.mutate()}
            disabled={runQC.isPending}
          >
            {runQC.isPending ? "Starting…" : "Run QC"}
          </button>
        </div>
      </div>
    </ModalBackdrop>
  );
}
