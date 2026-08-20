import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import {
  comparableCharts,
  type ChartAvailability,
} from "../lib/comparableCharts";
import { NxChart, type CompareNxSeries } from "./NxChart";

/**
 * Two objects' charts overlaid on shared axes.
 *
 * Reached from `DetailPanel` when `?sel=object:A&cmp=object:B` both parse
 * (C1). This view renders exactly the charts both objects can carry and an
 * explicit line for each it cannot -- never an empty axis, which would read
 * as "no data in these objects" rather than "these objects share no
 * comparable chart" (R4).
 *
 * Comparability is computed per chart from the objects' facts (C3); each
 * chart's props are derived from the facts by the same reading the
 * single-object panel uses (`sequence_nx_curve`, `total_bases`,
 * `assembly_genome_size`), so this view and `AssemblyFacts` cannot drift on
 * what a chart needs.
 */

interface Props {
  /** The first (primary) object id, from `sel`. */
  idA: string;
  /** The second (comparison) object id, from `cmp`. */
  idB: string;
}

/** Map an object's facts to an NxChart series, mirroring `AssemblyFacts`. */
function nxSeries(
  name: string,
  facts: Record<string, unknown>,
): CompareNxSeries | null {
  const curve = facts.sequence_nx_curve as [number, number][] | undefined;
  const totalBases = facts.total_bases as number | undefined;
  if (!Array.isArray(curve) || typeof totalBases !== "number") return null;
  return {
    curve,
    totalBases,
    genomeSize: facts.assembly_genome_size as number | undefined,
    label: name,
  };
}

/** Render one chart. Stage 1 has a single renderer; stage 2 adds branches
 *  here keyed on `chart.chartId` (R7) -- the table gates, this draws. */
function renderChart(
  availability: ChartAvailability,
  seriesA: CompareNxSeries | null,
  seriesB: CompareNxSeries | null,
) {
  switch (availability.chart.chartId) {
    case "nx":
      return seriesA && seriesB ? (
        <NxChart {...seriesA} compare={seriesB} />
      ) : null;
    default:
      return null;
  }
}

/** One overlay per available chart, an explicit reason per unavailable one. */
function ChartBlock({
  availability,
  seriesA,
  seriesB,
}: {
  availability: ChartAvailability;
  seriesA: CompareNxSeries | null;
  seriesB: CompareNxSeries | null;
}) {
  const { chart, available, missing } = availability;
  return (
    <div className="compare-chart">
      <div className="section-title">{chart.label}</div>
      {available && renderChart(availability, seriesA, seriesB)}
      {!available && (
        <div className="compare-unavailable">
          {missing.map((m) => (
            <div key={m.name}>
              <strong>{m.name}</strong> lacks{" "}
              {m.facts.map((f) => (
                <code key={f}>{f}</code>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function ComparisonView({ idA, idB }: Props) {
  const [params, setParams] = useSearchParams();

  // Both via the shared ["object", id] key, so if the user has already been
  // in either single-object panel the fetch is served from the query cache.
  const a = useQuery({
    queryKey: ["object", idA],
    queryFn: () => api.getObject(idA),
  });
  const b = useQuery({
    queryKey: ["object", idB],
    queryFn: () => api.getObject(idB),
  });

  // Matches clearSelection in ObjectDetail: dropping the comparison is not a
  // navigation step people expect the back button to undo.
  const clear = () => {
    const next = new URLSearchParams(params);
    next.delete("cmp");
    setParams(next, { replace: true });
  };

  if (a.isLoading || b.isLoading) {
    return (
      <div className="panel">
        <div className="panel-body">
          <div className="empty">
            <span className="spinner" /> Loading…
          </div>
        </div>
      </div>
    );
  }

  const objA = a.data;
  const objB = b.data;
  if (!objA || !objB) {
    return (
      <div className="panel">
        <div className="panel-body detail">
          <div className="error-box">
            One of the two objects no longer exists.
          </div>
        </div>
      </div>
    );
  }

  const seriesA = nxSeries(objA.name, objA.facts);
  const seriesB = nxSeries(objB.name, objB.facts);
  const rows = comparableCharts(objA.facts, objB.facts, objA.name, objB.name);
  const anyComparable = rows.some((r) => r.available);

  return (
    <div className="panel">
      <div className="panel-body detail">
        <div className="compare-header">
          <div className="compare-title">
            Comparing <strong>{objA.name}</strong> with{" "}
            <strong>{objB.name}</strong>
          </div>
          <button type="button" className="btn" onClick={clear}>
            End comparison
          </button>
        </div>

        {anyComparable ? (
          rows.map((r) => (
            <ChartBlock
              key={r.chart.chartId}
              availability={r}
              seriesA={seriesA}
              seriesB={seriesB}
            />
          ))
        ) : (
          <div className="warn-box">
            These two objects share no comparable chart: they carry none of
            the same facts any comparison view can draw. Pick another object
            of the same kind (for example, a second assembly).
          </div>
        )}
      </div>
    </div>
  );
}
