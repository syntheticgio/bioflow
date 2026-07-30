import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { DataObject } from "../api/types";

interface Props {
  obj: DataObject;
  /** True when the metadata editor holds unsaved edits. */
  metadataDirty?: boolean;
  /**
   * Omit the `.section` wrapper and its title, returning only the control.
   *
   * For callers that supply their own heading -- the Manage grid labels each
   * row itself, so the built-in title would render twice.
   */
  bare?: boolean;
}

/** Formats where reference-vs-reads is genuinely ambiguous. */
const CONVERTIBLE_FORMATS = ["fasta", "fastq"];

/**
 * Whether this file has a role worth offering to change.
 *
 * Exported so a caller laying out a label beside this component can drop the
 * whole row rather than leaving the label stranded over nothing -- the
 * component itself renders null here, which a static grid cannot see.
 * Mirrors `canPair` in PairEditor, which exists for the same reason.
 */
export function canConvertRole(obj: { role?: string | null; format: { kind: string } }): boolean {
  return (
    obj.role === "reference" ||
    CONVERTIBLE_FORMATS.includes(obj.format.kind.toLowerCase())
  );
}

/**
 * Converts a file between reads and reference.
 *
 * Both directions are the same PATCH with a different value, and the change is
 * cheap and reversible -- so a clean conversion converts on one click, with no
 * confirmation step that would be friction without benefit.
 *
 * The exception is unsaved metadata: DetailPanel remounts the editor on a role
 * change (its schema changes underneath), which discards in-progress edits.
 * That is correct but not something to do silently, so a dirty editor gets a
 * confirm step.
 */
export function RoleConverter({
  obj,
  metadataDirty = false,
  bare = false,
}: Props) {
  const qc = useQueryClient();
  const isReference = obj.role === "reference";
  const [confirming, setConfirming] = useState(false);

  // Saving elsewhere in the panel clears the hazard; don't leave a stale
  // warning on screen.
  useEffect(() => {
    if (!metadataDirty) setConfirming(false);
  }, [metadataDirty]);

  const convert = useMutation({
    mutationFn: (role: "reference" | null) => api.updateObject(obj.id, { role }),
    onSuccess: (_r, role) => {
      setConfirming(false);
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
  // invites confusion rather than solving a problem. Shares its condition with
  // `canConvertRole` so a caller's guard cannot disagree with what renders.
  if (!canConvertRole(obj)) {
    return null;
  }

  const doConvert = () => convert.mutate(isReference ? null : "reference");

  const onClick = () => {
    if (metadataDirty && !confirming) {
      setConfirming(true);
      return;
    }
    doConvert();
  };

  const body = (
    <>
      {confirming && (
        <div className="warn-box" style={{ marginBottom: 8 }}>
          You have unsaved metadata edits. Converting will discard them.
        </div>
      )}
      <div style={{ display: "flex", gap: 8 }}>
        <button
          type="button"
          className="btn"
          onClick={onClick}
          disabled={convert.isPending}
        >
          {convert.isPending
            ? "Converting…"
            : confirming
              ? "Convert anyway"
              : isReference
                ? "Convert back to reads"
                : "Convert to reference"}
        </button>
        {confirming && !convert.isPending && (
          <button
            type="button"
            className="btn"
            onClick={() => setConfirming(false)}
          >
            Cancel
          </button>
        )}
      </div>
      <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}>
        {isReference
          ? "Moves this back to the Reads section and restores the sequencing metadata fields. Nothing is lost either way."
          : "Marks this as a reference genome. It will move to the References section and show assembly metadata."}
      </div>
    </>
  );

  if (bare) return body;

  return (
    <div className="section">
      <div className="section-title">Role</div>
      {body}
    </div>
  );
}
