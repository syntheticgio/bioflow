import { useEffect, useState } from "react";
import {
  dockerReady,
  runFirstSetup,
  setupDefaults,
  validateSetupPort,
  validateStorage,
  type PortValidation,
  type StoragePathValidation,
} from "./commands";

interface Props {
  onInstalled: (installed: { storageLocation: string; port: number }) => void;
}

const STORAGE_MESSAGES: Record<StoragePathValidation["kind"], string | null> = {
  Ok: null,
  NotWritable: "BioFlow cannot create or write to this folder.",
  NotDockerShared:
    "On macOS this folder must be shared with Docker Desktop (Settings > Resources > File Sharing), or BioFlow will start with no data visible.",
};

const PORT_MESSAGES: Record<PortValidation["kind"], string | null> = {
  Ok: null,
  InUse: "This port is already in use. Pick another.",
};

export function SetupWizard({ onInstalled }: Props) {
  const [loaded, setLoaded] = useState(false);
  const [dockerIsReady, setDockerIsReady] = useState(false);
  const [storageLocation, setStorageLocation] = useState("");
  const [installDir, setInstallDir] = useState("");
  const [port, setPort] = useState(5173);

  const [storageValidation, setStorageValidation] = useState<StoragePathValidation>({
    kind: "Ok",
  });
  const [portValidation, setPortValidation] = useState<PortValidation>({ kind: "Ok" });

  const [installing, setInstalling] = useState(false);
  const [installError, setInstallError] = useState<string | null>(null);

  useEffect(() => {
    setupDefaults().then((d) => {
      setStorageLocation(d.storageLocation);
      setInstallDir(d.installDir);
      setPort(d.port);
      setLoaded(true);
    });
    dockerReady().then(setDockerIsReady);
  }, []);

  useEffect(() => {
    if (!storageLocation) return;
    const id = setTimeout(() => {
      validateStorage(storageLocation).then(setStorageValidation);
    }, 300);
    return () => clearTimeout(id);
  }, [storageLocation]);

  useEffect(() => {
    const id = setTimeout(() => {
      validateSetupPort(port).then(setPortValidation);
    }, 300);
    return () => clearTimeout(id);
  }, [port]);

  const canInstall =
    loaded &&
    storageLocation.length > 0 &&
    installDir.length > 0 &&
    storageValidation.kind === "Ok" &&
    portValidation.kind === "Ok";

  const storageProblem = storageValidation.kind !== "Ok";
  const portProblem = portValidation.kind !== "Ok";
  const hasProblems = storageProblem || portProblem;

  async function handleInstall() {
    setInstalling(true);
    setInstallError(null);
    try {
      await runFirstSetup({ storageLocation, installDir, port });
      onInstalled({ storageLocation, port });
    } catch (e) {
      setInstallError(String(e));
    } finally {
      setInstalling(false);
    }
  }

  if (!loaded) {
    return <p style={{ padding: 36 }}>Loading…</p>;
  }

  return (
    <div className="launcher-page">
      <header className="masthead">
        <div className="masthead-row">
          <div className="masthead-brand">
            <span className="masthead-word">BioFlow</span>
            <span className="masthead-kicker">Genomics pipeline desk</span>
          </div>
        </div>
        <div className="masthead-rule-thick" />
        <div className={`status-line${hasProblems ? " status-line-warn" : ""}`}>
          <span>
            {hasProblems
              ? `First run · ${[storageProblem, portProblem].filter(Boolean).length} thing${
                  storageProblem && portProblem ? "s" : ""
                } to fix`
              : "First run · not yet installed"}
          </span>
          <span>{dockerIsReady ? "Docker ready" : "Docker not detected"} · Launcher 0.1.0</span>
        </div>
        <div className="masthead-rule-thin" />
      </header>

      <div className="state-body">
        <h2 className="setup-intro">Set up BioFlow</h2>
        <p className="setup-lede">
          Choose where BioFlow keeps your data and which port it serves on. Nothing is
          copied or moved.
        </p>

        <div className="setup-fields">
          <div className="field">
            <div className="field-head">
              <span className={`field-label${storageProblem ? " field-label-warn" : ""}`}>
                Storage location
              </span>
            </div>
            <input
              className={`field-value-input${storageProblem ? " field-value-warn" : ""}`}
              value={storageLocation}
              onChange={(e) => setStorageLocation(e.target.value)}
              disabled={installing}
              aria-label="Storage location"
            />
            {STORAGE_MESSAGES[storageValidation.kind] ? (
              <span
                className="field-hint-warn"
                role={storageValidation.kind === "NotDockerShared" ? "note" : "alert"}
              >
                {STORAGE_MESSAGES[storageValidation.kind]}
              </span>
            ) : (
              <span className="field-hint">Projects, uploads and job logs all live here.</span>
            )}
          </div>

          <div className="setup-fields-row">
            <div className="field">
              <span className="field-label">Install directory</span>
              <input
                className="field-value-input"
                value={installDir}
                onChange={(e) => setInstallDir(e.target.value)}
                disabled={installing}
                aria-label="Install directory"
              />
              <span className="field-hint">Holds the compose file and .env.</span>
            </div>
            <div className="field">
              <span className={`field-label${portProblem ? " field-label-warn" : ""}`}>Port</span>
              <input
                className={`field-value-input field-value-numeric${
                  portProblem ? " field-value-warn" : ""
                }`}
                type="number"
                value={port}
                onChange={(e) => setPort(Number(e.target.value))}
                disabled={installing}
                aria-label="Port"
              />
              {PORT_MESSAGES[portValidation.kind] ? (
                <span className="field-hint-warn" role="alert">
                  {PORT_MESSAGES[portValidation.kind]}
                </span>
              ) : (
                <span className="field-hint">Free.</span>
              )}
            </div>
          </div>
        </div>

        <div className="setup-bar">
          <button
            className="btn btn-primary"
            onClick={handleInstall}
            disabled={!canInstall || installing}
          >
            {installing ? "Setting up…" : "Install"}
          </button>
          <span className="setup-bar-hint">
            {canInstall
              ? "Pulls the container images and starts the stack — a few minutes on first run."
              : "Fix the items above to continue."}
          </span>
        </div>

        {installError && (
          <pre role="alert" className="launcher-error" style={{ margin: "0 0 20px" }}>
            {installError}
          </pre>
        )}
      </div>
    </div>
  );
}
