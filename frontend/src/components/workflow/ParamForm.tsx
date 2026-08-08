/**
 * A parameter form generated from backend field metadata.
 *
 * The fields come from `aligner_registry`, which is also what `AlignDialog`
 * renders from -- a second hand-written copy here is the copy that goes stale
 * the first time a knob is added.
 *
 * Grouping is not decoration: a generated form is otherwise an
 * undifferentiated pile of inputs, and biology-vs-performance is roughly how
 * AlignDialog was already organized by hand.
 */

import { useState } from "react";
import type { ParamFieldMeta } from "../../api/types";

interface Props {
  fields: ParamFieldMeta[];
  values: Record<string, unknown>;
  onChange: (key: string, value: unknown) => void;
}

function Field({ field, value, onChange }: {
  field: ParamFieldMeta;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const current = value ?? field.default;
  return (
    <label className="param-field">
      <span className="param-label">
        {field.label}
        <em className="param-help">{field.help}</em>
      </span>
      {field.kind === "bool" ? (
        <input
          type="checkbox"
          checked={Boolean(current)}
          onChange={(e) => onChange(e.target.checked)}
        />
      ) : field.kind === "select" ? (
        <select value={String(current ?? "")} onChange={(e) => onChange(e.target.value)}>
          {(field.choices ?? []).map((choice) => (
            <option key={choice.value} value={choice.value}>
              {choice.label}
            </option>
          ))}
        </select>
      ) : field.kind === "int" ? (
        <input
          type="number"
          value={current === null || current === undefined ? "" : String(current)}
          min={field.min ?? undefined}
          max={field.max ?? undefined}
          // Empty means "leave it to the tool's own default", which is not the
          // same as zero -- coercing a cleared box to 0 would silently set a
          // thread count of nothing.
          onChange={(e) =>
            onChange(e.target.value === "" ? undefined : Number(e.target.value))
          }
        />
      ) : (
        <input
          type="text"
          value={String(current ?? "")}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </label>
  );
}

export function ParamForm({ fields, values, onChange }: Props) {
  const [showAdvanced, setShowAdvanced] = useState(false);
  const biology = fields.filter((f) => f.group === "biology");
  const performance = fields.filter((f) => f.group === "performance");

  return (
    <div className="param-form">
      {biology.map((field) => (
        <Field
          key={field.key}
          field={field}
          value={values[field.key]}
          onChange={(value) => onChange(field.key, value)}
        />
      ))}
      {performance.length > 0 && (
        <>
          <button
            type="button"
            className="btn subtle"
            onClick={() => setShowAdvanced((v) => !v)}
          >
            {showAdvanced ? "Hide" : "Show"} performance settings
          </button>
          {showAdvanced &&
            performance.map((field) => (
              <Field
                key={field.key}
                field={field}
                value={values[field.key]}
                onChange={(value) => onChange(field.key, value)}
              />
            ))}
        </>
      )}
    </div>
  );
}
