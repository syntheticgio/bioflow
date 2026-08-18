export type ParamSpecFamily = "aligner" | "assembler";

export interface ParameterSet {
  id: string;
  name: string;
  tool: string;
  family: ParamSpecFamily;
  params: Record<string, unknown>;
  revision: number;
}

export type RejectionReason =
  | "unknown_field"
  | "wrong_kind"
  | "out_of_range"
  | "invalid_choice";

export interface RejectedParam {
  key: string;
  reason: RejectionReason;
  detail: string;
  value: unknown;
}

export interface ResolveResult {
  applied: Record<string, unknown>;
  rejected: RejectedParam[];
  set: { id: string; name: string; revision: number };
}

/** Mirrors backend `AppliedParameterSet` (app/models/run.py), snake_cased for
 *  the wire. Sent on a launch request when the dialog applied a saved set,
 *  so the run can record which one configured it and whether the user then
 *  changed anything before launching. */
export interface AppliedParameterSetIn {
  set_id: string;
  name: string;
  revision: number;
  edited_after_apply: boolean;
}
