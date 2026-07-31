import { useEffect, useRef, useState } from "react";
import { accessionUrl } from "../lib/format";

const SVIEWER_SRC = "https://www.ncbi.nlm.nih.gov/projects/sviewer/js/sviewer.js";

/** Long enough for a cold NCBI fetch, short enough that an offline machine
 *  reaches the escape hatch rather than spinning forever. */
const LOAD_TIMEOUT_MS = 15_000;

let sviewerPromise: Promise<void> | null = null;

/**
 * Fetch NCBI's Sequence Viewer script, once per page load.
 *
 * Deliberately not imported at module scope: this is the app's only runtime
 * outbound dependency, and everything else here works with no network. Loading
 * it when the modal first opens keeps an offline machine fully functional
 * right up until someone asks for a chromosome view.
 */
function loadSviewer(): Promise<void> {
  if (sviewerPromise) return sviewerPromise;

  sviewerPromise = new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = SVIEWER_SRC;
    script.async = true;
    const timer = setTimeout(() => {
      // A failed load must not be cached, or a user who reconnects can never
      // retry without reloading the page.
      sviewerPromise = null;
      reject(new Error("timed out"));
    }, LOAD_TIMEOUT_MS);
    script.onload = () => {
      clearTimeout(timer);
      resolve();
    };
    script.onerror = () => {
      clearTimeout(timer);
      sviewerPromise = null;
      reject(new Error("failed to load"));
    };
    document.head.appendChild(script);
  });

  return sviewerPromise;
}

/**
 * NCBI's embedded genome browser for one chromosome.
 *
 * A modal rather than a third column: the viewer needs far more width than the
 * Quality tab's chart grid can give it.
 */
export function SequenceViewerModal({
  accession,
  onClose,
}: {
  accession: string;
  onClose: () => void;
}) {
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const mountRef = useRef<HTMLDivElement>(null);
  const ncbiUrl = accessionUrl("nucleotide_accession", accession);

  useEffect(() => {
    let cancelled = false;
    loadSviewer().then(
      () => !cancelled && setState("ready"),
      () => !cancelled && setState("failed"),
    );
    return () => {
      cancelled = true;
    };
  }, []);

  // Escape closes. A listener on document rather than onKeyDown on the
  // backdrop, which only fires when focus is already inside it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  // The script claims `.SeqViewerApp` nodes when it loads. Injecting the div
  // as raw HTML after that, rather than rendering it through React, keeps
  // React from reconciling a subtree the script owns and mutates.
  useEffect(() => {
    if (state !== "ready" || !mountRef.current) return;
    mountRef.current.innerHTML = "";
    const host = document.createElement("div");
    host.className = "SeqViewerApp";
    host.dataset.id = accession;
    host.dataset.tracks = "[key:gene_model_track]";
    host.dataset.width = "100%";
    mountRef.current.appendChild(host);
  }, [state, accession]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal sviewer-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <h2>
          {accession}
          {ncbiUrl && (
            <a
              className="sviewer-external"
              href={ncbiUrl}
              target="_blank"
              rel="noreferrer"
            >
              View at NCBI ↗
            </a>
          )}
        </h2>

        <div className="modal-body">
          {state === "loading" && (
            <div className="chrom-note">Loading the NCBI Sequence Viewer…</div>
          )}
          {state === "failed" && (
            <div className="error-box">
              Couldn’t load the NCBI Sequence Viewer. It’s fetched from
              ncbi.nlm.nih.gov, so this fails when you’re offline or NCBI is
              unreachable.
              {ncbiUrl && (
                <>
                  {" "}
                  <a href={ncbiUrl} target="_blank" rel="noreferrer">
                    View this sequence at NCBI
                  </a>{" "}
                  instead.
                </>
              )}
            </div>
          )}
          <div ref={mountRef} />
        </div>

        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
