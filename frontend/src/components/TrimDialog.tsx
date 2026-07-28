import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { DataObject, TrimParams } from "../api/types";

/**
 * Launch adapter trimming over a FASTQ file, or an R1/R2 pair.
 *
 * Defaults come from the server rather than being duplicated here: they are
 * fastp's own, and a second copy in the form would drift from the ones a run
 * actually uses.
 */
export function TrimDialog({
  object,
  selectedTool,
  onBack,
  onClose,
}: {
  object: DataObject;
  /**
   * The tool chosen in `PipelineToolSelector`. Display-only for now: fastp is
   * the only trimmer with a parameter model and a job handler, so this names
   * what will actually run rather than steering anything -- see
   * tool-selector-implementation.md §3.4 and pipeline-tool-additions-qc.md
   * §1.6 for the runners this is waiting on.
   */
  selectedTool?: string;
  /** Returns to the tool selector, keeping the chosen tool highlighted. */
  onBack?: () => void;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const { data: tools } = useQuery({
    queryKey: ["pipelines", "tools"],
    queryFn: api.pipelineTools,
    staleTime: 60_000,
  });

  const { data: defaults } = useQuery({
    queryKey: ["pipelines", "defaults"],
    queryFn: api.trimDefaults,
    staleTime: 60_000,
  });

  const { data: mate, isLoading: mateLoading } = useQuery({
    queryKey: ["pipelines", "mate", object.id],
    queryFn: () => api.detectMate(object.id),
  });

  const [paired, setPaired] = useState(true);
  const [overrides, setOverrides] = useState<Partial<TrimParams>>({});
  const [advanced, setAdvanced] = useState(false);

  const params = { ...defaults?.params, ...overrides } as TrimParams;
  const fastp = tools?.tools.find((t) => t.name === "fastp");
  const usePair = paired && mate != null;

  const launch = useMutation({
    mutationFn: () =>
      api.launchTrim({
        object_id: object.id,
        mate_object_id: usePair ? mate!.object_id : null,
        paired: usePair,
        params: overrides,
      }),
    onSuccess: (job) => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success(usePair ? "Trimming the pair" : "Trimming started");
      onClose();
      navigate("/activity");
      return job;
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const set = <K extends keyof TrimParams>(key: K, value: TrimParams[K]) =>
    setOverrides((o) => ({ ...o, [key]: value }));

  const ready = defaults != null && fastp?.available === true;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
        <h2>
          Trim reads
          {selectedTool && selectedTool !== "fastp" && (
            <span className="dialog-tool-subtitle"> — {selectedTool}</span>
          )}
          {onBack && (
            <button type="button" className="dialog-tool-back" onClick={onBack}>
              change tool
            </button>
          )}
        </h2>

        {selectedTool && selectedTool !== "fastp" && (
          <div className="warn-box" style={{ marginBottom: 12, fontSize: 12 }}>
            {selectedTool} was selected, but only fastp can be launched today
            — its parameters are shown below instead.
          </div>
        )}

        {fastp && !fastp.available && (
          <div className="error-box" style={{ marginBottom: 12 }}>
            {fastp.error ?? "fastp is not available"}
          </div>
        )}

        <div className="trim-inputs">
          <div className="trim-file">{object.name}</div>
          {mateLoading ? (
            <div className="trim-mate-note">Looking for a mate…</div>
          ) : mate ? (
            <label className="trim-check">
              <input
                type="checkbox"
                checked={paired}
                onChange={(e) => setPaired(e.target.checked)}
              />
              <span>
                Trim together with <strong>{mate.name}</strong>
              </span>
            </label>
          ) : (
            <div className="trim-mate-note">
              No paired mate found — trimming as single-end.
            </div>
          )}
          {mate && !paired && (
            <div className="warn-box" style={{ fontSize: 12 }}>
              Trimming mates separately can desynchronize them, which breaks
              paired alignment downstream.
            </div>
          )}
        </div>

        <div className="trim-fields">
          <label>
            <span>Min length</span>
            <input
              type="number"
              min={1}
              value={params.min_length ?? 15}
              onChange={(e) => set("min_length", Number(e.target.value))}
            />
            <small>Reads shorter than this after trimming are discarded.</small>
          </label>

          <label>
            <span>Quality threshold</span>
            <input
              type="number"
              min={0}
              max={40}
              value={params.quality_threshold ?? 15}
              onChange={(e) => set("quality_threshold", Number(e.target.value))}
            />
            <small>Phred score below which a base counts as unqualified.</small>
          </label>

          <label>
            <span>Threads</span>
            <input
              type="number"
              min={1}
              max={16}
              value={params.threads ?? 4}
              onChange={(e) => set("threads", Number(e.target.value))}
            />
            <small>More threads finish sooner but compete with other work.</small>
          </label>
        </div>

        <button
          type="button"
          className="trim-advanced-toggle"
          onClick={() => setAdvanced((a) => !a)}
          aria-expanded={advanced}
        >
          <span className="trim-chevron">{advanced ? "▾" : "▸"}</span>
          Adapters and filtering
        </button>

        {advanced && (
          <div className="trim-fields">
            <label className="trim-wide">
              <span>Adapter sequence (read 1)</span>
              <input
                type="text"
                placeholder={
                  usePair ? "auto-detected by overlap analysis" : "auto-detected"
                }
                value={params.adapter_r1 ?? ""}
                onChange={(e) => set("adapter_r1", e.target.value || null)}
              />
              <small>
                Leave empty unless you know the sequence — for paired reads
                fastp detects it from the overlap, which is more reliable.
              </small>
            </label>

            <label className="trim-check trim-wide">
              <input
                type="checkbox"
                checked={params.dedup ?? false}
                onChange={(e) => set("dedup", e.target.checked)}
              />
              <span>Remove duplicate reads</span>
            </label>

            <label className="trim-check trim-wide">
              <input
                type="checkbox"
                checked={params.trim_poly_g === true}
                onChange={(e) => set("trim_poly_g", e.target.checked ? true : null)}
              />
              <span>
                Force polyG trimming
                <small style={{ display: "block" }}>
                  Off by default because fastp enables it automatically for
                  two-colour instruments.
                </small>
              </span>
            </label>
          </div>
        )}

        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn primary"
            onClick={() => launch.mutate()}
            disabled={!ready || launch.isPending}
          >
            {launch.isPending ? "Starting…" : usePair ? "Trim pair" : "Trim"}
          </button>
        </div>
      </div>
    </div>
  );
}
