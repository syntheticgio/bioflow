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
 * Launch a read-backed phasing run over a called VCF.
 *
 * whatsHap phases the variants against one or more alignments on the same
 * reference. `phase` phases a single sample within one alignment; `polyphase`
 * phases across several -- distinct samples. The candidate alignments arrive
 * in `prefill.candidate_alignments`, seeded there by the suggestion card so
 * the dialog never re-lists the project to find them.
 */
export function PhaseDialog({
  object,
  onClose,
  prefill,
}: {
  object: DataObject;
  onClose: () => void;
  /**
   * The suggestion card's launch body, when opened by "Adjust…". Seeds the
   * picked alignments, mode and target so the dialog opens on that card's run
   * rather than the generic defaults. Null when reached another way.
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

  const seedIds: string[] = Array.isArray(prefill?.alignment_ids)
    ? (prefill?.alignment_ids as string[])
    : candidates.length > 0
      ? [candidates[0].id]
      : [];
  const seedMode: string = typeof prefill?.mode === "string" ? (prefill.mode as string) : "phase";

  const [selectedIds, setSelectedIds] = useState<string[]>(seedIds);
  const [mode, setMode] = useState<string>(seedMode);
  const [sample, setSample] = useState<string>("");
  const [ignoreReadGroups, setIgnoreReadGroups] = useState(false);
  const [distrustGenotypes, setDistrustGenotypes] = useState(false);
  const [indels, setIndels] = useState(false);
  const [threads, setThreads] = useState<number>(4);
  const [refusal, setRefusal] = useState<ResourceRefusalDetails | null>(null);

  const toggle = (id: string) =>
    setSelectedIds((ids) =>
      ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id],
    );

  const params = {
    ignore_read_groups: ignoreReadGroups,
    distrust_genotypes: distrustGenotypes,
    indels,
    threads,
  };

  const buildBody = (resourceOverride: boolean) => ({
    object_id: object.id,
    alignment_ids: selectedIds,
    mode,
    sample: mode === "phase" ? (sample || undefined) : undefined,
    params,
    resource_override: resourceOverride,
  });

  const launch = useMutation({
    mutationFn: (override: boolean) =>
      api.launchSuggestion("/pipelines/phase-variants", buildBody(override)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Phasing started");
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
      api.launchSuggestion("/pipelines/phase-variants", buildBody(true)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Launching without the memory check");
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const ready = selectedIds.length > 0;

  return (
    <ModalBackdrop onClick={onClose}>
      <div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
        <h2>Phase variants</h2>

        <div className="modal-body">
          <div className="trim-inputs">
            <div className="trim-file">{object.name}</div>
            <div className="trim-mate-note">
              Assign each variant to a haplotype using read-backed phasing
              (whatsHap).
            </div>
          </div>

          <div className="trim-fields">
            <label className="trim-wide">
              <span>Alignments to phase against</span>
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
                        type="checkbox"
                        checked={selectedIds.includes(c.id)}
                        onChange={() => toggle(c.id)}
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
              <small>
                One alignment is single-sample phasing; several is multi-sample
                (polyphase). They must all align to this VCF's reference.
              </small>
            </label>

            <label>
              <span>Mode</span>
              <select value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="phase">Single sample (phase)</option>
                <option value="polyphase">Multi-sample (polyphase)</option>
              </select>
              <small>
                Polyphase phases across the selected alignments as distinct
                samples.
              </small>
            </label>

            {mode === "phase" && (
              <label>
                <span>Sample name (optional)</span>
                <input
                  type="text"
                  value={sample}
                  placeholder="From the BAM's read group"
                  onChange={(e) => setSample(e.target.value)}
                />
                <small>
                  Defaults to the alignment's sample. Ignored in polyphase.
                </small>
              </label>
            )}

            <div className="phase-flags">
              <label className="phase-flag">
                <input
                  type="checkbox"
                  checked={ignoreReadGroups}
                  onChange={(e) => setIgnoreReadGroups(e.target.checked)}
                />
                <span>Ignore read groups</span>
              </label>
              <label className="phase-flag">
                <input
                  type="checkbox"
                  checked={distrustGenotypes}
                  onChange={(e) => setDistrustGenotypes(e.target.checked)}
                />
                <span>Distrust genotypes</span>
              </label>
              <label className="phase-flag">
                <input
                  type="checkbox"
                  checked={indels}
                  onChange={(e) => setIndels(e.target.checked)}
                />
                <span>Phase insertions and deletions</span>
              </label>
            </div>

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
                  ? `Phasing reserves ${(refusal.declared_mb ?? 0).toLocaleString()} MB, ` +
                    `more than the ${refusal.budget_mb.toLocaleString()} MB budget. ` +
                    `Nothing about the run changes that number.`
                  : `Phasing needs about ${(refusal.estimate_mb ?? 0).toLocaleString()} MB, ` +
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
            {launch.isPending ? "Starting…" : "Phase variants"}
          </button>
        </div>
      </div>
    </ModalBackdrop>
  );
}
