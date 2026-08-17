import { useEffect, useState } from "react";
import { api } from "../api/client";
import { ModalBackdrop } from "./ModalBackdrop";
import { Icn3dFrame } from "./Icn3dFrame";
import type { VariantStructure } from "../api/types";

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

  return (
    <ModalBackdrop onClick={onClose}>
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
              {/* Stated rather than highlighted: see Icn3dFrame. Naming the
                  residue keeps the view useful -- the reader can find it in
                  iCn3D’s own sequence panel -- without asserting a position
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

              <Icn3dFrame
                pdbId={pdbId}
                title={`iCn3D structure ${pdbId} for ${gene}`}
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
    </ModalBackdrop>
  );
}
