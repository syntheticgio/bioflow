import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, ApiRequestError } from "../api/client";
import { notify } from "../stores/messageStore";
import type { DataObject, ResourceRefusalDetails } from "../api/types";
import { NodeSelector } from "./NodeSelector";
import { ResourceRefusalCard } from "./ResourceRefusalCard";
import { ModalBackdrop } from "./ModalBackdrop";

const DEFAULT_DB_KEY = "standard-8";

function formatBytes(bytes: number): string {
  const gb = bytes / 1_000_000_000;
  if (gb >= 1) {
    return `${gb.toFixed(1)} GB`;
  }
  const mb = bytes / 1_000_000;
  return `${mb.toFixed(0)} MB`;
}

/**
 * Launch Kraken2 classification against one FASTQ read set.
 *
 * The database is a fixed choice of three, not something inferred from the
 * object -- there is no organism metadata to guess from here, unlike
 * CompletenessDialog's lineage. So this dialog is simpler in one respect: a
 * plain radio list with `standard-8` preselected, no "inferred" labelling.
 *
 * A database is reference data shared across every project, the same as a
 * compleasm lineage, so a classification job can take a while and must not
 * depend on the network partway through. Unlike CompletenessDialog, though,
 * this dialog does not offer a separate "download first" button: the
 * backend's launch_classify_reads already chains the download job behind
 * the classify job via `depends_on` when the database isn't on disk, so
 * launching is enough -- the dialog only needs to warn about it up front.
 */
export function ClassifyReadsDialog({
  object,
  onClose,
  prefill,
}: {
  object: DataObject;
  onClose: () => void;
  /**
   * A suggestion card's launch body, when the dialog was opened by "Adjust…".
   * The classify_reads card's launch body only carries object_id -- there is
   * no db_key to seed, so this only matters for a future mate_object_id.
   */
  prefill?: Record<string, unknown> | null;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const { data: dbs, isLoading, isError, error } = useQuery({
    queryKey: ["pipelines", "kraken-dbs"],
    queryFn: () => api.krakenDbs(),
    retry: false,
  });

  const [dbKey, setDbKey] = useState<string>(DEFAULT_DB_KEY);
  const [targetNode, setTargetNode] = useState("");
  const [refusal, setRefusal] = useState<ResourceRefusalDetails | null>(null);

  const mateObjectId = prefill?.mate_object_id as string | undefined;

  const selected = dbs?.find((db) => db.key === dbKey) ?? null;

  const launch = useMutation({
    mutationFn: () =>
      api.launchClassifyReads(
        {
          object_id: object.id,
          db_key: dbKey,
          ...(mateObjectId ? { mate_object_id: mateObjectId } : {}),
        },
        targetNode || undefined,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Classification started");
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
      api.launchClassifyReads({
        object_id: object.id,
        db_key: dbKey,
        ...(mateObjectId ? { mate_object_id: mateObjectId } : {}),
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

  return (
    <ModalBackdrop onClick={onClose}>
      <div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
        <h2>
          {object.format.kind === "fasta"
            ? "Identify this bin"
            : "Identify organisms"}
          <span className="dialog-tool-subtitle"> — Kraken2</span>
        </h2>

        <div className="modal-body">
          {isError && (
            <div className="error-box" style={{ marginBottom: 12 }}>
              {(error as Error)?.message ??
                "Kraken2 databases could not be loaded."}
            </div>
          )}

          <div className="trim-inputs">
            <div className="trim-file">{object.name}</div>
          </div>

          {isLoading ? (
            <div className="trim-mate-note">Loading databases…</div>
          ) : (
            <div className="trim-fields" role="radiogroup" aria-label="Kraken2 database">
              {dbs?.map((db) => (
                <label
                  key={db.key}
                  className="trim-wide"
                  style={{ display: "flex", alignItems: "flex-start", gap: 8 }}
                >
                  <input
                    type="radio"
                    name="kraken-db"
                    value={db.key}
                    checked={dbKey === db.key}
                    onChange={() => setDbKey(db.key)}
                    style={{ marginTop: 4 }}
                  />
                  <span>
                    <strong>{db.label}</strong>{" "}
                    <span className="trim-mate-note">
                      ({formatBytes(db.download_bytes)})
                    </span>
                    <br />
                    <small>{db.description}</small>
                  </span>
                </label>
              ))}
            </div>
          )}

          {selected && selected.present === false && (
            <div className="trim-mate-note" style={{ marginTop: 12 }}>
              This database isn't downloaded yet — the first run fetches ~
              {formatBytes(selected.download_bytes)} before classifying.
            </div>
          )}

          {refusal && (
            <ResourceRefusalCard
              estimateMb={refusal.estimate_mb}
              budgetMb={refusal.budget_mb}
              detail={refusal.detail}
              explanation={
                refusal.refusal === "declared"
                  ? `This classification run reserves ${(refusal.declared_mb ?? 0).toLocaleString()} MB, ` +
                    `more than the ${refusal.budget_mb.toLocaleString()} MB budget. ` +
                    `Nothing about the run changes that number.`
                  : `This classification run needs about ${(refusal.estimate_mb ?? 0).toLocaleString()} MB, ` +
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
            Runs as its own job. You can close this window; it runs in the
            background.
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
            disabled={dbs == null || dbKey == null || launch.isPending}
            onClick={() => launch.mutate()}
          >
            {launch.isPending ? "Starting…" : "Start classification"}
          </button>
        </div>
      </div>
    </ModalBackdrop>
  );
}
