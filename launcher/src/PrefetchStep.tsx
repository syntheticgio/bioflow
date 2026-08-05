import { useEffect, useState } from "react";
import mastheadImg from "./assets/broadhead-masthead.png";
import { fetchOptionalTools, installOptionalTool, type OptionalTool } from "./commands";

interface Props {
  /** The port the just-started stack answers on -- known from the fields
   *  SetupWizard already collected, not re-resolved here. */
  port: number;
  /** Fires once the user has either chosen tools (installs kicked off in
   *  the background, not awaited) or skipped. Either way, first-run setup
   *  is done and the launcher moves on to Run's health-gated wait and
   *  browser handoff -- installs continuing after that point is fine, they
   *  are ordinary background docker pulls the user can also just wait out. */
  onDone: () => void;
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / Math.pow(1024, i);
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

/**
 * The optional-tool prefetch offer -- task 9 of docs/superpowers/plans/
 * 2026-08-05-optional-tool-delivery.md, closing issue #40.
 *
 * Inserted between SetupWizard finishing and App.tsx's handleRun() call --
 * an inversion of the rest of first-run setup, which the plan calls out
 * explicitly: everything else in the wizard is answered *before* the stack
 * exists, but this step needs GET /pipelines/tools from the stack itself,
 * which only exists once `run_first_setup` has already brought it up. So
 * this screen appears *after* Install, not as part of the fields above it.
 *
 * The list of tools comes entirely from the stack's own API -- never
 * hardcoded here, per #40's first acceptance criterion. If the fetch
 * returns nothing (a genuinely on-demand-tool-free build, or the request
 * failed) this step renders nothing and calls onDone immediately, so a
 * user on a build with no optional tools -- or an unlucky one where the
 * request timed out -- sees no extra screen at all rather than an empty
 * one.
 */
export function PrefetchStep({ port, onDone }: Props) {
  const [tools, setTools] = useState<OptionalTool[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    fetchOptionalTools(port).then(setTools);
  }, [port]);

  // Nothing to offer: skip this screen entirely rather than show an empty
  // one. Covers both "this build has no on-demand tools" and "the stack
  // didn't answer in time" -- the caller (App.tsx) cannot tell those apart
  // and must not try to; either way there is nothing to ask about.
  useEffect(() => {
    if (tools !== null && tools.filter((t) => !t.available).length === 0) {
      onDone();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tools]);

  if (tools === null) {
    return null;
  }

  const offered = tools.filter((t) => !t.available);
  if (offered.length === 0) {
    // The effect above already scheduled onDone(); render nothing while
    // that happens rather than flash an empty list first.
    return null;
  }

  const totalBytes = offered
    .filter((t) => selected.has(t.name))
    .reduce((sum, t) => sum + (t.download_bytes ?? 0), 0);

  function toggle(name: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  async function handleContinue() {
    setStarting(true);
    // Fire-and-forget, deliberately not awaited in sequence: these are
    // ordinary background `docker pull`s, the same shape as any manual
    // pull a user could run themselves, and there is no job record or
    // progress bar to wire up for a launcher-initiated one (see this
    // screen's own module comment in commands.ts / optional_tools.rs on
    // why this bypasses the queue). Waiting here would block first-run
    // setup on a multi-gigabyte download the user explicitly chose to run
    // in the background.
    for (const tool of offered) {
      if (selected.has(tool.name) && tool.image) {
        installOptionalTool(tool.image).catch(() => {
          // Swallowed here on purpose: a failed background prefetch is not
          // a first-run setup failure. The tool simply stays "not
          // installed" and the Settings > Tools page offers it again,
          // the same as if prefetch had never been offered at all.
        });
      }
    }
    onDone();
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
          <span>First run · optional tools</span>
          <span>Launcher 0.1.0</span>
        </div>
        <div className="masthead-rule-thin" />
      </header>

      <div className="state-body">
        <h2 className="setup-intro">Download optional tools now?</h2>
        <p className="setup-lede">
          Some pipeline tools are large and download on first use instead of
          shipping in the image. You're already online and waiting — this is
          the easiest time to get them out of the way, especially if you plan
          to work offline later. Skipping costs nothing: every tool below
          still downloads automatically the first time you use it.
        </p>

        <div className="setup-fields">
          {offered.map((tool) => (
            <div key={tool.name} className="field">
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={selected.has(tool.name)}
                  onChange={() => toggle(tool.name)}
                  disabled={starting}
                />
                <span className="checkbox-box" aria-hidden="true">
                  {selected.has(tool.name) ? "✓" : ""}
                </span>
                <span className="checkbox-label">
                  {tool.name}
                  {" — "}
                  {tool.download_bytes ? formatBytes(tool.download_bytes) : "size unknown"}
                </span>
              </label>
            </div>
          ))}
        </div>

        <div className="setup-bar">
          <button
            className="btn btn-primary"
            onClick={handleContinue}
            disabled={starting}
          >
            {starting
              ? "Starting…"
              : selected.size > 0
                ? `Download ${formatBytes(totalBytes)} and continue`
                : "Continue"}
          </button>
          <span className="setup-bar-hint">
            {selected.size > 0
              ? "Downloads continue in the background — BioFlow opens right away."
              : "Nothing selected — you can install these later from Settings › Tools."}
          </span>
        </div>
      </div>
    </div>
  );
}
