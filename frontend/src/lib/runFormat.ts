import type {
  JobSummary,
  RunStatus,
  RunSummary,
  SystemLoad,
  WorkflowRunRow,
} from "../api/types";

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

/**
 * What counts as in flight, split three ways because the three mean
 * different things to a person.
 *
 * `BLOCKED` is separate rather than folded into `WAITING` because the answer
 * to "why isn't this running" differs: a waiting job is queued behind the
 * machine, a blocked one is queued behind another job. The activity page can
 * derive "recent" by negating running and waiting because a blocked job is
 * shown inside its run's card there; a flat list has no such grouping, so it
 * must claim blocked positively or show it as though it had finished.
 */
export const RUNNING = new Set(["running"]);
export const WAITING = new Set(["pending", "queued", "delayed"]);
export const BLOCKED = new Set(["blocked"]);

/** True for any job that has not reached a terminal state. */
export function isInFlight(state: string): boolean {
  return RUNNING.has(state) || WAITING.has(state) || BLOCKED.has(state);
}

/**
 * A spinner says "wait"; this says what for. The governor's admitted_classes
 * is authoritative about whether this job's class can start at all.
 */
export function waitingReason(job: JobSummary, load?: SystemLoad): string {
  if (job.cancel_requested) return "cancelling";
  if (job.state === "delayed") return "retrying after a failure";
  if (job.state === "blocked") return "waiting on an earlier step";
  if (!load) return "waiting";
  if (!load.admitted_classes.includes(job.job_class)) {
    return load.state === "CLOSED"
      ? "waiting: system loaded"
      : "waiting: system busy";
  }
  return "waiting for a free slot";
}

/** The file a job is about, falling back to its type. */
export function jobLabel(job: JobSummary): string {
  const payload = job.payload as Record<string, unknown>;
  const name = payload.r1_name ?? payload.name;
  if (typeof name === "string" && name) {
    const mate = payload.r2_name;
    return typeof mate === "string" && mate ? `${name} + ${mate}` : name;
  }
  return job.type;
}

/** A ledger line, from either kind of run. */
export type LedgerLine =
  | { kind: "run"; at: string; run: RunSummary }
  | { kind: "workflow"; at: string; run: WorkflowRunRow };

/**
 * Runs and workflow runs as one list, most recent first.
 *
 * Merged rather than concatenated (#93): the ledger reads as "what finished,
 * most recent first", and a block of workflows pinned to the end would break
 * that whatever their timestamps say. `updated_at` is an ISO-8601 UTC string
 * from the API, so a lexicographic compare is a chronological one and avoids
 * parsing a Date per row per render.
 */
export function mergeLedgerLines(
  runs: RunSummary[],
  workflows: WorkflowRunRow[],
): LedgerLine[] {
  return [
    ...runs.map((run) => ({ kind: "run" as const, at: run.updated_at, run })),
    ...workflows.map((run) => ({
      kind: "workflow" as const,
      at: run.updated_at,
      run,
    })),
  ].sort((a, b) => b.at.localeCompare(a.at));
}
