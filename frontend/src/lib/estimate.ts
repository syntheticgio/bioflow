import type { AlignEnvelope, MemoryModel } from "../api/types";

export type Band = "ok" | "warn" | "block";

/** Mirrors resource_estimator.WARN_FRACTION -- a heuristic, like the MemoryModel coefficients. */
const WARN_FRACTION = 0.7;

/**
 * The client half of the resource check.
 *
 * This duplicates arithmetic that also lives in Python, which is a real cost
 * -- but the alternative was a request per keystroke, and the coefficients
 * (not the formula) are what ship from the server, so the two stay in step as
 * long as this file matches `resource_estimator.py`. The backend re-runs the
 * authoritative check at launch, so a drift here degrades the preview rather
 * than letting a bad run through.
 */
export function estimateMb(
  model: MemoryModel,
  opts: {
    referenceBases: number;
    threads: number;
    sortMemoryMb: number;
    buildingIndex: boolean;
  },
): number {
  let indexMb =
    (opts.referenceBases * model.index_bytes_per_ref_base) / (1024 * 1024);
  if (opts.buildingIndex) indexMb *= model.index_build_multiplier;

  const workerMb = opts.threads * model.bytes_per_thread_mb;
  const sortMb = opts.threads * opts.sortMemoryMb;

  // Round up, not toward zero -- mirrors resource_estimator.py's math.ceil.
  // classify()'s BLOCK check is a strict `>`, so truncating a fractional
  // total down could turn a genuine BLOCK into a WARN.
  return Math.ceil(model.fixed_overhead_mb + indexMb + workerMb + sortMb);
}

export function classify(opts: {
  estimateMb: number;
  memBudgetMb: number | null;
  threads: number;
  cpuBudget: number | null;
}): Band {
  if (opts.memBudgetMb == null) return "ok";
  if (opts.estimateMb > opts.memBudgetMb) return "block";
  if (opts.estimateMb >= opts.memBudgetMb * WARN_FRACTION) return "warn";
  if (opts.cpuBudget != null && opts.threads > opts.cpuBudget) return "warn";
  return "ok";
}

/** The sentence shown in the banner. Names the dominant term, as the backend does. */
export function explain(
  model: MemoryModel,
  envelope: AlignEnvelope,
  opts: {
    threads: number;
    sortMemoryMb: number;
    buildingIndex: boolean;
  },
): string {
  const total = estimateMb(model, {
    referenceBases: envelope.reference_bases,
    threads: opts.threads,
    sortMemoryMb: opts.sortMemoryMb,
    buildingIndex: opts.buildingIndex,
  });

  const sortMb = opts.threads * opts.sortMemoryMb;
  const workerMb = opts.threads * model.bytes_per_thread_mb;
  let indexMb = Math.floor(
    (envelope.reference_bases * model.index_bytes_per_ref_base) / (1024 * 1024),
  );
  if (opts.buildingIndex) indexMb = Math.floor(indexMb * model.index_build_multiplier);

  const budget = envelope.mem_budget_mb
    ? ` of ${envelope.mem_budget_mb.toLocaleString()} MB available`
    : "";

  // worker_mb is folded into the non-sort side (the aligner's own footprint),
  // not compared against indexMb alone -- for a high-thread run, workerMb can
  // dominate, and comparing sort vs. index-only would misattribute the cause.
  // Mirrors resource_estimator.py's explain() exactly.
  const alignerSideMb = indexMb + workerMb + model.fixed_overhead_mb;
  const dominant =
    sortMb >= alignerSideMb
      ? `The sort buffer is ${sortMb.toLocaleString()} MB of that (${opts.threads} threads x ${opts.sortMemoryMb} MB each).`
      : `Most of it is ${opts.buildingIndex ? "building the index" : "the aligner itself"}: about ${Math.round(alignerSideMb).toLocaleString()} MB.`;

  return `Estimated ${total.toLocaleString()} MB${budget}. ${dominant}`;
}
