import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { DataObject } from "../api/types";

interface Props {
  obj: DataObject;
  /**
   * Omit the `.section` wrapper and its title, returning only the control.
   *
   * For callers that supply their own heading -- the Manage grid labels each
   * row itself, so the built-in title would render twice.
   */
  bare?: boolean;
}

/** Whether a file can take a paired-end mate.
 *
 * Only sequencing reads can: a role of null (format-derived, i.e. FASTQ) or
 * trimmed_reads. Every explicit non-read role -- reference, alignment,
 * variants, annotation, protein, transcript, counts, de_results,
 * assembly_graph -- is a result or a reference and has no mate to have.
 * The check is role-based rather than format-based on purpose: the point of
 * manual pairing is files whose conventional signals are missing, and
 * trimmed reads pair like any others.
 */
export function isReads(o: DataObject): boolean {
  return (o.role === null || o.role === "trimmed_reads") && o.sidecar_of === null;
}

/**
 * Whether this component renders anything at all.
 *
 * Exported so a caller laying out a static label/control grid can drop the
 * whole row rather than leave a "Paired end" label with nothing beside it.
 * The component applies the same test internally, so the two cannot diverge.
 */
export function canPair(o: DataObject): boolean {
  return isReads(o) && o.status === "ready";
}

/**
 * Marks two reads files as paired-end mates, or undoes it.
 *
 * Exists because filename inference is otherwise the only writer of the
 * pairing: files without an _R1/_R2 token in their names can never be paired,
 * however obvious the pairing is to the person looking at them.
 *
 * Candidates come from the project listing already in cache for the explorer,
 * filtered to the same predicate the server enforces. The server validates
 * again -- this filter is for usability, not for correctness.
 */
export function PairEditor({ obj, bare = false }: Props) {
  const qc = useQueryClient();
  const [mateId, setMateId] = useState("");
  const [readNumber, setReadNumber] = useState(1);

  const { data: siblings = [] } = useQuery({
    queryKey: ["objects", obj.project_id],
    queryFn: () => api.listObjects(obj.project_id),
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["object", obj.id] });
    // Both sides changed, and the mate may be open in another view.
    qc.invalidateQueries({ queryKey: ["objects", obj.project_id] });
    qc.invalidateQueries({ queryKey: ["search"] });
  };

  const pair = useMutation({
    mutationFn: () =>
      api.pairObject(obj.id, { mate_object_id: mateId, read_number: readNumber }),
    onSuccess: () => {
      setMateId("");
      invalidate();
      notify.success("Files paired");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const unpair = useMutation({
    mutationFn: () => api.unpairObject(obj.id),
    onSuccess: () => {
      invalidate();
      notify.success("Pairing removed");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  // A reference or a sidecar has no mate to have; offering the control would
  // only invite a rejection.
  if (!canPair(obj)) return null;

  // Both branches below end in a section, so the wrapper is applied in one
  // place rather than repeated as a ternary around each return.
  const wrap = (body: ReactNode) =>
    bare ? (
      <>{body}</>
    ) : (
      <div className="section">
        <div className="section-title">Paired end</div>
        {body}
      </div>
    );

  const mate = siblings.find((o) => o.id === obj.mate_object_id);

  if (obj.mate_object_id) {
    return wrap(
      <>
        <div style={{ fontSize: 12, marginBottom: 6 }}>
          {obj.read_number ? `R${obj.read_number}` : "Paired"}
          {" · mate: "}
          <span style={{ color: "var(--text-faint)" }}>
            {mate ? mate.name : "(file no longer in this project)"}
          </span>
        </div>
        <button
          type="button"
          className="btn"
          onClick={() => unpair.mutate()}
          disabled={unpair.isPending}
        >
          {unpair.isPending ? "Removing…" : "Remove pairing"}
        </button>
        <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}>
          Clears the pairing on both files. Neither file is deleted or changed.
        </div>
      </>,
    );
  }

  const candidates = siblings.filter(
    (o) => o.id !== obj.id && o.mate_object_id === null && isReads(o),
  );

  return wrap(
    <>
      {candidates.length === 0 ? (
        <div style={{ color: "var(--text-faint)", fontSize: 11 }}>
          No unpaired reads files in this project to pair with.
        </div>
      ) : (
        <>
          <div style={{ display: "flex", gap: 8, marginBottom: 6 }}>
            {/* No className: selects are styled globally in styles.css, the
                same way AlignDialog and SchemaMetadataEditor use them. */}
            <select
              value={mateId}
              onChange={(e) => setMateId(e.target.value)}
              style={{ flex: 1, minWidth: 0 }}
            >
              <option value="">Select mate…</option>
              {candidates.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
            <select
              value={readNumber}
              onChange={(e) => setReadNumber(Number(e.target.value))}
              title="Which half this file is"
            >
              <option value={1}>R1</option>
              <option value={2}>R2</option>
            </select>
          </div>
          <button
            type="button"
            className="btn"
            onClick={() => pair.mutate()}
            disabled={!mateId || pair.isPending}
          >
            {pair.isPending ? "Pairing…" : "Pair"}
          </button>
          <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}>
            The mate is set to R{3 - readNumber} automatically. Use this when the
            filenames have no R1/R2 marker for pairing to be detected from.
          </div>
        </>
      )}
    </>,
  );
}
