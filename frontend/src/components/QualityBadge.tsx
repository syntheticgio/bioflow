import { qualityClass, type ReadQuality } from "../lib/readQuality";

/**
 * The quality grade as a dot on a file's icon.
 *
 * Tiers 5/4 and 3/2 differ only by shade, so color is deliberately never the
 * only signal: the word sits beside it in the row, and the full tooltip --
 * including the numeric score -- hangs off the badge itself, so hovering the
 * icon alone answers "how good is this file?". That is also what keeps the
 * badge meaningful for colorblind users.
 */
export function QualityBadge({ quality }: { quality: ReadQuality }) {
  return (
    <span
      className={qualityClass(quality.tier)}
      title={quality.tooltip}
      aria-label={`Read quality: ${quality.word}, ${quality.tier} of 5`}
    />
  );
}
