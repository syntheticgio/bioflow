import { useEffect, useRef, useState } from "react";
import { finishStorageMigration, migrationProgress, startStorageMigration } from "./commands";
import { formatBytes, progressPercent } from "./migration-logic";

interface Props {
  currentLocation: string;
  port: number;
  networkExposed: boolean;
  onClose: () => void;
  onMigrated: (newLocation: string) => void;
}

const POLL_INTERVAL_MS = 500;

type Phase = "idle" | "migrating" | "finishing" | "error";

export function MigrateStorage({ currentLocation, port, networkExposed, onClose, onMigrated }: Props) {
  const [newLocation, setNewLocation] = useState("");
  const [keepOriginal, setKeepOriginal] = useState(false);
  const [validateByHash, setValidateByHash] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [bytesCopied, setBytesCopied] = useState(0);
  const [totalBytes, setTotalBytes] = useState(0);
  const [progressPhase, setProgressPhase] = useState<string>("Scanning");
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
    };
  }, []);

  async function handleStart() {
    setPhase("migrating");
    setError(null);
    try {
      await startStorageMigration({ newLocation, keepOriginal, validateByHash });
    } catch (e) {
      setError(String(e));
      setPhase("error");
      return;
    }

    pollRef.current = window.setInterval(async () => {
      const progress = await migrationProgress();
      if (!progress) return;

      setBytesCopied(progress.bytesCopied);
      setTotalBytes(progress.totalBytes);
      setProgressPhase(progress.phase);

      if (progress.error) {
        if (pollRef.current !== null) window.clearInterval(pollRef.current);
        setError(progress.error);
        setPhase("error");
        return;
      }

      if (progress.phase === "Complete") {
        if (pollRef.current !== null) window.clearInterval(pollRef.current);
        setPhase("finishing");
        try {
          await finishStorageMigration({ newLocation, port, networkExposed });
          onMigrated(newLocation);
        } catch (e) {
          setError(String(e));
          setPhase("error");
        }
      }
    }, POLL_INTERVAL_MS);
  }

  const busy = phase === "migrating" || phase === "finishing";
  const percent = progressPercent(bytesCopied, totalBytes);

  return (
    <div className="dialog-backdrop">
      <section className="dialog" aria-label="Migrate storage location">
        <h2 className="dialog-title">Migrate storage location</h2>
        <div className="dialog-rule" />

        <div className="dialog-body">
          <div className="dialog-fields">
            <div className="field">
              <span className="field-label">Current location</span>
              <input className="field-value-input" value={currentLocation} disabled readOnly aria-label="Current location" />
            </div>

            <div className="field">
              <span className="field-label">New location</span>
              <input
                className="field-value-input"
                value={newLocation}
                onChange={(e) => setNewLocation(e.target.value)}
                disabled={busy}
                aria-label="New location"
              />
            </div>

            <div className="field">
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={keepOriginal}
                  onChange={(e) => setKeepOriginal(e.target.checked)}
                  disabled={busy}
                />
                <span className="checkbox-box" aria-hidden="true">{keepOriginal ? "✓" : ""}</span>
                <span className="checkbox-label">Keep the original copy</span>
              </label>
            </div>

            <div className="field">
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={validateByHash}
                  onChange={(e) => setValidateByHash(e.target.checked)}
                  disabled={busy}
                />
                <span className="checkbox-box" aria-hidden="true">{validateByHash ? "✓" : ""}</span>
                <span className="checkbox-label">
                  Validate by hash (this may take hours depending on the size of the data)
                </span>
              </label>
            </div>

            {busy && (
              <div className="field" role="status">
                <span className="field-label">{progressPhase}</span>
                <span>
                  {formatBytes(bytesCopied)} / {formatBytes(totalBytes)} ({percent}%)
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
          <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={handleStart} disabled={busy || !newLocation}>
            {busy ? "Migrating…" : "Start migration"}
          </button>
        </div>
      </section>
    </div>
  );
}
