import { useState } from "react";
import { applySettings } from "./commands";
import type { Settings as SettingsValues } from "./types";
import { storageLocationChanged } from "./wizard-logic";
import { parseHardMemGb } from "./settings-logic";

interface Props {
  current: SettingsValues;
  onClose: () => void;
  onApplied: (settings: SettingsValues) => void;
}

export function Settings({ current, onClose, onApplied }: Props) {
  const [storageLocation, setStorageLocation] = useState(current.storageLocation);
  const [port, setPort] = useState(current.port);
  const [networkExposed, setNetworkExposed] = useState(current.networkExposed);
  const [hardMemGb, setHardMemGb] = useState(current.hardMemGb);
  const [error, setError] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);

  const storageChanged = storageLocationChanged(current.storageLocation, storageLocation);
  const hardMem = parseHardMemGb(hardMemGb);

  async function handleApply() {
    setApplying(true);
    setError(null);
    try {
      await applySettings({ storageLocation, port, networkExposed, hardMemGb });
      onApplied({ storageLocation, port, networkExposed, hardMemGb });
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
            <input
              className="field-value-input"
              value={storageLocation}
              onChange={(e) => setStorageLocation(e.target.value)}
              disabled={applying}
              aria-label="Storage location"
            />
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
            <input
              className="field-value-input field-value-numeric"
              type="number"
              value={port}
              onChange={(e) => setPort(Number(e.target.value))}
              disabled={applying}
              aria-label="Port"
            />
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

          <div className="field dialog-field-narrow">
            <span className="field-label">Hard memory limit (GB)</span>
            <input
              className="field-value-input field-value-numeric"
              type="number"
              min="1"
              step="1"
              placeholder="No limit"
              value={hardMemGb}
              onChange={(e) => setHardMemGb(e.target.value)}
              disabled={applying}
              aria-label="Hard memory limit in GB"
            />
            {hardMem.kind === "none" && (
              <span className="field-hint" role="note">
                No hard cap. BioFlow will not <em>plan</em> to exceed the memory
                budget you set inside the app, but a job that uses more than
                predicted can still go over. Nothing is killed.
              </span>
            )}
            {hardMem.kind === "set" && (
              <span className="field-hint-warn" role="note">
                BioFlow <em>cannot</em> exceed {hardMemGb} GB. A job that tries is
                killed and loses its work. Protects the machine; costs the job.
                Takes effect on restart.
              </span>
            )}
            {hardMem.kind === "invalid" && (
              <span className="field-hint-warn" role="note">
                Enter a whole number of GB (at least 1), or leave blank for no
                hard cap.
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
          <button
            className="btn btn-primary"
            onClick={handleApply}
            disabled={applying || hardMem.kind === "invalid"}
          >
            {applying ? "Applying…" : "Apply"}
          </button>
        </div>
      </section>
    </div>
  );
}
