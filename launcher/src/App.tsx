import { useEffect, useState } from "react";
import mastheadImg from "./assets/broadhead-masthead.png";
import { checkForUpdate, runStack, status, stopStack, updateStack } from "./commands";
import { PrefetchStep } from "./PrefetchStep";
import { Settings } from "./Settings";
import { SetupWizard } from "./SetupWizard";
import type { LauncherState, Settings as SettingsValues } from "./types";

const STATUS_POLL_INTERVAL_MS = 3000;
// The manifest check is a network call (bounded by GhcrClient's own
// timeout), so it polls far less often than status -- there is no reason to
// hit the registry every 3 seconds for a button that changes at most a few
// times a year.
const UPDATE_CHECK_POLL_INTERVAL_MS = 5 * 60 * 1000;

export function App() {
  const [state, setState] = useState<LauncherState>({ kind: "NotInstalled" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [updateAvailable, setUpdateAvailable] = useState(false);
  // Set only on the transition out of SetupWizard, never on an ordinary
  // Stop -> Run click -- the prefetch offer is a first-run thing, asked
  // once, not something to show every time the stack starts. Cleared the
  // moment PrefetchStep calls back, whether the user picked tools or
  // skipped; there is no "ask again later" path back into it this session.
  const [showPrefetch, setShowPrefetch] = useState(false);
  // Populated once first-run setup completes (or, on a relaunch of an
  // already-installed stack, left at these placeholders -- there is no
  // command yet to read .env back out, since nothing before this needed
  // to know the values outside of setup/settings themselves).
  const [settings, setSettings] = useState<SettingsValues>({
    storageLocation: "",
    port: 5173,
    networkExposed: false,
    hardMemGb: "",
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

  useEffect(() => {
    if (state.kind !== "Running") {
      setUpdateAvailable(false);
      return;
    }
    let cancelled = false;
    async function poll() {
      const available = await checkForUpdate();
      if (!cancelled) setUpdateAvailable(available);
    }
    poll();
    const id = setInterval(poll, UPDATE_CHECK_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [state.kind]);

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

  if (state.kind === "NotInstalled") {
    return <SetupWizard
      onInstalled={({ storageLocation, port }) => {
        setSettings((prev) => ({ ...prev, storageLocation, port }));
        // run_first_setup already brought the stack up (setup::install's
        // last step); what's left is exactly Run's health-gated wait and
        // browser handoff, so reuse handleRun rather than landing on
        // Stopped and asking for a second click.
        //
        // Prefetch (task 9, closing #40) needs the API reachable to call
        // GET /pipelines/tools, and health-gating is exactly what handleRun
        // already waits for -- so it is offered *after* this resolves, not
        // before, which is the ordering inversion the plan calls out: every
        // other first-run question is answered before the stack exists,
        // this one only makes sense once it does. handleRun also opens the
        // browser as its own side effect (run_stack's job, unconditional),
        // so by the time PrefetchStep renders the app is already open in a
        // tab -- the prefetch offer appears in the launcher window
        // alongside it, not gating it.
        setState({ kind: "Stopped" });
        setShowPrefetch(true);
        handleRun();
      }}
    />;
  }

  // Renders in place of the running/stopped screen, not layered over it --
  // showPrefetch is only ever true right after the health-gated wait above
  // resolves into state.kind === "Running", so this and the main screen
  // below never need to coexist. Guarding on state.kind too (rather than
  // showPrefetch alone) means a mid-wait error (state stays "Stopped",
  // handleRun's catch sets `error`) falls through to the ordinary Stopped
  // screen with its error box instead of hanging on a prefetch screen for
  // a stack that never became healthy.
  if (showPrefetch && state.kind === "Running") {
    return (
      <PrefetchStep port={settings.port} onDone={() => setShowPrefetch(false)} />
    );
  }

  const showFooter = state.kind === "Stopped" || state.kind === "Running";

  return (
    <div className="launcher-page">
      <header className="masthead">
        <div className="masthead-row">
          <div className="masthead-brand">
            <img src={mastheadImg} alt="BioFlow" className="masthead-logo" />
          </div>
          {state.kind !== "DockerUnavailable" && (
            <a
              href="#settings"
              className="masthead-settings-link"
              onClick={(e) => {
                e.preventDefault();
                setShowSettings(true);
              }}
            >
              Settings
            </a>
          )}
        </div>
        <div className="masthead-rule-thick" />

        {state.kind === "DockerUnavailable" && (
          <div className="status-line status-line-warn">
            <span>Docker unavailable</span>
            <span>Launcher 0.1.0</span>
          </div>
        )}
        {state.kind === "Stopped" && (
          <div className="status-line">
            <span>Stopped</span>
            <span>Launcher 0.1.0</span>
          </div>
        )}
        {state.kind === "Running" && (
          <div className="status-line">
            <span>
              <span className="status-dot" />
              Running · localhost:{settings.port}
            </span>
            <span>API healthy</span>
          </div>
        )}
        <div className="masthead-rule-thin" />
      </header>

      <div className="state-body">
        {state.kind === "DockerUnavailable" && !state.installed && (
          <div className="state-columns">
            <div>
              <h2 className="state-heading state-heading-standalone">Docker is not installed.</h2>
              <p className="state-body-text">
                BioFlow runs its services as containers, so Docker Desktop has to be
                installed and running before the stack can start.
              </p>
              <div className="state-actions">
                <a
                  className="btn btn-primary"
                  href="https://www.docker.com/products/docker-desktop/"
                  target="_blank"
                  rel="noreferrer"
                >
                  Download Docker Desktop
                </a>
                <button className="btn btn-secondary" onClick={() => status().then(setState)}>
                  Check again
                </button>
              </div>
            </div>
            <div className="sidebar">
              <span className="sidebar-aside-label">On macOS, also</span>
              <span className="sidebar-aside-text">
                Share your storage drive under Settings → Resources → File Sharing, then
                Apply &amp; Restart.
              </span>
            </div>
          </div>
        )}

        {state.kind === "DockerUnavailable" && state.installed && (
          <div className="state-columns">
            <div>
              <h2 className="state-heading state-heading-standalone">Waiting for Docker…</h2>
              <p className="state-body-text">
                Docker is installed but its daemon isn't reachable yet. The launcher checks
                again every few seconds — no action needed if it's still starting up.
              </p>
            </div>
          </div>
        )}

        {state.kind === "Stopped" && (
          <div className="state-columns">
            <div>
              <div className="state-kicker">Ready when you are</div>
              <h2 className="state-heading">BioFlow is stopped</h2>
              <p className="state-body-text">
                Start the stack to begin working — a cold start pulls nothing new, but
                waits for the API to report healthy before handing off to your browser.
              </p>
              <div className="state-actions">
                <button className="btn btn-primary" onClick={handleRun} disabled={busy}>
                  {busy ? "Starting…" : "Run"}
                </button>
              </div>
            </div>
          </div>
        )}

        {state.kind === "Running" && (
          <div className="state-columns">
            <div>
              <div className="state-kicker">The stack is up</div>
              <h2 className="state-heading">Serving on port {settings.port}</h2>
              <p className="state-body-text">
                The API is reporting healthy. Open the desk in your browser to get to
                work.
              </p>
              <div className="state-actions">
                <button
                  className="btn btn-primary"
                  onClick={() => window.open(`http://localhost:${settings.port}`)}
                >
                  Open BioFlow
                </button>
                <button className="btn btn-secondary" onClick={handleStop} disabled={busy}>
                  {busy ? "Stopping…" : "Stop"}
                </button>
                {updateAvailable && (
                  <button className="btn btn-warn" onClick={handleUpdate} disabled={busy}>
                    {busy ? "Updating…" : "Update available"}
                  </button>
                )}
              </div>
            </div>
            <div className="sidebar">
              <span className="sidebar-aside-label">Storage location</span>
              <span className="sidebar-path">{settings.storageLocation}</span>
            </div>
          </div>
        )}

        {error && (
          <pre role="alert" className="launcher-error" style={{ margin: "20px 0" }}>
            {error}
          </pre>
        )}
      </div>

      {showFooter && (
        <div className="launcher-footer">
          Closing this window leaves the stack running. Reopen the launcher and click Stop
          if you want to stop it.
        </div>
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
    </div>
  );
}
