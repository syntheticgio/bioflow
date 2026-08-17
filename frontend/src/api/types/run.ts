import type { JobClass, JobState, JobSummary } from "./job";

/** What a user asked for, and the jobs that served it.
 *
 * Mirrors the backend `RunKind` enum. Nothing switches exhaustively on this
 * today, which is why `assembly_download` went missing here for a while
 * without anything failing -- a reason to keep it in step deliberately rather
 * than a reason not to bother.
 */
export type RunKind =
  | "alignment"
  | "trim"
  | "sra_download"
  | "variant_calling"
  | "assembly_download"
  | "uniprot_download"
  | "quantify"
  | "differential_expression"
  | "assembly"
  | "reference_assembly";

/** Derived from member job states on the server, never stored. */
export type RunStatus =
  | "waiting"
  | "running"
  | "succeeded"
  | "failed"
  /** Finished, but an optional step (a header parse) did not succeed. */
  | "partial";

export type RunInputRole =
  | "reads"
  | "mate"
  | "reference"
  | "draft_assembly"
  | "alignment"
  | "primers"
  | "annotation"
  /** Appears many times in one run's inputs -- a DE run has one per sample. */
  | "counts"
  /** An additional read set's R1, concatenated into the alignment's R1 stream. */
  | "extra_reads"
  /** An additional read set's mate, concatenated into the alignment's R2 stream. */
  | "extra_mate";

export type RunJobRole =
  | "index"
  | "align"
  | "trim"
  | "index_bam"
  | "ingest"
  | "download"
  | "qc"
  | "call_variants"
  | "quantify"
  | "test"
  | "consensus"
  | "polish"
  | "scaffold";

export interface RunInput {
  object_id: string;
  name: string;
  role: RunInputRole;
}

export interface RunSummary {
  id: string;
  kind: RunKind;
  project_id: string;
  label: string;
  status: RunStatus;
  inputs: RunInput[];
  params: Record<string, unknown>;
  /** Which tool ran a trim run. Null for non-trim runs. */
  tool: string | null;
  outputs: string[];
  created_at: string;
  updated_at: string;
}

export interface RunMemberJob {
  job_id: string;
  role: RunJobRole;
  /** True when this run reused a job another run created. */
  shared: boolean;
  /** Null once the job has been pruned by the 30-day TTL. */
  type: string | null;
  state: JobState | null;
  /** Null for a pruned job. Drives the governor branch of waitingReason. */
  job_class: JobClass | null;
  cancel_requested: boolean;
  progress: JobSummary["progress"] | null;
  error: { code: string; message: string; retryable: boolean } | null;
  created_at: string | null;
  /** Declared demand. Null for a pruned job. */
  resources: { cpu: number; mem_mb: number; io: string } | null;
}

export interface RunDetail extends RunSummary {
  jobs: RunMemberJob[];
}
