import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";

/** Tags as removable chips plus an add box. Applied immediately. */
export function TagEditor({
  objectId,
  tags,
  onChanged,
}: {
  objectId: string;
  tags: string[];
  onChanged: () => void;
}) {
  const [draft, setDraft] = useState("");

  const mutate = useMutation({
    mutationFn: ({ add, remove }: { add?: string[]; remove?: string[] }) =>
      api.bulkTags([objectId], add ?? [], remove ?? []),
    onSuccess: onChanged,
    onError: (e: Error) => notify.error(e.message),
  });

  const addTag = () => {
    // Commas split so pasting "a, b, c" does the obvious thing.
    const parts = draft
      .split(",")
      .map((t) => t.trim())
      .filter((t) => t && !tags.includes(t));
    if (parts.length) mutate.mutate({ add: parts });
    setDraft("");
  };

  return (
    <div>
      <div style={{ marginBottom: 8 }}>
        {tags.length === 0 && (
          <span style={{ color: "var(--text-faint)", fontSize: 12 }}>No tags</span>
        )}
        {tags.map((t) => (
          <span key={t} className="chip" style={{ paddingRight: 3 }}>
            {t}
            <button
              type="button"
              onClick={() => mutate.mutate({ remove: [t] })}
              style={{
                marginLeft: 4,
                color: "var(--text-faint)",
                fontSize: 12,
                lineHeight: 1,
              }}
              title={`Remove ${t}`}
            >
              ×
            </button>
          </span>
        ))}
      </div>

      <div style={{ display: "flex", gap: 6 }}>
        <input
          value={draft}
          placeholder="add tag…"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              addTag();
            }
          }}
          style={{ flex: 1, fontSize: 12, padding: "4px 6px" }}
        />
        <button
          type="button"
          className="btn"
          onClick={addTag}
          disabled={!draft.trim() || mutate.isPending}
        >
          Add
        </button>
      </div>
    </div>
  );
}
