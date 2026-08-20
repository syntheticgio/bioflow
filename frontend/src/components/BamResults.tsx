import { useCallback, useState } from "react";
import { api } from "../api/client";
import { InfoMarker } from "./InfoMarker";
import { NodeSelector } from "./NodeSelector";
import { Stat } from "./Stat";
import type {
  BamStatsFacts,
  InsertSizeHistogramBucket,
  MapqHistogramBucket,
  ObjectDetail as ObjectDetailData,
} from "../api/types";
import { isStarMapqScale, mapqBucketLabel, mapqScaleNote } from "../lib/mapq";
import { AlignmentReport } from "./AlignmentReport";
import { BirdsEyeCoverageChart, CumulativeCoverageChart } from "./CoverageChart";
import { ContigTable } from "./ContigTable";
import { ContigDepthChart } from "./ContigDepthChart";
import { ContigDepthStrip } from "./ContigDepthStrip";
import { CoverageDepth } from "./CoverageDepth";
import { DepthHistogramChart } from "./DepthHistogramChart";
import { FeatureCoverage } from "./FeatureCoverage";
import { OnDemandCompute } from "./OnDemandCompute";
import { TranscriptQc } from "./TranscriptQc";

/**
 * What the alignment produced: mapped/unmapped totals, coverage across the
 * reference at a glance, the complete per-contig table, and the shape of
 * insert size and mapping quality.
 *
 * Works for every BAM, imported or pipeline-produced -- unlike the flagstat
 * numbers alone, which only exist for a BAM this app aligned. See
 * AlignmentReport's fallback to bam_stats_summary.
 */
export function BamResults({ obj }: { obj: ObjectDetailData }) {
  const f = obj.facts as BamStatsFacts;
  const [targetNode, setTargetNode] = useState("");

  const starScale = isStarMapqScale(obj.facts);
  const hasResults = f.bam_stats_status === "ok";
  const sortedCoordinate = obj.facts.sort_order === "coordinate";
  const hasIndex = obj.facts.has_index === true;
  const rnaApplicability = transcriptQcApplicability(obj);
  const rnaApplies =
    rnaApplicability.geneBody || rnaApplicability.featureDistribution;

  return (
    <>
      <AlignmentReport facts={obj.facts} />

      {rnaApplies && (
        // Independent of bam_stats: transcript QC needs a GTF, not a
        // coverage computation, so it must not wait behind "Compute
        // results" -- a user with a bare, unindexed RNA-seq BAM should
        // still be able to reach this.
        <TranscriptQc
          obj={obj}
          geneBody={rnaApplicability.geneBody}
          featureDistribution={rnaApplicability.featureDistribution}
        />
      )}

      <OnDemandCompute
        objectId={obj.id}
        jobType="run_bam_stats"
        launch={useCallback(
          () => api.launchBamStats(obj.id, targetNode || undefined),
          [obj.id, targetNode],
        )}
        hasResults={hasResults}
        title="Coverage &amp; per-contig detail"
        preflight={<NodeSelector value={targetNode} onChange={setTargetNode} />}
        body={
          !sortedCoordinate ? (
            <div className="warn-box">
              This BAM is not coordinate-sorted, which coverage statistics
              require.
            </div>
          ) : !hasIndex ? (
            <div className="warn-box">
              This BAM has no index (.bai). Compute results will index it
              first, then compute.
            </div>
          ) : (
            <div style={{ color: "var(--text-faint)", fontSize: 12, marginBottom: 8 }}>
              Coverage across the reference, a per-contig breakdown, and
              insert-size/MAPQ distributions — computed on demand from the
              BAM and its index.
            </div>
          )
        }
        buttonClass="btn"
        disabled={!sortedCoordinate}
      >
        {({ recomputeButton }) => (
          <>
            <div className="section">
              {f.bam_stats_coverage_bins && f.bam_stats_coverage_boundaries && (
                <BirdsEyeCoverageChart
                  bins={f.bam_stats_coverage_bins}
                  boundaries={f.bam_stats_coverage_boundaries}
                />
              )}
              <SummaryRow summary={f.bam_stats_summary} />
            </div>

            {/* Cards in a shared row carry no .section: its margin-top would
                offset every card after the first and break the aligned title
                baseline. The row's gap owns the spacing. */}
            <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
              {f.bam_stats_cumulative && f.bam_stats_cumulative.length > 0 && (
                <div style={{ flex: "1 1 300px" }}>
                  <CumulativeCoverageChart curve={f.bam_stats_cumulative} />
                </div>
              )}
              {f.bam_stats_contigs_top && f.bam_stats_contigs_top.length > 0 && (
                <div style={{ flex: "1 1 300px" }}>
                  <ContigDepthStrip
                    contigs={f.bam_stats_contigs_top}
                    totalContigs={f.bam_stats_summary?.total_contigs}
                  />
                </div>
              )}
              {f.bam_stats_contigs_top && f.bam_stats_contigs_top.length > 0 && (
                <div style={{ flex: "1 1 300px" }}>
                  <ContigDepthChart
                    contigs={f.bam_stats_contigs_top}
                    meanDepth={f.bam_stats_summary?.mean_depth}
                    totalContigs={f.bam_stats_summary?.total_contigs}
                  />
                </div>
              )}
              {f.bam_stats_depth_histogram &&
                f.bam_stats_depth_histogram.length > 0 &&
                f.bam_stats_depth_bucket_width != null && (
                  <div style={{ flex: "1 1 300px" }}>
                    <DepthHistogramChart
                      buckets={f.bam_stats_depth_histogram}
                      bucketWidth={f.bam_stats_depth_bucket_width}
                      meanDepth={f.bam_stats_summary?.mean_depth}
                    />
                  </div>
                )}
            </div>

            {f.bam_stats_report && (
              <ContigTable
                objectId={obj.id}
                reportPath={f.bam_stats_report}
                starMapqScale={starScale}
              />
            )}

            {/* Same rule as the coverage row above: no .section on the cards,
                so Insert size and Mapping quality sit on one baseline. */}
            <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
              {f.insert_size_histogram && f.insert_size_histogram.length > 0 && (
                <div style={{ flex: "1 1 300px" }}>
                  <div className="section-title">
                    Insert size
                    <InfoMarker metric="ui.chart_insert_size" />
                  </div>
                  <Histogram
                    data={f.insert_size_histogram}
                    xKey="insert_size"
                    yKey="count"
                    xLabel={(v) => `${v}`}
                  />
                </div>
              )}
              {f.mapq_histogram && f.mapq_histogram.length > 0 && (
                <div style={{ flex: "1 1 300px" }}>
                  <div className="section-title">
                    Mapping quality{starScale ? " (STAR scale)" : ""}
                    <InfoMarker metric="ui.chart_mapq" />
                  </div>
                  {/* Without this a reader has no way to see that these bars
                      are locus counts and the next BAM's are phred scores. */}
                  {starScale && (
                    <div
                      style={{
                        color: "var(--text-faint)",
                        fontSize: 11,
                        marginBottom: 6,
                      }}
                    >
                      {mapqScaleNote(true)}
                    </div>
                  )}
                  <Histogram
                    data={f.mapq_histogram}
                    xKey="mapq"
                    yKey="count"
                    xLabel={(v) => mapqBucketLabel(v, starScale)}
                  />
                </div>
              )}
            </div>

            <div className="section">
              <div className="section-title">Provenance</div>
              <dl className="kv">
                {obj.facts.aligned_by != null && (
                  <>
                    <dt>Aligner</dt>
                    <dd>
                      {String(obj.facts.aligned_by)}
                      {obj.facts.aligner_version ? ` ${obj.facts.aligner_version}` : ""}
                    </dd>
                  </>
                )}
                {Array.isArray(obj.facts.program_chain) && obj.facts.program_chain.length > 0 && (
                  <>
                    <dt>Program chain</dt>
                    {/* One PG line per invocation, so repeated tools repeat;
                        the distinct tools are what's worth reading. */}
                    <dd>{[...new Set(obj.facts.program_chain as string[])].join(" → ")}</dd>
                  </>
                )}
                {Array.isArray(obj.facts.sample_names) && obj.facts.sample_names.length > 0 && (
                  <>
                    <dt>Samples</dt>
                    <dd>{(obj.facts.sample_names as string[]).join(", ")}</dd>
                  </>
                )}
                {Array.isArray(obj.facts.platforms) && obj.facts.platforms.length > 0 && (
                  <>
                    <dt>Platforms</dt>
                    <dd>{(obj.facts.platforms as string[]).join(", ")}</dd>
                  </>
                )}
                {obj.facts.sort_order != null && (
                  <>
                    <dt>Sort order</dt>
                    <dd>{String(obj.facts.sort_order)}</dd>
                  </>
                )}
                <dt>Index</dt>
                <dd>{hasIndex ? "present" : "missing"}</dd>
              </dl>
              {recomputeButton}
            </div>
          </>
        )}
      </OnDemandCompute>

      {/* Independent job from bam_stats -- launched from its own
          Actions-tab card via the generic suggestion launcher, not from
          "Compute results" above -- so it lives outside OnDemandCompute
          and is gated on its own fact rather than `hasResults`. */}
      {f.feature_coverage_report && <FeatureCoverage objectId={obj.id} />}

      {/* Same independent-job reasoning as the feature coverage panel above,
          and gated on its own fact for the same reason. Placed after it so
          the two coverage panels read together: per annotated feature, then
          per window across the whole reference. */}
      {f.coverage_report && <CoverageDepth objectId={obj.id} />}
    </>
  );
}

function SummaryRow({ summary }: { summary?: BamStatsFacts["bam_stats_summary"] }) {
  if (!summary) return null;
  return (
    <div style={{ display: "flex", gap: 20, flexWrap: "wrap", fontSize: 12, marginTop: 8 }}>
      <Stat
        label="Contigs"
        metric="ui.bam_total_contigs"
        value={summary.total_contigs.toLocaleString()}
      />
      <Stat
        label="Mean depth"
        metric="ui.bam_mean_depth"
        value={`${summary.mean_depth.toFixed(1)}×`}
      />
      {summary.pct_covered_1x != null && (
        <Stat
          label="≥1×"
          metric="ui.bam_pct_covered"
          value={`${summary.pct_covered_1x.toFixed(1)}%`}
        />
      )}
      {summary.pct_covered_10x != null && (
        <Stat
          label="≥10×"
          metric="ui.bam_pct_covered"
          value={`${summary.pct_covered_10x.toFixed(1)}%`}
        />
      )}
      {summary.pct_covered_30x != null && (
        <Stat
          label="≥30×"
          metric="ui.bam_pct_covered"
          value={`${summary.pct_covered_30x.toFixed(1)}%`}
        />
      )}
    </div>
  );
}

/** Mirrors backend services/transcript_qc_gating.py -- keep the two in step. */
function transcriptQcApplicability(obj: ObjectDetailData) {
  const md = (obj.metadata ?? {}) as Record<string, unknown>;
  const molecule = md.molecule_type;
  if (molecule === "RNA") return { geneBody: true, featureDistribution: true };
  if (molecule === "DNA" || molecule === "Other")
    return { geneBody: false, featureDistribution: false };

  const assay = md.assay;
  if (assay === "RNA-seq") return { geneBody: true, featureDistribution: true };
  if (assay === "ChIP-seq" || assay === "ATAC-seq")
    return { geneBody: false, featureDistribution: true };
  if (assay) return { geneBody: false, featureDistribution: false };

  const aligner = String(obj.facts.aligned_by ?? "").toLowerCase();
  if (aligner === "star" || aligner === "hisat2")
    return { geneBody: true, featureDistribution: true };

  return { geneBody: false, featureDistribution: false };
}

/** A small inline bar histogram. Single-use and simple enough not to share
 * SequenceCharts.tsx's more general axis machinery. */
function Histogram<T extends MapqHistogramBucket | InsertSizeHistogramBucket>({
  data,
  xKey,
  yKey,
  xLabel,
}: {
  data: T[];
  xKey: keyof T;
  yKey: keyof T;
  xLabel: (v: number) => string;
}) {
  const w = 320;
  const h = 120;
  const pad = { top: 6, right: 6, bottom: 18, left: 6 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const maxCount = Math.max(...data.map((d) => Number(d[yKey])), 1);
  const barW = plotW / data.length;

  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`} style={{ maxWidth: w, display: "block" }}>
      {data.map((d, i) => {
        const count = Number(d[yKey]);
        const barH = (count / maxCount) * plotH;
        return (
          <rect
            key={i}
            x={pad.left + i * barW}
            y={pad.top + plotH - barH}
            width={Math.max(barW - 1, 1)}
            height={barH}
            fill="var(--accent)"
            opacity={0.8}
          >
            <title>
              {xLabel(Number(d[xKey]))}: {count.toLocaleString()}
            </title>
          </rect>
        );
      })}
      <text x={pad.left} y={h - 4} fontSize="9" fill="var(--text-faint)">
        {xLabel(Number(data[0][xKey]))}
      </text>
      <text x={w - pad.right} y={h - 4} fontSize="9" fill="var(--text-faint)" textAnchor="end">
        {xLabel(Number(data[data.length - 1][xKey]))}
      </text>
    </svg>
  );
}
