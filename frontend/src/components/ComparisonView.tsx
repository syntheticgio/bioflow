import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import {
  comparableCharts,
  type ChartAvailability,
} from "../lib/comparableCharts";
import { extractSeries, type ComparisonSeries } from "../lib/comparisonSeries";
import { NxChart } from "./NxChart";
import { BuscoCompareChart } from "./BuscoCompareChart";
import { DepthCompareChart } from "./DepthCompareChart";
import { QualityCompareChart } from "./QualityCompareChart";

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
 * chart's series is derived from the facts by the same reading the
 * single-object panel uses (`extractSeries` mirrors each panel), so this view
 * and the panels cannot drift on what a chart needs. Dispatching is a switch
 * on `chart.chartId` (R7): one table row in `comparableCharts.ts` plus one
 * branch here per chart.
 */

interface Props {
  /** The first (primary) object id, from `sel`. */
  idA: string;
  /** The second (comparison) object id, from `cmp`. */
  idB: string;
}

/** Render one chart from the two objects' series for it. Each branch mirrors
 *  its single-object panel's reading via `extractSeries`. */
function renderChart(
  chartId: string,
  seriesA: ComparisonSeries | null,
  seriesB: ComparisonSeries | null,
) {
  switch (chartId) {
    case "nx": {
      if (
        seriesA?.chartId !== "nx" ||
        seriesB?.chartId !== "nx"
      ) {
        return null;
      }
      return (
        <NxChart
          curve={seriesA.curve}
          totalBases={seriesA.totalBases}
          genomeSize={seriesA.genomeSize}
          label={seriesA.name}
          compare={{
            curve: seriesB.curve,
            totalBases: seriesB.totalBases,
            genomeSize: seriesB.genomeSize,
            label: seriesB.name,
          }}
        />
      );
    }
    case "busco": {
      if (
        seriesA?.chartId !== "busco" ||
        seriesB?.chartId !== "busco"
      ) {
        return null;
      }
      return (
        <BuscoCompareChart
          a={{
            name: seriesA.name,
            singlePct: seriesA.singlePct,
            duplicatedPct: seriesA.duplicatedPct,
            fragmentedPct: seriesA.fragmentedPct,
            missingPct: seriesA.missingPct,
          }}
          b={{
            name: seriesB.name,
            singlePct: seriesB.singlePct,
            duplicatedPct: seriesB.duplicatedPct,
            fragmentedPct: seriesB.fragmentedPct,
            missingPct: seriesB.missingPct,
          }}
        />
      );
    }
    case "qc": {
      if (seriesA?.chartId !== "qc" || seriesB?.chartId !== "qc") return null;
      return (
        <QualityCompareChart
          a={{ name: seriesA.name, curve: seriesA.curve }}
          b={{ name: seriesB.name, curve: seriesB.curve }}
        />
      );
    }
    case "depth": {
      if (seriesA?.chartId !== "depth" || seriesB?.chartId !== "depth") {
        return null;
      }
      return (
        <DepthCompareChart
          a={{
            name: seriesA.name,
            buckets: seriesA.buckets,
            bucketWidth: seriesA.bucketWidth,
          }}
          b={{
            name: seriesB.name,
            buckets: seriesB.buckets,
            bucketWidth: seriesB.bucketWidth,
          }}
        />
      );
    }
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
  seriesA: ComparisonSeries | null;
  seriesB: ComparisonSeries | null;
}) {
  const { chart, available, missing } = availability;
  return (
    <div className="compare-chart">
      <div className="section-title">{chart.label}</div>
      {available && renderChart(chart.chartId, seriesA, seriesB)}
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
              seriesA={extractSeries(r.chart.chartId, objA.name, objA.facts)}
              seriesB={extractSeries(r.chart.chartId, objB.name, objB.facts)}
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
