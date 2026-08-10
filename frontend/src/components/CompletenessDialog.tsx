import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { DataObject } from "../api/types";
import { NodeSelector } from "./NodeSelector";

/**
 * Launch compleasm against one assembly.
 *
 * The lineage is chosen from the object's organism metadata, not
 * auto-detected: compleasm's own `--autolineage` downloads several candidate
 * lineage datasets to decide, which is the expensive way to answer a question
 * this application mostly already knows. The dialog shows the inferred
 * lineage and lets the user change it -- the same "inferred, labelled as
 * inferred, overridable" shape AssembleDialog's genome size uses.
 *
 * A lineage dataset is reference data shared across every project, not
 * something this job fetches inline -- a completeness run can take hours and
 * must not depend on the network partway through. So this dialog checks
 * whether the chosen lineage is already downloaded and, if not, offers to
 * download it first; only once it is present does Score become available.
 */
export function CompletenessDialog({
  object,
  onClose,
}: {
  object: DataObject;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const { data: defaults, isLoading, isError, error } = useQuery({
    queryKey: ["pipelines", "completeness-defaults", object.id],
    queryFn: () => api.completenessDefaults(object.id),
    retry: false,
  });

  const [lineageOverride, setLineageOverride] = useState<string | null>(null);
  const [targetNode, setTargetNode] = useState("");
  const lineage = lineageOverride ?? defaults?.lineage ?? null;
  const odb = defaults?.odb ?? "odb12";

  // Only while the user has not touched it -- once they have, the value is
  // theirs and labelling it "inferred" would be false.
  const inferred = lineageOverride === null && defaults?.lineage != null;

  const { data: status, isLoading: statusLoading } = useQuery({
    queryKey: ["pipelines", "lineage-status", lineage, odb],
    queryFn: () => api.lineageStatus(lineage as string, odb),
    enabled: lineage != null,
  });

  const download = useMutation({
    mutationFn: () => api.downloadLineage({ lineage: lineage as string, odb }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success(`Downloading ${lineage}_${odb}`);
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const launch = useMutation({
    mutationFn: () =>
      api.launchCompleteness({ object_id: object.id, lineage, odb }, targetNode || undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Completeness scoring started");
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const lineagePresent = status?.present === true;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
        <h2>
          Assembly completeness
          <span className="dialog-tool-subtitle"> — compleasm</span>
        </h2>

        <div className="modal-body">
          {isError && (
            <div className="error-box" style={{ marginBottom: 12 }}>
              {(error as Error)?.message ??
                "Completeness cannot be scored for this file."}
            </div>
          )}

          <div className="trim-inputs">
            <div className="trim-file">{object.name}</div>
            {isLoading ? (
              <div className="trim-mate-note">Reading defaults…</div>
            ) : defaults?.organism ? (
              <div className="trim-mate-note">
                Organism: <strong>{defaults.organism}</strong>
              </div>
            ) : (
              <div className="trim-mate-note">
                No organism metadata on record — pick a lineage below.
              </div>
            )}
          </div>

          <div className="trim-fields">
            <label className="trim-wide">
              <span>Lineage</span>
              <input
                type="text"
                placeholder="e.g. bacteria, eukaryota, saccharomycetaceae"
                value={lineage ?? ""}
                onChange={(e) => setLineageOverride(e.target.value)}
              />
              <small>
                {inferred ? (
                  <>
                    Inferred from the organism above
                    {defaults?.specific === false && (
                      <>
                        {" "}
                        — this is a broad domain guess, not a specific match.
                        Narrow it if you know the organism's family or genus.
                      </>
                    )}
                    . Change it if this is wrong.
                  </>
                ) : (
                  <>
                    OrthoDB version: {odb}. Scores from different OrthoDB
                    versions are not comparable to each other.
                  </>
                )}
              </small>
            </label>
          </div>

          {lineage != null && !statusLoading && !lineagePresent && (
            <div className="trim-mate-note" style={{ marginTop: 12 }}>
              The {lineage}_{odb} lineage dataset is not downloaded yet.
              Download it once — it is then shared across every project.
            </div>
          )}

          <div className="trim-mate-note" style={{ marginTop: 12 }}>
            Runs as its own job, separate from contiguity (which needs no job
            at all). A bacterial genome scores in minutes; a vertebrate one
            can take hours. You can close this window; it runs in the
            background.
          </div>
        </div>

        <NodeSelector value={targetNode} onChange={setTargetNode} fullWidth />

        <div className="modal-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          {lineage != null && !statusLoading && !lineagePresent ? (
            <button
              type="button"
              className="primary"
              disabled={download.isPending}
              onClick={() => download.mutate()}
            >
              {download.isPending ? "Starting…" : `Download ${lineage}_${odb}`}
            </button>
          ) : (
            <button
              type="button"
              className="primary"
              disabled={
                defaults == null || lineage == null || launch.isPending
              }
              onClick={() => launch.mutate()}
            >
              {launch.isPending ? "Starting…" : "Score completeness"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
