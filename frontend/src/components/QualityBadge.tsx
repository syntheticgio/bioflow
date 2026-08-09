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
 * than drawn, which is why this is not a BioIcon glyph. `/5` is carried at a
 * smaller optical size so the tier reads first.
 *
 * The word still sits beside it in the row and the full tooltip -- including
 * the numeric score and any caveats -- still hangs off the badge itself.
 */
export function QualityBadge({ quality }: { quality: ReadQuality }) {
  return (
    <span
      className="q-badge"
      title={quality.tooltip}
      aria-label={`Read quality: ${quality.word}, ${quality.tier} of 5`}
    >
      <span className="q-badge-tier">{quality.tier}</span>
      <span className="q-badge-of" aria-hidden="true">
        /5
      </span>
    </span>
  );
}
