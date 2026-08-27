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
 * What clicking the Predict button should do, given the state it is showing.
 *
 * Extracted because the bug it exists to prevent was precisely a divergence
 * between the *label* and the *action*: the label switched to "View prediction"
 * on completion while the handler went on calling startProteinPrediction
 * unconditionally, so the button that said "view" re-queued the expensive job
 * (#884). Deriving both from one function is what keeps them in step, and makes
 * the rule testable without a DOM.
 */
export function predictButtonAction(
  state: PredictionState | "loading",
): "start" | "show" | "none" {
  if (state === "completed") return "show";
  if (state === "running" || state === "loading") return "none";
  return "start";
}

/** The label for a given state. Paired with `predictButtonAction` above. */
export function predictButtonLabel(
  state: PredictionState | "loading",
  progress: { pct: number } | null,
): string {
  if (state === "loading") return "Checking…";
  if (state === "running") {
    return progress ? `Predicting… (${Math.round(progress.pct)}%)` : "Predicting…";
  }
  if (state === "failed") return "Retry prediction";
  if (state === "completed") return "View prediction";
  return "Predict structure";
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
  // The completed status, kept so "View prediction" has something to hand back
  // to the panel. Without it the button had no result to show and the click
  // fell through to starting another (expensive) prediction.
  const completedRef = useRef<ProteinPredictionStatus | null>(null);

  // Held in a ref so it is not a dependency of the status-check effect below.
  // Callers pass an inline arrow, which is a new function on every render, so
  // depending on it directly re-ran the check on every parent render.
  const onCompleteRef = useRef(onPredictionComplete);
  onCompleteRef.current = onPredictionComplete;

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
          completedRef.current = status;
          onCompleteRef.current(status);
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
  }, [objectId, record.ordinal]);

  const startPolling = () => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const status = await api.proteinRecordPrediction(objectId, record.ordinal);
        if (!mountedRef.current) return;
        setPredictionState(status.state);
        setProgress(status.progress);
        if (status.state === "completed") {
          if (status.prediction) {
            completedRef.current = status;
            onCompleteRef.current(status);
          }
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
    const action = predictButtonAction(predictionState);
    if (action === "none") return;

    // "View prediction" must show the result that already exists, not queue
    // another one. The label changed on completion but the handler did not,
    // so clicking it re-ran the expensive job (#884).
    if (action === "show") {
      if (completedRef.current) {
        onCompleteRef.current(completedRef.current);
        return;
      }
      // Completed but nothing cached -- the status arrived without a
      // prediction body. Re-fetch rather than silently doing nothing, and
      // still never start a new job from this branch.
      try {
        const status = await api.proteinRecordPrediction(objectId, record.ordinal);
        if (!mountedRef.current) return;
        if (status.prediction) {
          completedRef.current = status;
          onCompleteRef.current(status);
        }
      } catch {
        if (mountedRef.current) setPredictionState("failed");
      }
      return;
    }

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

  const buttonText = predictButtonLabel(predictionState, progress);

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
          This record's header doesn't name a protein we can look up, and the
          underlying file could not be read to try a sequence search.
        </div>
      )}

      {state === "no_sequence_match" && (
        <div className="chrom-note">
          This record's header names no protein, and its sequence was not found
          in UniProt.
        </div>
      )}

      {/* no_candidate is the permanent sibling of lookup_failed above: UniProt
          was reached and returned nothing for this accession, so a retry would
          only re-read a cached answer -- no button. */}
      {state === "no_candidate" && (
        <div className="chrom-note">
          This accession didn't match any protein in UniProt.
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
          /* No Predict button here: with nothing selected there is no record
             to predict. It used to render one against a fabricated
             {ordinal: 0} record, so clicking it launched a prediction for the
             file's *first* protein and threw the result away (#884). */
          <div className="chrom-note">
            Select a protein on the left to look up its structure.
          </div>
        )}
      </div>
    </div>
  );
}
