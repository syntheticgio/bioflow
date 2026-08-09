import type { ReadQuality } from "../lib/readQuality";

/**
 * The quality grade as a set numeral on a file's icon.
 *
 * Replaces the 9px green-to-red dot. The traffic-light ramp was the one place
 * the app reached for colors the design system does not own, and it carried the
 * tier in hue alone -- 5/4 and 3/2 differed only by shade. The numeral states
 * the tier outright, so the grade survives greyscale, colorblindness, and the
 * 9px size the dot was fighting at.
 *
 * The serif is the chrome: the figure is set in the theme's own face rather
 * than drawn, which is why this is not a BioIcon glyph.
 *
 * `showDenominator` carries the `/5`. It is off in a file row, where the badge
 * rides a 30px glyph and the denominator made the mark nearly as wide as the
 * icon it sits on -- measured at 18px against a 24px icon, covering most of the
 * artwork. Nothing is lost there: the word sits in the row's metadata line, the
 * aria-label says "n of 5", and the tooltip carries the score in full. Turn it
 * on where the badge stands alone with no such context.
 */
export function QualityBadge({
  quality,
  showDenominator = false,
}: {
  quality: ReadQuality;
  showDenominator?: boolean;
}) {
  return (
    <span
      className="q-badge"
      title={quality.tooltip}
      aria-label={`Read quality: ${quality.word}, ${quality.tier} of 5`}
    >
      <span className="q-badge-tier">{quality.tier}</span>
      {showDenominator && (
        <span className="q-badge-of" aria-hidden="true">
          /5
        </span>
      )}
    </span>
  );
}
