import { useEffect, useState } from "react";
import mastheadImg from "./assets/broadhead-masthead.png";
import { LAUNCHER_VERSION_LABEL } from "./version";
import {
  discoverNodeConnection,
  dockerReady,
  installNodeLocal,
  installNodeRemote,
  setupDefaults,
  testSshConnection,
  validateStorage,
  type NodeConnectionInfo,
  type RemoteInfo,
  type StoragePathValidation,
} from "./commands";

interface Props {
  onInstalled: (installed: { storageLocation: string; port: number }) => void;
  onBack: () => void;
}

const STORAGE_MESSAGES: Record<StoragePathValidation["kind"], string | null> = {
  Ok: null,
  NotWritable: "BioFlow cannot create or write to this folder.",
  NotDockerShared:
    "On macOS this folder must be shared with Docker Desktop (Settings > Resources > File Sharing), or BioFlow will start with no data visible.",
};

type Step = "connect" | "details" | "install";

export function NodeSetup({ onInstalled, onBack }: Props) {
  const [loaded, setLoaded] = useState(false);
  const [dockerIsReady, setDockerIsReady] = useState(false);

  // Step state
  const [step, setStep] = useState<Step>("connect");

  // Connect step fields
  const [primaryHost, setPrimaryHost] = useState("");
  const [primaryPort, setPrimaryPort] = useState(5173);
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);
  const [connectionInfo, setConnectionInfo] = useState<NodeConnectionInfo | null>(null);

  // SSH (remote) fields
  const [useRemote, setUseRemote] = useState(false);
  const [sshHost, setSshHost] = useState("");
  const [sshUser, setSshUser] = useState("");
  const [sshPassword, setSshPassword] = useState("");
  const [sshPort, setSshPort] = useState(22);
  const [sshTesting, setSshTesting] = useState(false);
  const [sshResult, setSshResult] = useState<RemoteInfo | null>(null);
  const [sshError, setSshError] = useState<string | null>(null);

  // Details step fields
  const [nodeName, setNodeName] = useState("");
  const [storageLocation, setStorageLocation] = useState("");
  const [installDir, setInstallDir] = useState("");
  const [storageValidation, setStorageValidation] = useState<StoragePathValidation>({
    kind: "Ok",
  });

  // Install step
  const [installing, setInstalling] = useState(false);
  const [installError, setInstallError] = useState<string | null>(null);

  useEffect(() => {
    setupDefaults().then((d) => {
      setStorageLocation(d.storageLocation);
      setInstallDir(d.installDir);
      setLoaded(true);
    });
    dockerReady().then(setDockerIsReady);
  }, []);

  // When connection info loads, prefill the node name
  useEffect(() => {
    if (connectionInfo && !nodeName) {
      setNodeName(connectionInfo.suggested_node_name);
    }
  }, [connectionInfo]);

  // Debounced storage validation
  useEffect(() => {
    if (!storageLocation) return;
    const id = setTimeout(() => {
      validateStorage(storageLocation).then(setStorageValidation);
    }, 300);
    return () => clearTimeout(id);
  }, [storageLocation]);

  async function handleConnect() {
    setConnecting(true);
    setConnectError(null);
    try {
      const info = await discoverNodeConnection({ host: primaryHost, port: primaryPort });
      setConnectionInfo(info);
      setStep("details");
    } catch (e) {
      setConnectError(String(e));
    } finally {
      setConnecting(false);
    }
  }

  async function handleTestSsh() {
    setSshTesting(true);
    setSshError(null);
    setSshResult(null);
    try {
      const info = await testSshConnection({
        host: sshHost,
        user: sshUser,
        password: sshPassword || undefined,
        port: sshPort,
      });
      setSshResult(info);
    } catch (e) {
      setSshError(String(e));
    } finally {
      setSshTesting(false);
    }
  }

  const canProceedToInstall = connectionInfo && nodeName && storageValidation.kind === "Ok";
  const storageProblem = storageValidation.kind !== "Ok";

  async function handleInstall() {
    setInstalling(true);
    setInstallError(null);
    try {
      if (useRemote) {
        await installNodeRemote({
          mongoUrl: connectionInfo!.mongo_url,
          redisUrl: connectionInfo!.redis_url,
          apiUrl: connectionInfo!.api_url,
          nodeName,
          storageLocation,
          sshHost,
          sshUser,
          sshPassword: sshPassword || undefined,
          sshPort,
        });
      } else {
        await installNodeLocal({
          mongoUrl: connectionInfo!.mongo_url,
          redisUrl: connectionInfo!.redis_url,
          apiUrl: connectionInfo!.api_url,
          nodeName,
          storageLocation,
        });
      }
      onInstalled({ storageLocation, port: primaryPort });
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
            <img src={mastheadImg} alt="BioFlow" className="masthead-logo" />
          </div>
          <button className="masthead-settings-link" onClick={onBack}>
            ← Back to mode choice
          </button>
        </div>
        <div className="masthead-rule-thick" />
        <div className={`status-line${connectError || storageProblem ? " status-line-warn" : ""}`}>
          <span>
            {step === "connect" && "Connect to your BioFlow primary"}
            {step === "details" && "Configure compute node"}
            {step === "install" && "Ready to install"}
          </span>
          <span>{dockerIsReady ? "Docker ready" : "Docker not detected"} · {LAUNCHER_VERSION_LABEL}</span>
        </div>
        <div className="masthead-rule-thin" />
      </header>

      <div className="state-body">
        {/* Step 1: Connect to primary */}
        {step === "connect" && (
          <>
            <h2 className="setup-intro">Connect to the primary BioFlow machine</h2>
            <p className="setup-lede">
              Enter the hostname or IP address of the computer running BioFlow, and the
              port it serves on.
            </p>

            <div className="setup-fields">
              <div className="setup-fields-row">
                <div className="field" style={{ flex: 1 }}>
                  <span className="field-label">Primary host</span>
                  <input
                    className="field-value-input"
                    value={primaryHost}
                    onChange={(e) => setPrimaryHost(e.target.value)}
                    placeholder="e.g. 192.168.1.100 or mymac.local"
                    disabled={connecting}
                    aria-label="Primary host"
                  />
                  <span className="field-hint">
                    The IP or hostname of the computer running BioFlow.
                  </span>
                </div>
                <div className="field">
                  <span className="field-label">Port</span>
                  <input
                    className="field-value-input field-value-numeric"
                    type="number"
                    value={primaryPort}
                    onChange={(e) => setPrimaryPort(Number(e.target.value))}
                    disabled={connecting}
                    aria-label="Port"
                  />
                  <span className="field-hint">Usually 5173.</span>
                </div>
              </div>
            </div>

            <div className="setup-bar">
              <button
                className="btn btn-primary"
                onClick={handleConnect}
                disabled={!primaryHost || connecting}
              >
                {connecting ? "Connecting…" : "Connect"}
              </button>
              <span className="setup-bar-hint">
                {primaryHost
                  ? "Reaches out to the primary's API to discover connection details."
                  : "Enter the primary's hostname or IP address."}
              </span>
            </div>

            {connectError && (
              <pre role="alert" className="launcher-error" style={{ margin: "0 0 20px" }}>
                {connectError}
              </pre>
            )}
          </>
        )}

        {/* Step 2: Configure node */}
        {step === "details" && connectionInfo && (
          <>
            <h2 className="setup-intro">Configure your compute node</h2>
            <p className="setup-lede">
              The primary provided its connection details. Set a name for this node and
              choose where it stores its data.
            </p>

            <div className="setup-fields">
              <div className="field">
                <span className="field-label">Primary connection</span>
                <div className="field-value" style={{ fontSize: 13, wordBreak: "break-all" }}>
                  Mongo: {connectionInfo.mongo_url}
                  <br />
                  Redis: {connectionInfo.redis_url}
                </div>
                <span className="field-hint">
                  Auto-discovered from the primary — no manual wiring needed.
                </span>
              </div>

              <div className="field">
                <span className="field-label">Node name</span>
                <input
                  className="field-value-input"
                  value={nodeName}
                  onChange={(e) => setNodeName(e.target.value)}
                  disabled={installing}
                  aria-label="Node name"
                />
                <span className="field-hint">
                  A short label to identify this machine in the primary’s UI.
                </span>
              </div>

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
                  <span className="field-hint">
                    Where this node keeps its project data, job logs, and reference genomes.
                  </span>
                )}
              </div>

              <div className="field">
                <span className="field-label">Install directory</span>
                <div className="field-value">{installDir}</div>
                <span className="field-hint">
                  Fixed — holds the compose file and .env.
                </span>
              </div>

              {/* SSH / remote install toggle */}
              <div className="field">
                <label className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={useRemote}
                    onChange={(e) => setUseRemote(e.target.checked)}
                  />
                  <span className="checkbox-box" aria-hidden="true">
                    {useRemote ? "✓" : ""}
                  </span>
                  <span className="checkbox-label">Install on another machine via SSH</span>
                </label>
                <span className="checkbox-hint" role="note">
                  Instead of installing on this computer, run the worker on a remote
                  machine you can reach with SSH.
                </span>
              </div>

              {useRemote && (
                <>
                  <div className="setup-fields-row">
                    <div className="field" style={{ flex: 1 }}>
                      <span className="field-label">SSH host</span>
                      <input
                        className="field-value-input"
                        value={sshHost}
                        onChange={(e) => setSshHost(e.target.value)}
                        placeholder="e.g. 192.168.1.200"
                        disabled={sshTesting}
                        aria-label="SSH host"
                      />
                    </div>
                    <div className="field">
                      <span className="field-label">SSH port</span>
                      <input
                        className="field-value-input field-value-numeric"
                        type="number"
                        value={sshPort}
                        onChange={(e) => setSshPort(Number(e.target.value))}
                        disabled={sshTesting}
                        aria-label="SSH port"
                      />
                    </div>
                  </div>

                  <div className="setup-fields-row">
                    <div className="field" style={{ flex: 1 }}>
                      <span className="field-label">SSH user</span>
                      <input
                        className="field-value-input"
                        value={sshUser}
                        onChange={(e) => setSshUser(e.target.value)}
                        disabled={sshTesting}
                        aria-label="SSH user"
                      />
                    </div>
                    <div className="field" style={{ flex: 1 }}>
                      <span className="field-label">SSH password</span>
                      <input
                        className="field-value-input"
                        type="password"
                        value={sshPassword}
                        onChange={(e) => setSshPassword(e.target.value)}
                        disabled={sshTesting}
                        aria-label="SSH password"
                      />
                    </div>
                  </div>

                  <div className="setup-bar" style={{ marginTop: 8 }}>
                    <button
                      className="btn btn-secondary"
                      onClick={handleTestSsh}
                      disabled={!sshHost || !sshUser || sshTesting}
                    >
                      {sshTesting ? "Testing…" : "Test SSH connection"}
                    </button>
                    {sshResult && (
                      <span className="setup-bar-hint" style={{ color: "var(--ink-success)" }}>
                        Connected — {sshResult.hostname} ({sshResult.os_arch})
                        {sshResult.docker_ready ? " · Docker ready" : " · Docker not found"}
                      </span>
                    )}
                    {sshError && (
                      <pre role="alert" className="launcher-error" style={{ margin: "4px 0 0" }}>
                        {sshError}
                      </pre>
                    )}
                  </div>
                </>
              )}
            </div>

            <div className="setup-bar">
              <button
                className="btn btn-primary"
                onClick={() => setStep("install")}
                disabled={!canProceedToInstall}
              >
                Continue
              </button>
              <button
                className="btn btn-secondary"
                onClick={() => setStep("connect")}
                style={{ marginLeft: 8 }}
              >
                ← Back
              </button>
              <span className="setup-bar-hint">
                {canProceedToInstall
                  ? "Review the summary, then install."
                  : "Fix the items above to continue."}
              </span>
            </div>
          </>
        )}

        {/* Step 3: Install */}
        {step === "install" && (
          <>
            <h2 className="setup-intro">Install compute node</h2>
            <p className="setup-lede">
              {useRemote
                ? `This will install the BioFlow worker on ${sshUser}@${sshHost} via SSH.`
                : "This will pull the BioFlow worker image and connect it to your primary."}
            </p>

            <div className="setup-fields">
              <div className="field">
                <span className="field-label">Summary</span>
                <div className="field-value" style={{ fontSize: 13 }}>
                  Node name: {nodeName}
                  <br />
                  {useRemote ? (
                    <>
                      Remote: {sshUser}@{sshHost}:{sshPort}
                      <br />
                      Remote storage: {storageLocation}
                    </>
                  ) : (
                    <>
                      Storage: {storageLocation}
                      <br />
                      Install: {installDir}
                    </>
                  )}
                  <br />
                  Primary: {primaryHost}:{primaryPort}
                </div>
              </div>
            </div>

            <div className="setup-bar">
              <button
                className="btn btn-primary"
                onClick={handleInstall}
                disabled={installing}
              >
                {installing ? "Installing…" : "Install node"}
              </button>
              <button
                className="btn btn-secondary"
                onClick={() => setStep("details")}
                disabled={installing}
                style={{ marginLeft: 8 }}
              >
                ← Back
              </button>
              <span className="setup-bar-hint">
                {installing
                  ? "Pulling images and starting the worker — a few minutes on first run."
                  : "Starts only the worker service, not the full stack."}
              </span>
            </div>

            {installError && (
              <pre role="alert" className="launcher-error" style={{ margin: "0 0 20px" }}>
                {installError}
              </pre>
            )}
          </>
        )}
      </div>
    </div>
  );
}
