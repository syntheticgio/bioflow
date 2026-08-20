import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, ApiRequestError } from "../api/client";
import { ModalBackdrop } from "./ModalBackdrop";
import { ResourceRefusalCard } from "./ResourceRefusalCard";
import { notify } from "../stores/messageStore";
import type { DataObject, ResourceRefusalDetails } from "../api/types";

interface CandidateAlignment {
  id: string;
  name: string;
  sample: string;
}

/**
 * Launch a whatsHap haplotag run: stamp a phased VCF's phase sets onto one
 * alignment's reads.
 *
 * The candidate alignments arrive in `prefill.candidate_alignments`, seeded
 * there by the suggestion card so the dialog never re-lists the project to
 * find them.
 */
export function HaplotagDialog({
  object,
  onClose,
  prefill,
}: {
  object: DataObject;
  onClose: () => void;
  /**
   * The suggestion card's launch body, when opened by "Adjust…". Seeds the
   * picked alignment and target so the dialog opens on that card's run rather
   * than the generic defaults. Null when reached another way.
   */
  prefill?: Record<string, unknown> | null;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const candidates: CandidateAlignment[] = Array.isArray(
    prefill?.candidate_alignments,
  )
    ? (prefill?.candidate_alignments as CandidateAlignment[])
    : [];

  const seedId: string | null = Array.isArray(prefill?.alignment_ids)
    ? ((prefill?.alignment_ids as string[])[0] ?? null)
    : candidates.length > 0
      ? candidates[0].id
      : null;

  const [selectedId, setSelectedId] = useState<string | null>(seedId);
  const [sample, setSample] = useState<string>("");
  const [ignoreReadGroups, setIgnoreReadGroups] = useState(false);
  const [threads, setThreads] = useState<number>(4);
  const [refusal, setRefusal] = useState<ResourceRefusalDetails | null>(null);

  const params = {
    threads,
  };

  const buildBody = (resourceOverride: boolean) => ({
    object_id: object.id,
    alignment_ids: selectedId ? [selectedId] : [],
    sample: sample || undefined,
    ignore_read_groups: ignoreReadGroups,
    params,
    resource_override: resourceOverride,
  });

  const launch = useMutation({
    mutationFn: (override: boolean) =>
      api.launchSuggestion("/pipelines/haplotag", buildBody(override)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Haplotagging started");
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
      api.launchSuggestion("/pipelines/haplotag", buildBody(true)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Launching without the memory check");
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const ready = selectedId !== null;

  return (
    <ModalBackdrop onClick={onClose}>
      <div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
        <h2>Haplotag variants</h2>

        <div className="modal-body">
          <div className="trim-inputs">
            <div className="trim-file">{object.name}</div>
            <div className="trim-mate-note">
              Stamp this phased VCF's phase sets onto an alignment's reads
              (whatsHap).
            </div>
          </div>

          <div className="trim-fields">
            <label className="trim-wide">
              <span>Alignment to haplotag</span>
              {candidates.length === 0 ? (
                <div className="warn-box" style={{ fontSize: 12 }}>
                  No alignments against this variant set's reference were found
                  in the project.
                </div>
              ) : (
                <div className="phase-alignment-list">
                  {candidates.map((c) => (
                    <label key={c.id} className="phase-alignment-row">
                      <input
                        type="radio"
                        name="haplotag-alignment"
                        checked={selectedId === c.id}
                        onChange={() => setSelectedId(c.id)}
                      />
                      <span>{c.name}</span>
                      {c.sample && (
                        <span className="phase-alignment-sample">
                          {c.sample}
                        </span>
                      )}
                    </label>
                  ))}
                </div>
              )}
              <small>Pick one alignment to carry the phase sets onto.</small>
            </label>

            <label>
              <span>Sample name (optional)</span>
              <input
                type="text"
                value={sample}
                placeholder="From the BAM's read group"
                onChange={(e) => setSample(e.target.value)}
              />
              <small>
                Defaults to the alignment's sample. Only used when ignoring read
                groups.
              </small>
            </label>

            <label className="phase-flag">
              <input
                type="checkbox"
                checked={ignoreReadGroups}
                onChange={(e) => setIgnoreReadGroups(e.target.checked)}
              />
              <span>Ignore read groups</span>
            </label>

            <label>
              <span>Threads</span>
              <input
                type="number"
                min={1}
                max={16}
                value={threads}
                onChange={(e) => setThreads(Number(e.target.value))}
              />
            </label>
          </div>

          {refusal && (
            <ResourceRefusalCard
              estimateMb={refusal.estimate_mb}
              budgetMb={refusal.budget_mb}
              detail={refusal.detail}
              explanation={
                refusal.refusal === "declared"
                  ? `Haplotagging reserves ${(refusal.declared_mb ?? 0).toLocaleString()} MB, ` +
                    `more than the ${refusal.budget_mb.toLocaleString()} MB budget. ` +
                    `Nothing about the run changes that number.`
                  : `Haplotagging needs about ${(refusal.estimate_mb ?? 0).toLocaleString()} MB, ` +
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
            onClick={() => launch.mutate(false)}
            disabled={!ready || launch.isPending}
          >
            {launch.isPending ? "Starting…" : "Haplotag variants"}
          </button>
        </div>
      </div>
    </ModalBackdrop>
  );
}
