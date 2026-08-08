import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, ApiRequestError } from "../api/client";
import { notify } from "../stores/messageStore";
import { AlignerParamFields } from "./AlignerParamFields";
import { ModalBackdrop } from "./ModalBackdrop";
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
}: {
  object: DataObject;
  onBack?: () => void;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const { data: defaults, isLoading, isError, error } = useQuery({
    queryKey: ["pipelines", "assemble-defaults", object.id],
    queryFn: () => api.assembleDefaults(object.id),
    retry: false,
  });

  const assembler = defaults?.assembler ?? "flye";
  const { data: schema } = useQuery({
    queryKey: ["pipelines", "assembler-schema", assembler],
    queryFn: () => api.assemblerSchema(assembler),
    enabled: defaults != null,
  });

  const [overrides, setOverrides] = useState<Partial<AssemblyParams>>({});
  const [advanced, setAdvanced] = useState(false);
  // Populated from a 422's `details`. This path is reactive rather than
  // pre-flight: assembly has no envelope endpoint and no client-side mirror
  // of estimate_assembly_mb, so the server's refusal is what produces the card.
  const [refusal, setRefusal] = useState<ResourceRefusalDetails | null>(null);

  const params = { ...defaults, ...overrides } as Partial<AssemblyParams>;
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

  const launch = useMutation({
    mutationFn: () =>
      api.launchAssembly({ object_id: object.id, params }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Assembly started");
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => {
      if (e instanceof ApiRequestError && "estimate_mb" in e.details) {
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
                `This assembly needs about ${refusal.estimate_mb.toLocaleString()} MB, ` +
                `more than the ${refusal.budget_mb.toLocaleString()} MB available.`
              }
              replan={refusal.replan}
              onCancel={onClose}
              onEdit={() => setRefusal(null)}
              onLaunchAnyway={() => launchAnyway.mutate()}
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
