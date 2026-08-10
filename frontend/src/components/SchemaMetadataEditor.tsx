import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { MetadataField, MetadataSchema, ObjectRole } from "../api/types";
import { accessionUrl } from "../lib/format";

interface Props {
  value: Record<string, unknown>;
  formatKind: string;
  role: ObjectRole | null;
  onSave: (next: Record<string, unknown>) => void;
  saving?: boolean;
  /**
   * Mirrors the editor's internal dirty flag outward so a parent can warn
   * before an action that would discard in-progress edits. Optional: the
   * editor is fully functional without it.
   */
  onDirtyChange?: (dirty: boolean) => void;
  /**
   * Groups whose values another section already displays. Collapsed by
   * default so the same value is not on screen twice, and revealed by "show
   * all fields" -- they remain editable, just not duplicated at rest.
   */
  dedupeGroups?: string[];
}

type Custom = { key: string; value: string };

/**
 * Metadata editor driven by the format's schema.
 *
 * Suggested fields get proper inputs (select, number with unit, date); anything
 * else the file already carries, or that the user wants to add, still works as
 * a free key/value pair. The schema is a convenience, never a restriction — no
 * fixed vocabulary survives contact with a real lab.
 */
export function SchemaMetadataEditor({
  value,
  formatKind,
  role,
  onSave,
  saving,
  onDirtyChange,
  dedupeGroups = [],
}: Props) {
  const { data: schema } = useQuery({
    // Role is part of the key: a conversion changes which fields apply, and a
    // stale cache would serve the previous role's form.
    queryKey: ["metadata", "schema", formatKind, role],
    queryFn: () => api.metadataSchema(formatKind, role),
    staleTime: 5 * 60 * 1000, // schemas are static within a release
  });

  const [values, setValues] = useState<Record<string, unknown>>(value);
  const [custom, setCustom] = useState<Custom[]>([]);
  const [dirty, setDirty] = useState(false);
  const [showAll, setShowAll] = useState(false);

  const schemaKeys = new Set(
    (schema?.groups ?? []).flatMap((g) => g.fields.map((f) => f.key)),
  );

  // Re-sync when a different file is selected, but never clobber edits in
  // progress on the current one.
  useEffect(() => {
    if (dirty) return;
    setValues(value);
    setCustom(
      Object.entries(value)
        .filter(([k]) => !schemaKeys.has(k))
        .map(([k, v]) => ({ key: k, value: v == null ? "" : String(v) })),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, schema, dirty]);

  // Mirror dirty outward. Separate from the resync effect above so that
  // effect's dependency list and its early-bail behavior are untouched.
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  // How many of the custom rows came from the archive. Used only to choose
  // the wording below: with any SRA fields present, the whole set is
  // effectively the archive's, and naming that explains the variability.
  const sraCount = custom.filter((r) => r.key.startsWith("sra_")).length;

  const setField = (key: string, v: unknown) => {
    setValues((prev) => ({ ...prev, [key]: v }));
    setDirty(true);
  };

  const save = () => {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(values)) {
      if (!schemaKeys.has(k)) continue;
      // null clears a field server-side; "" is how an emptied input reads.
      out[k] = v === "" || v === undefined ? null : v;
    }
    for (const { key, value: v } of custom) {
      const k = key.trim();
      if (k) out[k] = v === "" ? null : v;
    }
    onSave(out);
    setDirty(false);
  };

  if (!schema) {
    return <div style={{ color: "var(--text-faint)", fontSize: 12 }}>Loading fields…</div>;
  }

  return (
    <div>
      {/* Groups sit side by side rather than in one tall stack: they are
          independent sets of short fields, and a single column makes the form
          taller than the screen for no gain. */}
      <div className="meta-groups">
        {schema.groups.map((group) => {
          // Show suggested fields and anything already filled in; the rest
          // hide behind "show all" so the form does not open as a wall of
          // inputs.
          //
          // Archive is the exception: the Public archive section above states
          // every one of these accessions read-only and links them out, so
          // showing them again as filled inputs is the same value twice on
          // one screen. They stay editable behind "show all" -- correcting a
          // wrong accession is the whole reason they are fields at all.
          const duplicatedAbove = !showAll && dedupeGroups.includes(group.group);
          const visible = duplicatedAbove
            ? []
            : group.fields.filter(
                (f) => showAll || f.suggested || hasValue(values[f.key]),
              );
          if (visible.length === 0) return null;

          return (
            <div className="meta-group" key={group.group}>
              <div className="meta-group-title">{group.group}</div>
              {visible.map((f) => (
                <FieldInput
                  key={f.key}
                  field={f}
                  value={values[f.key]}
                  onChange={(v) => setField(f.key, v)}
                />
              ))}
            </div>
          );
        })}
      </div>

      {custom.length > 0 && (
        <div className="custom-fields">
          <div className="custom-fields-head">
            <span className="custom-fields-title">Custom fields</span>
            {/* Says where these came from and why the list is not the same on
                every file -- otherwise a set that changes per file reads as
                the editor losing fields. */}
            <span className="custom-fields-note">
              {sraCount > 0
                ? `${custom.length} parsed from the SRA record for this file — the set differs file to file`
                : `${custom.length} not covered by the schema for this file type`}
            </span>
          </div>
          {custom.map((row, i) => (
            <div className="meta-row" key={i}>
              <input
                className="meta-key"
                value={row.key}
                placeholder="key"
                onChange={(e) => {
                  setCustom((c) =>
                    c.map((r, j) => (j === i ? { ...r, key: e.target.value } : r)),
                  );
                  setDirty(true);
                }}
              />
              <input
                value={row.value}
                placeholder="value"
                onChange={(e) => {
                  setCustom((c) =>
                    c.map((r, j) => (j === i ? { ...r, value: e.target.value } : r)),
                  );
                  setDirty(true);
                }}
              />
              <button
                type="button"
                className="icon-btn"
                title="Remove"
                onClick={() => {
                  setCustom((c) => c.filter((_, j) => j !== i));
                  setDirty(true);
                }}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <button type="button" className="btn" onClick={() => setShowAll(!showAll)}>
          {showAll ? "Show suggested only" : "Show all fields"}
        </button>
        <button
          type="button"
          className="btn"
          onClick={() => {
            setCustom((c) => [...c, { key: "", value: "" }]);
            setDirty(true);
          }}
        >
          + Custom field
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

function hasValue(v: unknown): boolean {
  return v !== undefined && v !== null && v !== "";
}

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: MetadataField;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const str = value == null ? "" : String(value);
  // Accession fields stay plain editable inputs; the link rides in the label
  // instead. Making the value itself a link would need a click-to-edit mode,
  // which is friction on a field people do correct by hand.
  const externalUrl = accessionUrl(field.key, str);

  return (
    // .meta-field lets the column flow keep a label and its input together:
    // split across a break they are unusable.
    <div className="meta-field" style={{ marginBottom: 7 }}>
      <label
        style={{
          display: "block",
          fontSize: 12,
          color: "var(--text-dim)",
          marginBottom: 2,
        }}
        title={field.help ?? undefined}
      >
        {field.label}
        {field.unit && (
          <span style={{ color: "var(--text-faint)" }}> ({field.unit})</span>
        )}
        {field.help && (
          <span
            title={field.help}
            style={{ color: "var(--text-faint)", cursor: "help" }}
            aria-label={field.help}
          >
            {" "}ⓘ
          </span>
        )}
        {externalUrl && (
          <a
            href={externalUrl}
            target="_blank"
            rel="noreferrer noopener"
            title={`Open ${str} at NCBI`}
            style={{ color: "var(--accent)", marginLeft: 6 }}
          >
            NCBI ↗
          </a>
        )}
      </label>

      {field.type === "enum" && field.open_vocabulary ? (
        /* A combo, not a <select>: these options come from a vocabulary we do
           not own, so the list is suggestions and the real answer is often not
           on it. A <select> here actively prevented recording the instrument
           models SRA writes ("NextSeq 550"), which is #66. Same idiom as
           ModelCombo.tsx, which argues the identical case for model ids. */
        <>
          <input
            list={`meta-${field.key}-options`}
            value={str}
            onChange={(e) => onChange(e.target.value)}
            spellCheck={false}
            autoComplete="off"
            style={{ width: "100%", padding: "5px 6px", fontSize: 13 }}
          />
          <datalist id={`meta-${field.key}-options`}>
            {field.options.map((o) => (
              <option key={o} value={o} />
            ))}
          </datalist>
        </>
      ) : field.type === "enum" ? (
        <select
          value={str}
          onChange={(e) => onChange(e.target.value)}
          style={{ width: "100%", padding: "5px 6px", fontSize: 13 }}
        >
          <option value="">—</option>
          {field.options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
          {/* A stored value outside the suggested list must remain selectable,
              or saving the form would silently discard it. */}
          {str && !field.options.includes(str) && <option value={str}>{str}</option>}
        </select>
      ) : field.type === "boolean" ? (
        <select
          value={str === "" ? "" : str === "true" ? "true" : "false"}
          onChange={(e) =>
            onChange(e.target.value === "" ? "" : e.target.value === "true")
          }
          style={{ width: "100%", padding: "5px 6px", fontSize: 13 }}
        >
          <option value="">—</option>
          <option value="true">Yes</option>
          <option value="false">No</option>
        </select>
      ) : (
        <input
          type={
            field.type === "number" || field.type === "integer"
              ? "number"
              : field.type === "date"
                ? "date"
                : "text"
          }
          step={field.type === "number" ? "any" : undefined}
          value={str}
          onChange={(e) => onChange(e.target.value)}
          style={{ width: "100%", fontSize: 13, padding: "5px 6px" }}
        />
      )}
    </div>
  );
}

export type { MetadataSchema };
