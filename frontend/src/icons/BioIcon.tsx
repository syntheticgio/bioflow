import type { ReactNode } from "react";

/**
 * BioFlow icon set.
 *
 * One 256-grid, 16-stroke geometry throughout -- Phosphor's own, so these sit
 * at the same optical weight as the duotone icons the design system specifies.
 * Every glyph is normalised to one optical size -- ink fits a ~200-unit span
 * centred on (128,128), with the per-glyph scale compensated in stroke-width so
 * all strokes still read at 16. Stroke colour is `currentColor`; the duotone fill and the second-ink strokes
 * read `--bio-accent` (default the theme's cyan) and `--bio-duo` (fill opacity),
 * so a row can tint the whole glyph by setting one custom property.
 *
 * Each concept carries three variants, distinguished by shape vocabulary:
 *   a -- enclosures: frames, tracks, cells
 *   b -- strokes: lines, arcs, curves
 *   c -- a mark: points and the fewest segments that still name the thing
 *
 * Once a variant is picked per concept, drop the other two and the `variant`
 * prop with them.
 */

export type BioIconVariant = "a" | "b" | "c";

type Glyph = { label: string; a: ReactNode; b: ReactNode; c: ReactNode };

export const BIO_ICONS: Record<string, Glyph> = {
  sequence: {
    label: "Sequence",
    a: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 40h192v176H32Z"></path>
        <path d="M60 88h136" strokeDasharray="18 14" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M60 128h136" strokeDasharray="18 14"></path>
        <path d="M60 168h96" strokeDasharray="18 14"></path>
        </g>
      </>
    ),
    b: (
      <>
        <path d="M28 92h50v72H28Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)" stroke="none"></path>
        <path d="M28 92h200v72H28Z"></path><path d="M78 92v72"></path><path d="M128 92v72"></path><path d="M178 92v72"></path>
      </>
    ),
    c: (
      <>
        <g transform="translate(-19.62 -24.38) scale(1.1905)" strokeWidth={13.44}>
        <path d="M40 88v80"></path><path d="M96 104v48"></path><path d="M152 80v96"></path><path d="M208 104v48"></path>
        </g>
      </>
    ),
  },
  reads: {
    label: "Reads",
    a: (
      <>
        <g transform="translate(0.73 -56.09) scale(1.1364)" strokeWidth={14.08}>
        <path d="M24 108h176"></path><path d="M32 216v-40"></path><path d="M72 216v-56"></path><path d="M112 216v-48"></path><path d="M152 216v-64"></path><path d="M192 216v-32"></path>
        </g>
      </>
    ),
    /* Same three-bar geometry as enrichment.b: a tapering list reads as "many
       records, most discarded", which fits a pile of reads at least as well
       as it fits a filtered gene set. Replaces the S-curve that was here --
       nothing else referenced that artwork directly. */
    b: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <path d="M40 64h176" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M40 128h112"></path><path d="M40 192h56"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 128h80"></path><path d="M152 128h80"></path>
        </g>
      </>
    ),
  },
  alignment: {
    label: "Alignment",
    a: (
      <>
        <g transform="translate(-5.33 7.17) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 40h192v56H32Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <path d="M56 136h72"></path><path d="M136 136h64"></path><path d="M88 192h96"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(4.92 20.31) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 112h208"></path>
        <path d="M72 72v80" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M136 72v80" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M192 72v80" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 -2.77) scale(0.9615)" strokeWidth={16.64}>
        <path d="M80 96h96"></path><path d="M24 176h208" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
  },
  variants: {
    label: "Variants",
    a: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 128h20"></path><path d="M72 104l24 24-24 24-24-24Z"></path><path d="M120 108l20 20-20 20"></path><path d="M184 104l24 24-24 24-24-24Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path><path d="M212 128h20"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(4.92 -13.35) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 208h208"></path><path d="M92 208v-52"></path><path d="M172 208v-84"></path>
        <path d="M92 118l20 20-20 20-20-20Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <path d="M172 86l20 20-20 20-20-20Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 14.54) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 176h208"></path><path d="M128 60l32 32-32 32-32-32Z"></path>
        </g>
      </>
    ),
  },
  annotation: {
    label: "Annotation",
    a: (
      <>
        <g transform="translate(4.92 -33.54) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 168h208"></path><path d="M40 148h36v40H40Z"></path><path d="M96 128h72v80H96Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path><path d="M188 148h36v40h-36Z"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 128h208"></path>
        <path d="M88 104l24 24-24 24" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M160 104l24 24-24 24" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 8.77) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 88h208"></path><path d="M40 160h60"></path><path d="M128 160h36"></path><path d="M192 160h32"></path>
        </g>
      </>
    ),
  },
  coverage: {
    label: "Coverage",
    a: (
      <>
        <g transform="translate(4.92 -31.62) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 216h208"></path><path d="M24 196c32 0 36-72 72-72s36 44 72 44 32-52 64-52v100H24Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(4.92 -10.46) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 208h208"></path>
        <path d="M24 172h34v-48h34v40h34v-84h34v64h34v-28h34" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 -0.85) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 196c40 0 40-124 104-124s64 124 104 124"></path>
        </g>
      </>
    ),
  },
  contact_map: {
    label: "Contact map",
    a: (
      <>
        <g transform="translate(-5.33 -17.83) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 56h192v168H32Z"></path><path d="M96 56v168"></path><path d="M160 56v168"></path><path d="M32 112h192"></path><path d="M32 168h192"></path><path d="M32 56h64v56H32Z" fill="currentColor" fillOpacity="0.45" stroke="none"></path><path d="M96 112h64v56H96Z" fill="currentColor" fillOpacity="0.45" stroke="none"></path><path d="M160 168h64v56h-64Z" fill="currentColor" fillOpacity="0.45" stroke="none"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(4.92 -37.38) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 216h208"></path>
        <path d="M48 216q48-176 96 0" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M112 216q52-116 104 0"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 224 224 32"></path>
        <circle cx="84" cy="172" r="22" fill="currentColor" stroke="none"></circle>
        <circle cx="172" cy="84" r="22" fill="currentColor" stroke="none"></circle>
        </g>
      </>
    ),
  },
  counts_matrix: {
    label: "Counts matrix",
    a: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 48h192v40H32Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path><path d="M32 48h192v160H32Z"></path><path d="M32 88h192"></path><path d="M32 128h192"></path><path d="M32 168h192"></path><path d="M104 48v160"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-5.33 -9.5) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 176c40 0 48-104 96-104s56 72 96 72" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M32 96c40 0 48 96 96 96s56-56 96-56"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-6.41 -8.56) scale(1.0753)" strokeWidth={14.88}>
        <g fill="currentColor" stroke="none">
        <circle cx="56" cy="56" r="10"></circle><circle cx="128" cy="56" r="22"></circle><circle cx="200" cy="56" r="13"></circle>
        <circle cx="56" cy="128" r="23"></circle><circle cx="128" cy="128" r="11"></circle><circle cx="200" cy="128" r="17"></circle>
        <circle cx="56" cy="200" r="14"></circle><circle cx="128" cy="200" r="20"></circle><circle cx="200" cy="200" r="9"></circle>
        </g>
        </g>
      </>
    ),
  },
  unrecognized: {
    label: "Unrecognized",
    a: (
      <>
        <g transform="translate(6.85 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M56 24h84l56 56v152H56Z"></path><path d="M140 24v56h56"></path>
        <path d="M84 208 180 112" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(6.85 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M56 24h84l56 56v152H56Z" strokeDasharray="22 18"></path><path d="M140 24v56h56" strokeDasharray="22 18"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-29.06 -50.53) scale(1.2270)" strokeWidth={13.04}>
        <path d="M88 104a40 40 0 1 1 40 40v18"></path>
        <circle cx="128" cy="212" r="15" fill="currentColor" stroke="none"></circle>
        </g>
      </>
    ),
  },
  reference_genome: {
    label: "Reference genome",
    a: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <rect x="88" y="24" width="80" height="88" rx="16"></rect>
        <rect x="88" y="144" width="80" height="88" rx="16"></rect>
        <path d="M88 64h80" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M88 188h80" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M128 32a32 32 0 0 1 32 32v128a32 32 0 0 1-64 0V64a32 32 0 0 1 32-32Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <path d="M96 128h64"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 -0.85) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 160h208"></path><path d="M60 160v-32"></path><path d="M108 160v-52"></path><path d="M156 160v-32"></path><path d="M204 160v-52"></path>
        </g>
      </>
    ),
  },
  protein_cds: {
    label: "Protein & CDS",
    a: (
      <>
        <g transform="translate(-5.33 -13.67) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 48h192v56H32Z"></path>
        <path d="M96 48v56"></path><path d="M160 48v56"></path>
        <circle cx="128" cy="176" r="40" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></circle>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-17.45 -3.82) scale(1.1364)" strokeWidth={14.08}>
        <path d="M40 72q44-40 88 0t88 0"></path>
        <path d="M40 160q44-40 88 0t88 0"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-5.33 -9.5) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 168l48-72 48 72 48-72 48 72"></path>
        </g>
      </>
    ),
  },
  expression: {
    label: "Expression",
    a: (
      <>
        <g transform="translate(-5.33 -34.5) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 96h192v40H32Z" fill="var(--bio-accent,#0088b0)" fillOpacity="0.55"></path>
        <path d="M32 136h192v40H32Z" fill="var(--bio-accent,#0088b0)" fillOpacity="0.16"></path>
        <path d="M32 176h192v40H32Z" fill="var(--bio-accent,#0088b0)" fillOpacity="0.34"></path>
        <path d="M32 96h192v120H32Z"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-5.33 0.92) scale(1.0417)" strokeWidth={15.36}>
        <path d="M64 60v-16h64v16"></path><path d="M96 44V28"></path>
        <path d="M32 88h64v128H32Z" fill="var(--bio-accent,#0088b0)" fillOpacity="0.5"></path>
        <path d="M96 88h64v128H96Z" fill="var(--bio-accent,#0088b0)" fillOpacity="0.14"></path>
        <path d="M160 88h64v128h-64Z" fill="var(--bio-accent,#0088b0)" fillOpacity="0.32"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 128h208" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M72 64h40v64H72Z"></path><path d="M144 128h40v64h-40Z"></path>
        </g>
      </>
    ),
  },
  sample: {
    label: "Sample",
    a: (
      <>
        <g transform="translate(-5.33 3) scale(1.0417)" strokeWidth={15.36}>
        <rect x="48" y="40" width="40" height="176" rx="20"></rect>
        <rect x="108" y="40" width="40" height="176" rx="20" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></rect>
        <rect x="168" y="40" width="40" height="176" rx="20"></rect>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-11.13 -6.78) scale(1.0870)" strokeWidth={14.72}>
        <path d="M92 32h72"></path>
        <path d="M100 32v156a28 28 0 0 0 56 0V32"></path>
        <path d="M100 136h56" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-36.1 -23.28) scale(1.2821)" strokeWidth={12.48}>
        <path d="M128 40c40 56 56 76 56 100a56 56 0 0 1-112 0c0-24 16-44 56-100Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        </g>
      </>
    ),
  },
  run: {
    label: "Run",
    a: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 48h192v160H32Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <path d="M32 88h192"></path><path d="M68 128l24 24-24 24"></path><path d="M120 176h64"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-11.13 -11.13) scale(1.0870)" strokeWidth={14.72}>
        <circle cx="128" cy="128" r="92" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></circle>
        <path d="M110 88l58 40-58 40Z"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-0.19 -12) scale(1.0000)" strokeWidth={16}>
        <path d="M128 40v56"></path>
        <path d="M128 96a72 72 0 1 0 52 22"></path>
        </g>
      </>
    ),
  },
  project: {
    label: "Project",
    a: (
      <>
        <g transform="translate(-5.33 -22) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 72h64l24 28h104v116H32Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-8.17 -1.79) scale(1.0638)" strokeWidth={15.04}>
        <path d="M56 40c48-16 96-16 144 0v176c-48-16-96-16-144 0Z"></path>
        <path d="M128 48v168" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <path d="M72 56H40v144h32"></path><path d="M184 56h32v144h-32"></path>
        <g fill="currentColor" stroke="none">
        <circle cx="98" cy="128" r="15"></circle><circle cx="128" cy="128" r="15"></circle><circle cx="158" cy="128" r="15"></circle>
        </g>
        </g>
      </>
    ),
  },
  metadata_tags: {
    label: "Metadata & tags",
    a: (
      <>
        <g transform="translate(-15.48 -6.78) scale(1.0870)" strokeWidth={14.72}>
        <path d="M136 32h88v88l-96 96-88-88Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <circle cx="180" cy="76" r="14"></circle>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 88h72"></path><path d="M144 88h80" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M32 168h56"></path><path d="M128 168h96" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <path d="M32 96h72v64H32Z" fill="currentColor" stroke="none"></path>
        <path d="M152 96h72v64h-72Z"></path>
        </g>
      </>
    ),
  },
  profile: {
    label: "Profile",
    a: (
      <>
        <g transform="translate(-11.13 -11.13) scale(1.0870)" strokeWidth={14.72}>
        <circle cx="128" cy="128" r="92" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></circle>
        <circle cx="128" cy="108" r="32"></circle>
        <path d="M70 194a68 68 0 0 1 116 0"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-32 -37) scale(1.2500)" strokeWidth={12.8}>
        <circle cx="128" cy="96" r="44"></circle>
        <path d="M48 212a84 84 0 0 1 160 0"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-49.78 -27.56) scale(1.3889)" strokeWidth={11.52}>
        <circle cx="128" cy="88" r="48" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></circle>
        <path d="M56 184h144"></path>
        </g>
      </>
    ),
  },
  shared_with_me: {
    label: "Shared with me",
    a: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <path d="M84 128 172 76"></path><path d="M84 128 172 180"></path>
        <circle cx="64" cy="128" r="24" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></circle>
        <circle cx="192" cy="64" r="24" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></circle>
        <circle cx="192" cy="192" r="24" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></circle>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-29.14 -24.38) scale(1.1905)" strokeWidth={13.44}>
        <path d="M104 48H48v160h56"></path>
        <path d="M120 128h96"></path><path d="M184 96l32 32-32 32"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <circle cx="100" cy="128" r="60"></circle>
        <circle cx="156" cy="128" r="60" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></circle>
        </g>
      </>
    ),
  },
  activity: {
    label: "Activity",
    a: (
      <>
        <g transform="translate(-18.15 -12.66) scale(1.0989)" strokeWidth={14.56}>
        <path d="M96 72h128"></path><path d="M96 128h96"></path><path d="M96 184h64"></path>
        <circle cx="56" cy="72" r="14" fill="var(--bio-accent,#0088b0)" stroke="none"></circle>
        <circle cx="56" cy="128" r="14" fill="currentColor" fillOpacity="0.35" stroke="none"></circle>
        <circle cx="56" cy="184" r="14" fill="currentColor" fillOpacity="0.35" stroke="none"></circle>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <circle cx="128" cy="128" r="88"></circle>
        <path d="M128 72v56l44 28" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 128h44l20-52 28 104 24-52h92"></path>
        </g>
      </>
    ),
  },
  trim: {
    label: "Trim",
    a: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 128h124"></path><path d="M172 128h52" strokeDasharray="14 16"></path>
        <path d="M148 96l32 64"></path><path d="M180 96l-32 64"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 128h120"></path><path d="M168 128h56" strokeDasharray="14 16"></path>
        <path d="M156 84v88" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 104h112v48H24Z" fill="currentColor" stroke="none"></path>
        <path d="M160 104h72v48h-72Z" strokeDasharray="14 12"></path>
        </g>
      </>
    ),
  },
  align: {
    label: "Align",
    a: (
      <>
        <g transform="translate(-5.33 -1.17) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 48h192v160H32Z"></path>
        <path d="M80 104h128v48H80Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-5.33 0.92) scale(1.0417)" strokeWidth={15.36}>
        <path d="M56 48 104 152"></path><path d="M200 48 152 152"></path>
        <path d="M24 200h208" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-15.48 -11.13) scale(1.0870)" strokeWidth={14.72}>
        <path d="M40 48v160" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M72 80h152"></path><path d="M72 128h104"></path><path d="M72 176h136"></path>
        </g>
      </>
    ),
  },
  assemble: {
    label: "Assemble",
    a: (
      <>
        <g transform="translate(-5.33 -3.25) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 60h72"></path><path d="M124 60h48"></path><path d="M192 60h32"></path>
        <path d="M128 96v40"></path><path d="M108 120l20 20 20-20"></path>
        <path d="M32 192h192" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 64 128 128"></path><path d="M32 192 128 128"></path>
        <path d="M128 128h104" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 128h72"></path><path d="M76 108l20 20-20 20"></path>
        <path d="M232 128h-72"></path><path d="M180 108l-20 20 20 20"></path>
        </g>
      </>
    ),
  },
  scaffold: {
    label: "Scaffold",
    a: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 104h64v48H24Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <path d="M124 104h44v48h-44Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <path d="M204 104h28v48h-28Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <path d="M88 128h36" strokeDasharray="10 12"></path><path d="M168 128h36" strokeDasharray="10 12"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 152h64"></path><path d="M112 152h32"></path><path d="M168 152h64"></path>
        <path d="M88 152q12-48 24 0" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M144 152q12-48 24 0" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-49.78 -49.78) scale(1.3889)" strokeWidth={11.52}>
        <path d="M40 176h64v-96" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M216 176h-64v-96" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
  },
  quantify: {
    label: "Quantify",
    a: (
      <>
        <g transform="translate(-5.33 -1.17) scale(1.0417)" strokeWidth={15.36}>
        <path d="M40 64h56"></path><path d="M112 64h40"></path><path d="M168 64h48"></path>
        <path d="M32 168h192"></path>
        <path d="M56 152h40v32H56Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <path d="M144 152h56v32h-56Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-5.33 -22) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 192h192" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M64 192v-48"></path><path d="M128 192v-96"></path><path d="M192 192v-64"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-49.78 -49.78) scale(1.3889)" strokeWidth={11.52}>
        <path d="M72 64v128"></path><path d="M120 64v128"></path><path d="M168 64v128"></path>
        <path d="M56 192 200 64" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
  },
  differential_expression: {
    label: "Differential expression",
    a: (
      <>
        <g transform="translate(-15.48 -6.78) scale(1.0870)" strokeWidth={14.72}>
        <path d="M40 32v184h184"></path>
        <path d="M40 88h184" strokeDasharray="12 14" stroke="var(--bio-accent,#0088b0)"></path>
        <g fill="currentColor" stroke="none">
        <circle cx="76" cy="64" r="12"></circle><circle cx="196" cy="56" r="12"></circle>
        <circle cx="104" cy="128" r="10"></circle><circle cx="160" cy="140" r="10"></circle><circle cx="132" cy="176" r="10"></circle>
        </g>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M128 32v192" strokeDasharray="14 16" stroke="var(--bio-accent,#0088b0)"></path>
        <g fill="currentColor" stroke="none">
        <circle cx="64" cy="72" r="14"></circle><circle cx="44" cy="132" r="12"></circle><circle cx="84" cy="176" r="12"></circle>
        <circle cx="192" cy="64" r="14"></circle><circle cx="212" cy="124" r="12"></circle><circle cx="172" cy="172" r="12"></circle>
        </g>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-32 -32) scale(1.2500)" strokeWidth={12.8}>
        <path d="M88 208V64"></path><path d="M60 92l28-28 28 28"></path>
        <path d="M168 48v144"></path><path d="M140 164l28 28 28-28"></path>
        </g>
      </>
    ),
  },
  variant_call: {
    label: "Variant call",
    a: (
      <>
        <g transform="translate(-17.45 -22.00) scale(1.1364)" strokeWidth={14.08}>
        <path d="M32 96h192v64H32Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <path d="M128 88l40 40-40 40-40-40Z"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <path d="M40 96h72"></path><path d="M144 96h72"></path>
        <path d="M40 160h72"></path><path d="M144 160h72"></path>
        <path d="M128 100l28 28-28 28-28-28Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-40.42 -0.95) scale(1.3158)" strokeWidth={12.16}>
        <path d="M128 84l44 44-44 44-44-44Z"></path>
        <path d="M128 48V24"></path><path d="M188 68l16-16"></path><path d="M68 68 52 52"></path>
        </g>
      </>
    ),
  },
  qc_report: {
    label: "QC report",
    a: (
      <>
        <g transform="translate(-0.21 -48.92) scale(1.2821)" strokeWidth={12.48}>
        <path d="M48 216v-56"></path><path d="M104 216v-104"></path><path d="M160 216v-72"></path>
        <path d="M40 96l24 24 56-60" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <path d="M40 184a88 88 0 1 1 176 0"></path>
        <path d="M128 184l44-52" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M64 120l-16-16"></path><path d="M128 96V72"></path><path d="M192 120l16-16"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-11.13 -11.13) scale(1.0870)" strokeWidth={14.72}>
        <circle cx="128" cy="64" r="26" fill="currentColor" stroke="none"></circle><circle cx="128" cy="132" r="26" fill="currentColor" fillOpacity="0.45" stroke="none"></circle><circle cx="128" cy="200" r="26"></circle>
        </g>
      </>
    ),
  },
  index: {
    label: "Index",
    a: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M48 32h160v192H48Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <path d="M80 32v192"></path><path d="M108 96h72"></path><path d="M108 136h72"></path><path d="M108 176h48"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 72h144"></path><path d="M32 128h144"></path><path d="M32 184h144"></path>
        <path d="M200 104h24v48h-24Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <path d="M80 40h96v176l-48-40-48 40Z"></path>
        </g>
      </>
    ),
  },
  fetch_from_archive: {
    label: "Fetch from archive",
    a: (
      <>
        <g transform="translate(-14.22 -7.56) scale(1.1111)" strokeWidth={14.4}>
        <path d="M48 56a80 24 0 1 0 160 0 80 24 0 1 0-160 0Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <path d="M48 56v72a80 24 0 0 0 160 0V56"></path><path d="M48 92a80 24 0 0 0 160 0"></path>
        <path d="M128 160v52"></path><path d="M104 188l24 24 24-24"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-32 -32) scale(1.2500)" strokeWidth={12.8}>
        <path d="M48 48h160"></path><path d="M48 88h160"></path><path d="M48 128h160"></path>
        <path d="M128 152v56" stroke="var(--bio-accent,#0088b0)"></path><path d="M104 184l24 24 24-24" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-7.03 -17.01) scale(1.1252)" strokeWidth={14.22}>
        <path d="M40 88a88 88 0 0 1 160 24"></path>
        <path d="M200 176a88 88 0 0 1-160-24"></path>
        <path d="M176 40v72h-72" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
  },
  search: {
    label: "Search",
    a: (
      <>
        <g transform="translate(-32 -32) scale(1.2500)" strokeWidth={12.8}>
        <circle cx="112" cy="112" r="64" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></circle>
        <path d="M158 158l50 50"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(4.92 -0.85) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 88h64"></path><path d="M24 168h64"></path>
        <circle cx="148" cy="120" r="56"></circle><path d="M188 160l44 44"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-5.33 -9.5) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 56h192l-72 80v72l-48-24v-48Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        </g>
      </>
    ),
  },
  download: {
    label: "Download",
    a: (
      <>
        <g transform="translate(-17.45 -8.36) scale(1.1364)" strokeWidth={14.08}>
        <path d="M128 32v112"></path><path d="M92 108l36 36 36-36"></path>
        <path d="M40 168v40h176v-40"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-24.38 -19.62) scale(1.1905)" strokeWidth={13.44}>
        <path d="M128 40v112"></path><path d="M92 116l36 36 36-36"></path><path d="M48 208h160"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-49.78 -65.33) scale(1.3889)" strokeWidth={11.52}>
        <path d="M56 104l72 72 72-72"></path>
        </g>
      </>
    ),
  },
  delete: {
    label: "Delete",
    a: (
      <>
        <g transform="translate(-17.45 -22) scale(1.1364)" strokeWidth={14.08}>
        <path d="M40 72h176"></path><path d="M96 72V44h64v28"></path>
        <path d="M64 72v148h128V72"></path><path d="M108 108v76"></path><path d="M148 108v76"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-32 -32) scale(1.2500)" strokeWidth={12.8}>
        <path d="M48 48h160v160H48Z"></path><path d="M96 96l64 64"></path><path d="M160 96l-64 64"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-70.4 -70.4) scale(1.5500)" strokeWidth={10.32}>
        <path d="M64 64l128 128"></path><path d="M192 64L64 192"></path>
        </g>
      </>
    ),
  },
  add_files: {
    label: "Add files",
    a: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <path d="M128 160V48"></path><path d="M92 84l36-36 36 36"></path>
        <path d="M40 168v40h176v-40"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-32 -32) scale(1.2500)" strokeWidth={12.8}>
        <path d="M48 48h160v160H48Z"></path><path d="M128 88v80"></path><path d="M88 128h80"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-49.78 -49.78) scale(1.3889)" strokeWidth={11.52}>
        <path d="M128 56v144"></path><path d="M56 128h144"></path>
        </g>
      </>
    ),
  },
  open: {
    label: "Open",
    a: (
      <>
        <g transform="translate(-11.13 -11.13) scale(1.0870)" strokeWidth={14.72}>
        <circle cx="128" cy="128" r="92" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></circle>
        <path d="M112 88l44 40-44 40"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-42 -32) scale(1.2500)" strokeWidth={12.8}>
        <path d="M96 48l80 80-80 80"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-19.62 -24.38) scale(1.1905)" strokeWidth={13.44}>
        <path d="M40 128h168"></path><path d="M156 76l52 52-52 52"></path>
        </g>
      </>
    ),
  },
  external_link: {
    label: "External link",
    a: (
      <>
        <g transform="translate(-32 -32) scale(1.2500)" strokeWidth={12.8}>
        <path d="M136 48H48v160h160v-88"></path><path d="M144 112L208 48"></path><path d="M160 48h48v48"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-79.36 -79.36) scale(1.6200)" strokeWidth={9.88}>
        <path d="M72 184L184 72"></path><path d="M112 72h72v72"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-29.45 -22) scale(1.2500)" strokeWidth={12.8}>
        <path d="M56 200a112 112 0 0 1 144-136"></path><path d="M140 40l60 24-24 60"></path>
        </g>
      </>
    ),
  },
  warning: {
    label: "Warning",
    a: (
      <>
        <g transform="translate(-5.33 -1.17) scale(1.0417)" strokeWidth={15.36}>
        <path d="M128 40l96 168H32Z" fill="#d6006c" fillOpacity="var(--bio-duo,0.15)"></path>
        <path d="M128 100v52"></path>
        <circle cx="128" cy="182" r="9" fill="currentColor" stroke="none"></circle>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-11.13 -11.13) scale(1.0870)" strokeWidth={14.72}>
        <circle cx="128" cy="128" r="92" fill="#d6006c" fillOpacity="var(--bio-duo,0.15)"></circle>
        <path d="M128 76v64"></path>
        <circle cx="128" cy="176" r="9" fill="currentColor" stroke="none"></circle>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-24.38 -19.62) scale(1.1905)" strokeWidth={13.44}>
        <path d="M128 40v112" stroke="#aa0b56"></path>
        <circle cx="128" cy="196" r="12" fill="#aa0b56" stroke="none"></circle>
        </g>
      </>
    ),
  },
  succeeded: {
    label: "Succeeded",
    a: (
      <>
        <g transform="translate(-11.13 -11.13) scale(1.0870)" strokeWidth={14.72}>
        <circle cx="128" cy="128" r="92" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></circle>
        <path d="M84 132l32 32 60-68"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-32 -32) scale(1.2500)" strokeWidth={12.8}>
        <path d="M48 136l48 48 112-112"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <rect x="32" y="32" width="192" height="192" rx="40" fill="currentColor" stroke="none"></rect>
        <path d="M88 132l28 28 56-62" stroke="var(--bio-knockout,#f3f2f2)"></path>
        </g>
      </>
    ),
  },
  pangenome: {
    label: "Pangenome",
    a: (
      <>
        <g transform="translate(4.47 2.51) scale(0.9804)" strokeWidth={16.32}>
        <path d="M24 128h40"></path><path d="M64 128q40-56 88 0"></path><path d="M64 128q40 56 88 0"></path>
        <path d="M152 128h20"></path><path d="M172 128q28-36 56 0"></path><path d="M172 128q28 36 56 0"></path>
        <circle cx="64" cy="128" r="12" fill="currentColor" stroke="none"></circle>
        <circle cx="152" cy="128" r="12" fill="currentColor" stroke="none"></circle>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 128h32"></path><path d="M56 128q44-60 88 0"></path><path d="M56 128q44 60 88 0"></path>
        <path d="M144 128h88"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 -0.85) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 152h208"></path><path d="M72 152q48-72 96 0" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
  },
  multiple_sequence_alignment: {
    label: "Multiple sequence alignment",
    a: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M136 32v192h32V32Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)" stroke="none"></path>
        <path d="M32 56h72"></path><path d="M136 56h88"></path>
        <path d="M32 104h104"></path><path d="M168 104h56"></path>
        <path d="M32 152h72"></path><path d="M136 152h56"></path>
        <path d="M32 200h104"></path><path d="M168 200h56"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 72h192" strokeDasharray="28 16"></path>
        <path d="M32 128h192" strokeDasharray="20 14"></path>
        <path d="M32 184h192" strokeDasharray="34 12"></path>
        <path d="M148 44v168" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-17.45 -26.55) scale(1.1364)" strokeWidth={14.08}>
        <path d="M40 80h176"></path><path d="M40 128h176"></path>
        <path d="M40 192h176" stroke="var(--bio-accent,#0088b0)" strokeWidth={31.68}></path>
        </g>
      </>
    ),
  },
  methylation: {
    label: "Methylation",
    a: (
      <>
        <g transform="translate(4.92 -6.62) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 196h208"></path>
        <path d="M72 196v-48"></path><path d="M128 196v-72"></path><path d="M184 196v-48"></path>
        <circle cx="72" cy="128" r="20" fill="currentColor" stroke="none"></circle>
        <circle cx="128" cy="104" r="20"></circle>
        <circle cx="184" cy="128" r="20" fill="currentColor" stroke="none"></circle>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-10.3 -8.17) scale(1.0638)" strokeWidth={15.04}>
        <circle cx="80" cy="128" r="44" fill="currentColor" stroke="none"></circle>
        <circle cx="180" cy="128" r="44"></circle>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-49.78 -60.89) scale(1.3889)" strokeWidth={11.52}>
        <path d="M128 208v-72"></path>
        <circle cx="128" cy="100" r="36" fill="currentColor" stroke="none"></circle>
        </g>
      </>
    ),
  },
  single_cell: {
    label: "Single cell",
    a: (
      <>
        <g transform="translate(-9.59 -5.69) scale(1.0386)" strokeWidth={15.4}>
        <path d="M40 76a52 52 0 0 1 84-32 52 52 0 0 1-16 84 52 52 0 0 1-68-52Z" strokeDasharray="16 16" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <g fill="currentColor" stroke="none">
        <circle cx="60" cy="76" r="13"></circle><circle cx="92" cy="52" r="13"></circle><circle cx="96" cy="92" r="13"></circle>
        <circle cx="184" cy="76" r="13"></circle><circle cx="212" cy="108" r="13"></circle><circle cx="176" cy="120" r="13"></circle>
        <circle cx="104" cy="188" r="13"></circle><circle cx="140" cy="212" r="13"></circle><circle cx="144" cy="172" r="13"></circle>
        </g>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-27.81 -32.47) scale(1.1628)" strokeWidth={13.76}>
        <g fill="currentColor" stroke="none">
        <circle cx="72" cy="80" r="16"></circle><circle cx="112" cy="112" r="16"></circle><circle cx="64" cy="132" r="16"></circle>
        <circle cx="180" cy="164" r="16"></circle><circle cx="204" cy="120" r="16"></circle><circle cx="148" cy="196" r="16"></circle>
        </g>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-32 -32) scale(1.2500)" strokeWidth={12.8}>
        <circle cx="128" cy="128" r="80" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></circle>
        <circle cx="128" cy="128" r="26" fill="currentColor" stroke="none"></circle>
        </g>
      </>
    ),
  },
  phylogeny: {
    label: "Phylogeny",
    a: (
      <>
        <g transform="translate(11.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M24 128h32"></path><path d="M56 60v136"></path>
        <path d="M56 60h48"></path><path d="M104 32v56"></path><path d="M104 32h96"></path><path d="M104 88h96"></path>
        <path d="M56 196h48"></path><path d="M104 168v56"></path><path d="M104 168h96"></path><path d="M104 224h96"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(1.91 -11.13) scale(1.0870)" strokeWidth={14.72}>
        <path d="M24 128h56"></path>
        <path d="M80 128 208 40"></path><path d="M80 128 208 216"></path><path d="M80 128h128"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 128h72"></path><path d="M96 64v128"></path><path d="M96 64h136"></path><path d="M96 192h136"></path>
        </g>
      </>
    ),
  },
  gwas: {
    label: "GWAS",
    a: (
      <>
        <g transform="translate(-5.33 -6.9) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 216h192"></path>
        <path d="M32 88h192" strokeDasharray="12 14" stroke="var(--bio-accent,#0088b0)"></path>
        <g fill="currentColor" stroke="none">
        <circle cx="56" cy="188" r="11"></circle><circle cx="88" cy="164" r="11"></circle>
        <circle cx="120" cy="180" r="11"></circle><circle cx="152" cy="56" r="13"></circle>
        <circle cx="152" cy="112" r="11"></circle><circle cx="184" cy="172" r="11"></circle><circle cx="212" cy="188" r="11"></circle>
        </g>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(5.36 25.17) scale(0.9434)" strokeWidth={16.96}>
        <path d="M24 176l32-20 28 14 24-12 20-116 24 122 28-18 24 14 32-16"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-5.33 20.71) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 152h192" strokeDasharray="14 16"></path>
        <circle cx="128" cy="80" r="26" fill="currentColor" stroke="none"></circle>
        </g>
      </>
    ),
  },
  structural_variant: {
    label: "Structural variant",
    a: (
      <>
        <g transform="translate(4.92 1.08) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 168h72"></path><path d="M160 168h72"></path>
        <path d="M96 168h64" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M96 96h64" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M120 76l-24 20 24 20" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M136 148l24 20-24 20" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 128h64"></path><path d="M168 128h64"></path>
        <path d="M88 128h80" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M112 104l-24 24 24 24" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M144 104l24 24-24 24" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 -14.31) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 96h208"></path>
        <path d="M88 176h80" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M116 152l-28 24 28 24" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
  },
  peaks: {
    label: "Peaks",
    a: (
      <>
        <g transform="translate(4.92 -6.62) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 168c28 0 32-96 56-96s28 96 56 96 32-72 56-72 20 72 40 72"></path>
        <path d="M56 208h48" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M164 208h40" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(5.36 3.47) scale(0.9434)" strokeWidth={16.96}>
        <path d="M24 184c32 0 32-104 60-104s28 104 60 104 28-80 56-80 20 80 36 80"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 192h48l56-128 56 128h48"></path>
        </g>
      </>
    ),
  },
  ortholog_groups: {
    label: "Ortholog groups",
    a: (
      <>
        <g transform="translate(-5.33 -9.5) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 64h64v56H32Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <path d="M160 64h64v56h-64Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <path d="M64 120v40"></path><path d="M192 120v40"></path>
        <path d="M64 160h128"></path>
        <path d="M128 160v40"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-49.78 -49.78) scale(1.3889)" strokeWidth={11.52}>
        <path d="M56 56h48v48H56Z"></path><path d="M152 56h48v48h-48Z"></path>
        <path d="M56 152h48v48H56Z"></path><path d="M152 152h48v48h-48Z"></path>
        <path d="M104 80h48" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M104 176h48" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <path d="M96 128h64" stroke="var(--bio-accent,#0088b0)"></path>
        <g fill="currentColor" stroke="none">
        <circle cx="68" cy="128" r="28"></circle><circle cx="188" cy="128" r="28"></circle>
        </g>
        </g>
      </>
    ),
  },
  protein_structure: {
    label: "Protein structure",
    a: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <path d="M40 40h176v176H40Z"></path>
        <path d="M76 180c48 0 24-56 52-56s4 56 52 56" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-5.33 1.84) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 176c0-52 96-28 96-80s-64-28-64 12"></path>
        <path d="M128 96c0 52 96 28 96 80"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <path d="M40 192c0-64 176-64 176-128"></path>
        <path d="M40 128c0-56 176 0 176-64" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
  },
  enrichment: {
    label: "Enrichment",
    a: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M32 48h192v160H32Z"></path>
        <circle cx="96" cy="128" r="52" fill="var(--bio-accent,#0088b0)" fillOpacity="0.5" stroke="none"></circle>
        <path d="M168 104v48"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <path d="M40 64h176" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M40 128h112"></path><path d="M40 192h56"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-11.06 -10.88) scale(1.0850)" strokeWidth={14.75}>
        <circle cx="128" cy="128" r="92"></circle>
        <path d="M128 36a92 92 0 0 1 80 138Z" fill="var(--bio-accent,#0088b0)" fillOpacity="0.5" stroke="none"></path>
        <path d="M128 36v92l80 46"></path>
        </g>
      </>
    ),
  },
  queued: {
    label: "Queued",
    a: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M64 40H24v176h40"></path><path d="M192 40h40v176h-40"></path>
        <path d="M72 88h112" stroke="var(--bio-accent,#0088b0)"></path><path d="M72 128h112"></path><path d="M72 168h112"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-11.13 -11.13) scale(1.0870)" strokeWidth={14.72}>
        <circle cx="128" cy="128" r="92"></circle>
        <path d="M128 72v56h52" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g fill="currentColor" stroke="none">
        <circle cx="46" cy="128" r="18"></circle><circle cx="128" cy="128" r="18"></circle>
        <circle cx="210" cy="128" r="18" fill="var(--bio-accent,#0088b0)"></circle>
        </g>
      </>
    ),
  },
  running: {
    label: "Running",
    a: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 72h100v112H24Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)" stroke="none"></path>
        <path d="M24 72h208v112H24Z"></path>
        <path d="M124 72v112" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-11.13 -11.13) scale(1.0870)" strokeWidth={14.72}>
        <path d="M128 36a92 92 0 1 1-92 92"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-27.32 -8.17) scale(1.0638)">
        <path d="M76 34 216 128 76 222Z" fill="currentColor" stroke="none"></path>
        </g>
      </>
    ),
  },
  projects: {
    label: "Projects",
    a: (
      <>
        <g transform="translate(-4 -8) scale(1)" strokeWidth={16}>
        <path d="M72 60h48l18 22h72"></path>
        <path d="M32 96h60l20 24h120v92H32Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-15.96 -12.66) scale(1.0989)" strokeWidth={14.56}>
        <path d="M40 56v144"></path>
        <path d="M72 72h150" stroke="var(--bio-accent,#0088b0)"></path><path d="M72 128h120"></path><path d="M72 184h150"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-20.84 -27.82) scale(1.1628)" fill="currentColor" stroke="none">
        <circle cx="64" cy="72" r="22"></circle><circle cx="192" cy="72" r="22"></circle>
        <circle cx="128" cy="196" r="22" fill="var(--bio-accent,#0088b0)"></circle>
        </g>
      </>
    ),
  },
  files: {
    label: "Files",
    a: (
      <>
        <g transform="translate(4 0) scale(1)" strokeWidth={16}>
        <path d="M84 28h72l52 52v20"></path>
        <path d="M40 60h84l56 56v112H40Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <path d="M124 60v56h56"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <path d="M88 40h104v104"></path>
        <path d="M168 216H64V112" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M56 32h96l48 48v144H56Z" fill="currentColor" stroke="none"></path>
        <path d="M152 32v48h48" stroke="var(--bio-knockout,#f3f2f2)"></path>
        </g>
      </>
    ),
  },
  ask: {
    label: "Ask",
    a: (
      <>
        <g transform="translate(-5.33 -9.5) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 48h192v128H120l-48 40v-40H32Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(6.43 4.47) scale(0.9804)" strokeWidth={16.32}>
        <circle cx="128" cy="116" r="92"></circle>
        <path d="M63 181 28 228l58-18" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-5.33 -1.17) scale(1.0417)">
        <path d="M32 48h192v112h-64l-24 40-24-40H32Z" fill="currentColor" stroke="none"></path>
        </g>
      </>
    ),
  },
  agent: {
    label: "Agent",
    // Optically centred, not geometrically: the antenna is a hairline and the
    // head is a solid block, so centring the ink box (y=131 of 256) drops the
    // face to y=156 and the icon reads low beside `projects` and `files` in
    // the footer strip. Shifted up 17 units (9.06 -> -8) to bring the head
    // centre to y=139. Not the full correction -- head-centring needs
    // ty=-19.4, which clips the antenna tip off the top of the viewBox.
    a: (
      <>
        <g transform="translate(-6.73 -8) scale(1.0526)" strokeWidth={15.2}>
        <path d="M128 72V38"></path>
        <rect x="40" y="72" width="176" height="136" rx="28" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></rect>
        <g fill="currentColor" stroke="none">
        <circle cx="128" cy="28" r="12"></circle><circle cx="92" cy="140" r="16"></circle><circle cx="164" cy="140" r="16"></circle>
        </g>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(0 4) scale(1)" strokeWidth={16}>
        <path d="M128 56V24"></path>
        <circle cx="128" cy="140" r="84"></circle>
        <path d="M100 132v20" stroke="var(--bio-accent,#0088b0)"></path><path d="M156 132v20" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)">
        <path d="M128 24c10 56 46 92 104 104-58 12-94 48-104 104-10-56-46-92-104-104 58-12 94-48 104-104Z" fill="currentColor" stroke="none"></path>
        </g>
      </>
    ),
  },
  live: {
    label: "Live",
    a: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <rect x="24" y="88" width="208" height="80" rx="40" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></rect>
        <circle cx="76" cy="128" r="18" fill="var(--bio-accent,#0088b0)" stroke="none"></circle>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-24.38 -24.38) scale(1.1905)" strokeWidth={13.44}>
        <circle cx="128" cy="128" r="22" fill="currentColor" stroke="none"></circle>
        <path d="M76 92a56 56 0 0 0 0 72"></path><path d="M180 92a56 56 0 0 1 0 72"></path>
        <path d="M44 60a104 104 0 0 0 0 136" stroke="var(--bio-accent,#0088b0)"></path><path d="M212 60a104 104 0 0 1 0 136" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <circle cx="128" cy="128" r="100" fill="var(--bio-accent,#0088b0)" stroke="none"></circle>
      </>
    ),
  },
  connection_closed: {
    label: "Connection closed",
    a: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <rect x="24" y="88" width="208" height="80" rx="40"></rect>
        <circle cx="76" cy="128" r="18"></circle>
        <path d="M52 172 204 84" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <circle cx="128" cy="128" r="22"></circle>
        <path d="M76 92a56 56 0 0 0 0 72"></path><path d="M180 92a56 56 0 0 1 0 72"></path>
        <path d="M40 216 216 40" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <circle cx="128" cy="128" r="100"></circle>
      </>
    ),
  },
  database_not_found: {
    label: "Database not found",
    a: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <path d="M56 68a72 28 0 0 1 144 0a72 28 0 0 1-144 0" strokeDasharray="20 16"></path>
        <path d="M56 68v120a72 28 0 0 0 144 0V68" strokeDasharray="20 16"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-5.33 9.25) scale(1.0417)" strokeWidth={15.36}>
        <path d="M32 60a96 28 0 0 1 192 0"></path>
        <path d="M32 128a96 28 0 0 1 192 0"></path>
        <path d="M32 196a96 28 0 0 1 192 0" strokeDasharray="18 15" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(-17.45 -26.5) scale(1.1364)" strokeWidth={14.08}>
        <ellipse cx="128" cy="88" rx="88" ry="32" fill="currentColor" stroke="none"></ellipse>
        <ellipse cx="128" cy="184" rx="88" ry="32" strokeDasharray="20 16"></ellipse>
        </g>
      </>
    ),
  },
  reads_raw: {
    label: "Reads (raw)",
    a: (
      <>
        <g transform="translate(0.73 -56.09) scale(1.1364)" strokeWidth={14.08}>
        <path d="M24 108h176"></path><path d="M32 216v-40"></path><path d="M72 216v-56"></path><path d="M112 216v-48"></path><path d="M152 216v-64"></path><path d="M192 216v-32"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-17.45 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <path d="M40 64h176"></path><path d="M40 128h112"></path><path d="M40 192h56"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 128h80"></path><path d="M152 128h80"></path>
        </g>
      </>
    ),
  },
  reads_trimmed: {
    label: "Reads (trimmed)",
    a: (
      <>
        <g transform="translate(0.73 -56.09) scale(1.1364)" strokeWidth={14.08}>
        <path d="M24 108h176"></path><path d="M32 216v-56"></path><path d="M72 216v-56"></path><path d="M112 216v-56"></path><path d="M152 216v-56"></path><path d="M192 216v-56"></path>
        <path d="M24 160h176" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(5.27 -17.45) scale(1.1364)" strokeWidth={14.08}>
        <path d="M40 64h136"></path><path d="M40 128h136"></path><path d="M40 192h136"></path>
        <path d="M176 40v176" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M52 128h64"></path><path d="M140 128h64"></path>
        <path d="M24 108v40" stroke="var(--bio-accent,#0088b0)"></path><path d="M232 108v40" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
  },
  // Platform variants of reads_raw/reads_trimmed.c. The platform is one
  // added ink cue on top of that same mark, never a colour swap: Illumina
  // staggers the mates onto two baselines, PacBio loops the gap (circular
  // template read on both strands), Nanopore threads one strand through a
  // pore. Cyan stays reserved for the trim cut, exactly as in the
  // platform-less marks. Only variant `c` is meaningful here -- these are
  // always rendered as the mark (see CHOSEN_VARIANT) -- but `a`/`b` are
  // filled in with the same drawing so the Glyph type stays uniform and a
  // future variant switch has something to fall back to.
  reads_raw_illumina: {
    label: "Reads (raw, Illumina)",
    a: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 100h84"></path><path d="M148 156h84"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 100h84"></path><path d="M148 156h84"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 100h84"></path><path d="M148 156h84"></path>
        </g>
      </>
    ),
  },
  reads_trimmed_illumina: {
    label: "Reads (trimmed, Illumina)",
    a: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M52 100h56"></path><path d="M148 156h56"></path>
        <path d="M24 82v36" stroke="var(--bio-accent,#0088b0)"></path><path d="M232 138v36" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M52 100h56"></path><path d="M148 156h56"></path>
        <path d="M24 82v36" stroke="var(--bio-accent,#0088b0)"></path><path d="M232 138v36" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M52 100h56"></path><path d="M148 156h56"></path>
        <path d="M24 82v36" stroke="var(--bio-accent,#0088b0)"></path><path d="M232 138v36" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
  },
  reads_raw_pacbio: {
    label: "Reads (raw, PacBio)",
    a: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 112h84"></path><path d="M148 112h84"></path>
        <path d="M108 112q20 60 40 0"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 112h84"></path><path d="M148 112h84"></path>
        <path d="M108 112q20 60 40 0"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 112h84"></path><path d="M148 112h84"></path>
        <path d="M108 112q20 60 40 0"></path>
        </g>
      </>
    ),
  },
  reads_trimmed_pacbio: {
    label: "Reads (trimmed, PacBio)",
    a: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M52 112h56"></path><path d="M148 112h56"></path>
        <path d="M108 112q20 60 40 0"></path>
        <path d="M24 92v40" stroke="var(--bio-accent,#0088b0)"></path><path d="M232 92v40" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M52 112h56"></path><path d="M148 112h56"></path>
        <path d="M108 112q20 60 40 0"></path>
        <path d="M24 92v40" stroke="var(--bio-accent,#0088b0)"></path><path d="M232 92v40" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M52 112h56"></path><path d="M148 112h56"></path>
        <path d="M108 112q20 60 40 0"></path>
        <path d="M24 92v40" stroke="var(--bio-accent,#0088b0)"></path><path d="M232 92v40" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
  },
  reads_raw_nanopore: {
    label: "Reads (raw, Oxford Nanopore)",
    a: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 128h72"></path><path d="M160 128h72"></path>
        <path d="M112 96v64"></path><path d="M144 96v64"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 128h72"></path><path d="M160 128h72"></path>
        <path d="M112 96v64"></path><path d="M144 96v64"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M24 128h72"></path><path d="M160 128h72"></path>
        <path d="M112 96v64"></path><path d="M144 96v64"></path>
        </g>
      </>
    ),
  },
  reads_trimmed_nanopore: {
    label: "Reads (trimmed, Oxford Nanopore)",
    a: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M52 128h44"></path><path d="M160 128h44"></path>
        <path d="M112 96v64"></path><path d="M144 96v64"></path>
        <path d="M24 108v40" stroke="var(--bio-accent,#0088b0)"></path><path d="M232 108v40" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M52 128h44"></path><path d="M160 128h44"></path>
        <path d="M112 96v64"></path><path d="M144 96v64"></path>
        <path d="M24 108v40" stroke="var(--bio-accent,#0088b0)"></path><path d="M232 108v40" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M52 128h44"></path><path d="M160 128h44"></path>
        <path d="M112 96v64"></path><path d="M144 96v64"></path>
        <path d="M24 108v40" stroke="var(--bio-accent,#0088b0)"></path><path d="M232 108v40" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
  },
  /* Agent header controls. Settings drops the gear (its teeth close up at
   * 13px) for sliders; delete keeps a bin in the outline forms and strikes the
   * bubble in the marks; restart is the set's only closed rotation. */
  agent_settings: {
    label: "Agent settings",
    a: (
      <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M40 72h176"></path><path d="M40 128h176"></path><path d="M40 184h176"></path>
        <circle cx="88" cy="72" r="22" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></circle>
        <circle cx="164" cy="128" r="22" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></circle>
        <circle cx="112" cy="184" r="22" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)" stroke="var(--bio-accent,#0088b0)"></circle>
      </g>
    ),
    b: (
      <g transform="translate(-11.13 -11.13) scale(1.0870)" strokeWidth={14.72}>
        <circle cx="128" cy="128" r="92"></circle>
        <path d="M128 128l58-58" stroke="var(--bio-accent,#0088b0)"></path>
      </g>
    ),
    c: (
      <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M40 96h176"></path><path d="M40 160h176"></path>
        <g stroke="none"><circle cx="104" cy="96" r="18" fill="#201e1d"></circle><circle cx="168" cy="160" r="18" fill="var(--bio-accent,#0088b0)"></circle></g>
      </g>
    ),
  },
  chat_delete: {
    label: "Delete chat",
    a: (
      <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M32 56h192v104H116l-44 40v-40H32Z" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></path>
        <path d="M40 196 216 36" stroke="var(--bio-accent,#0088b0)"></path>
      </g>
    ),
    b: (
      <g transform="translate(-11.13 -11.13) scale(1.0870)" strokeWidth={14.72}>
        <path d="M56 76h144"></path>
        <path d="M100 76V48h56v28"></path>
        <path d="M76 76l12 128h80l12-128"></path>
        <path d="M112 110v60" stroke="var(--bio-accent,#0088b0)"></path><path d="M144 110v60" stroke="var(--bio-accent,#0088b0)"></path>
      </g>
    ),
    c: (
      <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <path d="M32 56h192v104H116l-44 40v-40H32Z" fill="#201e1d" stroke="none"></path>
        <path d="M40 196 216 36" strokeWidth={26} stroke="var(--bio-accent,#0088b0)"></path>
      </g>
    ),
  },
  agent_restart: {
    label: "Restart agent",
    a: (
      <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={16.64}>
        <circle cx="128" cy="128" r="88" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)" stroke="none"></circle>
        <path d="M128 40a88 88 0 1 1-88 88"></path>
        <path d="M102 14l26 26-26 26" stroke="var(--bio-accent,#0088b0)"></path>
      </g>
    ),
    b: (
      <g transform="translate(-11.13 -11.13) scale(1.0870)" strokeWidth={14.72}>
        <path d="M42 97A92 92 0 0 1 214 97"></path>
        <path d="M214 159A92 92 0 0 1 42 159"></path>
        <path d="M178 84l38 12-12 38" stroke="var(--bio-accent,#0088b0)"></path>
        <path d="M78 172l-38-12 12-38"></path>
      </g>
    ),
    c: (
      <>
        <g transform="translate(4.92 4.92) scale(0.9615)" strokeWidth={22}>
          <path d="M128 40a88 88 0 1 1-88 88"></path>
        </g>
        <path d="M120 12 176 40 120 68Z" fill="var(--bio-accent,#0088b0)" stroke="none"></path>
      </>
    ),
  },
  user: {
    label: "User",
    a: (
      <>
        <g transform="translate(-11.13 -11.13) scale(1.0870)" strokeWidth={14.72}>
        <circle cx="128" cy="128" r="92" fill="var(--bio-accent,#0088b0)" fillOpacity="var(--bio-duo,0.15)"></circle>
        <path d="M92 62c0 42 72 24 72 66s-72 24-72 66"></path>
        <path d="M164 62c0 42-72 24-72 66s72 24 72 66"></path>
        </g>
      </>
    ),
    b: (
      <>
        <g transform="translate(-5.33 -5.33) scale(1.0417)" strokeWidth={15.36}>
        <path d="M96 32c0 48 64 48 64 96s-64 48-64 96"></path>
        <path d="M160 32c0 48-64 48-64 96s64 48 64 96"></path>
        <path d="M104 76h48" stroke="var(--bio-accent,#0088b0)"></path><path d="M104 180h48" stroke="var(--bio-accent,#0088b0)"></path>
        </g>
      </>
    ),
    c: (
      <>
        <g transform="translate(19.81 11.64) scale(0.9091)" strokeWidth={17.6}>
        <path d="M96 36c0 46 64 46 64 92s-64 46-64 92"></path>
        <g fill="var(--bio-accent,#0088b0)" stroke="none">
        <circle cx="96" cy="36" r="18"></circle><circle cx="96" cy="220" r="18"></circle>
        </g>
        </g>
      </>
    ),
  },
};

export type BioIconName = keyof typeof BIO_ICONS;

/**
 * The variant chosen per concept, reviewed 2026-08-09.
 *
 * Enclosures (`a`) are the house style; the exceptions are concepts where an
 * enclosure either fights the thing it names or collides with a neighbour:
 *
 *   sequence (c)       -- the frame in `a` reads as a document, not a sequence,
 *                         and would echo `counts_matrix` two rows down.
 *   reads, variants,   -- strokes carry these better: each is a thing that
 *   run, index,           happens along a line rather than sits inside a box.
 *   sample (b)            reads.b was swapped 2026-08-09 for the tapering
 *                         three-bar mark shared with enrichment.b -- reads
 *                         better as "many records" than the original S-curve.
 *   metadata_tags (c)  -- the mark is the tag; enclosing it doubles the shape.
 *
 * Section V interface chrome is `b` throughout, so the drawn verbs sit at the
 * same weight as the typographic characters (× › ▾ ▸ ↓ ↗) they replace.
 *
 * All three variants stay in the file: a concept can be re-picked by editing
 * one line here, with no artwork to recover.
 */
export const CHOSEN_VARIANT: Record<string, BioIconVariant> = {
  sequence: "c",
  // Tapering three-bar mark, shared with enrichment.b -- reads better as
  // "many records" than the bar-chart geometry in reads.a.
  reads: "b",
  variants: "b",
  run: "b",
  index: "b",
  sample: "b",
  metadata_tags: "c",
  // V. Interface chrome
  search: "b",
  download: "b",
  delete: "b",
  add_files: "b",
  open: "b",
  external_link: "b",
  warning: "b",
  succeeded: "b",
  // VII. Job status & agent chrome -- strokes over enclosures for queued,
  // running and live, for the same reason as Section V: these sit beside
  // running text at icon size, not in a card, so a mark reads cleaner than a
  // frame. ask and agent keep the house default (`a`) -- their enclosures
  // are the recognisable shape (speech bubble, robot head), unlike the rest
  // of this section.
  //
  // projects and files were `b` here too until the footer stats strip
  // ("7 projects · 150 files") started using them 2026-08-09: the folder
  // and dog-eared-document enclosures are what those words mean as objects
  // in this app, and that reads clearer at a glance than the stroke marks
  // did in the same spot -- the same call already made for ask/agent above.
  // Reverted to the house default (`a`).
  queued: "b",
  running: "b",
  live: "b",
  // Connection/storage status chrome, same reasoning as warning/succeeded
  // above: these read as a badge next to text, not a card.
  connection_closed: "b",
  database_not_found: "b",
  // Reads (raw) and Reads (trimmed) use the mark variant (`c`) per design
  // review: the file-list row already carries the Raw/Trimmed toggle state
  // in its label, so the icon only needs the fewest strokes that still read
  // as "a pile of reads", not a full enclosure.
  reads_raw: "c",
  reads_trimmed: "c",
  // Platform variants carry the same reasoning as reads_raw/reads_trimmed
  // above, plus one added ink cue for the instrument -- still the fewest
  // strokes that read as "a pile of reads", not a full enclosure.
  reads_raw_illumina: "c",
  reads_trimmed_illumina: "c",
  reads_raw_pacbio: "c",
  reads_trimmed_pacbio: "c",
  reads_raw_nanopore: "c",
  reads_trimmed_nanopore: "c",
  // Agent header controls keep the house default (`a`) -- the reviewed pick
  // for all three, replacing the ⚙️/🗑/🔄 emoji in the AI Agent drawer head.
  // Listed rather than left implicit so re-picking one is a one-line edit.
  agent_settings: "a",
  chat_delete: "a",
  agent_restart: "a",
  // user keeps the house default (`a`) -- explicitly chosen in review as the
  // replacement for the 🧬 emoji beside the profile name.
};

/** The house default for every concept not named above. */
const DEFAULT_VARIANT: BioIconVariant = "a";

export function variantFor(name: BioIconName): BioIconVariant {
  return CHOSEN_VARIANT[name] ?? DEFAULT_VARIANT;
}

export function BioIcon({
  name,
  variant,
  size = 22,
  className = "",
  title,
  decorative = false,
}: {
  name: BioIconName;
  /** Omit to get the reviewed choice for this concept. */
  variant?: BioIconVariant;
  size?: number;
  className?: string;
  title?: string;
  /** Set when the glyph sits inside a control that already names itself --
   *  a `title`-carrying icon button, say. Without it the svg contributes a
   *  second tooltip and a second accessible name for the one control. */
  decorative?: boolean;
}) {
  const glyph = BIO_ICONS[name];
  if (!glyph) return null;
  const chosen = variant ?? variantFor(name);
  if (decorative) {
    return (
      <svg
        viewBox="0 0 256 256"
        width={size}
        height={size}
        className={className}
        aria-hidden="true"
        focusable="false"
        fill="none"
        stroke="currentColor"
        strokeWidth={16}
        strokeLinecap="round"
        strokeLinejoin="round"
        style={{ display: "block", flexShrink: 0 }}
      >
        {glyph[chosen]}
      </svg>
    );
  }
  return (
    <svg
      viewBox="0 0 256 256"
      width={size}
      height={size}
      className={className}
      role="img"
      aria-label={title ?? glyph.label}
      fill="none"
      stroke="currentColor"
      strokeWidth={16}
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ display: "block", flexShrink: 0 }}
    >
      {(title ?? glyph.label) ? <title>{title ?? glyph.label}</title> : null}
      {glyph[chosen]}
    </svg>
  );
}

/** Format kind -> concept. One drawing serves a whole family: SAM/BAM/CRAM are
 *  all "alignment", BED/GFF/GTF all "annotation". The lettered label, where a
 *  view wants one, is the format string itself set beside the glyph -- not a
 *  colored pill baked into the artwork. */
const FORMAT_CONCEPTS: Record<string, BioIconName> = {
  fasta: "sequence",
  fa: "sequence",
  fn: "sequence",
  fna: "sequence",
  faa: "protein_cds",
  fastq: "reads",
  sam: "alignment",
  bam: "alignment",
  cram: "alignment",
  vcf: "variants",
  bcf: "variants",
  bed: "annotation",
  gff: "annotation",
  gtf: "annotation",
  wig: "coverage",
  bigwig: "coverage",
  bedgraph: "coverage",
  hic: "contact_map",
  tsv: "counts_matrix",
  csv: "counts_matrix",
  // Sidecars are the file the tools need, not the data anyone reads: a .fai
  // beside a reference is an index, and drawing it as "unrecognized" put a
  // question-mark page next to a perfectly well-understood file. Checked
  // against the real database, where .fai is the third-commonest kind with no
  // concept of its own.
  fai: "index",
  bai: "index",
  csi: "index",
  tbi: "index",
  // Assembly graph -- GFA is what an assembler emits before scaffolding.
  gfa: "assemble",
};

/** Role overrides format, exactly as categorizeFile() in ProjectExplorer does:
 *  the bytes cannot tell a reference genome from a pile of reads. */
const ROLE_CONCEPTS: Record<string, BioIconName> = {
  reference: "reference_genome",
  annotation: "annotation",
  protein: "protein_cds",
  transcript: "protein_cds",
  counts: "expression",
  de_results: "expression",
  // Deliberately not "annotation" -- that's precisely the conflation this
  // role exists to avoid (StringTie's proposed transcript models vs. a
  // downloaded, authoritative GFF3). "assemble" matches the concept already
  // used for the GFA assembly graph, the other role-carrying computed
  // result.
  assembled_transcripts: "assemble",
};

export function conceptFor(
  formatKind: string,
  role?: string | null,
): BioIconName {
  if (role && ROLE_CONCEPTS[role]) return ROLE_CONCEPTS[role];
  return FORMAT_CONCEPTS[formatKind?.toLowerCase()] ?? "unrecognized";
}

/** A read's originating instrument, so far as the icon set draws a
 *  distinction. Anything else (or nothing) falls back to the platform-less
 *  reads_raw/reads_trimmed mark. */
export type ReadPlatform = "illumina" | "pacbio" | "nanopore";

/**
 * Coarse platform read from the same two sources TrimDialog's isLongRead()
 * already checks, in the same priority order: qc_read_chemistry, when QC has
 * inferred it, is the more specific fact and wins; a file nobody has QC'd yet
 * falls back to the free-text instrument label, matched the way SRA and
 * manual metadata entry both write it (e.g. "ILLUMINA", "OXFORD_NANOPORE",
 * "Sequel II"). Not exhaustive -- this only needs to pick an icon, not be the
 * source of truth an aligner preset relies on.
 */
export function readPlatformFor(
  chemistry?: unknown,
  platformLabel?: unknown,
): ReadPlatform | null {
  switch (chemistry) {
    case "hifi":
    case "clr":
      return "pacbio";
    case "ont_simplex":
    case "ont_duplex":
      return "nanopore";
    case "short":
      return "illumina";
  }
  const label = String(platformLabel ?? "").toLowerCase();
  if (/nanopore|minion|gridion|promethion|flongle/.test(label)) return "nanopore";
  if (/pacbio|sequel|revio/.test(label)) return "pacbio";
  if (/illumina|hiseq|miseq|novaseq|nextseq/.test(label)) return "illumina";
  return null;
}

/** Human-readable platform label, or "Unspecified" when unknown. */
function readPlatformLabel(platformLabel: string | null): string {
  return platformLabel ?? "Unspecified";
}

/** Human-readable stage label for the reads raw/trimmed distinction. */
function stageLabel(readsStage: "raw" | "trimmed"): string {
  return readsStage === "trimmed" ? "Trimmed" : "Raw";
}

/**
 * Drop-in replacement for the old PNG-backed FileIcon: same props, same
 * default box, no image requests. `getFileIcon.ts` and icons/*.png go with it.
 */
export function FileIcon({
  formatKind,
  role,
  className = "",
  variant,
  size = 32,
  readsStage,
  readPlatform,
}: {
  formatKind: string;
  role?: string | null;
  className?: string;
  /** Omit to get the reviewed choice for the resolved concept. */
  variant?: BioIconVariant;
  size?: number;
  /** When the resolved concept is "reads", swap in the raw/trimmed mark
   *  instead -- lets the stage rail's Raw/Trimmed toggle carry into the row
   *  icon, not just the label. Ignored for every other concept, since only
   *  FASTQ has a raw/trimmed distinction to draw. */
  readsStage?: "raw" | "trimmed";
  /** When the resolved concept is "reads", add the instrument's ink cue to
   *  the raw/trimmed mark -- see readPlatformFor(). Ignored for every other
   *  concept, and ignored if the platform isn't one the icon set draws a
   *  distinction for. */
  readPlatform?: ReadPlatform | null;
}) {
  const concept = conceptFor(formatKind, role);
  const stageSuffix = readsStage === "trimmed" ? "trimmed" : "raw";
  const name =
    concept === "reads" && readsStage
      ? readPlatform
        ? (`reads_${stageSuffix}_${readPlatform}` as BioIconName)
        : (`reads_${stageSuffix}` as BioIconName)
      : concept;
  const platformLabel = readPlatform
    ? { illumina: "Illumina", pacbio: "PacBio", nanopore: "Nanopore" }[readPlatform]
    : null;
  return (
    <BioIcon
      name={name}
      variant={variant}
      size={size}
      className={className}
      title={
        concept === "reads"
          ? `${readPlatformLabel(platformLabel)} ${readsStage ? `(${stageLabel(readsStage)})` : ""} ${formatKind} file`.trim()
          : `${formatKind} file`
      }
    />
  );
}
