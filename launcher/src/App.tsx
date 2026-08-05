import { useEffect, useState } from "react";
import { runStack, status, stopStack, updateStack } from "./commands";
import { Settings } from "./Settings";
import { SetupWizard } from "./SetupWizard";
import type { LauncherState, Settings as SettingsValues } from "./types";

const STATUS_POLL_INTERVAL_MS = 3000;

export function App() {
  const [state, setState] = useState<LauncherState>({ kind: "NotInstalled" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  // Populated once first-run setup completes (or, on a relaunch of an
  // already-installed stack, left at these placeholders -- there is no
  // command yet to read .env back out, since nothing before this needed
  // to know the values outside of setup/settings themselves).
  const [settings, setSettings] = useState<SettingsValues>({
    storageLocation: "",
    port: 5173,
    networkExposed: false,
  });

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      const next = await status();
      if (!cancelled) setState(next);
    }
    poll();
    const id = setInterval(poll, STATUS_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  async function handleRun() {
    setBusy(true);
    setError(null);
    try {
      await runStack();
      setState({ kind: "Running" });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function handleStop() {
    setBusy(true);
    setError(null);
    try {
      await stopStack();
      setState({ kind: "Stopped" });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function handleUpdate() {
    setBusy(true);
    setError(null);
    try {
      await updateStack();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <h1>BioFlow</h1>

      {state.kind === "NotInstalled" && (
        <SetupWizard
          onInstalled={({ storageLocation, port }) => {
            setSettings((prev) => ({ ...prev, storageLocation, port }));
            // run_first_setup already brought the stack up (setup::install's
            // last step); what's left is exactly Run's health-gated wait and
            // browser handoff, so reuse handleRun rather than landing on
            // Stopped and asking for a second click.
            setState({ kind: "Stopped" });
            handleRun();
          }}
        />
      )}

      {state.kind === "DockerUnavailable" && !state.installed && (
        <div>
          <p>Docker is not installed.</p>
          <a href="https://www.docker.com/products/docker-desktop/" target="_blank" rel="noreferrer">
            Download Docker Desktop
          </a>
          <button onClick={() => status().then(setState)}>Check again</button>
        </div>
      )}

      {state.kind === "DockerUnavailable" && state.installed && (
        <div>
          <p>Waiting for Docker…</p>
        </div>
      )}

      {state.kind === "Stopped" && (
        <div>
          <p>Stopped.</p>
          <button onClick={handleRun} disabled={busy}>
            {busy ? "Starting…" : "Run"}
          </button>
        </div>
      )}

      {state.kind === "Running" && (
        <div>
          <p>Running.</p>
          <button onClick={() => window.open(`http://localhost:${settings.port}`)}>
            Open Browser
          </button>
          <button onClick={handleStop} disabled={busy}>
            {busy ? "Stopping…" : "Stop"}
          </button>
          <button onClick={handleUpdate} disabled={busy}>
            Update
          </button>
        </div>
      )}

      {state.kind !== "NotInstalled" && (
        <button onClick={() => setShowSettings(true)}>Settings</button>
      )}

      <p role="note">
        Closing this window leaves the stack running. Reopen the launcher and
        click Stop if you want to stop it.
      </p>

      {error && (
        <pre role="alert" style={{ whiteSpace: "pre-wrap" }}>
          {error}
        </pre>
      )}

      {showSettings && (
        <Settings
          current={settings}
          onClose={() => setShowSettings(false)}
          onApplied={(next) => {
            setSettings(next);
            setShowSettings(false);
          }}
        />
      )}
    </main>
  );
}
