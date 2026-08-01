import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { DeSample } from "../api/types";

/**
 * Launch a differential expression test across a project's counts files.
 *
 * The one dialog here that is not a settings screen for a file. Its subject is
 * a *design*: which samples, in which groups, compared which way round. That
 * is why differential expression has no Actions-tab card -- a card exists to
 * pre-answer a question, and none of these three can be pre-answered.
 *
 * The condition column is seeded from each counts file's `condition` metadata,
 * which rides forward from whatever the reads were tagged with. So a project
 * where six FASTQs were bulk-tagged "treated" at upload opens here already
 * filled in, and this becomes a confirmation rather than data entry.
 *
 * Replicate counts are shown live, before submitting, because the server
 * refuses a group with fewer than two and discovering that from a rejected
 * request is a worse version of the same conversation.
 */
export function DifferentialExpressionDialog({
  projectId,
  onClose,
}: {
  projectId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const { data: defaults, isLoading } = useQuery({
    queryKey: ["pipelines", "de-defaults", projectId],
    queryFn: () => api.deDefaults(projectId),
  });

  // Edits layered over the server's seed, rather than state initialised from
  // it in an effect: the seed arrives asynchronously, and an effect would
  // leave a frame where the table is empty and the replicate warnings below
  // describe a design nobody chose.
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [contrast, setContrast] = useState<{
    test: string;
    reference: string;
  } | null>(null);

  const samples = defaults?.samples ?? [];

  const conditionOf = (s: DeSample) => edits[s.object_id] ?? s.condition;

  // Only samples actually going into the run. A blank condition means "leave
  // this one out", so everything below is computed over these rather than over
  // every counts file in the project.
  const assigned = samples.filter((s) => conditionOf(s).trim() !== "");

  // Samples counted against a different annotation than the majority cannot be
  // merged, and the server refuses the whole run rather than dropping them.
  // Surfaced here so the refusal is visible before the click.
  //
  // Scoped to `assigned`, which is the whole point and was wrong first time
  // round: computed over every sample in the project, one leftover counts file
  // from an old annotation disabled the launch button on a design that did not
  // include it and that the server accepted perfectly happily. A file the user
  // has explicitly left out cannot make their run invalid.
  const majorityAnnotation = useMemo(() => {
    const counts = new Map<string, number>();
    for (const s of assigned) {
      if (s.annotation_sha256) {
        counts.set(s.annotation_sha256, (counts.get(s.annotation_sha256) ?? 0) + 1);
      }
    }
    let best: string | null = null;
    let bestN = 0;
    for (const [digest, n] of counts) {
      if (n > bestN) {
        best = digest;
        bestN = n;
      }
    }
    return best;
  }, [assigned]);

  const isMismatchedFor = (s: DeSample) =>
    s.annotation_sha256 != null &&
    majorityAnnotation != null &&
    s.annotation_sha256 !== majorityAnnotation;

  // Blocks the launch: these are in the design and cannot be merged.
  const mismatched = assigned.filter(isMismatchedFor);

  const byCondition = useMemo(() => {
    const groups = new Map<string, DeSample[]>();
    for (const s of assigned) {
      const c = conditionOf(s).trim();
      groups.set(c, [...(groups.get(c) ?? []), s]);
    }
    return groups;
  }, [assigned, edits]);

  const conditions = [...byCondition.keys()].sort();

  const chosen =
    contrast ??
    defaults?.contrast ??
    (conditions.length === 2
      ? { reference: conditions[0], test: conditions[1] }
      : null);

  const minReplicates = defaults?.min_replicates ?? 2;
  const thin = chosen
    ? [chosen.test, chosen.reference].filter(
        (c) => (byCondition.get(c)?.length ?? 0) < minReplicates
      )
    : [];

  const launch = useMutation({
    mutationFn: () =>
      api.launchDifferentialExpression({
        project_id: projectId,
        design: Object.fromEntries(
          assigned.map((s) => [s.object_id, conditionOf(s).trim()])
        ),
        contrast: chosen!,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Differential expression started");
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const ready =
    defaults != null &&
    defaults.available &&
    chosen != null &&
    chosen.test !== chosen.reference &&
    thin.length === 0 &&
    mismatched.length === 0;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
        <h2>
          Differential expression
          <span className="dialog-tool-subtitle"> — PyDESeq2</span>
        </h2>

        <div className="modal-body">
          {defaults != null && !defaults.available && (
            <div className="error-box" style={{ marginBottom: 12 }}>
              PyDESeq2 is not installed on this machine.
            </div>
          )}

          {isLoading && <div className="trim-mate-note">Reading counts files…</div>}

          {defaults != null && samples.length === 0 && (
            <div className="warn-box" style={{ marginBottom: 12, fontSize: 12 }}>
              This project has no counts files yet. Align your RNA-seq reads,
              then run “Count reads per gene” on each alignment.
            </div>
          )}

          {mismatched.length > 0 && (
            <div className="error-box" style={{ marginBottom: 12, fontSize: 12 }}>
              {mismatched.length === 1
                ? `${mismatched[0].sample} was counted against a different annotation (${mismatched[0].annotation_name ?? "unknown"})`
                : `${mismatched.length} samples were counted against a different annotation`}{" "}
              than the rest, so their genes are not comparable. Re-count them
              against one annotation before testing.
            </div>
          )}

          {samples.length > 0 && (
            <>
              <div className="section-note" style={{ marginBottom: 8 }}>
                Assign each sample to a condition. Blank means “leave this
                sample out”. Conditions are seeded from each file’s{" "}
                <code>condition</code> metadata.
              </div>

              <table className="facts-table" style={{ marginBottom: 12 }}>
                <thead>
                  <tr>
                    <th>Sample</th>
                    <th>Condition</th>
                    <th style={{ textAlign: "right" }}>Assigned</th>
                    <th style={{ textAlign: "right" }}>Genes seen</th>
                  </tr>
                </thead>
                <tbody>
                  {samples.map((s) => {
                    // Dimmed whenever it would not merge, assigned or not --
                    // seeing that a leftover file is unusable is useful, and
                    // only the assigned ones block the launch.
                    const isMismatched = isMismatchedFor(s);
                    return (
                      <tr
                        key={s.object_id}
                        // Flagged by dimming the row rather than by an extra
                        // cell: the mismatch is already spelled out in the box
                        // above, and a conditional <td> would shift this row's
                        // columns out of line with every other row.
                        style={isMismatched ? { opacity: 0.5 } : undefined}
                        title={
                          isMismatched
                            ? `Counted against ${s.annotation_name ?? "a different annotation"}, so it cannot be merged with the others`
                            : undefined
                        }
                      >
                        <td title={s.name}>{s.sample}</td>
                        <td>
                          <input
                            type="text"
                            list="de-conditions"
                            value={conditionOf(s)}
                            placeholder="—"
                            aria-label={`Condition for ${s.sample}`}
                            onChange={(e) =>
                              setEdits((prev) => ({
                                ...prev,
                                [s.object_id]: e.target.value,
                              }))
                            }
                          />
                        </td>
                        {/* A low assignment rate is the signature of wrong
                            strandedness or a mismatched annotation, and it is
                            far cheaper to notice here than in the results. */}
                        <td
                          style={{
                            textAlign: "right",
                            color:
                              s.assigned_pct != null && s.assigned_pct < 20
                                ? "var(--danger, #c33)"
                                : undefined,
                          }}
                          title={
                            s.assigned_pct != null && s.assigned_pct < 20
                              ? "Very few reads were assigned to genes — check the strandedness this was counted with."
                              : undefined
                          }
                        >
                          {s.assigned_pct != null ? `${s.assigned_pct}%` : "—"}
                        </td>
                        <td style={{ textAlign: "right" }}>
                          {s.genes_detected ?? "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>

              <datalist id="de-conditions">
                {[...new Set([...conditions, ...(defaults?.conditions ?? [])])].map(
                  (c) => (
                    <option key={c} value={c} />
                  )
                )}
              </datalist>

              <div className="trim-fields">
                <label>
                  <span>Test condition</span>
                  <select
                    value={chosen?.test ?? ""}
                    onChange={(e) =>
                      setContrast({
                        test: e.target.value,
                        reference: chosen?.reference ?? "",
                      })
                    }
                  >
                    <option value="">—</option>
                    {conditions.map((c) => (
                      <option key={c} value={c}>
                        {c} ({byCondition.get(c)?.length ?? 0})
                      </option>
                    ))}
                  </select>
                  <small>
                    Genes up in this group get a positive fold change.
                  </small>
                </label>

                <label>
                  <span>Reference condition</span>
                  <select
                    value={chosen?.reference ?? ""}
                    onChange={(e) =>
                      setContrast({
                        test: chosen?.test ?? "",
                        reference: e.target.value,
                      })
                    }
                  >
                    <option value="">—</option>
                    {conditions.map((c) => (
                      <option key={c} value={c}>
                        {c} ({byCondition.get(c)?.length ?? 0})
                      </option>
                    ))}
                  </select>
                  <small>The baseline everything is compared against.</small>
                </label>
              </div>

              {thin.length > 0 && (
                <div className="warn-box" style={{ marginTop: 10, fontSize: 12 }}>
                  {thin
                    .map(
                      (c) =>
                        `${c} has ${byCondition.get(c)?.length ?? 0} sample${
                          (byCondition.get(c)?.length ?? 0) === 1 ? "" : "s"
                        }`
                    )
                    .join("; ")}
                  . Each group needs at least {minReplicates}: with one sample
                  there is no way to tell a real difference from ordinary
                  variation between replicates.
                </div>
              )}

              {chosen != null &&
                chosen.test !== "" &&
                chosen.test === chosen.reference && (
                  <div className="warn-box" style={{ marginTop: 10, fontSize: 12 }}>
                    The contrast compares {chosen.test} with itself. Pick two
                    different conditions.
                  </div>
                )}
            </>
          )}
        </div>

        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={!ready || launch.isPending}
            onClick={() => launch.mutate()}
          >
            {launch.isPending
              ? "Starting…"
              : chosen
                ? `Test ${chosen.test} vs ${chosen.reference}`
                : "Run test"}
          </button>
        </div>
      </div>
    </div>
  );
}
