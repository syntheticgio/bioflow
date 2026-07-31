import { useState } from "react";

/**
 * A long value shown short, with a control to reveal the rest in place.
 *
 * Hashes, storage paths and adapter sequences are all long, opaque and
 * occasionally needed in full -- to paste into a shell, or to check against
 * another record. Truncating them keeps a key/value list scannable; leaving no
 * way to see the whole thing means the value is displayed but not usable.
 *
 * Expands inline rather than opening a tooltip or dialog: the full value is
 * usually wanted for selecting and copying, which neither of those allows.
 */
export function TruncatedValue({
  value,
  head = 16,
  mono = true,
  className,
}: {
  value: string | null | undefined;
  /** Characters to show before the ellipsis when collapsed. */
  head?: number;
  /** Long opaque values are nearly always identifiers. */
  mono?: boolean;
  className?: string;
}) {
  const [expanded, setExpanded] = useState(false);

  if (!value) return <>—</>;

  // Nothing to hide: showing "see more" beside a value that is already whole
  // would be a control that does nothing.
  const needsTruncation = value.length > head + 3;

  const classes = [mono ? "mono" : null, className].filter(Boolean).join(" ");

  if (!needsTruncation) {
    return <span className={classes || undefined}>{value}</span>;
  }

  return (
    <span className="truncated-value">
      <span className={`${classes} truncated-value-text`.trim()}>
        {expanded ? value : `${value.slice(0, head)}…`}
      </span>
      <button
        type="button"
        className="btn-text truncated-value-toggle"
        aria-expanded={expanded}
        onClick={() => setExpanded((v) => !v)}
        // The full value is what a screen reader should read when opened; the
        // control itself needs to say which value it belongs to.
        title={expanded ? "Show less" : value}
      >
        {expanded ? "see less" : "see more"}
      </button>
    </span>
  );
}
