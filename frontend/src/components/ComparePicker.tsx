import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import {
  COMPARABLE_CHARTS,
  hasChartFacts,
} from "../lib/comparableCharts";
import { ModalBackdrop } from "./ModalBackdrop";

/**
 * The "Compare with…" picker (C1, R1).
 *
 * Lists the current project's other objects, filtered to those sharing at
 * least one comparable chart with the object being viewed -- so an empty
 * comparison cannot be *constructed* by selection; it can only be reached by
 * hand-editing the URL, which the comparison view explains (R4).
 *
 * The choice is written as `?cmp=object:<id>` alongside the existing
 * `?sel=object:<id>`, as a normal navigation step, so the comparison is
 * linkable, bookmarkable, and back-button-correct (R2).
 */

interface Props {
  objectId: string;
  objectName: string;
  projectId: string;
  facts: Record<string, unknown>;
  onClose: () => void;
}

export function ComparePicker({
  objectId,
  objectName,
  projectId,
  facts,
  onClose,
}: Props) {
  const [params, setParams] = useSearchParams();
  const { data: objects } = useQuery({
    queryKey: ["objects", projectId],
    queryFn: () => api.listObjects(projectId),
  });

  // Objects that share at least one comparable chart with the current one,
  // excluding the object itself (comparing an object with itself renders a
  // single curve that says nothing).
  const candidates = (objects ?? []).filter(
    (o) =>
      o.id !== objectId &&
      COMPARABLE_CHARTS.some(
        (c) => hasChartFacts(facts, c.chartId) && hasChartFacts(o.facts, c.chartId),
      ),
  );

  const choose = (candidateId: string) => {
    // Preserve any existing params (e.g. the active tab) and layer the
    // comparison on top. setParams pushes by default, so the back button
    // undoes the comparison (R2).
    const next = new URLSearchParams(params);
    next.set("sel", `object:${objectId}`);
    next.set("cmp", `object:${candidateId}`);
    setParams(next);
    onClose();
  };

  return (
    <ModalBackdrop
      onClick={onClose}
      onKeyDown={(e) => e.key === "Escape" && onClose()}
    >
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>Compare with…</h2>
        <p className="modal-hint" style={{ marginTop: 4 }}>
          Choosing a second file shows its charts overlaid on this one's. Only
          files that share a comparable chart are offered.
        </p>
        {candidates.length === 0 ? (
          <p className="empty" style={{ padding: "12px 0" }}>
            No other file in this project shares a comparable chart with{" "}
            {objectName}.
          </p>
        ) : (
          <ul className="compare-picker-list">
            {candidates.map((o) => (
              <li key={o.id}>
                <button type="button" className="btn" onClick={() => choose(o.id)}>
                  {o.name}
                </button>
              </li>
            ))}
          </ul>
        )}
        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </ModalBackdrop>
  );
}
