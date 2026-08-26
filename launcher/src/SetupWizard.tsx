import { useEffect, useState } from "react";
import mastheadImg from "./assets/broadhead-masthead.png";
import { LAUNCHER_VERSION_LABEL } from "./version";
import {
  dockerReady,
  runFirstSetup,
  setupDefaults,
  validateSetupPort,
  validateStorage,
  type PortValidation,
  type StoragePathValidation,
} from "./commands";
import { canInstall as computeCanInstall, setupStatusText } from "./wizard-logic";
import { NodeSetup } from "./NodeSetup";

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
  const [installMode, setInstallMode] = useState<"full" | "node" | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [dockerIsReady, setDockerIsReady] = useState(false);
  const [storageLocation, setStorageLocation] = useState("");
  // Display-only: the launcher always installs to this fixed path, so
  // there's nothing to edit here, unlike storage location and port.
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

  const installIsAllowed = computeCanInstall({
    loaded,
    storageLocation,
    storageValidation,
    portValidation,
  });

  const storageProblem = storageValidation.kind !== "Ok";
  const portProblem = portValidation.kind !== "Ok";
  const hasProblems = storageProblem || portProblem;

  async function handleInstall() {
    setInstalling(true);
    setInstallError(null);
    try {
      await runFirstSetup({ storageLocation, port });
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

  // ── Node compute mode ────────────────────────────────────────────

  if (installMode === "node") {
    return (
      <NodeSetup
        onInstalled={onInstalled}
        onBack={() => setInstallMode(null)}
      />
    );
  }

  // ── Mode choice (first render) ───────────────────────────────────

  if (installMode === null) {
    return (
      <div className="launcher-page">
        <header className="masthead">
          <div className="masthead-row">
            <div className="masthead-brand">
              <img src={mastheadImg} alt="BioFlow" className="masthead-logo" />
            </div>
          </div>
          <div className="masthead-rule-thick" />
          <div className="status-line">
            <span>Setup</span>
            <span>{dockerIsReady ? "Docker ready" : "Docker not detected"} · {LAUNCHER_VERSION_LABEL}</span>
          </div>
          <div className="masthead-rule-thin" />
        </header>

        <div className="state-body">
          <h2 className="setup-intro">What do you want to do?</h2>
          <p className="setup-lede">
            The BioFlow launcher can either set up the full application on this
            computer, or connect it as a compute-only node to an existing BioFlow
            installation running somewhere else on your network.
          </p>

          <div className="state-columns">
            <div
              className="mode-card"
              onClick={() => setInstallMode("full")}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") setInstallMode("full");
              }}
            >
              <h3 className="mode-card-title">Set up on this computer</h3>
              <p className="mode-card-body">
                Install the complete BioFlow stack — database, API, web interface,
                and one worker — on this machine. The recommended choice for a
                first install.
              </p>
              <span className="mode-card-action">Start setup →</span>
            </div>

            <div
              className="mode-card"
              onClick={() => setInstallMode("node")}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") setInstallMode("node");
              }}
            >
              <h3 className="mode-card-title">Connect as a compute node</h3>
              <p className="mode-card-body">
                Connect this machine to an existing BioFlow installation as a
                compute-only worker. No database, no web UI — just a worker
                that picks up jobs from the primary. You can also install on a
                remote machine over SSH.
              </p>
              <span className="mode-card-action">Connect to primary →</span>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // ── Full install (existing flow) ──────────────────────────────────

  return (
    <div className="launcher-page">
      <header className="masthead">
        <div className="masthead-row">
          <div className="masthead-brand">
            <img src={mastheadImg} alt="BioFlow" className="masthead-logo" />
          </div>
        </div>
        <div className="masthead-rule-thick" />
        <div className={`status-line${hasProblems ? " status-line-warn" : ""}`}>
          <span>{setupStatusText({ storageProblem, portProblem })}</span>
          <span>{dockerIsReady ? "Docker ready" : "Docker not detected"} · {LAUNCHER_VERSION_LABEL}</span>
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
              <div className="field-value">{installDir}</div>
              <span className="field-hint">
                Fixed — holds the compose file and .env.
              </span>
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
            disabled={!installIsAllowed || installing}
          >
            {installing ? "Setting up…" : "Install"}
          </button>
          <span className="setup-bar-hint">
            {installIsAllowed
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
