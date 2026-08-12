import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { ObjectDetail as ObjectDetailData, AnnotationStatsFacts } from "../api/types";
import {
  AnnotationCoverageChart,
  BiotypeChart,
  FeatureDensityChart,
  FeatureTypeChart,
  LengthHistogram,
} from "./AnnotationCharts";
import { AnnotationFeatureTable } from "./AnnotationFeatureTable";
import { NodeSelector } from "./NodeSelector";

/**
 * What an annotation file contains: how many features of what kinds, where
 * they sit across the reference, how much of it they cover, and the full
 * searchable feature table.
 *
 * Two layers. The interval core -- density, coverage, length distribution --
 * renders for every supported format. The feature-type and biotype blocks
 * render only when the file carried them, which is what lets one view serve
 * both a published GFF3 and a peak-call BED.
 */
export function AnnotationResults({ obj }: { obj: ObjectDetailData }) {
  const qc = useQueryClient();
  const f = obj.facts as AnnotationStatsFacts;
  const [targetNode, setTargetNode] = useState("");

  const compute = useMutation({
    mutationFn: () => api.launchAnnotationStats(obj.id, targetNode || undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.info("Computing results");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  if (f.annotation_stats_status !== "ok") {
    return (
      <div className="section">
        <NodeSelector value={targetNode} onChange={setTargetNode} />
        <div className="section-title">Annotation summary</div>
        <div className="section-note">
          Feature counts by type, coverage across the reference, and the full
          searchable feature table — computed on demand from the annotation.
        </div>
        <button
          type="button"
          className="btn primary"
          onClick={() => compute.mutate()}
          disabled={compute.isPending}
        >
          {compute.isPending ? "Computing…" : "Compute results"}
        </button>
      </div>
    );
  }

  const contigs = f.annotation_per_contig ?? [];

  return (
    <>
      <div className="qc-provenance">
        {[
          f.annotation_source ?? null,
          f.genome_build ? `build ${f.genome_build}` : null,
          f.annotation_feature_count != null
            ? `${f.annotation_feature_count.toLocaleString()} features`
            : null,
          f.annotation_contig_count != null
            ? `${f.annotation_contig_count.toLocaleString()} sequences`
            : null,
          // Only when nonzero: a clean file should not display a zero.
          f.annotation_malformed_lines
            ? `${f.annotation_malformed_lines.toLocaleString()} unreadable lines`
            : null,
        ]
          .filter(Boolean)
          .join(" · ")}{" "}
        <button
          type="button"
          onClick={() => compute.mutate()}
          disabled={compute.isPending}
          style={{
            color: "var(--accent)",
            fontSize: 11,
            textTransform: "none",
            letterSpacing: 0,
          }}
        >
          {compute.isPending ? "recomputing…" : "recompute results"}
        </button>
      </div>

      {f.annotation_type_counts && (
        <div className="section">
          <div className="section-title">Features by type</div>
          <FeatureTypeChart counts={f.annotation_type_counts} />
        </div>
      )}

      {f.annotation_biotype_counts && (
        <div className="section">
          <div className="section-title">Features by biotype</div>
          <BiotypeChart counts={f.annotation_biotype_counts} />
        </div>
      )}

      {contigs.length > 0 && (
        <>
          <div className="section">
            <div className="section-title">Feature density</div>
            <FeatureDensityChart contigs={contigs} />
          </div>
          <div className="section">
            <div className="section-title">Annotated coverage</div>
            <div className="section-note">
              Fraction of each sequence covered by at least one feature.
              Overlapping features are counted once.
            </div>
            <AnnotationCoverageChart contigs={contigs} />
          </div>
        </>
      )}

      {f.annotation_length_histogram && (
        <div className="section">
          <div className="section-title">Feature lengths</div>
          <LengthHistogram bins={f.annotation_length_histogram} />
        </div>
      )}

      <AnnotationFeatureTable objectId={obj.id} facts={f} />
    </>
  );
}
