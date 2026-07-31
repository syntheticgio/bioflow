import { useEffect, useState } from "react";

interface Props {
  value: Record<string, unknown>;
  onSave: (next: Record<string, unknown>) => void;
  saving?: boolean;
}

type Pair = { key: string; value: string };

function toPairs(obj: Record<string, unknown>): Pair[] {
  return Object.entries(obj).map(([key, v]) => ({
    key,
    value: typeof v === "string" ? v : JSON.stringify(v),
  }));
}

/** Free-form key/value metadata. Phase 5 layers per-format schemas on top. */
export function MetadataEditor({ value, onSave, saving }: Props) {
  const [pairs, setPairs] = useState<Pair[]>(() => toPairs(value));
  const [dirty, setDirty] = useState(false);

  // Re-sync when a different entity is selected, but never clobber edits in
  // progress on the current one.
  useEffect(() => {
    if (!dirty) setPairs(toPairs(value));
  }, [value, dirty]);

  const update = (i: number, patch: Partial<Pair>) => {
    setPairs((p) => p.map((row, j) => (j === i ? { ...row, ...patch } : row)));
    setDirty(true);
  };

  const save = () => {
    const next: Record<string, unknown> = {};
    for (const { key, value: v } of pairs) {
      const k = key.trim();
      if (!k) continue;
      try {
        // Accept numbers, booleans and JSON so metadata stays queryable as the
        // type the user meant, not always a string.
        next[k] = JSON.parse(v);
      } catch {
        next[k] = v;
      }
    }
    onSave(next);
    setDirty(false);
  };

  return (
    <div>
      {pairs.map((p, i) => (
        <div className="meta-row" key={i}>
          <input
            value={p.key}
            placeholder="key"
            onChange={(e) => update(i, { key: e.target.value })}
          />
          <input
            value={p.value}
            placeholder="value"
            onChange={(e) => update(i, { value: e.target.value })}
          />
          <button
            type="button"
            className="icon-btn"
            title="Remove"
            onClick={() => {
              setPairs((rows) => rows.filter((_, j) => j !== i));
              setDirty(true);
            }}
          >
            ×
          </button>
        </div>
      ))}

      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        <button
          type="button"
          className="btn-text"
          onClick={() => {
            setPairs((p) => [...p, { key: "", value: "" }]);
            setDirty(true);
          }}
        >
          + Add field
        </button>
        <button
          type="button"
          className="btn primary"
          onClick={save}
          disabled={!dirty || saving}
        >
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  );
}
