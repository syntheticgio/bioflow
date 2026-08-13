import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { AnnotationFeature } from "../api/types";
import { notify } from "../stores/messageStore";

/** Fields editable on a feature, in the order the form renders them. */
const EDITABLE_FIELDS = ["type", "start", "end", "attributes"] as const;
type EditableField = (typeof EDITABLE_FIELDS)[number];

interface Props {
  objectId: string;
  row: AnnotationFeature;
  onClose: () => void;
}

/** Modal that edits one feature's editable columns. Each changed field is
 *  saved as its own pending AnnotationEdit; nothing is written to disk until
 *  the user materializes. */
export function AnnotationEditModal({ objectId, row, onClose }: Props) {
  const qc = useQueryClient();

  const [values, setValues] = useState<Record<EditableField, string>>({
    type: row.type ?? "",
    start: String(row.start),
    end: String(row.end),
    attributes: row.attributes ?? "",
  });

  const save = useMutation({
    mutationFn: async (fields: Partial<Record<EditableField, string>>) => {
      // One record per changed field, preserving column-level provenance.
      for (const [field, newValue] of Object.entries(fields)) {
        await api.saveAnnotationEdit(objectId, {
          line: row.line as number,
          field,
          new_value: newValue,
        });
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["annotationstats", "edits", objectId] });
      notify.success("Edit saved as pending change");
      onClose();
    },
    onError: (e: Error) => notify.error(e.message),
  });

  function submit() {
    const current: Record<EditableField, string> = {
      type: row.type ?? "",
      start: String(row.start),
      end: String(row.end),
      attributes: row.attributes ?? "",
    };
    const changed: Record<string, string> = {};
    for (const field of EDITABLE_FIELDS) {
      if (values[field] !== current[field]) changed[field] = values[field];
    }
    if (Object.keys(changed).length === 0) {
      onClose();
      return;
    }
    save.mutate(changed);
  }

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.5)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
      }}
      onClick={onClose}
    >
      <div
        style={{
          background: "var(--surface)",
          borderRadius: 8,
          padding: 16,
          minWidth: 420,
          maxWidth: 600,
          border: "1px solid var(--border)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ fontWeight: 600, marginBottom: 4 }}>
          Edit feature — L{row.line}
        </div>
        <div style={{ color: "var(--text-faint)", fontSize: 12, marginBottom: 12 }}>
          {row.contig}:{row.start.toLocaleString()}–{row.end.toLocaleString()}
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 12 }}>
            <span style={{ color: "var(--text-faint)" }}>Type</span>
            <input
              type="text"
              value={values.type}
              onChange={(e) => setValues((v) => ({ ...v, type: e.target.value }))}
            />
          </label>

          <div style={{ display: "flex", gap: 10 }}>
            <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 12, flex: 1 }}>
              <span style={{ color: "var(--text-faint)" }}>Start (1-based)</span>
              <input
                type="text"
                className="mono"
                value={values.start}
                onChange={(e) => setValues((v) => ({ ...v, start: e.target.value }))}
              />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 12, flex: 1 }}>
              <span style={{ color: "var(--text-faint)" }}>End</span>
              <input
                type="text"
                className="mono"
                value={values.end}
                onChange={(e) => setValues((v) => ({ ...v, end: e.target.value }))}
              />
            </label>
          </div>

          <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 12 }}>
            <span style={{ color: "var(--text-faint)" }}>
              Attributes (raw — covers name, biotype, strand, and qualifiers)
            </span>
            <textarea
              className="mono"
              rows={3}
              value={values.attributes}
              onChange={(e) => setValues((v) => ({ ...v, attributes: e.target.value }))}
            />
          </label>
        </div>

        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 14 }}>
          <button type="button" className="btn" onClick={onClose} disabled={save.isPending}>
            Cancel
          </button>
          <button type="button" className="btn primary" onClick={submit} disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save as pending"}
          </button>
        </div>
      </div>
    </div>
  );
}