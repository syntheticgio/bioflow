import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { CutadaptParams, DataObject, TrimmomaticParams, TrimParams } from "../api/types";

// Chemistries qc_stats.infer_chemistry can report for a long-read file. Kept
// in sync with ReadChemistry on the backend rather than imported, since the
// frontend has no access to backend enums.
const LONG_READ_CHEMISTRIES = new Set(["hifi", "clr", "ont_simplex", "ont_duplex"]);

/**
 * Whether fastp's short-read assumptions are the wrong tool for this file.
 *
 * Mirrors `is_long_read` on the backend: chemistry, when QC has already
 * inferred it, is the more specific fact and wins; a file nobody has QC'd
 * yet falls back to a coarse read of the platform label. Not the full
 * substring table `sam_platform` uses server-side -- this only needs to
 * catch the common instrument names well enough to warn, not to be the
 * source of truth the alignment preset relies on.
 */
function isLongRead(object: DataObject): boolean {
  const chemistry = object.facts?.qc_read_chemistry;
  if (typeof chemistry === "string" && chemistry) {
    return LONG_READ_CHEMISTRIES.has(chemistry);
  }
  const platform = String(object.metadata?.platform ?? "").toLowerCase();
  return /nanopore|minion|gridion|promethion|flongle|pacbio|sequel|revio/.test(platform);
}

/**
 * Launch adapter trimming over a FASTQ file, or an R1/R2 pair.
 *
 * Defaults come from the server rather than being duplicated here: they are
 * the active tool's own, and a second copy in the form would drift from the
 * ones a run actually uses.
 */
export function TrimDialog({
  object,
  selectedTool,
  onBack,
  onClose,
}: {
  object: DataObject;
  /** The tool chosen in `PipelineToolSelector`. Defaults to fastp. */
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

  const activeTool = selectedTool ?? "fastp";

  const { data: defaults } = useQuery({
    queryKey: ["pipelines", "defaults", activeTool],
    queryFn: () => api.trimDefaults(activeTool),
    staleTime: 60_000,
  });

  const { data: mate, isLoading: mateLoading } = useQuery({
    queryKey: ["pipelines", "mate", object.id],
    queryFn: () => api.detectMate(object.id),
  });

  const [paired, setPaired] = useState(true);
  const [overrides, setOverrides] = useState<Partial<TrimParams>>({});
  const [advanced, setAdvanced] = useState(false);

  const params = { ...defaults?.params, ...overrides };
  const activeToolInfo = tools?.tools.find((t) => t.name === activeTool);
  const usePair = paired && mate != null;

  const launch = useMutation({
    mutationFn: () =>
      api.launchTrim({
        object_id: object.id,
        mate_object_id: usePair ? mate!.object_id : null,
        paired: usePair,
        params: overrides,
        tool: activeTool,
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

  const ready = defaults != null && activeToolInfo?.available === true;
  const longRead = isLongRead(object);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
        <h2>
          Trim reads
          {onBack && (
            <button type="button" className="dialog-tool-back" onClick={onBack}>
              change tool
            </button>
          )}
        </h2>

        {activeToolInfo && !activeToolInfo.available && (
          <div className="error-box" style={{ marginBottom: 12 }}>
            {activeToolInfo.error ?? `${activeTool} is not available`}
          </div>
        )}

        {longRead && (
          <div className="warn-box" style={{ marginBottom: 12, fontSize: 12 }}>
            This looks like a long-read file. {activeTool}'s adapter detection
            and length filters are built for short reads, and default
            settings can discard most of an ONT or PacBio run — QC with
            NanoPlot is usually a better next step than trimming here.
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

        {activeTool === "fastp" && (
          <>
            <div className="trim-fields">
              <label>
                <span>Min length</span>
                <input
                  type="number"
                  min={1}
                  value={(params as TrimParams).min_length ?? 15}
                  onChange={(e) => setOverrides((o) => ({ ...o, min_length: Number(e.target.value) }))}
                />
                <small>Reads shorter than this after trimming are discarded.</small>
              </label>
              <label>
                <span>Quality threshold</span>
                <input
                  type="number"
                  min={0}
                  max={40}
                  value={(params as TrimParams).quality_threshold ?? 15}
                  onChange={(e) => setOverrides((o) => ({ ...o, quality_threshold: Number(e.target.value) }))}
                />
                <small>Phred score below which a base counts as unqualified.</small>
              </label>
              <label>
                <span>Threads</span>
                <input
                  type="number"
                  min={1}
                  max={16}
                  value={(params as TrimParams).threads ?? 4}
                  onChange={(e) => setOverrides((o) => ({ ...o, threads: Number(e.target.value) }))}
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
                    placeholder={usePair ? "auto-detected by overlap analysis" : "auto-detected"}
                    value={(params as TrimParams).adapter_r1 ?? ""}
                    onChange={(e) => setOverrides((o) => ({ ...o, adapter_r1: e.target.value || null }))}
                  />
                  <small>
                    Leave empty unless you know the sequence — for paired reads
                    fastp detects it from the overlap, which is more reliable.
                  </small>
                </label>
                <label className="trim-check trim-wide">
                  <input
                    type="checkbox"
                    checked={(params as TrimParams).dedup ?? false}
                    onChange={(e) => setOverrides((o) => ({ ...o, dedup: e.target.checked }))}
                  />
                  <span>Remove duplicate reads</span>
                </label>
                <label className="trim-check trim-wide">
                  <input
                    type="checkbox"
                    checked={(params as TrimParams).trim_poly_g === true}
                    onChange={(e) => setOverrides((o) => ({ ...o, trim_poly_g: e.target.checked ? true : null }))}
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
          </>
        )}

        {activeTool === "cutadapt" && (
          <div className="trim-fields">
            <label>
              <span>Min length</span>
              <input
                type="number"
                min={1}
                value={(params as CutadaptParams).min_length ?? 1}
                onChange={(e) => setOverrides((o) => ({ ...o, min_length: Number(e.target.value) }))}
              />
              <small>Reads shorter than this after trimming are discarded.</small>
            </label>
            <label>
              <span>Quality cutoff</span>
              <input
                type="number"
                min={0}
                max={40}
                value={(params as CutadaptParams).quality_cutoff ?? 20}
                onChange={(e) => setOverrides((o) => ({ ...o, quality_cutoff: Number(e.target.value) }))}
              />
              <small>3' quality trimming threshold (cutadapt's -q).</small>
            </label>
            <label className="trim-wide">
              <span>Adapter sequence (read 1)</span>
              <input
                type="text"
                placeholder="required — cutadapt has no auto-detection"
                value={(params as CutadaptParams).adapter_r1 ?? ""}
                onChange={(e) => setOverrides((o) => ({ ...o, adapter_r1: e.target.value || null }))}
              />
              <small>
                Unlike fastp, cutadapt does not detect adapters automatically —
                leave empty only if you want quality trimming with no adapter
                search.
              </small>
            </label>
            <label>
              <span>Threads</span>
              <input
                type="number"
                min={1}
                max={16}
                value={(params as CutadaptParams).threads ?? 4}
                onChange={(e) => setOverrides((o) => ({ ...o, threads: Number(e.target.value) }))}
              />
            </label>
          </div>
        )}

        {activeTool === "trimmomatic" && (
          <div className="trim-fields">
            <label>
              <span>Min length</span>
              <input
                type="number"
                min={1}
                value={(params as TrimmomaticParams).min_length ?? 36}
                onChange={(e) => setOverrides((o) => ({ ...o, min_length: Number(e.target.value) }))}
              />
              <small>Reads shorter than this are dropped (MINLEN).</small>
            </label>
            <label>
              <span>Sliding window quality</span>
              <input
                type="number"
                min={0}
                max={40}
                value={(params as TrimmomaticParams).sliding_window_quality ?? 15}
                onChange={(e) => setOverrides((o) => ({ ...o, sliding_window_quality: Number(e.target.value) }))}
              />
              <small>Average quality required within the sliding window.</small>
            </label>
            <label>
              <span>Threads</span>
              <input
                type="number"
                min={1}
                max={16}
                value={(params as TrimmomaticParams).threads ?? 4}
                onChange={(e) => setOverrides((o) => ({ ...o, threads: Number(e.target.value) }))}
              />
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
