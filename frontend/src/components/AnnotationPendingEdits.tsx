import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { AnnotationStatsFacts } from "../api/types";
import { notify } from "../stores/messageStore";

interface Props {
  objectId: string;
  facts: AnnotationStatsFacts;
}

/** Panel above the annotation table: lists pending edits as a reviewable diff
 *  and offers a Materialize button that queues a derived-object job. */
export function AnnotationPendingEdits({ objectId, facts }: Props) {
  const qc = useQueryClient();

  const editsQ = useQuery({
    queryKey: ["annotationstats", "edits", objectId],
    queryFn: () => api.annotationEdits(objectId),
    // Only for GFF/GTF, where editing is supported.
    enabled: facts.gff_version != null,
  });

  const materialize = useMutation({
    mutationFn: () => api.materializeAnnotationEdits(objectId),
    onSuccess: (job) => {
      notify.success(`Materialization queued — job ${job.id.slice(0, 12)}`);
      qc.invalidateQueries({ queryKey: ["annotationstats", "edits", objectId] });
    },
    onError: (err: Error) => {
      notify.error(err.message);
    },
  });

  const remove = useMutation({
    mutationFn: ({ line, field }: { line: number; field: string }) =>
      api.deleteAnnotationEdit(objectId, line, field),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["annotationstats", "edits", objectId] });
    },
  });

  const edits = editsQ.data ?? [];

  if (!facts.gff_version) return null;
  if (editsQ.isLoading) return null;
  if (edits.length === 0) return null;

  return (
    <div style={{
      marginBottom: 12,
      padding: "8px 12px",
      background: "var(--surface-raised)",
      borderRadius: 6,
      border: "1px solid var(--border)",
    }}>
      <div style={{ fontWeight: 600, marginBottom: 8 }}>
        {edits.length} pending edit{edits.length !== 1 ? "s" : ""}
      </div>
      <div style={{ maxHeight: 200, overflowY: "auto", marginBottom: 8 }}>
        <table style={{ width: "100%", fontSize: 13 }}>
          <tbody>
            {edits.map((e, i) => (
              <tr key={`${e.line}-${e.field}-${i}`}>
                <td style={{ whiteSpace: "nowrap", paddingRight: 8 }} className="mono">
                  L{e.line}
                </td>
                <td style={{ whiteSpace: "nowrap", paddingRight: 8 }}>
                  {e.field}
                </td>
                <td>
                  {e.old_value ? (
                    <span style={{ textDecoration: "line-through", opacity: 0.8 }}>
                      {e.old_value}
                    </span>
                  ) : (
                    <span style={{ opacity: 0.5, fontStyle: "italic" }}>—</span>
                  )}
                  {" → "}
                  <span>{e.new_value}</span>
                </td>
                <td style={{ textAlign: "right" }}>
                  <button
                    type="button"
                    className="btn"
                    style={{ padding: "0 6px", fontSize: 11 }}
                    onClick={() => remove.mutate({ line: e.line, field: e.field })}
                    disabled={remove.isPending}
                  >
                    ✕
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button
        type="button"
        className="btn primary"
        onClick={() => materialize.mutate()}
        disabled={materialize.isPending}
        style={{ fontSize: 13 }}
      >
        {materialize.isPending ? "Queuing…" : "Materialize edits"}
      </button>
    </div>
  );
}