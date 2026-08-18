import { useEffect, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ProteinRecordRow, ProteinStructureState } from "../api/types";
import { useDebounced } from "../lib/useDebounced";
import { Icn3dFrame } from "./Icn3dFrame";

const PAGE_SIZE = 50;

function uniprotUrl(accession: string): string {
  return `https://www.uniprot.org/uniprotkb/${encodeURIComponent(accession)}`;
}

/** Always rendered, always disabled -- prediction is not implemented in any
 *  of the four states, and a control that only sometimes appears would read
 *  as though it worked in the states where it's missing. */
function PredictButton() {
  return (
    <button
      type="button"
      className="btn"
      disabled
      title="Structure prediction isn't available yet."
    >
      Predict structure
    </button>
  );
}

/**
 * The structure panel for a selected record: resolves and renders on
 * selection rather than for the whole page, for the reason
 * StructureViewerModal records -- most records resolve to nothing, and
 * pre-resolving a page would spend a round trip per row to decide how
 * buttons look.
 */
function RecordStructure({
  objectId,
  record,
}: {
  objectId: string;
  record: ProteinRecordRow;
}) {
  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ["protein-record-structure", objectId, record.ordinal],
    queryFn: () => api.proteinRecordStructure(objectId, record.ordinal),
  });

  const state: ProteinStructureState | "loading" | "failed" = isError
    ? "failed"
    : isLoading || !data
      ? "loading"
      : data.state;

  return (
    <div>
      {state === "loading" && (
        <div className="chrom-note">Looking up {record.identifier}…</div>
      )}

      {state === "failed" && (
        <div className="error-box">
          Couldn't reach the server to look up {record.identifier}.
        </div>
      )}

      {/* lookup_failed is a UniProt outage, distinct from the request itself
          failing above -- both read the same to the user, but only this one
          is worth a retry rather than a reload. */}
      {state === "lookup_failed" && (
        <div className="error-box">
          Couldn't reach UniProt to look this up.{" "}
          <button
            type="button"
            className="btn"
            style={{ padding: "1px 8px", fontSize: 11 }}
            onClick={() => refetch()}
            disabled={isFetching}
          >
            Retry
          </button>
        </div>
      )}

      {state === "no_reference" && (
        <div className="chrom-note">
          This record's header doesn't name a protein we can look up. Headers
          from annotation tools usually don't.
        </div>
      )}

      {/* The common case, and written to read as ordinary: most proteins
          have no experimentally solved structure. */}
      {state === "no_structure" && (
        <div className="chrom-note">
          No experimental structure has been deposited for{" "}
          {data?.protein_name ?? data?.accession ?? record.identifier}. Most
          proteins don't have one.
        </div>
      )}

      {state === "resolved" && data && (
        <>
          <div className="chrom-note">
            {data.protein_name && <>{data.protein_name} · </>}
            {data.accession && (
              <a
                href={uniprotUrl(data.accession)}
                target="_blank"
                rel="noreferrer"
              >
                {data.accession} at UniProt ↗
              </a>
            )}
            {data.pdb_ids.length > 1 && (
              <> · showing 1 of {data.pdb_ids.length} structures</>
            )}
          </div>
          <Icn3dFrame
            pdbId={data.pdb_ids[0]}
            title={`iCn3D structure ${data.pdb_ids[0]} for ${record.identifier}`}
          />
        </>
      )}

      <div style={{ marginTop: 8 }}>
        <PredictButton />
      </div>
    </div>
  );
}

/**
 * The Structure tab: a searchable, paged list of a protein FASTA's records on
 * the left, and the selected record's resolved structure on the right.
 */
export function ProteinStructureTab({ objectId }: { objectId: string }) {
  const [page, setPage] = useState(0);
  const [searchInput, setSearchInput] = useState("");
  const [selected, setSelected] = useState<ProteinRecordRow | null>(null);

  const search = useDebounced(searchInput, 300);

  useEffect(() => {
    setPage(0);
  }, [search]);

  const { data, isLoading } = useQuery({
    queryKey: ["protein-records", objectId, page, search],
    queryFn: () =>
      api.proteinRecords(objectId, {
        offset: page * PAGE_SIZE,
        limit: PAGE_SIZE,
        q: search || undefined,
      }),
    placeholderData: keepPreviousData,
  });

  const rows = data?.rows ?? [];
  const hasNext = rows.length === PAGE_SIZE;

  return (
    <div className="section" style={{ display: "flex", gap: 16 }}>
      <div style={{ flex: "0 0 320px", minWidth: 0 }}>
        <div className="section-title">Proteins</div>

        <input
          type="text"
          placeholder="Search identifier or description…"
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          style={{ width: "100%", marginBottom: 8, boxSizing: "border-box" }}
        />

        {data?.truncated && (
          <div className="chrom-note">
            This file has more records than can be indexed exactly, so the
            count above is an estimate and the list may not show every
            protein.
          </div>
        )}

        {isLoading && !data ? (
          <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
            Loading…
          </div>
        ) : rows.length === 0 && data?.indexed === false ? (
          // Distinct from "no records match your search" below: this object
          // has never had protein indexing run at all, most likely because
          // its role was set to Protein after ingest rather than before --
          // ingest_headers is the only place indexing runs, and there is no
          // automatic re-index to catch a role changed later. Re-ingesting
          // is the actual fix, so say that instead of leaving the user to
          // guess why an apparently-valid protein FASTA shows nothing.
          <div className="chrom-note">
            This file's proteins haven't been indexed yet. Re-ingest the file
            to enable this view.
          </div>
        ) : rows.length === 0 ? (
          <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
            No records match this search.
          </div>
        ) : (
          <>
            <table className="trim-table">
              <thead>
                <tr>
                  <th>Identifier</th>
                  <th style={{ textAlign: "right" }}>Length</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.ordinal}
                    onClick={() => setSelected(row)}
                    style={{
                      cursor: "pointer",
                      background:
                        selected?.ordinal === row.ordinal
                          ? "var(--bg-elevated)"
                          : undefined,
                      // has_reference is false when the header names no
                      // accession the app can resolve -- true for every
                      // record of a de-novo-annotated proteome (Prokka/Bakta
                      // locus tags, for instance). Muting those rows lets a
                      // user tell at a glance which will resolve to
                      // something before clicking each one, same as the
                      // reduced-emphasis treatment other tables in this repo
                      // use for a row that is in a lesser state.
                      color: row.has_reference
                        ? undefined
                        : "var(--text-faint)",
                    }}
                    title={
                      row.has_reference
                        ? row.description
                        : `${row.description} (header names no protein we can look up)`
                    }
                  >
                    <td className="mono">{row.identifier}</td>
                    <td style={{ textAlign: "right" }}>
                      {row.length.toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                marginTop: 8,
                fontSize: 11,
                color: "var(--text-faint)",
              }}
            >
              <span>
                {data?.total != null
                  ? `${data.total.toLocaleString()} record${data.total === 1 ? "" : "s"}`
                  : `Showing ${rows.length}`}
              </span>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <button
                  type="button"
                  className="btn"
                  style={{ padding: "1px 8px", fontSize: 11 }}
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                  disabled={page === 0}
                >
                  Prev
                </button>
                <span>Page {page + 1}</span>
                <button
                  type="button"
                  className="btn"
                  style={{ padding: "1px 8px", fontSize: 11 }}
                  onClick={() => setPage((p) => p + 1)}
                  disabled={!hasNext}
                >
                  Next
                </button>
              </div>
            </div>
          </>
        )}
      </div>

      <div style={{ flex: 1, minWidth: 0 }}>
        {selected ? (
          <RecordStructure objectId={objectId} record={selected} />
        ) : (
          <div>
            <div className="chrom-note">
              Select a protein on the left to look up its structure.
            </div>
            <div style={{ marginTop: 8 }}>
              <PredictButton />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
