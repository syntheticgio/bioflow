import { useEffect, useState } from "react";
import mastheadImg from "./assets/broadhead-masthead.png";
import { checkForUpdate, currentSettings, listVersionOptions, openBioFlow, otherStacks, runStack, status, stopStack, updateStack, updateToStage } from "./commands";
import { MigrateStorage } from "./MigrateStorage";
import { PrefetchStep } from "./PrefetchStep";
import { Settings } from "./Settings";
import { SetupWizard } from "./SetupWizard";
import { NodeScreen } from "./NodeScreen";
import type { LauncherState, OtherStack, Settings as SettingsValues, VersionOptions } from "./types";
import { updateAffordance } from "./update-logic";
import type { UpdateInputs } from "./update-logic";

const STATUS_POLL_INTERVAL_MS = 3000;
// The manifest check is a network call (bounded by GhcrClient's own
// timeout), so it polls far less often than status -- there is no reason to
// hit the registry every 3 seconds for a button that changes at most a few
// times a year.
const UPDATE_CHECK_POLL_INTERVAL_MS = 5 * 60 * 1000;
// Worktree stacks come and go on the timescale of a developer starting one,
// not a status flip, so this polls far less often than status. It is two
// `docker` calls per poll and the answer is empty on most machines.
const OTHER_STACKS_POLL_INTERVAL_MS = 30 * 1000;

interface UpdateButtonProps extends UpdateInputs {
  busy: boolean;
  onUpdate: () => void;
  onStageUpdate: (targetTag: string) => void;
}

function deriveVersionMode(bioflowTag: string, developerRepo: string | null): "release" | "alpha" | "beta" | "developer" {
  if (developerRepo != null) return "developer";
  if (bioflowTag === "latest") return "release";
  if (bioflowTag.endsWith("-alpha")) return "alpha";
  if (bioflowTag.endsWith("-beta")) return "beta";
  return "release";
}

/** Renders `updateAffordance`'s three outcomes: nothing, a clickable
 * btn-warn, or a disabled button paired with the reason it's inert -- the
 * same "explain why this control is disabled" treatment Settings.tsx gives
 * storage location and port while the stack is running. A control that
 * vanishes silently leaves a user who forgot their mode with no
 * explanation. */
function UpdateButton({ bioflowTag, developerRepo, updateAvailable, versionOptions, busy, onUpdate, onStageUpdate }: UpdateButtonProps) {
  const affordance = updateAffordance({ bioflowTag, developerRepo, updateAvailable, versionOptions });

  if (affordance.kind === "hidden") return null;

  if (affordance.kind === "available") {
    return (
      <button className="btn btn-warn" onClick={onUpdate} disabled={busy}>
        {busy ? "Updating…" : "Update available"}
      </button>
    );
  }

  if (affordance.kind === "stage-update") {
    return (
      <button
        className="btn btn-warn"
        onClick={() => onStageUpdate(affordance.targetTag)}
        disabled={busy}
      >
        {busy ? "Updating…" : `Update to ${affordance.targetTag}`}
      </button>
    );
  }

  return (
    <span className="update-suppressed">
      <button className="btn btn-secondary" disabled>
        Update
      </button>
      <span className="field-hint" role="note">
        {affordance.reason}
      </span>
    </span>
  );
}

export function App() {
  const [state, setState] = useState<LauncherState>({ kind: "NotInstalled" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showMigrateStorage, setShowMigrateStorage] = useState(false);
  const [updateAvailable, setUpdateAvailable] = useState(false);
  const [versionOptions, setVersionOptions] = useState<VersionOptions | null>(null);
  // Worktree stacks running alongside this one (#320). Empty on an ordinary
  // single-stack machine, in which case nothing renders.
  const [stacks, setStacks] = useState<OtherStack[]>([]);
  // Set only on the transition out of SetupWizard, never on an ordinary
  // Stop -> Run click -- the prefetch offer is a first-run thing, asked
  // once, not something to show every time the stack starts. Cleared the
  // moment PrefetchStep calls back, whether the user picked tools or
  // skipped; there is no "ask again later" path back into it this session.
  const [showPrefetch, setShowPrefetch] = useState(false);
  // Populated once first-run setup completes (or, on a relaunch of an
  // already-installed stack, left at these placeholders until the mount
  // effect below reads the hard memory limit back out of .env).
  const [settings, setSettings] = useState<SettingsValues>({
    storageLocation: "",
    port: 5173,
    networkExposed: false,
    hardMemGb: "",
    bioflowTag: "latest",
    developerRepo: null,
  });

  // Runs once on mount to recover the settings fields a relaunch
  // cannot otherwise reconstruct from run_first_setup's return path: the
  // hard memory limit, the port, the storage location, and network
  // exposure. All are read back out of the install's .env (the source of
  // truth); a missing value leaves the placeholder untouched -- port's
  // placeholder is the compose default 5173, which is also what the stack
  // serves when .env omits WEB_PORT. This runs before Settings.tsx can ever
  // be opened, so there is no race with a value the user is mid-typing
  // there. Recovering storage location and network exposure matters: if
  // they stayed at their placeholders ("" and false), the next Apply would
  // silently rewrite .env to an empty data directory and loopback binding.
  useEffect(() => {
    let cancelled = false;
    currentSettings().then(
      ({ hardMemMb, port, storageLocation, networkExposed, bioflowTag, developerRepo }) => {
        if (cancelled) return;
        setSettings((prev) => ({
          ...prev,
          ...(hardMemMb != null ? { hardMemGb: String(hardMemMb / 1024) } : {}),
          ...(port != null ? { port } : {}),
          ...(storageLocation != null ? { storageLocation } : {}),
          ...(networkExposed != null ? { networkExposed } : {}),
          bioflowTag,
          developerRepo,
        }));
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

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

  // Polled regardless of state.kind: forgotten worktree stacks are exactly
  // as worth surfacing when this launcher's own stack is stopped -- arguably
  // more so, since "Stopped" plus a busy machine is the confusing case.
  // Skipped only when Docker itself is unreachable, when the answer would be
  // an empty list for a reason that has nothing to do with worktrees.
  useEffect(() => {
    if (state.kind === "DockerUnavailable" || state.kind === "NotInstalled") {
      setStacks([]);
      return;
    }
    let cancelled = false;
    async function poll() {
      const next = await otherStacks();
      if (!cancelled) setStacks(next);
    }
    poll();
    const id = setInterval(poll, OTHER_STACKS_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [state.kind]);

  // Only Release mode has a moving target to poll for. Developer builds and
  // pinned alpha/beta stages skip the interval entirely -- the backend
  // already returns false for them without a network call, so this is about
  // not making a pointless IPC round-trip every five minutes.
  useEffect(() => {
    if (state.kind !== "Running") {
      setUpdateAvailable(false);
      setVersionOptions(null);
      return;
    }

    const mode = deriveVersionMode(settings.bioflowTag, settings.developerRepo);
    let cancelled = false;

    if (mode === "release") {
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
    } else if (mode === "alpha" || mode === "beta") {
      async function poll() {
        const opts = await listVersionOptions();
        if (!cancelled) setVersionOptions(opts);
      }
      poll();
      const id = setInterval(poll, UPDATE_CHECK_POLL_INTERVAL_MS);
      return () => {
        cancelled = true;
        clearInterval(id);
      };
    }
    setUpdateAvailable(false);
    setVersionOptions(null);
  }, [state.kind, settings.bioflowTag, settings.developerRepo]);

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

  async function handleStageUpdate(targetTag: string) {
    const ok = window.confirm(
      `This will update BioFlow from ${settings.bioflowTag} to ${targetTag} and restart the stack. Continue?`,
    );
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      await updateToStage(targetTag);
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
        setState({ kind: "Stopped" });
        setShowPrefetch(true);
        handleRun();
      }}
    />;
  }

  // ── Compute-node screen ───────────────────────────────────────────

  if (state.kind === "NodeRunning" || state.kind === "NodeStopped") {
    return (
      <NodeScreen
        onOpenPrimary={() => {
          // Open the primary's BioFlow UI. The primary URL is embedded in
          // .env as MONGO_URL; derive from there by polling nodeStatus.
          // For now, just try to read from .env via the backend.
          window.open(`http://localhost:${settings.port}`);
        }}
      />
    );
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
          {state.kind === "Stopped" && (
            <a
              href="#migrate-storage"
              className="masthead-settings-link"
              onClick={(e) => {
                e.preventDefault();
                setShowMigrateStorage(true);
              }}
            >
              Migrate storage location…
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
            <span>
              Launcher 0.1.0
              <button
                className="btn-reload"
                title="Reload launcher"
                onClick={() => window.location.reload()}
              >
                ↻
              </button>
            </span>
          </div>
        )}
        {state.kind === "Running" && (
          <div className="status-line">
            <span>
              <span className="status-dot" />
              Running · localhost:{settings.port}
            </span>
            <span>
              API healthy
              <button
                className="btn-reload"
                title="Reload launcher"
                onClick={() => window.location.reload()}
              >
                ↻
              </button>
            </span>
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
                  onClick={() => openBioFlow(settings.port)}
                >
                  Open BioFlow
                </button>
                <button className="btn btn-secondary" onClick={handleStop} disabled={busy}>
                  {busy ? "Stopping…" : "Stop"}
                </button>
                <UpdateButton
                  bioflowTag={settings.bioflowTag}
                  developerRepo={settings.developerRepo}
                  updateAvailable={updateAvailable}
                  versionOptions={versionOptions}
                  busy={busy}
                  onUpdate={handleUpdate}
                  onStageUpdate={handleStageUpdate}
                />
              </div>
            </div>
            <div className="sidebar">
              <span className="sidebar-aside-label">Storage location</span>
              <span className="sidebar-path">{settings.storageLocation}</span>
            </div>
          </div>
        )}

        {/* Worktree stacks running alongside this one (#320). Read-only on
            purpose -- this launcher owns the `biopipe` project and nothing
            else; a worktree stack is brought down with
            `ops/worktree-up.sh --down` from the worktree that started it.
            Renders nothing at all when the list is empty, which is the
            ordinary single-stack case. */}
        {stacks.length > 0 && (
          <div className="other-stacks">
            <div className="other-stacks-label">
              {stacks.length === 1
                ? "1 other BioFlow stack is running"
                : `${stacks.length} other BioFlow stacks are running`}
            </div>
            <ul className="other-stacks-list">
              {stacks.map((s) => (
                <li key={s.project} className="other-stacks-item">
                  <span className="other-stacks-slug">{s.slug}</span>
                  {s.webPort != null && (
                    <button
                      type="button"
                      className="other-stacks-port"
                      onClick={() => openBioFlow(s.webPort as number)}
                    >
                      localhost:{s.webPort}
                    </button>
                  )}
                </li>
              ))}
            </ul>
            <div className="other-stacks-note">
              Started from a git worktree. Stop one with{" "}
              <code>./ops/worktree-up.sh --down</code> in that worktree.
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
          running={state.kind === "Running"}
          onClose={() => setShowSettings(false)}
          onApplied={(next) => {
            setSettings(next);
            setShowSettings(false);
          }}
        />
      )}

      {showMigrateStorage && (
        <MigrateStorage
          currentLocation={settings.storageLocation}
          port={settings.port}
          networkExposed={settings.networkExposed}
          onClose={() => setShowMigrateStorage(false)}
          onMigrated={(newLocation) => {
            setSettings((prev) => ({ ...prev, storageLocation: newLocation }));
            setShowMigrateStorage(false);
          }}
        />
      )}
    </div>
  );
}
