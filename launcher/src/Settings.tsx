import { useEffect, useState } from "react";
import { applySettings, listVersionOptions, rebuildDeveloper } from "./commands";
import type { Settings as SettingsValues, VersionOptions } from "./types";
import { storageLocationChanged } from "./wizard-logic";
import { parseHardMemGb } from "./settings-logic";

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

type VersionMode = "release" | "alpha" | "beta" | "developer";

export function Settings({ current, running, onClose, onApplied }: Props) {
  const [storageLocation, setStorageLocation] = useState(current.storageLocation);
  const [port, setPort] = useState(current.port);
  const [networkExposed, setNetworkExposed] = useState(current.networkExposed);
  const [hardMemGb, setHardMemGb] = useState(current.hardMemGb);
  const [error, setError] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [versionOptions, setVersionOptions] = useState<VersionOptions | null>(null);

  // Determine current version mode from the loaded settings
  const [versionMode, setVersionMode] = useState<VersionMode>(() => {
    if (current.developerRepo) return "developer";
    if (current.bioflowTag === "latest") return "release";
    if (current.bioflowTag.endsWith("-alpha")) return "alpha";
    if (current.bioflowTag.endsWith("-beta")) return "beta";
    return "release"; // fallback
  });
  const [developerRepo, setDeveloperRepo] = useState(current.developerRepo ?? "");

  // Fetch available version options on mount
  useEffect(() => {
    listVersionOptions().then(setVersionOptions).catch(() => {
      // Silently degrade to Release-only -- the dropdown must open without
      // a network dependency.
      setVersionOptions({ release: "latest", alpha: null, beta: null });
    });
  }, []);

  const storageChanged = !running && storageLocationChanged(current.storageLocation, storageLocation);
  const hardMem = parseHardMemGb(hardMemGb);

  async function handleApply() {
    setApplying(true);
    setError(null);
    try {
      // Compute the bioflowTag and developerRepo from the selected version mode
      let bioflowTag: string;
      let devRepo: string | null = null;
      if (versionMode === "developer") {
        bioflowTag = "latest"; // ignored when developerRepo is set
        devRepo = developerRepo || null;
      } else if (versionMode === "release") {
        bioflowTag = "latest";
      } else if (versionMode === "alpha") {
        bioflowTag = versionOptions?.alpha ?? "latest";
      } else {
        // beta
        bioflowTag = versionOptions?.beta ?? "latest";
      }

      await applySettings({
        storageLocation,
        port,
        networkExposed,
        hardMemGb,
        bioflowTag,
        developerRepo: devRepo,
      });
      onApplied({
        storageLocation,
        port,
        networkExposed,
        hardMemGb,
        bioflowTag,
        developerRepo: devRepo,
      });
    } catch (e) {
      setError(String(e));
    } finally {
      setApplying(false);
    }
  }

  async function handleRebuild() {
    setRebuilding(true);
    setError(null);
    try {
      await rebuildDeveloper();
      // After rebuild, the stack is already re-upped, so fire onApplied
      // with the same settings (version mode unchanged).
      onApplied({
        storageLocation,
        port,
        networkExposed,
        hardMemGb,
        bioflowTag: current.bioflowTag,
        developerRepo: current.developerRepo,
      });
    } catch (e) {
      setError(String(e));
    } finally {
      setRebuilding(false);
    }
  }

  const alphaEnabled = versionOptions?.alpha != null;
  const betaEnabled = versionOptions?.beta != null;

  return (
    <div className="dialog-backdrop">
      <section className="dialog" aria-label="Settings">
        <h2 className="dialog-title">Settings</h2>
        <div className="dialog-rule" />

        <div className="dialog-body">
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
                  Existing data does not move -- copy it yourself first if you want it to
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

            {/* ── Version mode ─────────────────────────────────── */}
            <div className="field">
              <span className="field-label">Version</span>
              <div className="field-row">
                <select
                  className="field-value-input"
                  value={versionMode}
                  onChange={(e) => setVersionMode(e.target.value as VersionMode)}
                  disabled={applying || rebuilding}
                  aria-label="BioFlow version"
                >
                  <option value="release">Release</option>
                  <option value="alpha" disabled={!alphaEnabled}>
                    {alphaEnabled ? `Alpha (${versionOptions?.alpha})` : "Alpha (unavailable)"}
                  </option>
                  <option value="beta" disabled={!betaEnabled}>
                    {betaEnabled ? `Beta (${versionOptions?.beta})` : "Beta (unavailable)"}
                  </option>
                  <option value="developer">Developer (local build)</option>
                </select>
              </div>
              <span className="field-hint" role="note">
                Release is the latest stable version. Alpha and Beta are pre-release
                stages published to GHCR. Developer builds and runs from a local checkout.
              </span>
            </div>

            {versionMode === "developer" && (
              <div className="field">
                <span className="field-label">Local repo path</span>
                <div className="field-row">
                  <input
                    className="field-value-input"
                    value={developerRepo}
                    onChange={(e) => setDeveloperRepo(e.target.value)}
                    disabled={applying || rebuilding}
                    placeholder="/path/to/bioflow-checkout"
                    aria-label="Local repository path"
                  />
                  <button
                    className="btn btn-secondary"
                    onClick={handleRebuild}
                    disabled={applying || rebuilding || !developerRepo.trim()}
                    style={{ marginLeft: 8, whiteSpace: "nowrap" }}
                  >
                    {rebuilding ? "Building…" : "Rebuild"}
                  </button>
                </div>
                <span className="field-hint" role="note">
                  Builds Docker images from this local checkout and restarts the stack
                  against them. Only needed after code changes; the initial build happens
                  when you Apply with Developer mode selected.
                </span>
              </div>
            )}
          </div>

          {error && (
            <pre role="alert" className="launcher-error" style={{ marginTop: 16 }}>
              {error}
            </pre>
          )}

        </div>

        <div className="dialog-actions">
          <button className="btn btn-secondary" onClick={onClose} disabled={applying || rebuilding}>
            Cancel
          </button>
          <button
            className="btn btn-primary"
            onClick={handleApply}
            disabled={applying || rebuilding || hardMem.kind === "invalid"}
          >
            {applying ? "Applying…" : "Apply"}
          </button>
        </div>
      </section>
    </div>
  );
}
