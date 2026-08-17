import { useEffect, useState } from "react";

/** NCBI's own documented embed path. `?pdbid=` loads a PDB entry; the
 *  remaining parameters are the ones their embedding example uses to strip
 *  the standalone app's chrome down to something that belongs in a panel. */
const ICN3D_BASE = "https://www.ncbi.nlm.nih.gov/Structure/icn3d/";

/** Long enough for a cold NCBI load on a slow link, short enough that an
 *  offline machine reaches the escape hatch rather than staring at a spinner.
 *  Matches SequenceViewerModal's budget for the same reason. */
const LOAD_TIMEOUT_MS = 15_000;

/**
 * The iframe URL for one PDB entry.
 *
 * Deliberately carries no residue selection. iCn3D's selector needs an
 * explicit chain (`select $1BK5.A:428`), and this app cannot yet say which
 * chain holds the variant's protein: even entries that are "one protein" are
 * routinely multi-chain (4W6Z has four), and 1EE4 holds two different
 * proteins across six. Guessing a chain would highlight a real residue on the
 * wrong molecule -- the same confidently-wrong answer the resolver's length
 * guard exists to prevent, and not something a viewer can walk back. The
 * residue is stated in text instead, and highlighting waits for the SIFTS
 * chain mapping the design defers.
 */
function icn3dUrl(pdbId: string): string {
  const params = new URLSearchParams({
    pdbid: pdbId,
    // NCBI's embed example: no popups, no command bar, no notes, no title.
    closepopup: "1",
    showcommand: "0",
    shownote: "0",
    showtitle: "0",
    mobilemenu: "1",
  });
  return `${ICN3D_BASE}?${params.toString()}`;
}

/**
 * Embeds an iCn3D iframe for viewing a PDB structure.
 *
 * Owns its own load-state management and renders both the loading/failure
 * UI and the iframe itself. Provides an escape-hatch link when offline.
 */
export function Icn3dFrame({
  pdbId,
  title,
}: {
  pdbId: string;
  title: string;
}) {
  const [frameState, setFrameState] = useState<"loading" | "ready" | "failed">(
    "loading",
  );

  // An iframe fires no error event for a request that never answers, so a
  // slow or blocked NCBI would otherwise leave the panel blank indefinitely.
  useEffect(() => {
    setFrameState("loading");
    const timer = setTimeout(
      () => setFrameState((s) => (s === "loading" ? "failed" : s)),
      LOAD_TIMEOUT_MS,
    );
    return () => clearTimeout(timer);
  }, [pdbId]);

  return (
    <>
      {frameState === "failed" && (
        <div className="error-box">
          Couldn't load iCn3D. It's fetched from ncbi.nlm.nih.gov, so
          this fails when you're offline or NCBI is unreachable.{" "}
          <a
            href={icn3dUrl(pdbId)}
            target="_blank"
            rel="noreferrer"
          >
            Open {pdbId} at NCBI
          </a>{" "}
          instead.
        </div>
      )}

      <iframe
        title={title}
        src={icn3dUrl(pdbId)}
        onLoad={() => setFrameState("ready")}
        onError={() => setFrameState("failed")}
        style={{
          width: "100%",
          height: "60vh",
          border: "none",
          display: frameState === "failed" ? "none" : "block",
        }}
        // The viewer needs WebGL and fullscreen; nothing else is
        // granted, and it stays cross-origin so it cannot reach into
        // this page.
        allow="fullscreen"
        sandbox="allow-scripts allow-same-origin allow-popups"
      />
    </>
  );
}
