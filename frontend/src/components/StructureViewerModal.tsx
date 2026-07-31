import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { VariantStructure } from "../api/types";

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

function uniprotUrl(accession: string): string {
  return `https://www.uniprot.org/uniprotkb/${encodeURIComponent(accession)}`;
}

/**
 * The 3D structure of the protein a variant changes.
 *
 * Resolution happens here rather than in the table: two thirds of genes have
 * no structure, so resolving a whole page would spend dozens of requests to
 * decide how buttons look. The cost is that "no structure available" is
 * reached *after* a click, which is why that state is written as an ordinary
 * answer rather than as a failure -- it is the most common thing this modal
 * has to say.
 */
export function StructureViewerModal({
  objectId,
  gene,
  aaChange,
  aaPos,
  onClose,
}: {
  objectId: string;
  gene: string;
  /** The variant's amino-acid change, e.g. `866I>866L`, shown verbatim. */
  aaChange: string | null;
  aaPos: number | null;
  onClose: () => void;
}) {
  const [result, setResult] = useState<VariantStructure | null>(null);
  const [state, setState] = useState<"resolving" | "done" | "failed">(
    "resolving",
  );
  const [frameState, setFrameState] = useState<"loading" | "ready" | "failed">(
    "loading",
  );

  useEffect(() => {
    let cancelled = false;
    setState("resolving");
    api.variantStructure(objectId, gene).then(
      (r) => {
        if (cancelled) return;
        setResult(r);
        setState("done");
      },
      () => !cancelled && setState("failed"),
    );
    return () => {
      cancelled = true;
    };
  }, [objectId, gene]);

  // Escape closes. On document rather than the backdrop, which only sees keys
  // once focus is already inside it -- and focus here is usually in an iframe
  // on another origin, where nothing of ours can listen at all.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const pdbId = result?.pdb_ids?.[0] ?? null;

  // An iframe fires no error event for a request that never answers, so a
  // slow or blocked NCBI would otherwise leave the panel blank indefinitely.
  useEffect(() => {
    if (!pdbId) return;
    setFrameState("loading");
    const timer = setTimeout(
      () => setFrameState((s) => (s === "loading" ? "failed" : s)),
      LOAD_TIMEOUT_MS,
    );
    return () => clearTimeout(timer);
  }, [pdbId]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal sviewer-modal" onClick={(e) => e.stopPropagation()}>
        <h2>
          {gene}
          {aaChange && <span className="sviewer-position"> {aaChange}</span>}
          {result?.accession && (
            <a
              className="sviewer-external"
              href={uniprotUrl(result.accession)}
              target="_blank"
              rel="noreferrer"
            >
              {result.accession} at UniProt ↗
            </a>
          )}
        </h2>

        <div className="modal-body">
          {state === "resolving" && (
            <div className="chrom-note">Looking up {gene} at UniProt…</div>
          )}

          {state === "failed" && (
            <div className="error-box">
              Couldn’t reach the server to look up {gene}.
            </div>
          )}

          {/* The common case, and written to read as ordinary. Roughly two
              thirds of genes with a residue-changing variant have no solved
              structure, so this is what the modal says most of the time and
              it must not look like something went wrong. */}
          {state === "done" && !pdbId && (
            <div className="chrom-note">
              {result?.accession ? (
                <>
                  No experimental structure has been deposited for {gene}
                  {result.accession && <> ({result.accession})</>}. Most
                  proteins don’t have one.
                </>
              ) : (
                <>
                  Couldn’t identify a reviewed protein for {gene} in this
                  organism, so there’s no structure to show.
                </>
              )}
            </div>
          )}

          {state === "done" && pdbId && (
            <>
              {/* Stated rather than highlighted: see icn3dUrl. Naming the
                  residue keeps the view useful -- the reader can find it in
                  iCn3D's own sequence panel -- without asserting a position
                  on a chain this app has not identified. */}
              <div className="chrom-note">
                Showing {pdbId}
                {result?.pdb_ids && result.pdb_ids.length > 1 && (
                  <> of {result.pdb_ids.length} structures</>
                )}
                {aaPos != null && (
                  <>
                    {" "}
                    · residue {aaPos} is not highlighted; use iCn3D’s sequence
                    panel to locate it
                  </>
                )}
              </div>

              {frameState === "failed" && (
                <div className="error-box">
                  Couldn’t load iCn3D. It’s fetched from ncbi.nlm.nih.gov, so
                  this fails when you’re offline or NCBI is unreachable.{" "}
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
                title={`iCn3D structure ${pdbId} for ${gene}`}
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
          )}
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
