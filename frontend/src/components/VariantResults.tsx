import { useCallback, useState } from "react";
import { api } from "../api/client";
import type { ObjectDetail as ObjectDetailData, VcfStatsFacts } from "../api/types";
import { AiSummary } from "./AiSummary";
import { FactsColumns } from "./FactsColumns";
import { InfoMarker } from "./InfoMarker";
import { NodeSelector } from "./NodeSelector";
import { OnDemandCompute } from "./OnDemandCompute";
import { Stat } from "./Stat";
import { DistributionChart, IndelLengthChart, VariantDensityChart } from "./VariantCharts";
import { VariantTable } from "./VariantTable";

/**
 * What variant calling produced: call-set size and Ti/Tv at a glance, where
 * variants sit across the reference, the shape of QUAL/depth/substitutions,
 * and the complete filterable variant table.
 */
export function VariantResults({ obj }: { obj: ObjectDetailData }) {
  const f = obj.facts as VcfStatsFacts;
  const [targetNode, setTargetNode] = useState("");

  const hasResults = f.vcf_stats_status === "ok";

  return (
    <OnDemandCompute
      objectId={obj.id}
      jobType="run_vcf_stats"
      launch={useCallback(
        () => api.launchVcfStats(obj.id, targetNode || undefined),
        [obj.id, targetNode],
      )}
      hasResults={hasResults}
      title="Variant summary"
      preflight={<NodeSelector value={targetNode} onChange={setTargetNode} />}
      body={
        <div className="section-note">
          Call counts, Ti/Tv, QUAL and depth distributions, and the complete
          filterable variant table — computed on demand from the VCF.
        </div>
      }
    >
      {({ recomputeButton }) => {
        const summary = f.vcf_stats_summary;
        const samples = Array.isArray(obj.facts.sample_names)
          ? (obj.facts.sample_names as string[])
          : [];

        // bcftools call never stamps PASS, so no caller info plus no filters at
        // all reads as "this caller doesn't use FILTER" rather than "no reads
        // passed" -- see the pass_pct comment on VariantSummary.
        const calledBy =
          typeof obj.facts.variants_called_by === "string"
            ? obj.facts.variants_called_by +
              (typeof obj.facts.variant_caller_version === "string"
                ? ` ${obj.facts.variant_caller_version}`
                : "")
            : null;

        return (
          <>
            <div className="qc-provenance">
              {[
                f.vcf_stats_tool_version ? `bcftools ${f.vcf_stats_tool_version}` : null,
                calledBy ? `called by ${calledBy}` : null,
                summary ? `${summary.samples} sample${summary.samples === 1 ? "" : "s"}` : null,
              ]
                .filter(Boolean)
                .join(" · ")}{" "}
              {recomputeButton}
            </div>

            {/* Unconditional (not nested inside the zero-variants branch below):
                AiSummary already self-suppresses when there's nothing stored and
                no model reachable, and a summary can still be stored here even if
                the call set is currently empty -- e.g. after a re-run dropped the
                count to zero following an earlier non-zero run. Nesting this
                inside the zero-variants branch would make that stored summary
                permanently unreachable. */}
            <AiSummary
              facts={obj.facts}
              objectId={obj.id}
              fingerprint={obj.summary_fingerprint ?? undefined}
              factPrefix="ai_variant_summary"
              statusFn={() => api.variantSummaryStatus()}
              launchFn={(id) => api.launchVariantSummary(id)}
              emptyLabel="No summary yet for this file."
            />

            {summary && summary.variants === 0 ? (
              <div className="section">
                <div className="section-note">
                  No variants were called. For a strict caller against a clean
                  sample this is a normal outcome, not a failure.
                </div>
              </div>
            ) : (
              <>
                {summary && (
                  <div className="section">
                    <SummaryRow summary={summary} />
                  </div>
                )}

                {f.vcf_stats_density_bins && f.vcf_stats_density_bounds && (
                  <div className="section">
                    <div className="section-title">
                      Variant density
                      <InfoMarker metric="ui.chart_variant_density" />
                    </div>
                    <VariantDensityChart
                      bins={f.vcf_stats_density_bins}
                      boundaries={f.vcf_stats_density_bounds}
                    />
                    <div className="section-note">
                      Bar heights use a square-root scale, not a straight count, so
                      that regions with just a few variants still show up next to
                      the densest spots — read this as "where are the variants,"
                      not as an exact ratio between bars. Hover a bar for its
                      actual count.
                    </div>
                  </div>
                )}

                <div className="qc-charts">
                  {f.vcf_stats_qual_histogram && f.vcf_stats_qual_histogram.length > 0 && (
                    <div className="qc-chart">
                      <div className="section-title">
                        QUAL
                        <InfoMarker metric="ui.chart_vcf_qual" />
                      </div>
                      <DistributionChart
                        buckets={f.vcf_stats_qual_histogram}
                        label="QUAL"
                        format={(v) => v.toFixed(0)}
                      />
                    </div>
                  )}
                  {f.vcf_stats_depth_histogram && f.vcf_stats_depth_histogram.length > 0 && (
                    <div className="qc-chart">
                      <div className="section-title">
                        Depth
                        <InfoMarker metric="ui.chart_vcf_depth" />
                      </div>
                      <DistributionChart
                        buckets={f.vcf_stats_depth_histogram}
                        label="depth"
                        format={(v) => `${v.toFixed(0)}×`}
                      />
                    </div>
                  )}
                  {f.vcf_stats_indel_lengths && f.vcf_stats_indel_lengths.length > 0 && (
                    <div className="qc-chart">
                      <div className="section-title">
                        Indel length
                        <InfoMarker metric="ui.chart_indel_lengths" />
                      </div>
                      <IndelLengthChart lengths={f.vcf_stats_indel_lengths} />
                    </div>
                  )}
                </div>

                <FactsColumns>
                  {f.vcf_stats_substitutions && f.vcf_stats_substitutions.length > 0 && (
                    <div className="section">
                      <div className="section-title">
                        Substitution types
                        <InfoMarker metric="ui.vcf_substitutions" />
                      </div>
                      <SubstitutionsTable rows={f.vcf_stats_substitutions} />
                    </div>
                  )}

                  {f.vcf_stats_filters && f.vcf_stats_filters.length > 0 && (
                    <div className="section">
                      <div className="section-title">
                        Filters
                        <InfoMarker metric="ui.vcf_filters" />
                      </div>
                      <FiltersTable rows={f.vcf_stats_filters} />
                    </div>
                  )}
                </FactsColumns>

                {f.vcf_stats_contigs && f.vcf_stats_contigs.length > 0 && (
                  <div className="section">
                    <div className="section-title">
                      Per-contig counts
                      <InfoMarker metric="ui.vcf_per_contig" />
                    </div>
                    <table className="trim-table">
                      <thead>
                        <tr>
                          <th>Contig</th>
                          <th style={{ textAlign: "right" }}>Length</th>
                          <th style={{ textAlign: "right" }}>Variants</th>
                          <th style={{ textAlign: "right" }}>Per kb</th>
                          <th style={{ textAlign: "right" }}>SNPs</th>
                          <th style={{ textAlign: "right" }}>Indels</th>
                        </tr>
                      </thead>
                      <tbody>
                        {f.vcf_stats_contigs.map((row) => (
                          <tr key={row.contig}>
                            <td className="mono">{row.contig}</td>
                            <td style={{ textAlign: "right" }}>{row.length.toLocaleString()}</td>
                            <td style={{ textAlign: "right" }}>{row.variants.toLocaleString()}</td>
                            <td style={{ textAlign: "right" }}>{row.per_kb.toFixed(2)}</td>
                            <td style={{ textAlign: "right" }}>{row.snps.toLocaleString()}</td>
                            <td style={{ textAlign: "right" }}>{row.indels.toLocaleString()}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                <VariantTable
                  objectId={obj.id}
                  reportPath={f.vcf_stats_report}
                  contigs={f.vcf_stats_contigs ?? []}
                  filters={f.vcf_stats_filters ?? []}
                  samples={samples}
                />
              </>
            )}
          </>
        );
      }}
    </OnDemandCompute>
  );
}

function SummaryRow({ summary }: { summary: NonNullable<VcfStatsFacts["vcf_stats_summary"]> }) {
  return (
    <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
      <Stat
        label="Variants"
        metric="ui.vcf_variants"
        value={summary.variants.toLocaleString()}
      />
      <Stat label="SNPs" metric="ui.vcf_snps" value={summary.snps.toLocaleString()} />
      <Stat
        label="Indels"
        metric="ui.vcf_indels"
        value={summary.indels.toLocaleString()}
      />
      <Stat label="Ti/Tv" metric="ui.vcf_ti_tv" value={summary.ti_tv.toFixed(2)} />
      {summary.pass_pct != null && (
        <Stat
          label="PASS"
          metric="ui.vcf_pass_pct"
          value={`${summary.pass_pct.toFixed(1)}%`}
        />
      )}
      {summary.multiallelic > 0 && (
        <Stat
          label="Multiallelic"
          metric="ui.vcf_multiallelic"
          value={summary.multiallelic.toLocaleString()}
        />
      )}
    </div>
  );
}

function SubstitutionsTable({ rows }: { rows: { type: string; count: number }[] }) {
  const max = Math.max(...rows.map((r) => r.count), 1);
  return (
    <table className="trim-table">
      <thead>
        <tr>
          <th>Type</th>
          <th style={{ textAlign: "right" }}>Count</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.type}>
            <td className="mono">{r.type}</td>
            <td style={{ textAlign: "right" }}>{r.count.toLocaleString()}</td>
            <td style={{ width: "40%" }}>
              <div
                style={{
                  height: 8,
                  width: `${(r.count / max) * 100}%`,
                  background: "var(--accent)",
                }}
              />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function FiltersTable({ rows }: { rows: { filter: string; count: number }[] }) {
  const total = rows.reduce((sum, r) => sum + r.count, 0) || 1;
  return (
    <table className="trim-table">
      <thead>
        <tr>
          <th>Filter</th>
          <th style={{ textAlign: "right" }}>Count</th>
          <th style={{ textAlign: "right" }}>%</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.filter}>
            <td>
              {r.filter === "PASS" ? (
                <span className="badge ready">PASS</span>
              ) : r.filter === "." ? (
                "no filter applied"
              ) : (
                r.filter
              )}
            </td>
            <td style={{ textAlign: "right" }}>{r.count.toLocaleString()}</td>
            <td style={{ textAlign: "right" }}>{((r.count / total) * 100).toFixed(1)}%</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
