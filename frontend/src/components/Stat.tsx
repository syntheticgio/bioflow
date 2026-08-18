import { InfoMarker } from "./InfoMarker";

/**
 * One summary statistic: a small label above a large value.
 *
 * Previously defined twice, in BamResults and VariantResults, with the same
 * props and different type scales -- so two views a user reads as a set
 * rendered their headline numbers differently. This is VariantResults'
 * treatment, the newer of the two.
 *
 * The marker lives on the label rather than beside the value: these are the
 * numbers most likely to be copied into a methods section, and several of
 * them -- the ≥N× breadth figures, Ti/Tv -- do not mean what their label
 * alone suggests. `metric` is optional at the type level only because a
 * caller may render a Stat before its registry entry exists; the label
 * coverage test in metricInfo.test.ts is what stops that from lasting.
 */
export function Stat({
  label,
  value,
  metric,
}: {
  label: string;
  value: string;
  /** Registry key for the InfoMarker. See lib/metricInfo.ts. */
  metric?: string;
}) {
  return (
    <div>
      <div
        style={{
          textTransform: "uppercase",
          fontSize: 11,
          letterSpacing: "0.06em",
          color: "var(--text-faint)",
        }}
      >
        {label}
        {metric && <InfoMarker metric={metric} />}
      </div>
      <div style={{ fontSize: 22, fontWeight: 600 }}>{value}</div>
    </div>
  );
}
