import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { CountsParams, DataObject, Strandedness } from "../api/types";

const STRAND_LABELS: Record<Strandedness, string> = {
  0: "Unstranded",
  1: "Forward (FR / F)",
  2: "Reverse (RF / R — dUTP protocols)",
};

/**
 * Launch a per-gene count over an aligned BAM.
 *
 * Two of the three settings here are derived rather than asked, and the dialog
 * says which. That is the point of the screen: both strandedness and paired
 * counting fail *silently* when wrong -- a strandedness mismatch returns
 * counts near zero, and counting reads instead of fragments doubles every
 * number -- so the useful thing is not another form field but a visible answer
 * to "where did this value come from, and should I trust it".
 */
export function QuantifyDialog({
  object,
  onClose,
}: {
  object: DataObject;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const { data: defaults, isLoading } = useQuery({
    queryKey: ["pipelines", "quantify-defaults", object.id],
    queryFn: () => api.quantifyDefaults(object.id),
  });

  const [annotationId, setAnnotationId] = useState<string | null>(null);
  const [overrides, setOverrides] = useState<Partial<CountsParams>>({});
  const [advanced, setAdvanced] = useState(false);

  const params = {
    ...(defaults?.params as CountsParams | undefined),
    ...overrides,
  } as CountsParams;

  // The server returns `{}` only when the project has no annotation at all,
  // in which case there is nothing to count against and nothing was derived.
  const hasDerivedParams =
    defaults != null && Object.keys(defaults.params ?? {}).length > 0;

  const annotations = defaults?.annotations ?? [];
  const chosenAnnotationId =
    annotationId ?? defaults?.annotation_id ?? annotations[0]?.id ?? null;
  const chosenAnnotation = annotations.find((a) => a.id === chosenAnnotationId);

  const launch = useMutation({
    mutationFn: () =>
      api.launchQuantify({
        bam_id: object.id,
        annotation_id: chosenAnnotationId,
        params: overrides,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.success("Counting started");
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const unavailable = defaults != null && !defaults.available;
  const ready =
    defaults != null && defaults.available && chosenAnnotationId != null;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal trim-modal" onClick={(e) => e.stopPropagation()}>
        <h2>
          Count reads per gene
          <span className="dialog-tool-subtitle"> — featureCounts</span>
        </h2>

        <div className="modal-body">
          {unavailable && (
            <div className="error-box" style={{ marginBottom: 12 }}>
              featureCounts is not installed on this machine.
            </div>
          )}

          <div className="trim-inputs">
            <div className="trim-file">{object.name}</div>
            {/* Rendered only when the server actually derived something.
                These two lines read as statements of fact about the BAM, so
                showing the component's own fallbacks here would assert
                "single-end" about an alignment nobody had looked at -- which
                is exactly what happened before the defaults endpoint learned
                to answer even when the annotation is ambiguous. */}
            {isLoading ? (
              <div className="trim-mate-note">Reading alignment details…</div>
            ) : hasDerivedParams ? (
              <>
                <div className="trim-mate-note">
                  Strandedness:{" "}
                  <strong>{STRAND_LABELS[params.strandedness ?? 0]}</strong>
                  {defaults?.strandedness_source === "alignment" ? (
                    <> — read from this BAM's aligner settings.</>
                  ) : (
                    <>
                      {" "}
                      — <em>not recorded on this BAM</em>, defaulting to
                      unstranded.
                    </>
                  )}
                </div>
                <div className="trim-mate-note">
                  Counting <strong>{params.paired ? "fragments" : "reads"}</strong>
                  {params.paired
                    ? " — this alignment is paired-end, so a mate pair counts once."
                    : " — this alignment looks single-end."}
                </div>
              </>
            ) : null}
          </div>

          {defaults != null && annotations.length === 0 && (
            <div className="warn-box" style={{ marginBottom: 12, fontSize: 12 }}>
              This project has no gene annotation. Download one alongside the
              assembly from NCBI, or upload a GTF.
            </div>
          )}

          {annotations.length > 0 && (
            <div className="trim-fields">
              <label className="trim-wide">
                <span>Annotation</span>
                <select
                  value={chosenAnnotationId ?? ""}
                  onChange={(e) => setAnnotationId(e.target.value)}
                >
                  {annotations.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name}
                    </option>
                  ))}
                </select>
                <small>
                  {chosenAnnotation?.kind === "gff" ? (
                    <>
                      GFF3 files carry no <code>gene_id</code>, so genes are
                      grouped by <code>locus_tag</code> instead. If a GTF of the
                      same assembly is available, prefer it.
                    </>
                  ) : (
                    <>
                      Must describe the same assembly this BAM was aligned
                      against. A mismatched annotation does not fail — it
                      returns a counts file with almost nothing assigned.
                    </>
                  )}
                </small>
              </label>
            </div>
          )}

          <button
            type="button"
            className="trim-advanced-toggle"
            onClick={() => setAdvanced((a) => !a)}
            aria-expanded={advanced}
          >
            <span className="trim-chevron">{advanced ? "▾" : "▸"}</span>
            Counting and performance
          </button>

          {advanced && (
            <div className="trim-fields">
              <label>
                <span>Strandedness</span>
                <select
                  value={params?.strandedness ?? 0}
                  onChange={(e) =>
                    setOverrides((o) => ({
                      ...o,
                      strandedness: Number(e.target.value) as Strandedness,
                    }))
                  }
                >
                  {([0, 1, 2] as const).map((s) => (
                    <option key={s} value={s}>
                      {STRAND_LABELS[s]}
                    </option>
                  ))}
                </select>
                <small>
                  Must match the library prep. Getting this wrong does not
                  error: it attributes reads to the opposite strand and returns
                  counts near zero throughout.
                </small>
              </label>

              <label>
                <span>Paired-end</span>
                <select
                  value={params?.paired ? "yes" : "no"}
                  onChange={(e) =>
                    setOverrides((o) => ({
                      ...o,
                      paired: e.target.value === "yes",
                    }))
                  }
                >
                  <option value="no">Count reads (single-end)</option>
                  <option value="yes">Count fragments (paired-end)</option>
                </select>
                <small>
                  Counting reads on paired data doubles every number. Derived
                  from the alignment; change it only if that was wrong.
                </small>
              </label>

              <label>
                <span>Multi-mapping reads</span>
                <select
                  value={params?.count_multi_mapping ? "yes" : "no"}
                  onChange={(e) =>
                    setOverrides((o) => ({
                      ...o,
                      count_multi_mapping: e.target.value === "yes",
                    }))
                  }
                >
                  <option value="no">Ignore (recommended)</option>
                  <option value="yes">Count against every feature</option>
                </select>
                <small>
                  Counting them inflates gene families and multi-copy loci, and
                  breaks the assumption differential expression makes about
                  what a count is.
                </small>
              </label>

              <label>
                <span>Threads</span>
                <input
                  type="number"
                  min={1}
                  max={16}
                  value={params?.threads ?? 4}
                  onChange={(e) =>
                    setOverrides((o) => ({
                      ...o,
                      threads: Number(e.target.value),
                    }))
                  }
                />
              </label>
            </div>
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
            {launch.isPending ? "Starting…" : "Count reads"}
          </button>
        </div>
      </div>
    </div>
  );
}
