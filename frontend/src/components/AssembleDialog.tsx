import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, ApiRequestError } from "../api/client";
import { notify } from "../stores/messageStore";
import { AlignerParamFields } from "./AlignerParamFields";
import { ModalBackdrop } from "./ModalBackdrop";
import { NodeSelector } from "./NodeSelector";
import { ParameterSetPicker } from "./ParameterSetPicker";
import { ResourceRefusalCard } from "./ResourceRefusalCard";
import type {
  AssemblyParams,
  DataObject,
  ResourceRefusalDetails,
} from "../api/types";

const CHEMISTRY_LABELS: Record<string, string> = {
  hifi: "PacBio HiFi",
  clr: "PacBio CLR",
  ont_simplex: "Oxford Nanopore",
  ont_duplex: "Oxford Nanopore (duplex)",
};

/** `12071326` -> `12.07 Mb`. Bases are unreadable at genome scale. */
function formatBases(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} Gb`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)} Mb`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} kb`;
  return `${n} bases`;
}

/**
 * Which of the user's edits survive switching assembler.
 *
 * Only the two fields every assembler shares (`_SHARED_FIELDS` on the server:
 * threads and genome size) carry over. Everything else is assembler-specific
 * -- `k` for ABySS, `mode` for SPAdes and Flye, `min_contig_len` for MEGAHIT
 * -- and the new schema's own defaults are better than a stale value from a
 * tool that is no longer selected.
 *
 * `mode` is the one that would actively mislead if carried: both Flye and
 * SPAdes have a field by that name, and their vocabularies do not overlap at
 * all (`nano-raw` vs `isolate`), so a surviving `mode` would be silently
 * invalid rather than merely stale.
 */
export function overridesSurvivingAssemblerChange(
  overrides: Partial<AssemblyParams>,
): Partial<AssemblyParams> {
  const kept: Partial<AssemblyParams> = {};
  if (overrides.threads !== undefined) kept.threads = overrides.threads;
  if (overrides.genome_size !== undefined)
    kept.genome_size = overrides.genome_size;
  return kept;
}

/**
 * Launch a de novo assembly.
 *
 * The assembler and its input mode are chosen by the server from the chemistry
 * QC inferred on the reads, for the reason VariantDialog gives for callers:
 * the user has no basis to pick from a list of modes graded by error rate, and
 * the machine already knows the answer. The advanced section is where they
 * override it, which matters more here than elsewhere -- BioFlow cannot tell
 * which basecaller produced a Nanopore run, so its simplex default is
 * deliberately the conservative one.
 *
 * Genome size is the one field that behaves unlike the others: it is not
 * passed to the assembler at all. Flye has not required it since 2.8, and it
 * is collected so BioFlow can estimate the memory a run will need. The dialog
 * says so rather than letting someone believe it steers the assembly.
 */
export function AssembleDialog({
  object,
  onBack,
  onClose,
  prefill,
}: {
  object: DataObject;
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

  const { data: defaults, isLoading, isError, error } = useQuery({
    queryKey: ["pipelines", "assemble-defaults", object.id],
    queryFn: () => api.assembleDefaults(object.id),
    retry: false,
  });

  // Which assemblers these reads may be assembled with, and which of them the
  // chemistry defaults to. Object-scoped because compatibility depends on the
  // reads: a paired-layout assembler cannot take long reads.
  const { data: selectable } = useQuery({
    queryKey: ["pipelines", "assemblers", object.id],
    queryFn: () => api.listAssemblers(object.id),
  });

  // Null until the user picks, so the dialog follows the server's default
  // until then -- `defaults` and `selectable` resolve independently and either
  // may land first, and seeding this from whichever arrived would freeze an
  // undefined into the selection.
  const [picked, setPicked] = useState<string | null>(null);

  // `defaults.assembler` is the chemistry's choice, which is not necessarily
  // installed: `default_assembly_params` answers ABySS for short reads on an
  // image that does not ship it. The listing already resolves that -- it marks
  // the first *installed compatible* assembler as default -- so its answer
  // wins whenever the two disagree, and the heading, the schema and the
  // selection stay the same assembler rather than the dialog claiming ABySS
  // while its picker shows SPAdes.
  const listedDefault = selectable?.assemblers.find((a) => a.is_default);
  const serverDefault = defaults?.assembler;
  const serverDefaultSelectable = selectable?.assemblers.some(
    (a) => a.assembler === serverDefault && a.compatible,
  );
  const assembler =
    picked ??
    (serverDefaultSelectable ? serverDefault : listedDefault?.assembler) ??
    serverDefault ??
    "flye";

  const { data: schema } = useQuery({
    queryKey: ["pipelines", "assembler-schema", assembler],
    queryFn: () => api.assemblerSchema(assembler),
    enabled: defaults != null,
  });

  // The assemble card deliberately sends only `object_id`: genome-size
  // inference is an async database read and its builders are synchronous, so
  // `/pipelines/assemble` fills the params in itself. That means a prefill
  // here is usually empty and the dialog opens on its own defaults -- the
  // same ones the card would have launched with, by design.
  // Seeded from the card, not merged after the fact: `params` below layers
  // overrides onto the server's defaults, so putting the card's values in
  // as the initial overrides makes the card's run the one the dialog opens
  // on, with every field still editable. A lazy initializer rather than an
  // effect -- an effect would repaint the dialog a frame after it mounted,
  // and several derived values here read `params` synchronously.
  const [overrides, setOverrides] = useState<Partial<AssemblyParams>>(
    () => (prefill?.params as Partial<AssemblyParams>) ?? {},
  );
  const [advanced, setAdvanced] = useState(false);
  // Populated from a 422's `details`. This path is reactive rather than
  // pre-flight: assembly has no envelope endpoint and no client-side mirror
  // of estimate_assembly_mb, so the server's refusal is what produces the card.
  const [refusal, setRefusal] = useState<ResourceRefusalDetails | null>(null);
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

  // `assembler` last: it is the one key the picker owns outright, and neither
  // the server defaults nor a prefill from a suggestion card may override the
  // selection the user is looking at.
  const params = {
    ...defaults,
    ...overrides,
    assembler,
  } as Partial<AssemblyParams>;
  const chemistry = object.facts?.qc_read_chemistry as string | undefined;

  // Only while the user has not touched it. Once they have, the number is
  // theirs and labelling it "inferred" would be false.
  const inferred =
    defaults?.genome_size_source === "inferred" &&
    overrides.genome_size === undefined;

  const fields = schema?.fields ?? [];
  const biology = fields.filter(
    (f) => f.group === "biology" && f.key !== "genome_size",
  );
  const performance = fields.filter((f) => f.group === "performance");

  // Shallow compare: has anything the applied set resolved changed since it
  // was applied? Only the keys the set actually touched are checked -- a
  // field the set never set is not the set's business.
  const editedAfterApply =
    appliedValues !== null &&
    Object.entries(appliedValues).some(
      ([k, v]) => (params as Record<string, unknown>)[k] !== v,
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
      api.launchAssembly(
        { object_id: object.id, params, from_parameter_set: fromParameterSet },
        targetNode || undefined,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Assembly started");
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
      api.launchAssembly({
        object_id: object.id,
        params,
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

  return (
    <ModalBackdrop onClick={onClose}>
      <div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
        <h2>
          Assemble
          <span className="dialog-tool-subtitle"> — {assembler}</span>
          {onBack && (
            <button type="button" className="dialog-tool-back" onClick={onBack}>
              change tool
            </button>
          )}
        </h2>

        <div className="modal-body">
          {isError && (
            <div className="error-box" style={{ marginBottom: 12 }}>
              {(error as Error)?.message ??
                "These reads cannot be assembled here."}
            </div>
          )}

          <div className="trim-inputs">
            <div className="trim-file">{object.name}</div>
            {isLoading ? (
              <div className="trim-mate-note">Reading assembly defaults…</div>
            ) : (
              <div className="trim-mate-note">
                {chemistry ? (
                  <>
                    Read chemistry:{" "}
                    <strong>{CHEMISTRY_LABELS[chemistry] ?? chemistry}</strong>
                    {params.mode && <> — assembling as {params.mode}</>}
                  </>
                ) : (
                  <>No read chemistry on record. Run QC on these reads first.</>
                )}
              </div>
            )}
          </div>

          {/* Incompatible assemblers are disabled rather than omitted: a user
              wondering why MEGAHIT is missing for a Nanopore file is better
              served by seeing it greyed out with the reason than by it not
              existing. Only rendered once there is a real choice to make. */}
          {selectable && selectable.assemblers.length > 1 && (
            <div className="trim-fields">
              <label className="trim-wide">
                <span>Assembler</span>
                <select
                  value={assembler}
                  onChange={(e) => {
                    setPicked(e.target.value);
                    // Assembler-specific values do not survive the switch --
                    // see overridesSurvivingAssemblerChange.
                    setOverrides(overridesSurvivingAssemblerChange(overrides));
                  }}
                >
                  {selectable.assemblers.map((entry) => (
                    <option
                      key={entry.assembler}
                      value={entry.assembler}
                      disabled={!entry.compatible}
                      title={entry.incompatible_reason || undefined}
                    >
                      {entry.assembler}
                      {entry.is_default ? " (default)" : ""}
                      {entry.compatible ? "" : " — not for these reads"}
                    </option>
                  ))}
                </select>
                <small>
                  Set from the read chemistry. Change it if you know better —
                  MEGAHIT, for instance, completes on community samples where
                  metaSPAdes runs out of memory.
                </small>
              </label>
            </div>
          )}

          {/* Rendered by hand rather than through AlignerParamFields, which is
              the only field here that needs it: the provenance line underneath
              is the whole reason genome_size_source exists, and a generated
              input has nowhere to put it. */}
          <div className="trim-fields">
            <label className="trim-wide">
              <span>Genome size</span>
              <input
                type="text"
                placeholder="optional — e.g. 4.6m or 3.1g"
                value={
                  overrides.genome_size !== undefined
                    ? String(overrides.genome_size ?? "")
                    : defaults?.genome_size != null
                      ? String(defaults.genome_size)
                      : ""
                }
                onChange={(e) =>
                  setOverrides((o) => ({
                    ...o,
                    genome_size: e.target.value as unknown as number,
                    genome_size_source: "user",
                  }))
                }
              />
              <small>
                {inferred && defaults?.genome_size != null ? (
                  <>
                    Filled in as{" "}
                    <strong>{formatBases(defaults.genome_size)}</strong> from{" "}
                    {defaults.genome_size_from ?? "another file in this project"}
                    . Change it if that is not the right genome.
                  </>
                ) : (
                  <>
                    Optional, and not passed to the assembler — it is used to
                    estimate how much memory this run needs. Leave it blank if
                    you do not know; that is normal for a genome with no
                    reference.
                  </>
                )}
              </small>
            </label>
          </div>

          {assembler && (
            <ParameterSetPicker
              tool={assembler}
              family="assembler"
              currentParams={params}
              onApply={(values) => {
                Object.entries(values).forEach(([k, v]) =>
                  setOverrides((o) => ({ ...o, [k]: v })),
                );
                setAppliedValues(values);
              }}
              onAppliedSetChange={(s) => {
                setAppliedSet(s);
                if (!s) setAppliedValues(null);
              }}
            />
          )}

          {biology.length > 0 && (
            <div className="trim-fields">
              <AlignerParamFields
                fields={biology}
                params={params}
                onChange={(key, value) =>
                  setOverrides((o) => ({ ...o, [key]: value }))
                }
              />
            </div>
          )}

          <button
            type="button"
            className="trim-advanced-toggle"
            onClick={() => setAdvanced((a) => !a)}
            aria-expanded={advanced}
          >
            <span className="trim-chevron">{advanced ? "▾" : "▸"}</span>
            Performance
          </button>

          {advanced && performance.length > 0 && (
            <div className="trim-fields">
              <AlignerParamFields
                fields={performance}
                params={params}
                onChange={(key, value) =>
                  setOverrides((o) => ({ ...o, [key]: value }))
                }
              />
            </div>
          )}

          {refusal && (
            <ResourceRefusalCard
              estimateMb={refusal.estimate_mb}
              budgetMb={refusal.budget_mb}
              detail={refusal.detail}
              explanation={
                refusal.refusal === "declared"
                  ? `This assembly reserves ${(refusal.declared_mb ?? 0).toLocaleString()} MB, ` +
                    `more than the ${refusal.budget_mb.toLocaleString()} MB budget. ` +
                    `Nothing about the run changes that number.`
                  : `This assembly needs about ${(refusal.estimate_mb ?? 0).toLocaleString()} MB, ` +
                    `more than the ${refusal.budget_mb.toLocaleString()} MB available.`
              }
              replan={refusal.replan ?? null}
              onCancel={onClose}
              onEdit={() => setRefusal(null)}
              onLaunchAnyway={() => launchAnyway.mutate()}
              launchAnywayPending={launchAnyway.isPending}
              onAcceptReplan={(p) => {
                setOverrides((o) => ({ ...o, ...p }));
                setRefusal(null);
              }}
            />
          )}

          <div className="trim-mate-note" style={{ marginTop: 12 }}>
            Assembly runs for hours and cannot be resumed — a retry starts over.
            You can close this window; it runs in the background.
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
            disabled={defaults == null || launch.isPending}
            onClick={() => launch.mutate()}
          >
            {launch.isPending ? "Starting…" : "Assemble"}
          </button>
        </div>
      </div>
    </ModalBackdrop>
  );
}
