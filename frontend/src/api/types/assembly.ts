import type { AppliedParameterSetIn } from "./parameter-set";
import type { ParamFieldMeta } from "./pipeline";

// Every member of the backend's `Assembler` enum. ABySS and MEGAHIT were
// missing here long after both shipped -- harmless while the dialog only ever
// rendered whatever the server picked, and wrong the moment #785 let a user
// name one, since `AssemblyParams.assembler` is typed from this.
export type AssemblerName =
  | "flye"
  | "hifiasm"
  | "spades"
  | "abyss"
  | "megahit";

/** One row of the assembler picker.
 *
 * `compatible` and `available` are different questions: the listing omits what
 * this build cannot run at all, and flags what it can run but not on these
 * reads. Only the second is rendered, disabled, with its reason. */
export interface SelectableAssembler {
  assembler: AssemblerName;
  compatible: boolean;
  incompatible_reason: string;
  is_default: boolean;
  layout: "single" | "paired";
}

export interface AssemblerListing {
  assemblers: SelectableAssembler[];
}

export interface AssemblerSchema {
  assembler: AssemblerName;
  available: boolean;
  unavailable_reason: string;
  layout: "single" | "paired";
  fields: ParamFieldMeta[];
}

export interface AssemblyParams {
  assembler: AssemblerName;
  /** The running mode, whose choices come from the assembler's schema. SPAdes
   *  spells metagenome mode here (`"meta"`, metaSPAdes) because it is
   *  exclusive with its other modes; Flye spells it in `meta` below because
   *  it is orthogonal to its accuracy mode. */
  mode: string;
  threads: number;
  iterations: number;
  /** Metagenome mode (Flye's `--meta`), for a mixed-community sample rather
   *  than a single organism. Flye only -- see `mode` for SPAdes. */
  meta?: boolean;
  /** k-mer length. ABySS only, and its one substantive knob. Absent here
   *  until #785 gave the dialog a picker: the fields were only ever rendered
   *  from the schema, so nothing typed named them. */
  k?: number;
  /** Contigs shorter than this are not written out. MEGAHIT only. */
  min_contig_len?: number;
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
  resource_override?: boolean;
}
