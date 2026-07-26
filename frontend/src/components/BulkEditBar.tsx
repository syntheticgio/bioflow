import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";

/**
 * Applies metadata or tags to a multi-selection.
 *
 * Values are merged server-side, never replacing the whole metadata document,
 * so setting one field across a cohort cannot silently erase the others.
 */
export function BulkEditBar({
  objectIds,
  onDone,
}: {
  objectIds: string[];
  onDone: () => void;
}) {
  const qc = useQueryClient();
  const [mode, setMode] = useState<"none" | "metadata" | "tags">("none");
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [tags, setTags] = useState("");

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["search"] });
    qc.invalidateQueries({ queryKey: ["objects"] });
    qc.invalidateQueries({ queryKey: ["object"] });
  };

  const applyMetadata = useMutation({
    mutationFn: () => api.bulkMetadata(objectIds, { [key.trim()]: value }),
    onSuccess: (r) => {
      invalidate();
      notify.success(`Updated ${r.modified} file(s)`);
      for (const w of r.warnings ?? []) notify.warn(w.message);
      setKey("");
      setValue("");
      setMode("none");
      onDone();
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const applyTags = useMutation({
    mutationFn: () =>
      api.bulkTags(
        objectIds,
        tags.split(",").map((t) => t.trim()).filter(Boolean),
      ),
    onSuccess: (r) => {
      invalidate();
      notify.success(`Tagged ${r.modified} file(s)`);
      setTags("");
      setMode("none");
      onDone();
    },
    onError: (e: Error) => notify.error(e.message),
  });

  return (
    <div
      style={{
        padding: "8px 12px",
        borderBottom: "1px solid var(--border)",
        background: "var(--bg-elevated)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
        <strong>{objectIds.length} selected</strong>
        <button
          type="button"
          className="btn"
          style={{ padding: "2px 8px", fontSize: 12 }}
          onClick={() => setMode(mode === "metadata" ? "none" : "metadata")}
        >
          Set field
        </button>
        <button
          type="button"
          className="btn"
          style={{ padding: "2px 8px", fontSize: 12 }}
          onClick={() => setMode(mode === "tags" ? "none" : "tags")}
        >
          Add tags
        </button>
        <button
          type="button"
          onClick={onDone}
          style={{ marginLeft: "auto", color: "var(--text-faint)", fontSize: 11 }}
        >
          clear
        </button>
      </div>

      {mode === "metadata" && (
        <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
          <input
            value={key}
            placeholder="field"
            onChange={(e) => setKey(e.target.value)}
            style={{ flex: 1, fontSize: 12, padding: "4px 6px" }}
          />
          <input
            value={value}
            placeholder="value"
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && key.trim() && applyMetadata.mutate()}
            style={{ flex: 1, fontSize: 12, padding: "4px 6px" }}
          />
          <button
            type="button"
            className="btn primary"
            style={{ padding: "2px 10px", fontSize: 12 }}
            onClick={() => applyMetadata.mutate()}
            disabled={!key.trim() || applyMetadata.isPending}
          >
            Apply
          </button>
        </div>
      )}

      {mode === "tags" && (
        <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
          <input
            value={tags}
            placeholder="tag1, tag2"
            onChange={(e) => setTags(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && tags.trim() && applyTags.mutate()}
            style={{ flex: 1, fontSize: 12, padding: "4px 6px" }}
          />
          <button
            type="button"
            className="btn primary"
            style={{ padding: "2px 10px", fontSize: 12 }}
            onClick={() => applyTags.mutate()}
            disabled={!tags.trim() || applyTags.isPending}
          >
            Apply
          </button>
        </div>
      )}
    </div>
  );
}
