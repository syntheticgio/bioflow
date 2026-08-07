import { useState } from "react";
import { applySettings } from "./commands";
import type { Settings as SettingsValues } from "./types";
import { storageLocationChanged } from "./wizard-logic";

interface Props {
  current: SettingsValues;
  /** Whether the stack is currently Running -- storage location and port
   * can't be changed from here while it is (recreating containers under a
   * live session is a Stop-first operation, and for storage specifically,
   * this field never moves data -- see MigrateStorage.tsx for that flow,
   * only offered once Stopped). Both render as plain text instead of
   * inputs in that state, informative rather than an editable-looking
   * field that silently does nothing. */
  running: boolean;
  onClose: () => void;
  onApplied: (settings: SettingsValues) => void;
}

export function Settings({ current, running, onClose, onApplied }: Props) {
  const [storageLocation, setStorageLocation] = useState(current.storageLocation);
  const [port, setPort] = useState(current.port);
  const [networkExposed, setNetworkExposed] = useState(current.networkExposed);
  const [error, setError] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);

  const storageChanged = !running && storageLocationChanged(current.storageLocation, storageLocation);

  async function handleApply() {
    setApplying(true);
    setError(null);
    try {
      await applySettings({ storageLocation, port, networkExposed });
      onApplied({ storageLocation, port, networkExposed });
    } catch (e) {
      setError(String(e));
    } finally {
      setApplying(false);
    }
  }

  return (
    <div className="dialog-backdrop">
      <section className="dialog" aria-label="Settings">
        <h2 className="dialog-title">Settings</h2>
        <div className="dialog-rule" />

        <div className="dialog-fields">
          <div className="field">
            <div className="field-head">
              <span className="field-label">Storage location</span>
            </div>
            {running ? (
              <span className="field-value" aria-label="Storage location">
                {storageLocation}
              </span>
            ) : (
              <input
                className="field-value-input"
                value={storageLocation}
                onChange={(e) => setStorageLocation(e.target.value)}
                disabled={applying}
                aria-label="Storage location"
              />
            )}
            {running && (
              <span className="field-hint" role="note">
                Stop the stack to change this.
              </span>
            )}
            {storageChanged && (
              <span className="field-hint-warn" role="note">
                Changing the storage location points BioFlow at a different folder.
                Existing data does not move — copy it yourself first if you want it to
                carry over.
              </span>
            )}
          </div>

          <div className="field dialog-field-narrow">
            <span className="field-label">Port</span>
            {running ? (
              <span className="field-value field-value-numeric" aria-label="Port">
                {port}
              </span>
            ) : (
              <input
                className="field-value-input field-value-numeric"
                type="number"
                value={port}
                onChange={(e) => setPort(Number(e.target.value))}
                disabled={applying}
                aria-label="Port"
              />
            )}
            {running && (
              <span className="field-hint" role="note">
                Stop the stack to change this.
              </span>
            )}
          </div>

          <div className="field">
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={networkExposed}
                onChange={(e) => setNetworkExposed(e.target.checked)}
                disabled={applying}
              />
              <span className="checkbox-box" aria-hidden="true">
                {networkExposed ? "✓" : ""}
              </span>
              <span className="checkbox-label">
                Allow access from other devices on my network
              </span>
            </label>
            {networkExposed && (
              <span className="checkbox-hint" role="note">
                Anyone on your network can reach BioFlow with no login required.
              </span>
            )}
          </div>
        </div>

        {error && (
          <pre role="alert" className="launcher-error" style={{ marginTop: 16 }}>
            {error}
          </pre>
        )}

        <div className="dialog-actions">
          <button className="btn btn-secondary" onClick={onClose} disabled={applying}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={handleApply} disabled={applying}>
            {applying ? "Applying…" : "Apply"}
          </button>
        </div>
      </section>
    </div>
  );
}
