import { useEffect, useId, useRef, useState } from "react";
import { accessionUrl } from "../lib/format";

const SVIEWER_SRC = "https://www.ncbi.nlm.nih.gov/projects/sviewer/js/sviewer.js";

/** Long enough for a cold NCBI fetch, short enough that an offline machine
 *  reaches the escape hatch rather than spinning forever.
 *
 *  This covers the whole sequence, not just the script tag: `SeqViewOnReady`
 *  polls every 100ms for as long as `window.SeqView` is undefined and never
 *  gives up on its own, so a dependency that fails to arrive would otherwise
 *  hang here silently. */
const LOAD_TIMEOUT_MS = 15_000;

/**
 * The parts of NCBI's untyped globals this file actually touches.
 *
 * `sviewer.js` is a loader: it defines `SeqViewOnReady` synchronously, then
 * fetches ExtJS and the viewer itself, and only then does `window.SeqView`
 * appear. Both globals are therefore optional -- neither exists until the
 * script has run, and `SeqView` lags `SeqViewOnReady`.
 */
declare global {
  interface Window {
    SeqViewOnReady?: (callback: () => void, scope?: unknown) => void;
    SeqView?: {
      App: {
        new (divId: string): SeqViewApp;
        findAppByDivId(divId: string): SeqViewApp | null;
        getApps(): SeqViewApp[];
      };
      m_Apps?: SeqViewApp[];
    };
  }
}

interface SeqViewApp {
  load(params: string): void;
  m_DivId?: string;
}

let sviewerPromise: Promise<void> | null = null;

/**
 * Fetch NCBI's Sequence Viewer and resolve once `SeqView` is usable.
 *
 * Deliberately not imported at module scope: this is the app's only runtime
 * outbound dependency, and everything else here works with no network. Loading
 * it when the modal first opens keeps an offline machine fully functional
 * right up until someone asks for a chromosome view.
 *
 * Resolution waits for `SeqViewOnReady`, not for `script.onload`. The script
 * at that URL is only a loader -- when its onload fires it has merely started
 * fetching ExtJS and the viewer, and `window.SeqView` is still undefined.
 * `SeqViewOnReady` is the hook NCBI provides for exactly this gap.
 */
function loadSviewer(): Promise<void> {
  if (sviewerPromise) return sviewerPromise;

  sviewerPromise = new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = SVIEWER_SRC;
    script.async = true;

    // A failed load must not be cached, or a user who reconnects can never
    // retry without reloading the page.
    const fail = (reason: string) => {
      sviewerPromise = null;
      reject(new Error(reason));
    };

    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      fail("timed out");
    }, LOAD_TIMEOUT_MS);

    script.onload = () => {
      if (settled) return;
      // The loader defines this synchronously as it runs. If it is still
      // missing, something other than NCBI's loader answered for that URL,
      // and there is no readiness hook to wait on.
      if (typeof window.SeqViewOnReady !== "function") {
        settled = true;
        clearTimeout(timer);
        fail("SeqViewOnReady not defined");
        return;
      }
      window.SeqViewOnReady(() => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve();
      });
    };

    script.onerror = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fail("failed to load");
    };

    document.head.appendChild(script);
  });

  return sviewerPromise;
}

/** Distinguishes concurrent or reopened viewers. `useId` is unique per React
 *  instance, but a remount reuses it, and NCBI keys its app registry on the
 *  div id -- so a counter is mixed in to keep a reopen from colliding with an
 *  instance that is still registered. */
let mountSeq = 0;

/**
 * NCBI's embedded genome browser for one chromosome.
 *
 * A modal rather than a third column: the viewer needs far more width than the
 * Quality tab's chart grid can give it.
 *
 * Uses NCBI's dynamic instantiation path (`new SeqView.App(id)` + `app.load()`)
 * rather than the declarative `class='SeqViewerApp'` form. Their docs rule the
 * declarative form out for a div that starts hidden, which a modal's always is.
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
  const reactId = useId();
  // Stable for the life of this component instance; fresh across remounts.
  const divIdRef = useRef<string>(`sviewer-${reactId.replace(/:/g, "")}-${mountSeq++}`);
  const divId = divIdRef.current;
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

  useEffect(() => {
    const SeqView = window.SeqView;
    if (state !== "ready" || !SeqView || !mountRef.current) return;

    // The viewer takes ownership of this node and mutates it directly, so it
    // is created outside React's tree rather than rendered as JSX.
    const host = document.createElement("div");
    host.id = divId;
    mountRef.current.appendChild(host);

    const app = new SeqView.App(divId);
    app.load(
      `embedded=true&id=${encodeURIComponent(accession)}&tracks=[key:gene_model_track]`,
    );

    return () => {
      // SeqView.App exposes no teardown API -- no destroy(), remove(), or
      // dispose(). Its constructor pushes the instance into the global
      // SeqView.m_Apps array and nothing ever removes it, so an app object
      // and whatever it still references outlive this component no matter
      // what we do here.
      //
      // Dropping the entry ourselves is the closest available cleanup: it is
      // the array findAppByDivId() scans, so leaving a stale entry behind
      // would make a later viewer on a recycled div id resolve to a dead app.
      // Instance identity is safe to splice because App tracks its own index
      // in m_Idx, and findAppByIndex() matches on that field rather than on
      // array position.
      const apps = SeqView.m_Apps;
      if (apps) {
        const i = apps.indexOf(app);
        if (i >= 0) apps.splice(i, 1);
      }
      // Detaches the DOM the viewer built. Any timers, ajax callbacks, or
      // document-level listeners it registered are not reachable from here
      // and are not cleaned up.
      host.remove();
    };
  }, [state, accession, divId]);

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
