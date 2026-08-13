/**
 * One summary statistic: a small label above a large value.
 *
 * Previously defined twice, in BamResults and VariantResults, with the same
 * props and different type scales -- so two views a user reads as a set
 * rendered their headline numbers differently. This is VariantResults'
 * treatment, the newer of the two.
 */
export function Stat({ label, value }: { label: string; value: string }) {
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
      </div>
      <div style={{ fontSize: 22, fontWeight: 600 }}>{value}</div>
    </div>
  );
}
