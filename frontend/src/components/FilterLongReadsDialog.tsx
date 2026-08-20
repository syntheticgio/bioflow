import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { ModalBackdrop } from "./ModalBackdrop";
import { NodeSelector } from "./NodeSelector";
import { notify } from "../stores/messageStore";
import type { DataObject, FiltlongParams } from "../api/types";

/** Defaults for Filtlong long-read length/quality filtering.

   Mirrors `filtlong_runner.FiltlongParams` on the backend. These are the
   starting values the dialog seeds from -- a server defaults endpoint is
   overkill for one tool with no per-chemistry variation. */
const DEFAULTS: FiltlongParams = {
  min_length: 1000,
  min_mean_q: 10,
  keep_percent: 90,
  target_bases: null,
  threads: 4,
};

/**
 * Launch Filtlong length/quality filtering over a single FASTQ of long reads.
 *
 * Filtlong takes one read stream at a time and has no paired-end mode, so
 * this dialog has no mate handling unlike `TrimDialog`.
 */
export function FilterLongReadsDialog({
  object,
  onClose,
  prefill,
}: {
  object: DataObject;
  onClose: () => void;
  /**
   * A suggestion card's launch body, when the dialog was opened by "Adjust…".
   * Seeds the form fields to match the card's run.
   */
  prefill?: Record<string, unknown> | null;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const [targetNode, setTargetNode] = useState("");

  // Seeded from the card, not merged after the fact: `params` layers the
  // field values onto the defaults, so putting the card's values in as the
  // initial state makes the card's run the one the dialog opens on, with
  // every field still editable.
  const [params, setParams] = useState<Partial<FiltlongParams>>(() => {
    const cardParams = (prefill?.params as Partial<FiltlongParams> | undefined);
    return cardParams ? { ...DEFAULTS, ...cardParams } : DEFAULTS;
  });

  const launch = useMutation({
    mutationFn: () =>
      api.launchFilterLongReads(
        {
          object_id: object.id,
          mate_object_id: null,
          params: params,
        },
        targetNode || undefined,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["suggestions", object.id] });
      notify.success("Filtering queued");
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const handleChange = <K extends keyof FiltlongParams>(
    key: K,
    value: string,
  ) => {
    setParams((p) => ({ ...p, [key]: value === "" ? null : Number(value) }));
  };

  return (
    <ModalBackdrop onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>Filter long reads</h2>

        <div className="modal-body">
          <div className="trim-file">{object.name}</div>

          <p className="section-note" style={{ fontSize: 12 }}>
            Filtlong keeps reads by length and quality, unlike adapter
            trimming. It takes a single read stream -- no mate pairing.
          </p>

          <div className="trim-fields">
            <label>
              <span>Min length</span>
              <input
                type="number"
                min={1}
                value={params.min_length ?? DEFAULTS.min_length}
                onChange={(e) => handleChange("min_length", e.target.value)}
              />
              <small>
                Discard reads shorter than this many bases.
              </small>
            </label>

            <label>
              <span>Min mean quality</span>
              <input
                type="number"
                min={0}
                value={params.min_mean_q ?? DEFAULTS.min_mean_q}
                onChange={(e) => handleChange("min_mean_q", e.target.value)}
              />
              <small>
                Discard reads whose average Phred quality is below this.
              </small>
            </label>

            <label>
              <span>Keep percent</span>
              <input
                type="number"
                min={0}
                max={100}
                value={params.keep_percent ?? DEFAULTS.keep_percent}
                onChange={(e) => handleChange("keep_percent", e.target.value)}
              />
              <small>
                Keep only the top N percent of reads (by length then
                quality).
              </small>
            </label>

            <label>
              <span>Target bases</span>
              <input
                type="number"
                min={1}
                value={params.target_bases ?? ""}
                onChange={(e) => handleChange("target_bases", e.target.value)}
                placeholder="off"
              />
              <small>
                Keep reads until this many bases are retained, then discard
                the rest. Leave empty to use keep_percent instead.
              </small>
            </label>

            <label>
              <span>Threads</span>
              <input
                type="number"
                min={1}
                max={16}
                value={params.threads ?? DEFAULTS.threads}
                onChange={(e) => handleChange("threads", e.target.value)}
              />
              <small>
                More threads finish sooner but compete with other work.
              </small>
            </label>
          </div>
        </div>

        <NodeSelector value={targetNode} onChange={setTargetNode} fullWidth />

        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn primary"
            onClick={() => launch.mutate()}
            disabled={launch.isPending}
          >
            {launch.isPending ? "Starting…" : "Filter reads"}
          </button>
        </div>
      </div>
    </ModalBackdrop>
  );
}
