import { useEffect, useState } from "react";
import mastheadImg from "./assets/broadhead-masthead.png";
import { LAUNCHER_VERSION_LABEL } from "./version";
import { nodeStatus, runNode, stopNode } from "./commands";
import type { NodeStatus } from "./commands";

const STATUS_POLL_INTERVAL_MS = 5000;

interface Props {
  onOpenPrimary?: () => void;
}

export function NodeScreen({ onOpenPrimary }: Props) {
  const [status, setStatus] = useState<NodeStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nodeRunning, setNodeRunning] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const s = await nodeStatus();
        if (!cancelled) {
          setStatus(s);
          setNodeRunning(s.running);
        }
      } catch {
        // Node status is advisory — don't surface transient failures.
      }
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
      await runNode();
      setNodeRunning(true);
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
      await stopNode();
      setNodeRunning(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

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
          <span>
            {nodeRunning ? (
              <>
                <span className="status-dot" />
                Worker running
              </>
            ) : (
              "Worker stopped"
            )}
          </span>
          <span>Compute node · {LAUNCHER_VERSION_LABEL}</span>
        </div>
        <div className="masthead-rule-thin" />
      </header>

      <div className="state-body">
        <div className="state-columns">
          <div>
            <div className="state-kicker">Compute node</div>
            <h2 className="state-heading">
              {status?.node_name ?? "Unnamed node"}
            </h2>
            <p className="state-body-text">
              {nodeRunning
                ? "The worker is running and connected to your primary. Jobs assigned to this node will appear in the primary's UI."
                : "The worker is stopped. Start it to let this machine pick up jobs from the primary."}
            </p>

            <div className="state-actions">
              {nodeRunning ? (
                <>
                  <button className="btn btn-secondary" onClick={handleStop} disabled={busy}>
                    {busy ? "Stopping…" : "Stop worker"}
                  </button>
                  {status?.primary_url && onOpenPrimary && (
                    <button
                      className="btn btn-primary"
                      onClick={onOpenPrimary}
                    >
                      Open BioFlow
                    </button>
                  )}
                </>
              ) : (
                <button className="btn btn-primary" onClick={handleRun} disabled={busy}>
                  {busy ? "Starting…" : "Start worker"}
                </button>
              )}
            </div>
          </div>

          <div className="sidebar">
            {status?.primary_url && (
              <>
                <span className="sidebar-aside-label">Primary</span>
                <span className="sidebar-aside-text">
                  Connected to {status.primary_url}
                </span>
              </>
            )}
            <span className="sidebar-aside-label">Node name</span>
            <span className="sidebar-aside-text">
              {status?.node_name ?? "—"}
            </span>
          </div>
        </div>

        {error && (
          <pre role="alert" className="launcher-error" style={{ margin: "20px 0" }}>
            {error}
          </pre>
        )}
      </div>

      <div className="launcher-footer">
        Closing this window leaves the worker running. Reopen the launcher and click
        Stop if you want to stop it.
      </div>
    </div>
  );
}
