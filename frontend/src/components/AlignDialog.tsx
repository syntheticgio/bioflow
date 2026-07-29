import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { AlignerParamFields } from "./AlignerParamFields";
import { classify, estimateMb, explain } from "../lib/estimate";
import { notify } from "../stores/messageStore";
import type {
  AlignParams,
  AlignerName,
  DataObject,
  ReadGroup,
} from "../api/types";

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

  const { data: schema } = useQuery({
    queryKey: ["pipelines", "aligner-schema", params?.aligner],
    queryFn: () => api.alignerSchema(params.aligner),
    enabled: !!params?.aligner,
  });

  const { data: envelope } = useQuery({
    queryKey: ["pipelines", "align-envelope", object.id, chosenId],
    queryFn: () => api.alignEnvelope(object.id, chosenId!),
    enabled: chosenId != null,
  });

  // The same arithmetic the backend runs at launch. Local so the numbers move
  // with the sliders; the backend re-checks authoritatively, so a drift here
  // costs a wrong preview rather than a bad run.
  const model = envelope?.models[params?.aligner ?? ""] ?? null;
  const estimate =
    model && envelope
      ? estimateMb(model, {
          referenceBases: envelope.reference_bases,
          threads: params?.threads ?? 4,
          sortMemoryMb: params?.sort_memory_mb ?? 1024,
          buildingIndex: needsIndex,
        })
      : null;
  const band =
    estimate != null && envelope
      ? classify({
          estimateMb: estimate,
          memBudgetMb: envelope.mem_budget_mb,
          threads: params?.threads ?? 4,
          cpuBudget: envelope.cpu_budget,
        })
      : "ok";
  const bandMessage =
    model && envelope && band !== "ok"
      ? explain(model, envelope, {
          threads: params?.threads ?? 4,
          sortMemoryMb: params?.sort_memory_mb ?? 1024,
          buildingIndex: needsIndex,
        })
      : null;

  // The banner below tells the user to change threads/sort_memory_mb, both of
  // which live in the "performance" disclosure -- so once the resource band
  // is anything but "ok", that disclosure must be visible or the fix it's
  // pointing at is hidden. Auto-expand for both warn and block and ignore a
  // manual collapse while either holds: for "block" the launch button is
  // disabled, so hiding the very fields needed to unblock it would defeat the
  // point of showing the banner at all; treating "warn" the same way keeps
  // the rule a simple one-liner instead of a second state to reason about,
  // and a user can still collapse the section once the band returns to "ok".
  const showAdvanced = advanced || band !== "ok";

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
    alignerInfo?.available === true &&
    band !== "block";

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

        <div className="modal-body">
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

        {schema && (
          <div className="trim-fields">
            <AlignerParamFields
              fields={schema.fields.filter((f) => f.group === "biology")}
              params={params}
              onChange={(k, v) =>
                // The registry's field metadata is validated server-side (see
                // test_aligner_registry.py's TestFieldMetadataMatchesParams),
                // so a field key reaching here is guaranteed to correspond to a
                // real AlignParams property of a compatible type -- but that
                // guarantee is enforced by a Python test, not by TypeScript,
                // since the schema is fetched at runtime. This cast is where
                // that trust boundary is crossed.
                set(k as keyof AlignParams, v as AlignParams[keyof AlignParams])
              }
            />
          </div>
        )}

        <button
          type="button"
          className="trim-advanced-toggle"
          onClick={() => setAdvanced((a) => !a)}
          aria-expanded={showAdvanced}
        >
          <span className="trim-chevron">{showAdvanced ? "▾" : "▸"}</span>
          Performance
        </button>

        {showAdvanced && schema && (
          <div className="trim-fields">
            <AlignerParamFields
              fields={schema.fields.filter((f) => f.group === "performance")}
              params={params}
              onChange={(k, v) =>
                // See the comment on the biology-fields onChange above -- same
                // trust boundary, same registry-vs-TypeScript tradeoff.
                set(k as keyof AlignParams, v as AlignParams[keyof AlignParams])
              }
            />
          </div>
        )}

        {bandMessage && (
          <div className={band === "block" ? "error-box" : "warn-box"}>
            {bandMessage}
            {band === "block" && (
              <div style={{ marginTop: 4 }}>
                Reduce threads or sort memory, or choose an aligner with a
                smaller index.
              </div>
            )}
          </div>
        )}
        </div>

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
