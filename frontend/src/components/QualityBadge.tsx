import type { ReadQuality } from "../lib/readQuality";

/**
 * The quality grade as a set numeral on a file's icon: "3/5" in the design
 * review's B -- Serif figure.
 *
 * Replaces the 9px green-to-red dot. The traffic-light ramp was the one place
 * the app reached for colors the design system does not own, and it carried
 * the tier in hue alone -- 5/4 and 3/2 differed only by shade. The numeral
 * states the tier outright, so the grade survives greyscale and the 9px size
 * the dot was fighting at; color is reinstated on top of that as emphasis,
 * not as the only signal.
 *
 * The serif is the chrome: the figure is set in the theme's own face rather
 * than drawn, which is why this is not a BioIcon glyph.
 *
 * Color is binary, matching `readQuality`'s own tier words rather than a
 * five-step ramp: 5/4/3 (Excellent/Good/Fair) are still usable data and read
 * in the accent; 2/1 (Poor/Unsuitable) are a real problem and read in error
 * red. There is no amber step -- "Fair" is the last tier this app calls
 * usable, so it gets the "fine" color, not a warning one.
 */
export function QualityBadge({ quality }: { quality: ReadQuality }) {
  return (
    <span
      className={`q-badge ${quality.tier <= 2 ? "q-badge-low" : ""}`}
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
