import type { AlignParams, ParamFieldMeta } from "../api/types";

/**
 * Renders parameter inputs from registry metadata.
 *
 * Generated rather than hand-written per aligner: four tools with six-odd
 * knobs each is twenty-plus inputs, and the copy explaining them belongs
 * beside the validation that enforces them. The cost is that `help` text
 * lives in a Python table -- worth it while the fields stay this regular,
 * and reversible per-field if one tool ever needs bespoke layout.
 */
export function AlignerParamFields({
  fields,
  params,
  onChange,
}: {
  fields: ParamFieldMeta[];
  params: Partial<AlignParams>;
  onChange: (key: string, value: unknown) => void;
}) {
  return (
    <>
      {fields.map((f) => {
        const hasValue = Object.prototype.hasOwnProperty.call(params, f.key);
        const value = hasValue
          ? (params as Record<string, unknown>)[f.key]
          : f.default;
        const isOptionalToggle = f.kind === "bool" && f.default == null;
        const isOptionalNumeric =
          (f.kind === "int" || f.kind === "float") && f.default == null;
        const optionalSentinel =
          f.kind === "select" &&
          typeof f.default === "string" &&
          f.choices.some((c) => c.value === f.default)
            ? f.default
            : null;

        if (f.kind === "bool") {
          return (
            <label key={f.key} className="trim-check trim-wide">
              <input
                type="checkbox"
                checked={Boolean(value)}
                onChange={(e) =>
                  onChange(
                    f.key,
                    isOptionalToggle
                      ? (e.target.checked ? true : undefined)
                      : e.target.checked,
                  )
                }
              />
              <span>
                {f.label}
                <small style={{ display: "block" }}>{f.help}</small>
              </span>
            </label>
          );
        }

        if (f.kind === "select") {
          return (
            <label key={f.key}>
              <span>{f.label}</span>
              <select
                value={String(value ?? "")}
                onChange={(e) =>
                  onChange(
                    f.key,
                    optionalSentinel !== null &&
                      e.target.value === optionalSentinel
                      ? undefined
                      : e.target.value,
                  )
                }
              >
                {f.choices.map((c) => (
                  <option key={c.value} value={c.value}>
                    {c.label}
                  </option>
                ))}
              </select>
              <small>{f.help}</small>
            </label>
          );
        }

        return (
          <label key={f.key}>
            <span>{f.label}</span>
            <input
              type={f.kind === "int" || f.kind === "float" ? "number" : "text"}
              {...(f.kind === "float" ? { step: "any" } : {})}
              {...(f.min != null ? { min: f.min } : {})}
              {...(f.max != null ? { max: f.max } : {})}
              value={String(value ?? "")}
              onChange={(e) => {
                if (f.kind === "int" || f.kind === "float") {
                  const next = e.target.value;
                  if (isOptionalNumeric && next === "") {
                    onChange(f.key, undefined);
                    return;
                  }
                  onChange(f.key, Number(next));
                  return;
                }
                onChange(f.key, e.target.value);
              }}
            />
            <small>{f.help}</small>
          </label>
        );
      })}
    </>
  );
}
