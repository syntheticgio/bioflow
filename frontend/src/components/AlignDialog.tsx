import { useState, useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { AlignerParamFields } from "./AlignerParamFields";
import {
  ADVANCED_PRESET_VALUE,
  BOWTIE2_CUSTOM_PRESET_VALUE,
  BOWTIE2_DEFAULT_PRESET_ID,
  hasInsertRangeError,
  hasReportingError,
  initialPresetSelection,
  isBowtie2PairOnlyField,
  shouldClearPresetOnFieldEdit,
} from "./alignDialogPresets";
import { ModalBackdrop } from "./ModalBackdrop";
import { NodeSelector } from "./NodeSelector";
import { ParameterSetPicker } from "./ParameterSetPicker";
import { ResourceRefusalCard } from "./ResourceRefusalCard";
import { isReads } from "./PairEditor";
import { supersededBySelection } from "../lib/pairing";
import { classify, estimateMb, explain } from "../lib/estimate";
import { formatBytes } from "../lib/format";
import { notify } from "../stores/messageStore";
import type {
  AlignParams,
  AlignerName,
  DataObject,
  ReadGroup,
  ReplanResult,
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
  prefill,
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

  const { data: defaults } = useQuery({
    queryKey: ["pipelines", "align-defaults", object.id],
    queryFn: () => api.alignDefaults(object.id),
  });

  const { data: refs, isLoading: refsLoading } = useQuery({
    queryKey: ["pipelines", "references", object.project_id],
    queryFn: () => api.references(object.project_id),
  });

  // The whole project's object list, so the additional-reads picker can offer
  // every FASTQ that is not already in this launch. `references` above is a
  // curated subset; a read file is not a reference and must come from here.
  const { data: objects, isLoading: objectsLoading } = useQuery({
    queryKey: ["projects", object.project_id, "objects"],
    queryFn: () => api.listObjects(object.project_id),
  });

  const { data: mate, isLoading: mateLoading } = useQuery({
    queryKey: ["pipelines", "mate", object.id],
    queryFn: () => api.detectMate(object.id),
  });

  const [referenceId, setReferenceId] = useState<string | null>(
    () => (prefill?.reference_id as string) ?? null,
  );
  const [paired, setPaired] = useState(
    () => (prefill?.paired as boolean) ?? true,
  );
  // Seeded from the card, not merged after the fact: `params` below layers
  // overrides onto the server's defaults, so putting the card's values in
  // as the initial overrides makes the card's run the one the dialog opens
  // on, with every field still editable. A lazy initializer rather than an
  // effect -- an effect would repaint the dialog a frame after it mounted,
  // and several derived values here read `params` synchronously.
  const [overrides, setOverrides] = useState<Partial<AlignParams>>(
    () => (prefill?.params as Partial<AlignParams>) ?? {},
  );
  const [rgOverrides, setRgOverrides] = useState<Partial<ReadGroup>>({});
  const [advanced, setAdvanced] = useState(false);
  const [chunked, setChunked] = useState(false);
  // Which schema preset is selected in the UI. Null means "not initialized
  // yet" rather than "use no preset": Bowtie2 needs an explicit default once
  // its schema arrives, while other aligners keep their existing advanced
  // behavior.
  const [presetOverride, setPresetOverride] = useState<string | null>(null);
  // Dismissed by "Edit parameters": the band is still "block", but the user
  // has asked to go back to the fields rather than be shown the card again.
  // Reset whenever the band leaves "block" so a fresh refusal re-renders it.
  const [cardDismissed, setCardDismissed] = useState(false);
  const [targetNode, setTargetNode] = useState("");
  // Which saved parameter set (if any) configured this launch, and the
  // values it resolved to at apply time -- the latter is what
  // `editedAfterApply` below diffs the current merged params against.
  const [appliedSet, setAppliedSet] = useState<
    { setId: string; name: string; revision: number } | null
  >(null);
  const [appliedValues, setAppliedValues] = useState<Record<
    string,
    unknown
  > | null>(null);

  // One additional read set: the file itself and, when the run is paired and
  // a mate was found, the mate that rides along with it. The mate is stored
  // next to its R1 so the pair cannot drift apart as sets are reordered.
  interface AdditionalSetState {
    objectId: string;
    mateObjectId: string | null;
  }
  const [additionalSets, setAdditionalSets] = useState<AdditionalSetState[]>(
    [],
  );
  const [addingSet, setAddingSet] = useState(false);
  // Reveals the raw reads held back because a trimmed file derived from them
  // is already in this launch. Off by default: the common case is that the
  // trimmed version is the one wanted, and listing both reads as duplicates.
  const [showSuperseded, setShowSuperseded] = useState(false);
  const [setError, setSetError] = useState<string | null>(null);

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
  const allObjects = objects ?? [];

  // Prefer a file explicitly marked as a reference; otherwise the first FASTA.
  const chosenId =
    referenceId ??
    references.find((r) => r.role === "reference")?.object_id ??
    references[0]?.object_id ??
    null;
  const chosen = references.find((r) => r.object_id === chosenId) ?? null;

  const usePair = paired && mate != null;

  // Every file already spoken for in this launch: the primary, its mate when
  // the run is paired, and each set's members. The picker must not offer any
  // of them, and an auto-resolved mate must not collide with one.
  const usedIds = new Set<string>([
    object.id,
    ...(usePair && mate ? [mate.object_id] : []),
    ...additionalSets.flatMap((s) => [
      s.objectId,
      ...(s.mateObjectId ? [s.mateObjectId] : []),
    ]),
  ]);
  const namesById = new Map(allObjects.map((o) => [o.id, o.name]));

  // Every input this launch would have to download first. The mate is looked
  // up in `allObjects` rather than read off `mate`, which is a pairing
  // suggestion carrying only ids and names -- not the locality this needs.
  const mateObject = usePair && mate ? allObjects.find((o) => o.id === mate.object_id) : null;
  const remoteInputs: { name: string; size: number }[] = [
    object,
    ...(mateObject ? [mateObject] : []),
    ...(chosen ? [{ ...chosen, id: chosen.object_id }] : []),
  ]
    .filter((o) => o.locality === "remote")
    .map((o) => ({ name: o.name, size: o.size }));
  // The raw parents of files already added here. Offering these reads as the
  // dialog listing files you have already selected, because a trimmed file and
  // the raw it came from carry the same sample name (#564) -- so they are held
  // back behind `showSuperseded` rather than listed by default.
  const supersededIds = supersededBySelection(usedIds, allObjects);
  const selectable = allObjects.filter(
    (o) =>
      o.format.kind === "fastq" &&
      o.status === "ready" &&
      isReads(o) &&
      !usedIds.has(o.id),
  );
  const eligible = selectable.filter((o) => !supersededIds.has(o.id));
  // Valid choices in every respect except that something derived from them is
  // already in this launch. Revealed by the checkbox below, for the case where
  // the raw reads are wanted in place of the trimmed ones.
  const supersededEligible = selectable.filter((o) => supersededIds.has(o.id));
  // A paired run needs every set to have a mate; the backend refuses a
  // mateless set at launch. Surfaced here so the Launch button is not what
  // reveals it.
  const matelessSets = additionalSets.filter((s) => s.mateObjectId == null);
  const setPairingBlocked = usePair && matelessSets.length > 0;
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

  const presetSeed = initialPresetSelection({
    aligner,
    params,
    presets: schema?.presets,
  });
  const presetSelection = presetOverride ?? presetSeed;
  const activePresetId =
    presetSelection && schema?.presets?.[presetSelection] ? presetSelection : null;
  const insertRangeError = hasInsertRangeError(params);
  const reportingError = hasReportingError(params);
  const biologyFields = schema?.fields.filter((f) => {
    if (f.group !== "biology") return false;
    if (aligner === "bowtie2" && !usePair && isBowtie2PairOnlyField(f.key)) {
      return false;
    }
    if (!schema.presets) return true;
    if (aligner === "bowtie2") return true;
    return presetSelection === ADVANCED_PRESET_VALUE;
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

  // Fetched when the card is about to show, so the button's presence is
  // decided before the user sees it rather than on click.
  const { data: replan } = useQuery<ReplanResult>({
    queryKey: ["pipelines", "replan", "align_reads", params, envelope?.reference_bases, needsIndex],
    // replan_service's align proposer reads reference_bases/building_index
    // directly; they're properties of the reference, not of AlignParams, so
    // they're not in `params` itself and must be added here. Mirrors the same
    // gap already handled server-side in pipeline_service.py's own internal
    // replan() call (see the comment there).
    queryFn: () =>
      api.replan("align_reads", {
        ...params,
        reference_bases: envelope?.reference_bases ?? 0,
        building_index: needsIndex,
      } as unknown as Record<string, unknown>),
    // `band` only ever leaves "ok" once `envelope` has loaded (it's derived
    // from `estimate`, which is null until `envelope` is truthy -- see
    // above), so by the time this query is enabled `envelope` is guaranteed
    // non-null and `reference_bases` is real data, not the `?? 0` fallback.
    enabled: band === "block",
  });

  // A new refusal must re-show the card even if a previous one was dismissed.
  useEffect(() => {
    if (band !== "block") setCardDismissed(false);
  }, [band]);

  useEffect(() => {
    setPresetOverride(null);
  }, [aligner]);

  useEffect(() => {
    if (!schema?.presets) {
      setPresetOverride(null);
      return;
    }
    setPresetOverride((current) => current ?? presetSeed);
  }, [presetSeed, schema?.presets]);

  useEffect(() => {
    if (
      aligner !== "bowtie2" ||
      !schema?.presets?.[BOWTIE2_DEFAULT_PRESET_ID] ||
      presetSelection !== BOWTIE2_DEFAULT_PRESET_ID ||
      (typeof params.preset === "string" && params.preset.length > 0)
    ) {
      return;
    }
    const preset = schema.presets[BOWTIE2_DEFAULT_PRESET_ID];
    setOverrides((current) =>
      current.preset === BOWTIE2_DEFAULT_PRESET_ID
        ? current
        : {
            ...current,
            ...preset.values,
            preset: BOWTIE2_DEFAULT_PRESET_ID,
          },
    );
  }, [aligner, params.preset, presetSelection, schema?.presets]);

  // A single-end run must not send mates: the backend rejects a set that
  // declares a mate in a single-end alignment. Stripping them on the toggle
  // keeps the state honest with the run's mode.
  useEffect(() => {
    if (!usePair) {
      setAdditionalSets((sets) =>
        sets.map((s) =>
          s.mateObjectId == null ? s : { ...s, mateObjectId: null },
        ),
      );
    }
  }, [usePair]);

  // The other direction of the same rule: a set added while the run was
  // single-end has no mate, and turning pairing on makes it invalid. Try to
  // resolve one now, exactly as adding it under a paired run would have. The
  // functional update guards against racing the user removing or replacing
  // the set mid-resolve, and the backend is still authoritative at launch.
  useEffect(() => {
    if (!usePair) return;
    for (const s of additionalSets) {
      if (s.mateObjectId != null) continue;
      api
        .detectMate(s.objectId)
        .then((suggested) => {
          if (!suggested) return;
          setAdditionalSets((cur) =>
            cur.map((x) =>
              x.objectId === s.objectId && x.mateObjectId == null
                ? { ...x, mateObjectId: suggested.object_id }
                : x,
            ),
          );
        })
        .catch(() => undefined);
    }
  }, [usePair]);

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

  // Shallow compare: has anything the applied set resolved changed since it
  // was applied? Only the keys the set actually touched are checked -- a
  // field the set never set is not the set's business.
  const editedAfterApply =
    appliedValues !== null &&
    Object.entries(appliedValues).some(
      ([k, v]) => (params as unknown as Record<string, unknown>)[k] !== v,
    );

  const fromParameterSet = appliedSet
    ? {
        set_id: appliedSet.setId,
        name: appliedSet.name,
        revision: appliedSet.revision,
        edited_after_apply: editedAfterApply,
      }
    : undefined;

  const launch = useMutation({
    mutationFn: () =>
      api.launchAlignment({
        object_id: object.id,
        reference_id: chosenId!,
        mate_object_id: usePair ? mate!.object_id : null,
        paired: usePair,
        additional_read_sets: additionalSets.map((s) => ({
          object_id: s.objectId,
          mate_object_id: s.mateObjectId,
        })),
        read_group: readGroup,
        // `overrides` alone omits `selectedTool` -- that's merged into the
        // *display* `params` above (line ~75) but never written back into
        // `overrides` itself, so a launch sent `overrides` untouched would
        // carry no `aligner` key at all and the server would silently fall
        // back to its own default. Send the full merged `params` so the
        // aligner actually chosen in the tool selector is the one that runs.
        params: { ...params, chunked },
        from_parameter_set: fromParameterSet,
      }, targetNode || undefined),
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

  const launchAnyway = useMutation({
    mutationFn: () =>
      api.launchAlignment({
        object_id: object.id,
        reference_id: chosenId!,
        mate_object_id: usePair ? mate!.object_id : null,
        paired: usePair,
        additional_read_sets: additionalSets.map((s) => ({
          object_id: s.objectId,
          mate_object_id: s.mateObjectId,
        })),
        read_group: readGroup,
        params: { ...params, chunked },
        from_parameter_set: fromParameterSet,
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

  const set = <K extends keyof AlignParams>(key: K, value: AlignParams[K]) =>
    setOverrides((o) => {
      const clearPreset = shouldClearPresetOnFieldEdit({
        aligner,
        activeSelection: presetSelection,
        presets: schema?.presets,
        key: String(key),
      });
      if (clearPreset) setPresetOverride(BOWTIE2_CUSTOM_PRESET_VALUE);
      return {
        ...o,
        [key]: value,
        ...(clearPreset ? { preset: "" } : {}),
      };
    });

  const setRg = <K extends keyof ReadGroup>(key: K, value: ReadGroup[K]) =>
    setRgOverrides((o) => ({ ...o, [key]: value }));

  const removeSet = (index: number) =>
    setAdditionalSets((sets) => sets.filter((_, i) => i !== index));

  const addSet = async (objectId: string) => {
    if (!objectId || addingSet) return;
    setSetError(null);
    setAddingSet(true);
    try {
      if (usedIds.has(objectId)) {
        setSetError("That file is already in this launch.");
        return;
      }
      // Resolve the set's mate up front, like the primary's is resolved, so
      // the chip shows the pair together. The backend re-suggests and
      // re-validates authoritatively at launch; this is for display and for
      // refusing the obvious no-mate-in-a-paired-run mistake early.
      const suggested = usePair ? await api.detectMate(objectId) : null;
      const mateId = suggested?.object_id ?? null;
      if (usePair && mateId == null) {
        setSetError(
          `${namesById.get(objectId) ?? objectId} has no detected mate, and ` +
            "this alignment is paired. Uncheck the mate pairing or pair the " +
            "files first.",
        );
        return;
      }
      if (mateId && usedIds.has(mateId)) {
        setSetError(
          `${namesById.get(mateId) ?? mateId} is already used in this launch.`,
        );
        return;
      }
      setAdditionalSets((sets) => [...sets, { objectId, mateObjectId: mateId }]);
    } catch {
      setSetError("Could not look up a mate for that file.");
    } finally {
      setAddingSet(false);
    }
  };


  const rgComplete =
    // Platform is deliberately not required: the SAM spec says to omit @RG PL
    // when the technology is unknown, so a file whose instrument model is not
    // in the SAM vocabulary has no platform to offer and must still be
    // alignable. Sample and library remain required -- ReadGroup.from_dict
    // rejects those server-side.
    !!readGroup?.sample && !!readGroup?.library;
  const ready =
    defaults != null &&
    chosenId != null &&
    rgComplete &&
    alignerInfo?.available === true &&
    band !== "block" &&
    !insertRangeError &&
    !reportingError &&
    !setPairingBlocked;

  return (
    <ModalBackdrop onClick={onClose}>
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

          {additionalSets.map((s, i) => {
            const r1Name = namesById.get(s.objectId) ?? s.objectId;
            const mateName = s.mateObjectId
              ? namesById.get(s.mateObjectId) ?? s.mateObjectId
              : null;
            return (
              <div className="extra-read-row" key={s.objectId}>
                <span className="extra-read-names">
                  {r1Name}
                  {mateName && (
                    <span className="extra-read-mate"> + {mateName}</span>
                  )}
                </span>
                <button
                  type="button"
                  className="extra-read-remove"
                  onClick={() => removeSet(i)}
                  aria-label={`Remove ${r1Name}`}
                >
                  ×
                </button>
              </div>
            );
          })}

          {setPairingBlocked && matelessSets[0] && (
            <div className="error-box" style={{ marginTop: 8 }}>
              {namesById.get(matelessSets[0].objectId) ?? matelessSets[0].objectId}{" "}
              has no mate, and this alignment is paired. Remove it or pair the
              files first.
            </div>
          )}

          <div className="extra-read-add">
            {objectsLoading ? (
              <div className="trim-mate-note">Looking for other read files…</div>
            ) : (
              <>
                <select
                  value=""
                  disabled={addingSet}
                  onChange={(e) => void addSet(e.target.value)}
                >
                  <option value="">Add another read file…</option>
                  {eligible.map((o) => (
                    <option key={o.id} value={o.id}>
                      {o.name}
                    </option>
                  ))}
                  {showSuperseded &&
                    supersededEligible.map((o) => (
                      <option key={o.id} value={o.id}>
                        {o.name} (raw)
                      </option>
                    ))}
                </select>
                {supersededEligible.length > 0 && (
                  <label className="trim-check">
                    <input
                      type="checkbox"
                      checked={showSuperseded}
                      onChange={(e) => setShowSuperseded(e.target.checked)}
                    />
                    <span>Show raw versions of reads already added</span>
                  </label>
                )}
              </>
            )}
            {setError && (
              <div className="error-box" style={{ marginTop: 8 }}>
                {setError}
              </div>
            )}
          </div>
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
            {/* Named separately from the index note above: both delay the
                alignment, but a download is time on someone else's network
                and is worth stating in bytes. */}
            {remoteInputs.length > 0 && (
              <div className="warn-box" style={{ fontSize: 12 }}>
                {remoteInputs.length === 1
                  ? `${remoteInputs[0].name} is stored remotely — about ${formatBytes(remoteInputs[0].size)} will be downloaded before the alignment starts.`
                  : `${remoteInputs.length} inputs are stored remotely — about ${formatBytes(
                      remoteInputs.reduce((n, r) => n + r.size, 0),
                    )} will be downloaded before the alignment starts.`}
              </div>
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

        {/* Schema-defined presets stay in the main dialog flow. Bowtie2 keeps
            its fields editable under a named preset; other aligners keep the
            existing advanced-only override path. */}
        {schema?.presets && (
          <div className="trim-fields">
            <label>
              <span>Alignment preset</span>
              <select
                value={presetSelection ?? ""}
                onChange={(e) => {
                  // `schema?.presets` narrows the JSX above but not a nested
                  // closure -- TypeScript can't carry that narrowing across a
                  // function boundary, so it's re-checked here.
                  const presets = schema?.presets;
                  if (!presets) return;
                  const value = e.target.value;
                  setPresetOverride(value);
                  if (
                    value &&
                    value !== ADVANCED_PRESET_VALUE &&
                    value !== BOWTIE2_CUSTOM_PRESET_VALUE &&
                    presets[value]
                  ) {
                    const preset = presets[value];
                    setOverrides((o) => ({ ...o, ...preset.values, preset: value }));
                  } else if (
                    value === ADVANCED_PRESET_VALUE ||
                    value === BOWTIE2_CUSTOM_PRESET_VALUE
                  ) {
                    setOverrides((o) => ({ ...o, preset: "" }));
                  }
                }}
              >
                {Object.entries(schema.presets).map(([id, preset]) => (
                  <option key={id} value={id}>
                    {preset.label}
                  </option>
                ))}
                <option
                  value={
                    aligner === "bowtie2"
                      ? BOWTIE2_CUSTOM_PRESET_VALUE
                      : ADVANCED_PRESET_VALUE
                  }
                >
                  {aligner === "bowtie2" ? "Custom" : "Advanced / fine-grained"}
                </option>
              </select>
              {activePresetId && schema.presets[activePresetId] && (
                <small>{schema.presets[activePresetId].description}</small>
              )}
              {presetSelection === BOWTIE2_CUSTOM_PRESET_VALUE && (
                <small>Editing the fields below uses a custom Bowtie2 setup.</small>
              )}
              {presetSelection === ADVANCED_PRESET_VALUE && (
                <small>Show all individual parameters for full control.</small>
              )}
            </label>
          </div>
        )}

        {aligner && (
          <ParameterSetPicker
            tool={aligner}
            family="aligner"
            currentParams={params as unknown as Record<string, unknown>}
            onApply={(values) => {
              const nextParams = { ...params, ...values } as Partial<AlignParams>;
              Object.entries(values).forEach(([k, v]) =>
                setOverrides((o) => ({
                  ...o,
                  [k]: v as AlignParams[keyof AlignParams],
                })),
              );
              setPresetOverride(
                initialPresetSelection({
                  aligner,
                  params: nextParams,
                  presets: schema?.presets,
                }),
              );
              setAppliedValues(values);
            }}
            onAppliedSetChange={(s) => {
              setAppliedSet(s);
              if (!s) setAppliedValues(null);
            }}
          />
        )}

        {schema && (
          <div className="trim-fields">
            <AlignerParamFields
              fields={biologyFields ?? []}
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
            {insertRangeError && (
              <div className="error-box" style={{ fontSize: 12 }}>
                Minimum insert size must be less than or equal to maximum insert size.
              </div>
            )}
            {reportingError && (
              <div className="error-box" style={{ fontSize: 12 }}>
                Choose either &ldquo;Report all alignments&rdquo; or a positive report limit, not both.
              </div>
            )}
          </div>
        )}

        {envelope?.chunking?.supported && (
          <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 12, fontSize: 13 }}>
            <input
              type="checkbox"
              checked={chunked}
              onChange={(e) => setChunked(e.target.checked)}
            />
            Chunked alignment — split reference and align in parallel
            {chunked && envelope.chunking && (
              <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
                ({envelope.chunking.total_sequences} sequences)
              </span>
            )}
          </label>
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

        {band === "block" && !cardDismissed ? (
          <ResourceRefusalCard
            estimateMb={estimate ?? 0}
            budgetMb={envelope?.mem_budget_mb ?? 0}
            // The align path computes its estimate client-side from the
            // envelope's published coefficients (lib/estimate.ts), so it
            // knows the source without asking the server: these are the
            // published coefficients by construction, never a measured
            // number. If the envelope ever starts serving a measured
            // estimate, this line must change with it.
            detail="from published tool coefficients"
            explanation={bandMessage ?? ""}
            replan={replan ?? null}
            onCancel={onClose}
            onEdit={() => setCardDismissed(true)}
            onLaunchAnyway={() => launchAnyway.mutate()}
            launchAnywayPending={launchAnyway.isPending}
            onAcceptReplan={(p) => {
              setOverrides((o) => ({ ...o, ...p }));
              setCardDismissed(true);
            }}
          />
        ) : (
          bandMessage && (
            <div className={band === "block" ? "error-box" : "warn-box"}>
              {bandMessage}
            </div>
          )
        )}
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
    </ModalBackdrop>
  );
}
