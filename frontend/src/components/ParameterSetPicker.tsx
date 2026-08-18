import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import type { ParamSpecFamily, RejectedParam } from "../api/types";

/**
 * Pick, save, rename, and delete saved parameter sets for one tool.
 *
 * Applied values are reported through `onApply` rather than written directly,
 * so the dialog stays the single owner of form state and the generated field
 * renderers need no change.
 *
 * The drift notice is deliberately persistent rather than a toast. Batch work
 * means applying the same stale set thirty times, and a notification that
 * disappears after four seconds is one the user stops reading by sample three
 * -- which would satisfy "flag the rest" on paper while failing in practice.
 */
export function ParameterSetPicker({
  tool,
  family,
  currentParams,
  onApply,
  onAppliedSetChange,
}: {
  tool: string;
  family: ParamSpecFamily;
  currentParams: Record<string, unknown>;
  onApply: (values: Record<string, unknown>) => void;
  onAppliedSetChange: (
    applied: { setId: string; name: string; revision: number } | null,
  ) => void;
}) {
  const qc = useQueryClient();
  const [selected, setSelected] = useState("");
  const [rejected, setRejected] = useState<RejectedParam[]>([]);
  const [appliedCount, setAppliedCount] = useState<{ applied: number; total: number } | null>(
    null,
  );
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const { data: support } = useQuery({
    queryKey: ["parameter-sets", "supported", family, tool],
    queryFn: () => api.parameterSetsSupported(family, tool),
    enabled: !!tool,
  });

  const { data: sets = [] } = useQuery({
    queryKey: ["parameter-sets", tool],
    queryFn: () => api.listParameterSets(tool),
    enabled: !!tool && support?.supported === true,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["parameter-sets", tool] });

  const applyMutation = useMutation({
    mutationFn: (id: string) => api.resolveParameterSet(id),
    onSuccess: (result) => {
      onApply(result.applied);
      onAppliedSetChange({
        setId: result.set.id,
        name: result.set.name,
        revision: result.set.revision,
      });
      setRejected(result.rejected);
      const total = Object.keys(result.applied).length + result.rejected.length;
      setAppliedCount({ applied: Object.keys(result.applied).length, total });
      setNotice(result.set.name);
    },
    onError: () => setError("Could not apply that preset."),
  });

  const saveMutation = useMutation({
    mutationFn: (name: string) =>
      api.createParameterSet({ name, tool, family, params: currentParams }),
    onSuccess: (created) => {
      invalidate();
      setSelected(created.id);
      setError(null);
    },
    onError: (e: unknown) =>
      setError(
        e instanceof Error && e.message.includes("409")
          ? "A preset with that name already exists for this tool."
          : "Could not save that preset.",
      ),
  });

  const renameMutation = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) =>
      api.updateParameterSet(id, { name }),
    onSuccess: invalidate,
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteParameterSet(id),
    onSuccess: () => {
      invalidate();
      setSelected("");
      onAppliedSetChange(null);
      setRejected([]);
      setNotice(null);
    },
  });

  function handleSelect(id: string) {
    setSelected(id);
    setRejected([]);
    setNotice(null);
    if (id) applyMutation.mutate(id);
    else onAppliedSetChange(null);
  }

  function handleSave() {
    const name = window.prompt("Save these settings as:");
    if (name?.trim()) saveMutation.mutate(name.trim());
  }

  function handleRename() {
    const current = sets.find((s) => s.id === selected);
    if (!current) return;
    const name = window.prompt("Rename preset:", current.name);
    if (name?.trim()) renameMutation.mutate({ id: selected, name: name.trim() });
  }

  function handleDelete() {
    const current = sets.find((s) => s.id === selected);
    if (current && window.confirm(`Delete preset "${current.name}"?`)) {
      deleteMutation.mutate(selected);
    }
  }

  // A tool whose spec declares no fields can only ever save an empty set.
  // Rendering nothing is better than offering a control that silently does
  // nothing -- HIFIASM and SPADES are in this state today.
  if (support && !support.supported) return null;

  return (
    <div className="preset-picker">
      <label className="preset-row">
        <span>Preset</span>
        <select value={selected} onChange={(e) => handleSelect(e.target.value)}>
          <option value="">Choose…</option>
          {sets.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
        <button type="button" onClick={handleSave}>
          Save current as…
        </button>
        <button type="button" onClick={handleRename} disabled={!selected}>
          Rename
        </button>
        <button type="button" onClick={handleDelete} disabled={!selected}>
          Delete
        </button>
      </label>

      {error && <p className="preset-error">{error}</p>}

      {notice && rejected.length > 0 && (
        <div className="preset-drift" role="status">
          <p>
            Applied &ldquo;{notice}&rdquo; — {appliedCount?.applied} of {appliedCount?.total}{" "}
            settings.
          </p>
          <ul>
            {rejected.map((r) => (
              <li key={r.key}>
                <strong>{r.key}</strong> not applied — {r.detail}.
              </li>
            ))}
          </ul>
          <button
            type="button"
            onClick={() => {
              setNotice(null);
              setRejected([]);
            }}
          >
            Dismiss
          </button>
        </div>
      )}
    </div>
  );
}
