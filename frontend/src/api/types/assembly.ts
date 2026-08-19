import type { AppliedParameterSetIn } from "./parameter-set";
import type { ParamFieldMeta } from "./pipeline";

export type AssemblerName = "flye" | "hifiasm" | "spades";

export interface AssemblerSchema {
  assembler: AssemblerName;
  available: boolean;
  unavailable_reason: string;
  layout: "single" | "paired";
  fields: ParamFieldMeta[];
}

export interface AssemblyParams {
  assembler: AssemblerName;
  mode: string;
  threads: number;
  iterations: number;
  /** Bases. Null when nothing in the project could say, which is the normal
   *  case for de novo work rather than a misconfiguration. */
  genome_size?: number | null;
  /** Where the number came from. "inferred" is what the dialog labels, so a
   *  guess is never shown as though it were measured. */
  genome_size_source?: "unset" | "user" | "inferred";
  /** The assembly the inferred size was read off, e.g. "R64 (GCF_000146045.2)".
   *  Names the assembly rather than the file, since every component of one
   *  download carries the same figure. */
  genome_size_from?: string;
}

export interface AssembleRequest {
  object_id: string;
  params?: Partial<AssemblyParams>;
  resource_override?: boolean;
  /** Present only when the dialog applied a saved parameter set. */
  from_parameter_set?: AppliedParameterSetIn;
}

export interface CompletenessDefaults {
  organism: string | null;
  /** The inferred lineage name, or null when there is nothing to infer from --
   *  the dialog then requires the user to pick one rather than guessing. */
  lineage: string | null;
  odb: string;
  /** Whether `lineage` is a genus/family-level match rather than the broad
   *  domain fallback (bacteria/eukaryota) -- the dialog says so, the same
   *  "inferred, labelled as inferred" honesty the assemble dialog's genome
   *  size uses. */
  specific: boolean;
}

export interface CompletenessRequest {
  object_id: string;
  lineage?: string | null;
  odb?: string | null;
  resource_override?: boolean;
}

export interface LineageDownloadRequest {
  lineage: string;
  odb?: string | null;
}

export interface ScaffoldRequest {
  draft_object_id: string;
  reference_object_id?: string | null;
  divergence?: string | null;
  resource_override?: boolean;
}

export interface LineageStatus {
  lineage: string;
  odb: string;
  present: boolean;
}

/** One Kraken2 database choice for the classify-reads dialog, as returned by
 *  `GET /pipelines/kraken-dbs` -- `present` is a disk probe, not a static
 *  fact, so it can flip between requests as downloads complete. */
export interface KrakenDbInfo {
  key: string;
  label: string;
  description: string;
  download_bytes: number;
  present: boolean;
}

export interface ClassifyReadsRequest {
  object_id: string;
  db_key: string;
  mate_object_id?: string;
}
