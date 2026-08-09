/**
 * Emit standalone SVG files from BioIcon.tsx.
 *
 * The component is the source of truth: this script parses its JSX rather than
 * holding a second copy of any path data, so a glyph edited in the component
 * and re-run here cannot drift. Nothing in the app imports these files -- they
 * exist for the places React cannot reach (docs, README, favicons, slides).
 *
 * Output:
 *   frontend/src/icons/svg/<concept>.svg        the reviewed variant
 *   frontend/src/icons/svg/variants/<concept>-<a|b|c>.svg   all three
 *
 * Run: node frontend/scripts/generate-icon-svgs.mjs
 */
import {
  readFileSync,
  writeFileSync,
  mkdirSync,
  rmSync,
  readdirSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(here, "../src/icons/BioIcon.tsx");
const OUT = resolve(here, "../src/icons/svg");

const source = readFileSync(SRC, "utf8");

/** Pull `CHOSEN_VARIANT` straight from the component so the two cannot diverge. */
function parseChosen() {
  const block = source.match(
    /export const CHOSEN_VARIANT: Record<string, BioIconVariant> = \{([\s\S]*?)\n\};/,
  );
  if (!block) throw new Error("CHOSEN_VARIANT not found in BioIcon.tsx");
  const chosen = {};
  for (const m of block[1].matchAll(/^\s*([a-z_]+):\s*"([abc])",/gm)) {
    chosen[m[1]] = m[2];
  }
  return chosen;
}

/** Walk BIO_ICONS, returning { name: { label, a, b, c } } with raw JSX bodies. */
function parseGlyphs() {
  const start = source.indexOf("export const BIO_ICONS");
  if (start < 0) throw new Error("BIO_ICONS not found in BioIcon.tsx");
  const body = source.slice(start);

  const glyphs = {};
  // Each concept: two-space indent, then its three variant bodies.
  const conceptRe = /^  ([a-z_0-9]+): \{\n\s*label: "([^"]+)",\n([\s\S]*?)\n  \},$/gm;
  for (const c of body.matchAll(conceptRe)) {
    const [, name, label, inner] = c;
    const variants = {};
    const varRe = /^\s{4}([abc]): \(\n([\s\S]*?)\n\s{4}\),$/gm;
    for (const v of inner.matchAll(varRe)) {
      variants[v[1]] = v[2];
    }
    if (variants.a && variants.b && variants.c) {
      glyphs[name] = { label, ...variants };
    }
  }
  return glyphs;
}

/**
 * JSX body -> SVG markup.
 *
 * The glyph bodies are plain SVG elements wrapped in a fragment; the only
 * React-isms are the `<>` fragment, JSX attribute casing, and `{...}` numeric
 * expressions. Anything unexpected throws rather than silently emitting a
 * broken file.
 */
function jsxToSvg(jsx) {
  let out = jsx
    .replace(/^\s*<>\n?/, "")
    .replace(/\n?\s*<\/>\s*$/, "")
    .replace(/strokeWidth=\{([\d.]+)\}/g, 'stroke-width="$1"')
    .replace(/strokeWidth="([\d.]+)"/g, 'stroke-width="$1"')
    .replace(/strokeDasharray=/g, "stroke-dasharray=")
    .replace(/strokeLinecap=/g, "stroke-linecap=")
    .replace(/strokeLinejoin=/g, "stroke-linejoin=")
    .replace(/fillOpacity=/g, "fill-opacity=")
    .replace(/strokeOpacity=/g, "stroke-opacity=")
    .replace(/clipPath=/g, "clip-path=")
    .replace(/fillRule=/g, "fill-rule=")
    .replace(/clipRule=/g, "clip-rule=");

  const leftover = out.match(/\s[a-z]+[A-Z][a-zA-Z]*=/);
  if (leftover) {
    throw new Error(`unconverted JSX attribute ${leftover[0].trim()}`);
  }
  if (out.includes("{") || out.includes("}")) {
    throw new Error("unconverted JSX expression remains");
  }
  return out
    .split("\n")
    .map((l) => l.replace(/^\s{8}/, "  ").trimEnd())
    .filter(Boolean)
    .join("\n");
}

/**
 * The CSS custom properties that tint a glyph do not survive into a standalone
 * file used as `<img>` or in a doc, so the fallbacks are inlined -- the same
 * values the component falls back to when a host sets nothing.
 */
/** Labels carry `&` ("Metadata & tags", "Protein & CDS"), which is illegal raw
 *  in an XML attribute -- an unescaped one makes the file fail to parse. */
function xmlAttr(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function wrap(inner, label) {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256"
  role="img" aria-label="${xmlAttr(label)}"
  fill="none" stroke="currentColor" stroke-width="16"
  stroke-linecap="round" stroke-linejoin="round">
${inner}
</svg>
`;
}

const chosen = parseChosen();
const glyphs = parseGlyphs();
const names = Object.keys(glyphs);
if (names.length === 0) throw new Error("parsed zero glyphs -- check BioIcon.tsx shape");

rmSync(OUT, { recursive: true, force: true });
mkdirSync(`${OUT}/variants`, { recursive: true });

let count = 0;
for (const name of names) {
  const g = glyphs[name];
  const pick = chosen[name] ?? "a";
  for (const v of ["a", "b", "c"]) {
    writeFileSync(
      `${OUT}/variants/${name}-${v}.svg`,
      wrap(jsxToSvg(g[v]), `${g.label} (${v})`),
    );
    count++;
  }
  writeFileSync(`${OUT}/${name}.svg`, wrap(jsxToSvg(g[pick]), g.label));
  count++;
}

// Names whose chosen variant was recorded but which no longer exist as glyphs
// would silently do nothing, so say so.
const orphans = Object.keys(chosen).filter((n) => !glyphs[n]);
if (orphans.length) {
  console.warn(`warning: CHOSEN_VARIANT names no glyph: ${orphans.join(", ")}`);
}

/**
 * A malformed file still writes fine and only fails at the point something
 * tries to render it -- which is why this checks rather than trusting the
 * string building. An unescaped `&` in a label got past review once already.
 */
function assertWellFormed(dir) {
  const bad = [];
  for (const f of readdirSync(dir, { recursive: true })) {
    if (!String(f).endsWith(".svg")) continue;
    const text = readFileSync(resolve(dir, String(f)), "utf8");
    const attrs = text.slice(0, text.indexOf(">") + 1);
    if (/&(?!(amp|lt|gt|quot|apos|#\d+);)/.test(attrs)) {
      bad.push(`${f}: unescaped & in attributes`);
    }
    const opens = (text.match(/<(?!\/)(?!\?)[a-z]/g) ?? []).length;
    const closes = (text.match(/<\/[a-z]/g) ?? []).length;
    const selfClosing = (text.match(/\/>/g) ?? []).length;
    if (opens !== closes + selfClosing) {
      bad.push(`${f}: ${opens} open vs ${closes}+${selfClosing} closed`);
    }
  }
  if (bad.length) {
    console.error(`${bad.length} malformed file(s):`);
    for (const b of bad.slice(0, 10)) console.error("  " + b);
    process.exit(1);
  }
}

assertWellFormed(OUT);

console.log(`${names.length} concepts -> ${count} SVG files in src/icons/svg/`);
