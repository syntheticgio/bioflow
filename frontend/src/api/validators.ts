/** Runtime guards for API responses that drive irreversible or destructive UI
 *  actions (delete confirmations, run-launch confirmations). Everywhere else,
 *  the compile-time types in `types.ts` are trusted as-is -- see issue #408.
 *
 *  Each `assertX` throws a plain `Error` naming the exact field and what was
 *  found instead of what was expected, so a shape mismatch surfaces at the
 *  fetch boundary rather than as a crash deep inside a render. */
import type { ActiveJob, DeletionPreview, RunInput, RunSummary, WorkflowRunSummary } from "./types";

function fail(context: string, field: string, expected: string, value: unknown): never {
  throw new Error(
    `${context}: expected \`${field}\` to be ${expected}, got ${describe(value)}`,
  );
}

function describe(value: unknown): string {
  if (value === undefined) return "undefined";
  if (value === null) return "null";
  return `${typeof value} (${JSON.stringify(value)})`;
}

function isString(v: unknown): v is string {
  return typeof v === "string";
}

function isNumber(v: unknown): v is number {
  return typeof v === "number";
}

function isBoolean(v: unknown): v is boolean {
  return typeof v === "boolean";
}

function assertActiveJob(v: unknown, index: number): asserts v is ActiveJob {
  const context = `DeletionPreview.active_jobs[${index}]`;
  if (typeof v !== "object" || v === null) fail(context, "(item)", "an object", v);
  const j = v as Record<string, unknown>;
  if (!isString(j.id)) fail(context, "id", "a string", j.id);
  if (!isString(j.job_type)) fail(context, "job_type", "a string", j.job_type);
  if (!isString(j.state)) fail(context, "state", "a string", j.state);
}

/** Guards the response behind the project-delete confirmation dialog
 *  (`ProjectDangerZone`) -- counts, byte totals, block state, and the active
 *  jobs list are rendered directly from this shape. */
export function assertDeletionPreview(v: unknown): asserts v is DeletionPreview {
  const context = "DeletionPreview";
  if (typeof v !== "object" || v === null) fail(context, "(response)", "an object", v);
  const p = v as Record<string, unknown>;

  if (!Array.isArray(p.project_ids)) fail(context, "project_ids", "an array", p.project_ids);
  if (!isNumber(p.child_project_count))
    fail(context, "child_project_count", "a number", p.child_project_count);
  if (!isNumber(p.object_count)) fail(context, "object_count", "a number", p.object_count);
  if (!isNumber(p.total_bytes)) fail(context, "total_bytes", "a number", p.total_bytes);
  if (!isNumber(p.run_count)) fail(context, "run_count", "a number", p.run_count);
  if (!isNumber(p.job_count)) fail(context, "job_count", "a number", p.job_count);
  if (!isNumber(p.upload_session_count))
    fail(context, "upload_session_count", "a number", p.upload_session_count);
  if (!isBoolean(p.blocked)) fail(context, "blocked", "a boolean", p.blocked);

  if (!Array.isArray(p.active_jobs)) fail(context, "active_jobs", "an array", p.active_jobs);
  p.active_jobs.forEach((job, i) => assertActiveJob(job, i));
}

/** Guards the response shown as the run-launch confirmation in
 *  `WorkflowCanvas` ("Launched "{label}" — {status}."). */
export function assertWorkflowRunSummary(v: unknown): asserts v is WorkflowRunSummary {
  const context = "WorkflowRunSummary";
  if (typeof v !== "object" || v === null) fail(context, "(response)", "an object", v);
  const r = v as Record<string, unknown>;

  if (!isString(r.id)) fail(context, "id", "a string", r.id);
  if (!isString(r.definition_id)) fail(context, "definition_id", "a string", r.definition_id);
  if (!isNumber(r.definition_version))
    fail(context, "definition_version", "a number", r.definition_version);
  if (!isString(r.label)) fail(context, "label", "a string", r.label);
  if (!isString(r.status)) fail(context, "status", "a string", r.status);
}

function assertRunInput(v: unknown, index: number): asserts v is RunInput {
  const context = `RunSummary.inputs[${index}]`;
  if (typeof v !== "object" || v === null) fail(context, "(item)", "an object", v);
  const i = v as Record<string, unknown>;
  if (!isString(i.object_id)) fail(context, "object_id", "a string", i.object_id);
  if (!isString(i.name)) fail(context, "name", "a string", i.name);
  if (!isString(i.role)) fail(context, "role", "a string", i.role);
}

/** Guards the run summaries shown in the activity ledger (`RunLedger`) --
 *  "what was queued, against which inputs". Does not cover the `jobs` field
 *  `RunDetail` adds; the ledger's list/expand view only needs `RunSummary`. */
export function assertRunSummary(v: unknown): asserts v is RunSummary {
  const context = "RunSummary";
  if (typeof v !== "object" || v === null) fail(context, "(response)", "an object", v);
  const r = v as Record<string, unknown>;

  if (!isString(r.id)) fail(context, "id", "a string", r.id);
  if (!isString(r.kind)) fail(context, "kind", "a string", r.kind);
  if (!isString(r.project_id)) fail(context, "project_id", "a string", r.project_id);
  if (!isString(r.label)) fail(context, "label", "a string", r.label);
  if (!isString(r.status)) fail(context, "status", "a string", r.status);
  if (r.tool !== null && !isString(r.tool)) fail(context, "tool", "a string or null", r.tool);
  if (!Array.isArray(r.outputs)) fail(context, "outputs", "an array", r.outputs);
  if (!isString(r.created_at)) fail(context, "created_at", "a string", r.created_at);
  if (!isString(r.updated_at)) fail(context, "updated_at", "a string", r.updated_at);

  if (!Array.isArray(r.inputs)) fail(context, "inputs", "an array", r.inputs);
  r.inputs.forEach((input, i) => assertRunInput(input, i));
}

/** Validates every element of a list response with a single-item asserter,
 *  e.g. `assertRunSummaryList(runs)` for `api.listRuns()`. */
export function assertEach<T>(
  assertItem: (v: unknown) => asserts v is T,
  items: unknown[],
): asserts items is T[] {
  items.forEach(assertItem);
}
