import type { JobState, JobSummary } from "./job";
import type { ParamFieldMeta } from "./pipeline";
import type { RunStatus } from "./run";

/* ---------------------------------------------------------------- workflows */

/** What may flow down a wire. Mirrors the backend `PortType`: `role: null`
 *  means "any role for this format", which is the honest type for a port that
 *  genuinely does not care -- QC's, for instance. */
export interface PortType {
  /** Set when the port names exactly one format -- how nearly every port is
   *  declared. Null when it names several, in which case read `formats`. */
  format: string | null;
  /** Set when the port accepts several formats (annotation export takes
   *  GFF/GTF/BED but refuses GenBank). Null for single-format ports. */
  formats?: string[] | null;
  role: string | null;
}

export interface PortMeta {
  name: string;
  type: PortType;
  required: boolean;
  /** Whether this port takes several wires, collected into a list for the
   *  launcher. Only the one-wire-per-port rule relaxes -- type checking still
   *  applies to each wire. */
  multiple?: boolean;
}

export interface ToolOption {
  value: string;
  label: string;
}

export interface ToolChoice {
  /** Where the chosen tool lives in `node.params`. */
  param_key: string;
  options: ToolOption[];
  default: string;
}

export interface PortSet {
  inputs: PortMeta[];
  outputs: PortMeta[];
}

/** One entry of the canvas palette, served by `/workflows/node-types`.
 *  Generated from the backend registry rather than hand-listed here, so a tool
 *  added there reaches the canvas without a second edit. */
export interface NodeTypeMeta {
  node_type: string;
  label: string;
  /** The default port set -- what a freshly-dropped node has. */
  inputs: PortMeta[];
  outputs: PortMeta[];
  /** Null for node types that run exactly one tool. */
  tool_choice?: ToolChoice | null;
  /** Every option's ports, keyed by tool value. Lets the canvas re-shape a
   *  node on a dropdown change with no round trip. */
  ports_by_tool?: Record<string, PortSet>;
  /** Fields declared on the spec, for node types whose parameters do not
   *  vary by tool. Empty for most. The aligner's per-tool schema is fetched
   *  separately. */
  param_fields?: ParamFieldMeta[];
}

export interface NodePosition {
  x: number;
  y: number;
}

export interface WorkflowNode {
  node_id: string;
  kind: "input" | "action";
  /** ACTION only: keys into the palette. */
  node_type?: string | null;
  params: Record<string, unknown>;
  continue_on_failure: boolean;
  position?: NodePosition;
  /** INPUT only. The label is why input nodes are explicit rather than implied
   *  by an unwired port: "tumor reads" and "normal reads" are the same type and
   *  only a name tells them apart. */
  label?: string | null;
  accepts?: PortType | null;
  /** INPUT only. A slot that binds several files, whose single outgoing wire
   *  carries the set. May only feed a multi port. */
  multiple?: boolean;
}

export interface WorkflowEdge {
  from_node: string;
  from_port: string;
  to_node: string;
  to_port: string;
}

export interface WorkflowDefinition {
  id: string;
  name: string;
  description: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  version: number;
}

export interface WorkflowDefinitionInput {
  name: string;
  description: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
}

export interface WorkflowRunSummary {
  id: string;
  definition_id: string;
  definition_version: number;
  label: string;
  status: string;
}

/** One problem with a saved graph. `node_id`/`port` are what let the canvas
 *  mark the offending node rather than only showing a message. */
export interface GraphValidationError {
  code: string;
  message: string;
  node_id: string | null;
  port: string | null;
}

export interface SkippedRun {
  run_id: string;
  label: string;
  reason: string;
}

/** An unsaved graph derived from previous runs. `skipped` is the part that
 *  must be shown: a run the canvas cannot represent is reported, never
 *  silently dropped. */
export interface DerivedGraph {
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  skipped: SkippedRun[];
}

/** One workflow run in the activity listing.
 *
 * `status` is derived server-side from node states, never stored -- the same
 * vocabulary as `RunStatus`, so the existing STATUS_LABELS apply. The counts
 * ride along so a collapsed row reads "1 of 3" without a second request. */
export interface WorkflowRunRow {
  id: string;
  definition_id: string;
  definition_version: number;
  project_id: string;
  label: string;
  status: RunStatus;
  node_total: number;
  node_done: number;
  node_failed: number;
  created_at: string;
  updated_at: string;
}

export interface WorkflowNodeJob {
  job_id: string;
  /** Null when the job has been pruned by the 30-day TTL. */
  type: string | null;
  state: JobState | null;
  progress: JobSummary["progress"] | null;
  error: { code: string; message: string } | null;
}

export interface WorkflowNodeRow {
  node_id: string;
  kind: "input" | "action";
  node_type: string | null;
  label: string;
  state: "pending" | "running" | "succeeded" | "failed" | "cancelled" | "skipped";
  attempt: number;
  /** Set only for the 9 node types that create a PipelineRun. */
  run_id: string | null;
  jobs: WorkflowNodeJob[];
  outputs: string[];
}

export interface WorkflowRunDetail {
  id: string;
  definition_id: string;
  label: string;
  status: RunStatus;
  nodes: WorkflowNodeRow[];
}
