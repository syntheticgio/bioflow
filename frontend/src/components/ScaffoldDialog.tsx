import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { ModalBackdrop } from "./ModalBackdrop";
import { NodeSelector } from "./NodeSelector";
import { notify } from "../stores/messageStore";
import type { DataObject } from "../api/types";

const DIVERGENCE_OPTIONS = [
  { value: "same_species", label: "Same species", hint: "minimap2 -x asm5 (default)" },
  { value: "same_genus", label: "Same genus / a close relative", hint: "minimap2 -x asm10" },
  { value: "distant", label: "A more distant relative", hint: "minimap2 -x asm20" },
] as const;

/**
 * Launch RagTag against a draft assembly and a chosen reference.
 *
 * Unlike PolishDialog-that-never-was -- polishing launches straight from its
 * Actions-tab card, with no dialog at all, because one project usually has
 * one eligible short-read set -- scaffolding needs a real chooser. A project
 * holding two reference-role assemblies for one organism is the *ordinary*
 * case here (see the backend card's own comment: the real yeast project
 * carries both the GCA and GCF genomic FASTA), so the card refuses whenever
 * there is more than one candidate and this dialog is where the launch
 * actually happens for that case.
 *
 * `api.references` is the same endpoint AlignDialog uses, and deliberately
 * looser than the card's own filter: it returns every FASTA of reference
 * shape in the project, not just ones marked `role=reference`. A human is
 * choosing here, so the dialog can afford to show more candidates than the
 * card can safely guess between -- the same reasoning the backend's own
 * `build_align_card` comment gives for the same asymmetry.
 */
export function ScaffoldDialog({
  object,
  onClose,
  prefill,
}: {
  object: DataObject;
  onClose: () => void;
  /**
   * A suggestion card's launch body, when the dialog was opened by "Adjust…".
   *
   * Seeds the fields the card had already decided, so the dialog opens on
   * that card's run rather than on the generic defaults. Null when opened
   * from the Computations row, which is the unchanged path.
   */
  prefill?: Record<string, unknown> | null;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const { data: refs, isLoading: refsLoading } = useQuery({
    queryKey: ["pipelines", "references", object.project_id],
    queryFn: () => api.references(object.project_id),
  });

  // `reference_object_id`, not `reference_id`: the scaffold endpoint names
  // both of its objects (`draft_object_id` / `reference_object_id`), unlike
  // align and variants which key the subject as `object_id` / `bam_id`.
  const [referenceId, setReferenceId] = useState<string | null>(
    () => (prefill?.reference_object_id as string) ?? null,
  );
  const [divergence, setDivergence] = useState<string>("same_species");
  const [targetNode, setTargetNode] = useState("");

  const references = (refs?.references ?? []).filter(
    (r) => r.object_id !== object.id,
  );
  const preferredId = references.find((r) => r.role === "reference")?.object_id;
  const chosenId = referenceId ?? preferredId ?? references[0]?.object_id ?? null;

  const launch = useMutation({
    mutationFn: () =>
      api.launchScaffold({
        draft_object_id: object.id,
        reference_object_id: chosenId,
        divergence,
      }, targetNode || undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Scaffolding started");
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const ready = chosenId != null && !launch.isPending;

  return (
    <ModalBackdrop onClick={onClose}>
      <div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
        <h2>
          Scaffold assembly
          <span className="dialog-tool-subtitle"> — RagTag</span>
        </h2>

        <div className="modal-body">
          <div className="trim-inputs">
            <div className="trim-file">{object.name}</div>
          </div>

          <div className="trim-fields">
            <label className="trim-wide">
              <span>Reference assembly</span>
              {refsLoading ? (
                <div className="trim-mate-note">Loading references…</div>
              ) : references.length === 0 ? (
                <div className="trim-mate-note">
                  No reference assembly in this project. Upload or download
                  one first.
                </div>
              ) : (
                <select
                  value={chosenId ?? ""}
                  onChange={(e) => setReferenceId(e.target.value)}
                >
                  {references.map((r) => (
                    <option key={r.object_id} value={r.object_id}>
                      {r.name}
                      {r.role === "reference" ? "" : " (not marked as a reference)"}
                    </option>
                  ))}
                </select>
              )}
            </label>

            <label className="trim-wide">
              <span>Divergence from the reference</span>
              <select
                value={divergence}
                onChange={(e) => setDivergence(e.target.value)}
              >
                {DIVERGENCE_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
              <small>
                {
                  DIVERGENCE_OPTIONS.find((o) => o.value === divergence)?.hint
                }
                . A closer setting than the truth finds fewer alignments and
                places fewer contigs, which looks like a poor assembly rather
                than a wrong setting.
              </small>
            </label>
          </div>

          <div className="trim-mate-note" style={{ marginTop: 12 }}>
            Scaffolds are ordered and oriented to match the reference. Real
            structural differences between your sample and the reference —
            a translocation, a fusion, a different chromosome count — will
            not appear in the result; it will show the reference's
            arrangement with your sample's sequence in it.
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
            disabled={!ready}
            onClick={() => launch.mutate()}
          >
            {launch.isPending ? "Starting…" : "Scaffold"}
          </button>
        </div>
      </div>
    </ModalBackdrop>
  );
}
