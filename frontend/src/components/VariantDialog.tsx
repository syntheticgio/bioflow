import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, ApiRequestError } from "../api/client";
import { formatBytes } from "../lib/format";
import { ModalBackdrop } from "./ModalBackdrop";
import { ResourceRefusalCard } from "./ResourceRefusalCard";
import { notify } from "../stores/messageStore";
import type {
  DataObject,
  ResourceRefusalDetails,
  VariantCallerName,
  VariantParams,
} from "../api/types";

const CALLER_LABELS: Record<VariantCallerName, string> = {
  clair3: "Clair3 — deep learning, for long reads",
  bcftools: "bcftools — pileup, for short reads",
  deepvariant: "DeepVariant — not available on this machine",
};

const CHEMISTRY_LABELS: Record<string, string> = {
  hifi: "PacBio HiFi",
  clr: "PacBio CLR",
  ont_simplex: "Oxford Nanopore",
  ont_duplex: "Oxford Nanopore (duplex)",
  short: "Short read (Illumina)",
  unknown: "Unknown",
};

/**
 * Launch a variant calling run over an aligned BAM.
 *
 * The caller is chosen by the server from the chemistry QC inferred on the
 * reads, not by the user picking from a list they have no basis to choose
 * from. The advanced section exposes the override for the cases where they do.
 */
export function VariantDialog({
  object,
  selectedTool,
  onBack,
  onClose,
  prefill,
}: {
  object: DataObject;
  /** The caller chosen in `PipelineToolSelector`, if the user came via one. */
  selectedTool?: VariantCallerName;
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

  const { data: defaults, isLoading } = useQuery({
    queryKey: ["pipelines", "variant-defaults", object.id],
    queryFn: () => api.variantDefaults(object.id),
  });

  // Only consulted when the server could not resolve a reference itself --
  // an uploaded BAM, which carries no record of what it was aligned against.
  const { data: refs } = useQuery({
    queryKey: ["pipelines", "references", object.project_id],
    queryFn: () => api.references(object.project_id),
    enabled: defaults?.needs_reference === true,
  });

  const [referenceId, setReferenceId] = useState<string | null>(
    () => (prefill?.reference_id as string) ?? null,
  );
  // Seeded from the card, not merged after the fact: `params` below layers
  // overrides onto the server's defaults, so putting the card's values in
  // as the initial overrides makes the card's run the one the dialog opens
  // on, with every field still editable. A lazy initializer rather than an
  // effect -- an effect would repaint the dialog a frame after it mounted,
  // and several derived values here read `params` synchronously.
  const [overrides, setOverrides] = useState<Partial<VariantParams>>(
    () => (prefill?.params as Partial<VariantParams>) ?? {},
  );
  const [advanced, setAdvanced] = useState(false);
  // Set from the server's refusal, not chosen up front: the backend is what
  // knows the tool is on-demand and not yet pulled (details.needs ===
  // "install_tool"), and re-deriving that client-side would be a second
  // place this fact could go stale. Cleared on close/tool change so a stale
  // offer never survives past the refusal that produced it.
  const [installOffer, setInstallOffer] = useState<{
    tool: string;
    downloadBytes: number | null;
  } | null>(null);
  // Populated from a 422's `details`, the same reactive path AssembleDialog
  // uses -- variant calling has no client-side estimate to check pre-flight.
  // Checked ahead of the install-tool offer below: refuse_if_over_budget runs
  // before the install-consent check in launch_variant_calling, so a request
  // can only ever come back with one of these, never both.
  const [refusal, setRefusal] = useState<ResourceRefusalDetails | null>(null);

  // `selectedTool` wins over the server default, for the same reason it does
  // in AlignDialog: `callerInfo` below is derived from it synchronously, and
  // seeding an effect would leave a window where the availability warning
  // describes a caller the user did not choose.
  const params = {
    ...defaults?.params,
    ...overrides,
    ...(selectedTool ? { caller: selectedTool } : {}),
  } as VariantParams;

  const caller = params?.caller ?? null;
  const chemistry = defaults?.chemistry ?? null;
  const callerInfo = defaults?.callers.find((c) => c.name === caller);

  const references = refs?.references ?? [];
  const chosenReferenceId =
    referenceId ??
    defaults?.reference_id ??
    references.find((r) => r.role === "reference")?.object_id ??
    references[0]?.object_id ??
    null;

  const isClr = chemistry === "clr";
  const referenceResolved = !defaults?.needs_reference || chosenReferenceId != null;

  const launch = useMutation({
    mutationFn: (consent: boolean) =>
      api.launchVariantCalling({
        bam_id: object.id,
        reference_id: chosenReferenceId,
        caller,
        params: overrides,
        install_optional: consent,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success(
        installOffer
          ? `Installing ${installOffer.tool} and calling variants once it finishes`
          : "Variant calling started",
      );
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => {
      // refuse_if_over_budget runs before the install-consent check in
      // launch_variant_calling, so a refusal and an install offer never
      // arrive on the same response -- checked first here for the same
      // reason.
      if (e instanceof ApiRequestError && "refusal" in e.details) {
        setRefusal(e.details as unknown as ResourceRefusalDetails);
        return;
      }
      // details.needs === "install_tool" is launch_variant_calling's own
      // refusal shape for an on-demand caller that has not been pulled yet
      // (pipeline_service._require_or_offer_install) -- the same
      // details.needs vocabulary the .bai/.fai refusals already use for
      // "run index_bam first". Anything else is a real error.
      if (e instanceof ApiRequestError && e.details.needs === "install_tool") {
        setInstallOffer({
          tool: String(e.details.tool ?? caller),
          downloadBytes:
            typeof e.details.download_bytes === "number"
              ? e.details.download_bytes
              : null,
        });
        return;
      }
      notify.error(e.message);
    },
  });

  const launchAnyway = useMutation({
    mutationFn: () =>
      api.launchVariantCalling({
        bam_id: object.id,
        reference_id: chosenReferenceId,
        caller,
        params: overrides,
        install_optional: installOffer != null,
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

  const ready =
    defaults != null &&
    caller != null &&
    !isClr &&
    referenceResolved &&
    callerInfo?.available !== false;

  return (
    <ModalBackdrop onClick={onClose}>
      <div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
        <h2>
          Call variants
          {caller && <span className="dialog-tool-subtitle"> — {caller}</span>}
          {onBack && (
            <button type="button" className="dialog-tool-back" onClick={onBack}>
              change tool
            </button>
          )}
        </h2>

        <div className="modal-body">
          {isClr && (
            <div className="error-box" style={{ marginBottom: 12 }}>
              PacBio CLR reads are not suitable for variant calling: their error
              rate is too high for any caller here to produce reliable results.
              Use HiFi/CCS reads instead.
            </div>
          )}

          {caller === "deepvariant" && (
            <div className="warn-box" style={{ marginBottom: 12 }}>
              DeepVariant has no arm64 build and is not installed. Use Clair3
              for long reads, or bcftools for short reads.
            </div>
          )}

          {callerInfo && !callerInfo.available && caller !== "deepvariant" && (
            <div className="error-box" style={{ marginBottom: 12 }}>
              {caller} is not available on this machine.
            </div>
          )}

          <div className="trim-inputs">
            <div className="trim-file">{object.name}</div>
            {isLoading ? (
              <div className="trim-mate-note">Reading alignment details…</div>
            ) : (
              <div className="trim-mate-note">
                {chemistry ? (
                  <>
                    Read chemistry:{" "}
                    <strong>{CHEMISTRY_LABELS[chemistry] ?? chemistry}</strong>
                    {caller && !isClr && <> — calling with {caller}</>}
                  </>
                ) : (
                  <>
                    No read chemistry on record — defaulting to the short-read
                    caller. Run QC on the reads to detect it.
                  </>
                )}
              </div>
            )}
            {defaults?.reference_name && (
              <div className="trim-mate-note">
                Reference: <strong>{defaults.reference_name}</strong>
              </div>
            )}
          </div>

          {defaults?.needs_reference && (
            <div className="trim-fields">
              <label className="trim-wide">
                <span>Reference</span>
                {references.length === 0 ? (
                  <div className="warn-box" style={{ fontSize: 12 }}>
                    No FASTA reference in this project. Upload the one this BAM
                    was aligned against.
                  </div>
                ) : (
                  <select
                    value={chosenReferenceId ?? ""}
                    onChange={(e) => setReferenceId(e.target.value)}
                  >
                    {references.map((r) => (
                      <option key={r.object_id} value={r.object_id}>
                        {r.name}
                        {r.role === "reference"
                          ? ""
                          : " (not marked as a reference)"}
                      </option>
                    ))}
                  </select>
                )}
                <small>
                  This BAM carries no record of its reference. Calling against
                  the wrong one produces confident, wrong results rather than an
                  error — so this has to be the genome it was aligned to.
                </small>
              </label>
            </div>
          )}

          <button
            type="button"
            className="trim-advanced-toggle"
            onClick={() => setAdvanced((a) => !a)}
            aria-expanded={advanced}
          >
            <span className="trim-chevron">{advanced ? "▾" : "▸"}</span>
            Caller and performance
          </button>

          {advanced && (
            <div className="trim-fields">
              <label>
                <span>Caller</span>
                <select
                  value={caller ?? ""}
                  onChange={(e) =>
                    setOverrides((o) => ({
                      ...o,
                      caller: e.target.value as VariantCallerName,
                    }))
                  }
                >
                  {(["clair3", "bcftools", "deepvariant"] as const).map((c) => {
                    const info = defaults?.callers.find((x) => x.name === c);
                    return (
                      <option
                        key={c}
                        value={c}
                        disabled={c === "deepvariant" || info?.available === false}
                      >
                        {CALLER_LABELS[c]}
                      </option>
                    );
                  })}
                </select>
                <small>
                  Chosen from the read chemistry by default. Overriding it is
                  reasonable if you know something QC did not.
                </small>
              </label>

              <label>
                <span>Threads</span>
                <input
                  type="number"
                  min={1}
                  max={16}
                  value={params?.threads ?? 4}
                  onChange={(e) =>
                    setOverrides((o) => ({
                      ...o,
                      threads: Number(e.target.value),
                    }))
                  }
                />
                <small>
                  More threads finish sooner but compete with other work.
                </small>
              </label>
            </div>
          )}
          {installOffer && (
            <div className="warn-box" style={{ marginBottom: 12 }}>
              {installOffer.tool} is not installed. It runs as a separate
              container image and is downloaded on first use
              {installOffer.downloadBytes
                ? ` (about ${formatBytes(installOffer.downloadBytes)})`
                : ""}
              . Press "Install and call" to download it and start calling
              variants once it finishes.
            </div>
          )}

          {refusal && (
            <ResourceRefusalCard
              estimateMb={refusal.estimate_mb}
              budgetMb={refusal.budget_mb}
              detail={refusal.detail}
              explanation={
                refusal.refusal === "declared"
                  ? `Variant calling reserves ${(refusal.declared_mb ?? 0).toLocaleString()} MB, ` +
                    `more than the ${refusal.budget_mb.toLocaleString()} MB budget. ` +
                    `Nothing about the run changes that number.`
                  : `Variant calling needs about ${(refusal.estimate_mb ?? 0).toLocaleString()} MB, ` +
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
            onClick={() => launch.mutate(installOffer != null)}
            disabled={!ready || launch.isPending}
          >
            {launch.isPending
              ? "Starting…"
              : installOffer
                ? "Install and call"
                : "Call variants"}
          </button>
        </div>
      </div>
    </ModalBackdrop>
  );
}
