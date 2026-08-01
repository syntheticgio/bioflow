import type { RunStatus, RunSummary } from "../api/types";

/**
 * The shared vocabulary for describing a run to a person.
 *
 * Lives here rather than beside one component because two views now render the
 * same run in different shapes -- the activity page's lead story and its ledger
 * -- and a run labelled "Build index" in one place and "index" in the other
 * would read as two different things happening.
 */

/** Job roles, named for what the step does rather than what it is called. */
export const ROLE_LABELS: Record<string, string> = {
  index: "Build index",
  align: "Align",
  trim: "Trim",
  index_bam: "Index BAM",
  ingest: "Read headers",
};

export const STATUS_LABELS: Record<RunStatus, string> = {
  waiting: "waiting",
  running: "running",
  succeeded: "succeeded",
  failed: "failed",
  partial: "partial",
};

/** The parameters worth showing in a summary, labelled for a person. */
export function describeParams(params: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {};
  if (params.aligner) out.Aligner = String(params.aligner);
  if (params.preset) out["Read type"] = String(params.preset);
  if (params.threads) out.Threads = String(params.threads);
  if (params.mark_duplicates) out.Duplicates = "marked";

  const rg = params.read_group as Record<string, unknown> | undefined;
  if (rg?.sample) {
    out["Read group"] = [rg.sample, rg.library, rg.platform]
      .filter(Boolean)
      .join(" · ");
  }

  // Trim parameters, which share the row with alignment ones.
  if (params.min_length) out["Min length"] = String(params.min_length);
  if (params.quality_threshold) out["Min quality"] = String(params.quality_threshold);
  return out;
}

/** A label/value pair as the activity page's param grids want it. */
export interface RunFact {
  k: string;
  v: string;
}

/**
 * Everything a run's parameter grid shows, in reading order: what went in, how
 * it was configured, what came out, and what ran it.
 *
 * Inputs come first because they are what the user picked; `describeParams`
 * settings follow. `tool` is last and often absent -- the API sets it only for
 * trim runs (`RunSummary.tool`), and it carries no version, so this renders
 * "fastp" rather than inventing "fastp 0.24".
 */
export function runFacts(run: RunSummary): RunFact[] {
  const facts: RunFact[] = [];

  const reads = run.inputs.filter((i) => i.role === "reads" || i.role === "mate");
  if (reads.length > 0) {
    facts.push({ k: "Reads", v: reads.map((i) => i.name).join(" + ") });
  }

  const reference = run.inputs.find((i) => i.role === "reference");
  if (reference) facts.push({ k: "Reference", v: reference.name });

  for (const [k, v] of Object.entries(describeParams(run.params))) {
    facts.push({ k, v });
  }

  if (run.outputs.length > 0) {
    facts.push({
      k: "Produced",
      v: `${run.outputs.length} ${run.outputs.length === 1 ? "file" : "files"}`,
    });
  }

  if (run.tool) facts.push({ k: "Engine", v: run.tool });

  return facts;
}
