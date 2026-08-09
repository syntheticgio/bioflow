import type { DataObject } from "../api/types";

/**
 * A 1-5 read quality grade with the reasoning behind it.
 *
 * Base quality drives the tier; composition problems demote it. GC never
 * does -- the organism's expected GC is unknown, so 31% is only "wrong" if
 * you assume human.
 */
export interface ReadQuality {
  tier: 1 | 2 | 3 | 4 | 5;
  word: "Excellent" | "Good" | "Fair" | "Poor" | "Unsuitable";
  /** What produced the tier, e.g. "Q30 92.1%". Shown in the tooltip. */
  basis: string;
  /** Demotion reasons, already human-readable. Empty when nothing demoted. */
  caveats: string[];
  /** Assembled hover text for every surface. */
  tooltip: string;
}

const WORDS = {
  5: "Excellent",
  4: "Good",
  3: "Fair",
  2: "Poor",
  1: "Unsuitable",
} as const;

/** Illumina conventions: Q30 is the industry yardstick. */
const Q30_TIERS: [number, 1 | 2 | 3 | 4 | 5][] = [
  [0.9, 5],
  [0.8, 4],
  [0.7, 3],
  [0.55, 2],
];

/** Used only when fastp has not run and all we have is ingest's mean. */
const MEAN_Q_TIERS: [number, 1 | 2 | 3 | 4 | 5][] = [
  [36, 5],
  [32, 4],
  [28, 3],
  [22, 2],
];

/**
 * Assays where PCR or amplification makes high duplication expected rather
 * than a defect. Values match the vocabulary in backend/app/metadata/sra.py.
 */
const HIGH_DUP_EXPECTED = new Set([
  "RNA-seq",
  "Amplicon",
  "Targeted panel",
  "ChIP-seq",
  "ATAC-seq",
]);

const DUP_LIMIT = 0.5;
const N_LIMIT = 1.0; // percent
const TAIL_COLLAPSE_Q = 20;
const HEALTHY_MEAN_Q = 30;

/**
 * Percent N at a single cycle above which that cycle counts as failed.
 * Well clear of the sub-1% noise floor of clean Illumina data; a genuine
 * cycle failure spikes far higher. Kept in step with the reference line
 * NContentChart draws, so the chart and the grade cannot disagree.
 */
const N_SPIKE_LIMIT = 5.0;

function tierFrom(
  value: number,
  table: [number, 1 | 2 | 3 | 4 | 5][],
): 1 | 2 | 3 | 4 | 5 {
  for (const [threshold, tier] of table) {
    if (value >= threshold) return tier;
  }
  return 1;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** The N percentage from ingest's base_composition, if it is there. */
function nPercent(facts: Record<string, unknown>): number | null {
  const comp = facts.base_composition;
  if (!Array.isArray(comp)) return null;
  for (const entry of comp) {
    if (entry && typeof entry === "object" && (entry as { base?: string }).base === "N") {
      return num((entry as { percent?: unknown }).percent);
    }
  }
  return null;
}

/** The worst cycle in fastp's per-position N curve, if there is one. */
function worstNCycle(
  facts: Record<string, unknown>,
): { position: number; percent: number } | null {
  const curve = facts.qc_n_per_position;
  if (!Array.isArray(curve)) return null;
  let worst: { position: number; percent: number } | null = null;
  for (const entry of curve) {
    if (!entry || typeof entry !== "object") continue;
    const percent = num((entry as { percent?: unknown }).percent);
    const position = num((entry as { position?: unknown }).position);
    if (percent === null || position === null) continue;
    if (!worst || percent > worst.percent) worst = { position, percent };
  }
  return worst;
}

/**
 * Grade a read file, or null when there is nothing honest to say.
 *
 * Null (the "sixth state") covers three cases that all mean "no grade":
 * the object is not a read file, it is still ingesting, or its quality facts
 * are missing. Rendering nothing reads as "not applicable"; a word like
 * "Unknown" would imply we measured and failed.
 */
export function readQuality(obj: DataObject): ReadQuality | null {
  if (obj.format?.kind !== "fastq") return null;
  if (obj.status !== "ready") return null;

  const facts = (obj.facts ?? {}) as Record<string, unknown>;
  const before = (facts.qc_before_filtering ?? {}) as Record<string, unknown>;

  const q30 = num(before.q30_rate);
  const meanQ = num(facts.mean_quality);

  // Prefer fastp's Q30 -- it is the whole file, where mean_quality is a
  // 200k-read sample and a coarser signal.
  let tier: 1 | 2 | 3 | 4 | 5;
  let basis: string;
  if (q30 !== null) {
    tier = tierFrom(q30, Q30_TIERS);
    basis = `Q30 ${(q30 * 100).toFixed(1)}%`;
  } else if (meanQ !== null) {
    tier = tierFrom(meanQ, MEAN_Q_TIERS);
    basis = `mean Q${meanQ.toFixed(1)}`;
  } else {
    return null;
  }

  const caveats: string[] = [];
  const assay = typeof obj.metadata?.assay === "string" ? obj.metadata.assay : null;

  // Duplication. Suppressed entirely when the assay explains it, because
  // penalising an amplicon library for amplifying is noise, not signal.
  const dup = num(facts.qc_duplication_rate);
  if (dup !== null && dup > DUP_LIMIT && !(assay && HIGH_DUP_EXPECTED.has(assay))) {
    tier = Math.max(1, tier - 1) as 1 | 2 | 3 | 4 | 5;
    caveats.push(
      `${Math.round(dup * 100)}% duplication; normal for amplicon or RNA-seq.`,
    );
  }

  // Ambiguous bases. Assay-independent: no library design wants N.
  //
  // Two rules for one defect, so only one may fire. A collapsed cycle usually
  // drags the whole-file average over the aggregate threshold as well, and
  // demoting twice for one problem overstates it. The spike is the more
  // specific diagnosis -- it names the cycle, which is what makes it
  // actionable -- so it wins and the aggregate rule stands down.
  const spike = worstNCycle(facts);
  if (spike && spike.percent > N_SPIKE_LIMIT) {
    tier = Math.max(1, tier - 1) as 1 | 2 | 3 | 4 | 5;
    caveats.push(
      `Cycle ${spike.position} is ${spike.percent.toFixed(0)}% N; ` +
        "a specific cycle failed.",
    );
  } else {
    const n = nPercent(facts);
    if (n !== null && n > N_LIMIT) {
      tier = Math.max(1, tier - 1) as 1 | 2 | 3 | 4 | 5;
      caveats.push(`${+n.toFixed(2)}% ambiguous (N) bases.`);
    }
  }

  // A healthy average can hide cycles that collapsed at the read's end,
  // which is exactly what trimming fixes -- so it is worth surfacing.
  const minPos = num(facts.min_position_quality);
  if (minPos !== null && minPos < TAIL_COLLAPSE_Q && (meanQ ?? 0) >= HEALTHY_MEAN_Q) {
    tier = Math.max(1, tier - 1) as 1 | 2 | 3 | 4 | 5;
    caveats.push(
      `Quality drops to Q${minPos.toFixed(0)} at some cycles; consider trimming.`,
    );
  }

  const word = WORDS[tier];
  const lines = [`${word} (${tier}/5) — ${basis}`, ...caveats];
  // Only worth suggesting while it would change something: the hint exists to
  // explain a duplication demotion the user can legitimately lift.
  if (!assay && dup !== null && dup > DUP_LIMIT) {
    lines.push("Set Assay under Metadata to refine this score.");
  }

  return { tier, word, basis, caveats, tooltip: lines.join("\n") };
}
