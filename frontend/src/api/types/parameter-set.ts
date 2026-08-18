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
