import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { DataObject } from "../api/types";

interface Props {
  obj: DataObject;
}

/** Formats where reference-vs-reads is genuinely ambiguous. */
const CONVERTIBLE_FORMATS = ["fasta", "fastq"];

/**
 * Converts a file between reads and reference.
 *
 * Both directions are the same PATCH with a different value, and the change is
 * cheap and reversible -- so there is no confirmation step, which would be
 * friction without benefit.
 */
export function RoleConverter({ obj }: Props) {
  const qc = useQueryClient();
  const isReference = obj.role === "reference";

  const convert = useMutation({
    mutationFn: (role: "reference" | null) => api.updateObject(obj.id, { role }),
    onSuccess: (_r, role) => {
      qc.invalidateQueries({ queryKey: ["object", obj.id] });
      // The left panel re-sections off this value.
      qc.invalidateQueries({ queryKey: ["objects", obj.project_id] });
      qc.invalidateQueries({ queryKey: ["search"] });
      notify.success(
        role === "reference"
          ? `${obj.name} is now a reference`
          : `${obj.name} is now reads`,
      );
    },
    onError: (e: Error) => notify.error(e.message),
  });

  // A BAM or VCF has an unambiguous role already; offering to convert it
  // invites confusion rather than solving a problem.
  if (
    !isReference &&
    !CONVERTIBLE_FORMATS.includes(obj.format.kind.toLowerCase())
  ) {
    return null;
  }

  return (
    <div className="section">
      <div className="section-title">Role</div>
      <button
        type="button"
        className="btn"
        onClick={() => convert.mutate(isReference ? null : "reference")}
        disabled={convert.isPending}
      >
        {convert.isPending
          ? "Converting…"
          : isReference
            ? "Convert back to reads"
            : "Convert to reference"}
      </button>
      <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}>
        {isReference
          ? "Moves this back to the Reads section and restores the sequencing metadata fields. Nothing is lost either way."
          : "Marks this as a reference genome. It will move to the References section and show assembly metadata."}
      </div>
    </div>
  );
}
