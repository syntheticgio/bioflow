import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type {
  AlignParams,
  AlignerName,
  AlignPreset,
  DataObject,
  ReadGroup,
} from "../api/types";

const PRESET_LABELS: Record<AlignPreset, string> = {
  sr: "Short read (Illumina)",
  "map-ont": "Oxford Nanopore",
  "map-pb": "PacBio",
};

/**
 * Launch an alignment of FASTQ reads against a reference.
 *
 * Defaults -- read group, aligner, preset -- come from the server, which reads
 * them from the file's own metadata. A second copy of that logic here would
 * drift from the one a run actually uses, and the platform mapping in
 * particular is not obvious enough to duplicate.
 */
export function AlignDialog({
  object,
  selectedTool,
  onBack,
  onClose,
}: {
  object: DataObject;
  /**
   * The aligner chosen in `PipelineToolSelector`. Unlike TrimDialog's prop,
   * this one steers the run: both minimap2 and bwa-mem2 have working handlers
   * (see align_handlers.py), so the choice is real -- it overrides
   * `params.aligner` below, taking the place of the `<select>` this replaced.
   */
  selectedTool?: AlignerName;
  /** Returns to the tool selector, keeping the chosen aligner highlighted. */
  onBack?: () => void;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const { data: defaults } = useQuery({
    queryKey: ["pipelines", "align-defaults", object.id],
    queryFn: () => api.alignDefaults(object.id),
  });

  const { data: refs, isLoading: refsLoading } = useQuery({
    queryKey: ["pipelines", "references", object.project_id],
    queryFn: () => api.references(object.project_id),
  });

  const { data: mate, isLoading: mateLoading } = useQuery({
    queryKey: ["pipelines", "mate", object.id],
    queryFn: () => api.detectMate(object.id),
  });

  const [referenceId, setReferenceId] = useState<string | null>(null);
  const [paired, setPaired] = useState(true);
  const [overrides, setOverrides] = useState<Partial<AlignParams>>({});
  const [rgOverrides, setRgOverrides] = useState<Partial<ReadGroup>>({});
  const [advanced, setAdvanced] = useState(false);

  // `selectedTool` wins over both the server default and the advanced
  // override, and does so on every render rather than via an effect that
  // seeds `overrides` once: `alignerInfo` and `needsIndex` below are derived
  // from `params.aligner` synchronously, and an effect fires after the
  // dialog has already painted with whatever `defaults.params.aligner` was --
  // a real window where the ready/index state reflects the wrong aligner,
  // not just a cosmetic flicker.
  const params = {
    ...defaults?.params,
    ...overrides,
    ...(selectedTool ? { aligner: selectedTool } : {}),
  } as AlignParams;
  const readGroup = { ...defaults?.read_group, ...rgOverrides } as ReadGroup;

  const references = refs?.references ?? [];
  // Prefer a file explicitly marked as a reference; otherwise the first FASTA.
  const chosenId =
    referenceId ??
    references.find((r) => r.role === "reference")?.object_id ??
    references[0]?.object_id ??
    null;
  const chosen = references.find((r) => r.object_id === chosenId) ?? null;

  const usePair = paired && mate != null;
  const aligner = params?.aligner;
  const alignerInfo = defaults?.aligners.find((a) => a.name === aligner);

  // Whether this run has to build an index first. Surfaced rather than left as
  // a surprise: an index build can take considerably longer than the alignment.
  const needsIndex =
    chosen != null && aligner != null
      ? !chosen.indexes[aligner] || !chosen.indexes.fai
      : false;

  const launch = useMutation({
    mutationFn: () =>
      api.launchAlignment({
        object_id: object.id,
        reference_id: chosenId!,
        mate_object_id: usePair ? mate!.object_id : null,
        paired: usePair,
        read_group: readGroup,
        params: overrides,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success(
        needsIndex ? "Building the index, then aligning" : "Alignment started",
      );
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const set = <K extends keyof AlignParams>(key: K, value: AlignParams[K]) =>
    setOverrides((o) => ({ ...o, [key]: value }));

  const setRg = <K extends keyof ReadGroup>(key: K, value: ReadGroup[K]) =>
    setRgOverrides((o) => ({ ...o, [key]: value }));

  const rgComplete =
    !!readGroup?.sample && !!readGroup?.library && !!readGroup?.platform;
  const ready =
    defaults != null &&
    chosenId != null &&
    rgComplete &&
    alignerInfo?.available === true;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
        <h2>
          Align reads
          {aligner && <span className="dialog-tool-subtitle"> — {aligner}</span>}
          {onBack && (
            <button type="button" className="dialog-tool-back" onClick={onBack}>
              change tool
            </button>
          )}
        </h2>

        {alignerInfo && !alignerInfo.available && (
          <div className="error-box" style={{ marginBottom: 12 }}>
            {alignerInfo.name} is not available on this machine.
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
                Align together with <strong>{mate.name}</strong>
              </span>
            </label>
          ) : (
            <div className="trim-mate-note">
              No paired mate found — aligning as single-end.
            </div>
          )}
        </div>

        <div className="trim-fields">
          <label className="trim-wide">
            <span>Reference</span>
            {refsLoading ? (
              <div className="trim-mate-note">Looking for references…</div>
            ) : references.length === 0 ? (
              <div className="warn-box" style={{ fontSize: 12 }}>
                No FASTA reference in this project. Upload one first.
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
            {needsIndex && (
              <small>
                This reference has no {aligner} index yet — one will be built
                first, which can take longer than the alignment itself.
              </small>
            )}
          </label>
        </div>

        <fieldset className="trim-fields">
          <legend>
            Read group
            <small style={{ display: "block", fontWeight: "normal" }}>
              Variant callers refuse to run without these, and adding them later
              means rewriting the whole BAM.
            </small>
          </legend>
          <label>
            <span>Sample</span>
            <input
              type="text"
              value={readGroup?.sample ?? ""}
              onChange={(e) => setRg("sample", e.target.value)}
            />
          </label>
          <label>
            <span>Library</span>
            <input
              type="text"
              value={readGroup?.library ?? ""}
              onChange={(e) => setRg("library", e.target.value)}
            />
          </label>
          <label>
            <span>Platform</span>
            <input
              type="text"
              value={readGroup?.platform ?? ""}
              onChange={(e) => setRg("platform", e.target.value)}
            />
          </label>
        </fieldset>

        <button
          type="button"
          className="trim-advanced-toggle"
          onClick={() => setAdvanced((a) => !a)}
          aria-expanded={advanced}
        >
          <span className="trim-chevron">{advanced ? "▾" : "▸"}</span>
          Aligner and performance
        </button>

        {advanced && (
          <div className="trim-fields">
            {/* No aligner <select> here: the tool selector is now where that
                choice is made (PipelineToolSelector, via `selectedTool`),
                seeded into `params.aligner` above. `aligner` still drives
                everything below exactly as the select's value used to. */}
            {params?.aligner === "minimap2" && (
              <label>
                <span>Read type</span>
                <select
                  value={params?.preset || "sr"}
                  onChange={(e) => set("preset", e.target.value as AlignPreset)}
                >
                  {defaults?.presets.map((p) => (
                    <option key={p} value={p}>
                      {PRESET_LABELS[p] ?? p}
                    </option>
                  ))}
                </select>
                <small>
                  The wrong choice aligns long reads poorly rather than failing.
                </small>
              </label>
            )}

            <label>
              <span>Threads</span>
              <input
                type="number"
                min={1}
                max={16}
                value={params?.threads ?? 4}
                onChange={(e) => set("threads", Number(e.target.value))}
              />
              <small>More threads finish sooner but compete with other work.</small>
            </label>

            <label>
              <span>Sort memory (MB per thread)</span>
              <input
                type="number"
                min={64}
                step={256}
                value={params?.sort_memory_mb ?? 1024}
                onChange={(e) => set("sort_memory_mb", Number(e.target.value))}
              />
              <small>samtools spills to disk when it runs out, which is slower.</small>
            </label>

            <label className="trim-check trim-wide">
              <input
                type="checkbox"
                checked={params?.mark_duplicates ?? false}
                onChange={(e) => set("mark_duplicates", e.target.checked)}
              />
              <span>
                Mark duplicates
                <small style={{ display: "block" }}>
                  Standard for DNA-seq variant calling. Wrong for RNA-seq and
                  amplicon data, where duplicates are expected.
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
            {launch.isPending
              ? "Starting…"
              : needsIndex
                ? "Build index and align"
                : usePair
                  ? "Align pair"
                  : "Align"}
          </button>
        </div>
      </div>
    </div>
  );
}
