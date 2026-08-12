import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type {
  BamStatsFacts,
  FeatureDistribution,
  GeneBodyPoint,
  ObjectDetail as ObjectDetailData,
} from "../api/types";

/**
 * RNA-seq QC: where reads sit within a transcript, and where they sit
 * relative to gene structure.
 *
 * On demand rather than automatic, because applicability is inferred rather
 * than known -- there is no stored RNA-vs-DNA flag on a BAM. The button turns
 * a soft signal into a suggestion the user confirms.
 */
export function TranscriptQc({
  obj,
  geneBody,
  featureDistribution,
}: {
  obj: ObjectDetailData;
  geneBody: boolean;
  featureDistribution: boolean;
}) {
  const qc = useQueryClient();
  const f = obj.facts as BamStatsFacts;

  // GTF objects in this BAM's project -- self-fetched rather than threaded
  // down from DetailPanel, which has no project-wide object list in scope
  // today. Filtered client-side on format.kind rather than adding a new
  // backend query param for what is, at project scale, a small list.
  const { data: projectObjects } = useQuery({
    queryKey: ["objects", obj.project_id],
    queryFn: () => api.listObjects(obj.project_id),
  });
  const gtfs = (projectObjects ?? [])
    .filter((o) => o.format.kind === "gtf")
    .map((o) => ({ id: o.id, name: o.name }));

  // Not synced from `gtfs` via an initializer or effect -- `gtfs` is `[]`
  // until the query resolves, and a `useState` initializer only runs once,
  // so it would lock `gtfId` to "" forever whenever a project has exactly
  // one GTF (the common case), permanently disabling the button below.
  // Recomputing the effective id every render fixes that without an effect;
  // `gtfId` itself stays reserved for an explicit user pick via the select.
  const [gtfId, setGtfId] = useState("");
  const effectiveGtfId = gtfId || gtfs[0]?.id || "";

  const compute = useMutation({
    mutationFn: () => api.launchTranscriptQc(obj.id, effectiveGtfId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.info("Computing transcript QC");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const hasResults = f.transcript_qc_status === "ok";

  if (!hasResults) {
    return (
      <div className="section">
        <div className="section-title">RNA-seq transcript QC</div>
        {gtfs.length === 0 ? (
          // The state every project starts in -- say what to add, rather
          // than leaving a disabled button with no explanation.
          <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
            These charts need a gene annotation (GTF) in this project. Add one
            from NCBI or import your own, then come back.
          </div>
        ) : (
          <>
            <div style={{ color: "var(--text-faint)", fontSize: 12, marginBottom: 8 }}>
              Where reads fall within transcripts (5'→3' bias) and across
              exons, introns, and intergenic space.
            </div>
            {gtfs.length > 1 && (
              <select
                value={effectiveGtfId}
                onChange={(e) => setGtfId(e.target.value)}
                style={{ marginRight: 8 }}
              >
                {gtfs.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                  </option>
                ))}
              </select>
            )}
            <button
              type="button"
              className="btn"
              onClick={() => compute.mutate()}
              disabled={compute.isPending || !effectiveGtfId}
            >
              {compute.isPending ? "Computing…" : "Compute transcript QC"}
            </button>
          </>
        )}
      </div>
    );
  }

  return (
    // Cards in a shared row carry no .section: its margin-top would offset
    // the second card and break the aligned title baseline.
    <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
      {geneBody && f.gene_body_coverage && f.gene_body_coverage.length > 0 && (
        <div style={{ flex: "1 1 300px" }}>
          <div className="section-title">Gene body coverage</div>
          <GeneBodyChart curve={f.gene_body_coverage} />
          <Provenance
            annotation={f.transcript_qc_annotation}
            reads={f.transcript_qc_sampled_reads}
          />
        </div>
      )}
      {featureDistribution && f.feature_distribution && (
        <div style={{ flex: "1 1 300px" }}>
          <div className="section-title">Read distribution</div>
          <FeatureBar counts={f.feature_distribution} />
          <Provenance
            annotation={f.transcript_qc_annotation}
            reads={f.transcript_qc_sampled_reads}
          />
        </div>
      )}
    </div>
  );
}

function Provenance({ annotation, reads }: { annotation?: string; reads?: number }) {
  return (
    <div style={{ fontSize: 10, color: "var(--text-faint)", marginTop: 4 }}>
      {annotation ? `${annotation} · ` : ""}
      {reads != null ? `${reads.toLocaleString()} reads sampled` : ""}
    </div>
  );
}

/**
 * Coverage from the 5' end to the 3' end, averaged over genes.
 *
 * A curve that climbs steeply toward the 3' end means the RNA was degraded
 * before sequencing: poly-A selection captures only the surviving 3' tail.
 */
function GeneBodyChart({ curve }: { curve: GeneBodyPoint[] }) {
  const w = 340;
  const h = 150;
  const pad = { top: 10, right: 10, bottom: 22, left: 30 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const x = (p: number) => pad.left + (p / 99) * plotW;
  const y = (v: number) => pad.top + plotH - v * plotH;
  const line = curve
    .map((p, i) => `${i ? "L" : "M"} ${x(p.percentile)} ${y(p.coverage)}`)
    .join(" ");

  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`} style={{ maxWidth: w, display: "block" }}>
      {[0, 0.5, 1].map((v) => (
        <g key={v}>
          <line
            x1={pad.left}
            x2={w - pad.right}
            y1={y(v)}
            y2={y(v)}
            stroke="var(--border)"
            strokeWidth="1"
          />
          <text x={pad.left - 4} y={y(v) + 3} textAnchor="end" fontSize="9" fill="var(--text-faint)">
            {v}
          </text>
        </g>
      ))}
      <path d={line} fill="none" stroke="var(--accent)" strokeWidth="1.8" />
      <text x={pad.left} y={h - 6} fontSize="9" fill="var(--text-faint)">
        5′
      </text>
      <text x={w - pad.right} y={h - 6} fontSize="9" fill="var(--text-faint)" textAnchor="end">
        3′
      </text>
    </svg>
  );
}

/**
 * Exonic / intronic / intergenic as one stacked bar.
 *
 * A stacked bar rather than a pie: three categories at very uneven
 * proportions are easier to read and to compare between samples this way,
 * and it matches the app's existing visual language.
 */
function FeatureBar({ counts }: { counts: FeatureDistribution }) {
  const total = counts.exonic + counts.intronic + counts.intergenic;
  if (total === 0) return null;

  const segments = [
    { label: "Exonic", value: counts.exonic, opacity: 1 },
    { label: "Intronic", value: counts.intronic, opacity: 0.66 },
    { label: "Intergenic", value: counts.intergenic, opacity: 0.33 },
  ];

  const w = 340;
  const barH = 26;
  let offset = 0;

  return (
    <div>
      <svg width="100%" viewBox={`0 0 ${w} ${barH}`} style={{ maxWidth: w, display: "block" }}>
        {segments.map((s) => {
          const width = (s.value / total) * w;
          const x = offset;
          offset += width;
          return (
            <rect key={s.label} x={x} y={0} width={width} height={barH} fill="var(--accent)" opacity={s.opacity}>
              <title>
                {s.label}: {s.value.toLocaleString()} (
                {((100 * s.value) / total).toFixed(1)}%)
              </title>
            </rect>
          );
        })}
      </svg>
      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginTop: 6, fontSize: 11 }}>
        {segments.map((s) => (
          <div key={s.label} style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <span
              style={{
                width: 9,
                height: 9,
                background: "var(--accent)",
                opacity: s.opacity,
                display: "inline-block",
              }}
            />
            <span style={{ color: "var(--text-faint)" }}>{s.label}</span>
            <span style={{ fontWeight: 600 }}>
              {((100 * s.value) / total).toFixed(1)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
