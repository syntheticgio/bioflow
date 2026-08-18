import type {
  BlockedReason,
  JobSummary,
  RunKind,
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

/**
 * What each kind of run *does*, as a phrase to head its card.
 *
 * The stored `label` names the operands and not the verb: an alignment reads
 * "DRR1066343 (paired) → GCF_000146045.2_R64_genomic.fna", which says what
 * went in and what it was aligned against but never says "aligned". Downloads
 * and trims happen to lead with a verb already ("Download SRR37688468 from
 * SRA", "Trim sample (paired)"), so the activity page read as though only some
 * runs knew what they were.
 *
 * Fixed here rather than in the stored label on purpose. `PipelineRun.label`
 * is denormalized so a run stays readable after its inputs are deleted -- it
 * is a record of what was asked for, and rewriting old ones to a new phrasing
 * would be editing history to match today's UI. Deriving the verb from `kind`
 * costs nothing, needs no migration, and fixes every run already in the
 * database.
 *
 * `Record<RunKind, string>` rather than a partial map: a kind with no entry
 * would render a card with no action line and nothing would fail, which is the
 * silent-skip shape CLAUDE.md warns about for enum-keyed registries. This is
 * the derivable case -- every member needs a phrase and no member has data
 * behind it -- so the type makes a missing one a compile error, and
 * `runFormat.test.ts` covers it at runtime for the same reason.
 */
export const KIND_ACTIONS: Record<RunKind, string> = {
  alignment: "Aligning reads to a reference",
  trim: "Trimming adapters and low-quality bases",
  sra_download: "Downloading sequencing runs from SRA",
  variant_calling: "Calling variants against a reference",
  assembly_download: "Downloading a published assembly",
  uniprot_download: "Downloading protein sequences from UniProt",
  quantify: "Counting reads per gene",
  differential_expression: "Testing for differential expression",
  assembly: "Assembling reads into contigs",
  reference_assembly: "Improving an assembly against a reference",
};

/** The action phrase for a run, or undefined for a kind we do not know.
 *
 *  Undefined rather than a guess: the API could serve a `kind` this build has
 *  never heard of (an older frontend against a newer backend), and inventing
 *  "Running alignment" from the raw string would put a machine token where a
 *  sentence goes. The card drops the line instead.
 */
export function kindAction(kind: RunKind): string | undefined {
  return KIND_ACTIONS[kind];
}

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
  // "Tool", matching what `runFacts` calls `run.tool` below. These never
  // appear on one card -- alignment names its tool in params, trim in the
  // run's own `tool` field -- so calling one "Aligner" and the other "Engine"
  // gave the same fact two names depending on which card you were reading.
  if (params.aligner) out.Tool = String(params.aligner);
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

  const extraReads = run.inputs.filter(
    (i) => i.role === "extra_reads" || i.role === "extra_mate",
  );
  if (extraReads.length > 0) {
    facts.push({
      k: "Additional reads",
      v: extraReads.map((i) => i.name).join(" + "),
    });
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

  if (run.tool) facts.push({ k: "Tool", v: run.tool });

  if (run.from_parameter_set) {
    const preset = run.from_parameter_set;
    facts.push({
      k: "Preset",
      v: `${preset.name} (rev ${preset.revision}${preset.edited_after_apply ? ", edited" : ""})`,
    });
  }

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

/** The fields `waitingReason` actually reads. Narrowed so a run member job
 *  — which is not a JobSummary and has no payload — can ask the same
 *  question and get the same words back (#457). */
export type WaitingJob = {
  state: string;
  job_class: string;
  cancel_requested?: boolean;
};

/** MB as the largest unit that keeps the number readable. */
function mb(value: number): string {
  return value >= 1024 ? `${(value / 1024).toFixed(1)} GB` : `${value} MB`;
}

/**
 * The recorded gate as a sentence.
 *
 * Fact rather than inference: these numbers are the ones claim.lua actually
 * compared, so they answer "is it waiting on resources" with the amounts
 * instead of a guess (#457).
 */
export function formatBlockedReason(reason: BlockedReason): string {
  switch (reason.gate) {
    case "cpu":
      return `waiting on CPU — needs ${reason.need}, ${reason.free} free`;
    case "mem":
      return reason.need != null && reason.free != null
        ? `waiting on memory — needs ${mb(reason.need)}, ${mb(reason.free)} free`
        : "waiting on memory";
    case "io":
      return "waiting on disk — another heavy job is reading";
    case "class":
      return "waiting: system loaded";
  }
}

/**
 * True when this job can never be claimed on this machine.
 *
 * Compared against the *total* budget, not free headroom: headroom recovers
 * as other jobs finish, a budget does not. The two need different words
 * because only one of them ends on its own.
 */
export function isUnsatisfiable(
  resources: { mem_mb: number } | null,
  load?: SystemLoad,
): boolean {
  const budget = load?.memory.budget_bytes;
  if (!resources || budget == null) return false;
  return resources.mem_mb * 1024 * 1024 > budget;
}

/** The unsatisfiable sentence, naming both numbers. */
export function unsatisfiableReason(
  resources: { mem_mb: number },
  budget: number,
): string {
  return `cannot start here — needs ${mb(resources.mem_mb)}, this machine's budget is ${mb(
    Math.round(budget / (1024 * 1024)),
  )}`;
}

/**
 * A spinner says "wait"; this says what for. The governor's admitted_classes
 * is authoritative about whether this job's class can start at all.
 */
export function waitingReason(
  job: WaitingJob,
  load?: SystemLoad,
  reason?: BlockedReason | null,
): string {
  if (job.cancel_requested) return "cancelling";
  if (job.state === "delayed") return "retrying after a failure";
  if (job.state === "blocked") return "waiting on an earlier step";
  // A recorded reason is what the queue actually decided; everything below is
  // inference from global state. Kept as the fallback so a cold or expired
  // key degrades to the previous behaviour rather than to a blank (#457).
  if (reason) return formatBlockedReason(reason);
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
