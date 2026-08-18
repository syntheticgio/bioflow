import { useEffect, useRef, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type {
  PredictionState,
  ProteinPredictionStatus,
  ProteinRecordRow,
  ProteinStructureState,
} from "../api/types";
import { useDebounced } from "../lib/useDebounced";
import { Icn3dFrame } from "./Icn3dFrame";

const PAGE_SIZE = 50;
const POLL_INTERVAL_MS = 5_000;

function uniprotUrl(accession: string): string {
  return `https://www.uniprot.org/uniprotkb/${encodeURIComponent(accession)}`;
}

/** Confidence key rendered below predicted structures. */
function PlddtLegend() {
  return (
    <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 4 }}>
      Confidence:{" "}
      <span style={{ color: "#0055ff" }}>██ Very high (90+)</span>{" "}
      <span style={{ color: "#66ccff" }}>██ Confident (70-90)</span>{" "}
      <span style={{ color: "#ffff00" }}>██ Low (50-70)</span>{" "}
      <span style={{ color: "#ff6600" }}>██ Very low ({'<'}50)</span>
    </div>
  );
}

/**
 * Stateful Predict button that checks prediction status, starts predictions,
 * polls for progress, and shows results.
 */
function PredictButton({
  objectId,
  record,
  onPredictionComplete,
}: {
  objectId: string;
  record: ProteinRecordRow;
  onPredictionComplete: (status: ProteinPredictionStatus) => void;
}) {
  const [predictionState, setPredictionState] = useState<PredictionState | "loading">("loading");
  const [progress, setProgress] = useState<{ pct: number; message: string } | null>(null);
  const [isStarting, setIsStarting] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const mountedRef = useRef(true);

  // Check prediction status on mount
  useEffect(() => {
    mountedRef.current = true;
    api
      .proteinRecordPrediction(objectId, record.ordinal)
      .then((status) => {
        if (!mountedRef.current) return;
        setPredictionState(status.state);
        setProgress(status.progress);
        if (status.state === "completed" && status.prediction) {
          onPredictionComplete(status);
        }
      })
      .catch(() => {
        if (mountedRef.current) {
          setPredictionState("failed");
        }
      });

    return () => {
      mountedRef.current = false;
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [objectId, record.ordinal, onPredictionComplete]);

  const startPolling = () => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const status = await api.proteinRecordPrediction(objectId, record.ordinal);
        if (!mountedRef.current) return;
        setPredictionState(status.state);
        setProgress(status.progress);
        if (status.state === "completed") {
          if (status.prediction) onPredictionComplete(status);
          if (pollRef.current) clearInterval(pollRef.current);
          pollRef.current = null;
        }
        if (status.state === "failed") {
          if (pollRef.current) clearInterval(pollRef.current);
          pollRef.current = null;
        }
      } catch {
        if (!mountedRef.current) return;
        // Keep polling on transient errors
      }
    }, POLL_INTERVAL_MS);
  };

  const handleClick = async () => {
    setIsStarting(true);
    try {
      await api.startProteinPrediction(objectId, record.ordinal);
      setPredictionState("running");
      setProgress({ pct: 0, message: "Starting prediction…" });
      startPolling();
    } catch {
      setPredictionState("failed");
    } finally {
      setIsStarting(false);
    }
  };

  const isRunning = predictionState === "running";
  const isDisabled = isRunning || isStarting || predictionState === "loading";

  let buttonText = "Predict structure";
  if (predictionState === "loading") buttonText = "Checking…";
  if (isRunning && progress) buttonText = `Predicting… (${Math.round(progress.pct)}%)`;
  if (isRunning && !progress) buttonText = "Predicting…";
  if (predictionState === "failed") buttonText = "Retry prediction";
  if (predictionState === "completed") buttonText = "View prediction";

  return (
    <div>
      <button
        type="button"
        className="btn"
        disabled={isDisabled}
        onClick={handleClick}
        title={
          isRunning
            ? "Prediction in progress"
            : predictionState === "completed"
              ? "View the predicted structure"
              : "Predict the 3D structure of this protein"
        }
      >
        {buttonText}
      </button>
      {isRunning && progress && (
        <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 4 }}>
          {progress.message}
        </div>
      )}
    </div>
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
  predictionStatus,
  onPredictionStatusChange,
}: {
  objectId: string;
  record: ProteinRecordRow;
  predictionStatus: ProteinPredictionStatus | null;
  onPredictionStatusChange: (status: ProteinPredictionStatus | null) => void;
}) {
  const setPredictionStatus = onPredictionStatusChange;

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

      {state === "no_structure" && (
        <div className="chrom-note">
          No experimental structure has been deposited for{" "}
          {data?.protein_name ?? data?.accession ?? record.identifier}. Most
          proteins don't have one.
        </div>
      )}

      {/* Predicted structure takes priority over experimental */}
      {predictionStatus?.prediction && (
        <>
          <div className="chrom-note">
            Predicted structure · {predictionStatus.prediction.model_name} v
            {predictionStatus.prediction.model_version} · Mean pLDDT:{" "}
            {(predictionStatus.prediction.mean_plddt * 100).toFixed(0)}
          </div>
          <Icn3dFrame
            pdbId={undefined}
            pdbUrl={predictionStatus.prediction.pdb_url}
            title={`Predicted structure for ${record.identifier}`}
          />
          <PlddtLegend />
        </>
      )}

      {/* Fall back to experimental if no prediction */}
      {!predictionStatus?.prediction && state === "resolved" && data && (
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
        <PredictButton
          objectId={objectId}
          record={record}
          onPredictionComplete={setPredictionStatus}
        />
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
  const [selectedPredictionStatus, setSelectedPredictionStatus] = useState<ProteinPredictionStatus | null>(null);

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
                    onClick={() => {
                      setSelected(row);
                      setSelectedPredictionStatus(null);
                    }}
                    style={{
                      cursor: "pointer",
                      background:
                        selected?.ordinal === row.ordinal
                          ? "var(--bg-elevated)"
                          : undefined,
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
          <RecordStructure
            objectId={objectId}
            record={selected}
            predictionStatus={selectedPredictionStatus}
            onPredictionStatusChange={setSelectedPredictionStatus}
          />
        ) : (
          <div>
            <div className="chrom-note">
              Select a protein on the left to look up its structure.
            </div>
            <div style={{ marginTop: 8 }}>
              <PredictButton
                objectId={objectId}
                record={
                  {
                    ordinal: 0,
                    identifier: "",
                    description: "",
                    length: 0,
                    has_reference: false,
                  } as ProteinRecordRow
                }
                onPredictionComplete={() => {}}
              />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
