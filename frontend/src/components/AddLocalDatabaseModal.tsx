import { useState } from "react";
import { api, ApiRequestError } from "../api/client";
import { ModalBackdrop } from "./ModalBackdrop";
import {
  LOCAL_DATABASE_CATEGORY_LABELS,
  type LocalDatabaseCategory,
  type LocalDatabaseEntry,
} from "../api/types";

interface Props {
  onCreated: (entry: LocalDatabaseEntry) => void;
  onClose: () => void;
}

const CATEGORY_OPTIONS = Object.entries(LOCAL_DATABASE_CATEGORY_LABELS) as [
  LocalDatabaseCategory,
  string,
][];

/**
 * The form that submits a local database, following AddProfileModal's shape:
 * local state per field, inline validation, busy-state submit, errors shown
 * in an inline error-box rather than a toast (this modal can be opened from
 * a page with no toast host mounted, same reasoning as AddProfileModal).
 */
export function AddLocalDatabaseModal({ onCreated, onClose }: Props) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [category, setCategory] = useState<LocalDatabaseCategory>(CATEGORY_OPTIONS[0][0]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !url.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const entry = await api.submitLocalDatabase({
        name: name.trim(),
        url: url.trim(),
        category,
      });
      onCreated(entry);
    } catch (err) {
      setError(
        err instanceof ApiRequestError ? err.message : "Could not submit the database",
      );
      setBusy(false);
    }
  };

  return (
    <ModalBackdrop onClick={onClose} onKeyDown={(e) => e.key === "Escape" && onClose()}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>Submit a database</h2>

        <form onSubmit={submit}>
          <div className="modal-body">
            <label htmlFor="ldb-name">Name</label>
            <input
              id="ldb-name"
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Lab reference genome"
            />

            <label htmlFor="ldb-url">URL</label>
            <input
              id="ldb-url"
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://…"
            />

            <label htmlFor="ldb-category">Category</label>
            <select
              id="ldb-category"
              value={category}
              onChange={(e) => setCategory(e.target.value as LocalDatabaseCategory)}
            >
              {CATEGORY_OPTIONS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>

            {error && <div className="error-box">{error}</div>}
          </div>

          <div className="modal-actions">
            <button type="button" className="btn" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn primary" disabled={!name.trim() || !url.trim() || busy}>
              {busy ? "Adding…" : "Add database"}
            </button>
          </div>
        </form>
      </div>
    </ModalBackdrop>
  );
}
