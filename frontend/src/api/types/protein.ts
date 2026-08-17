export interface ProteinRecordRow {
  ordinal: number;
  identifier: string;
  description: string;
  length: number;
  /** Whether the header named an accession at all. Distinguishes "nothing to
   *  look up" from "looked up and found no structure" without a round trip
   *  per row. */
  has_reference: boolean;
}

export interface ProteinRecords {
  total: number;
  /** The file held more records than the index cap, so this list is partial. */
  truncated: boolean;
  rows: ProteinRecordRow[];
}

/** Which of four sentences the viewer shows. Sent by the server rather than
 *  derived here: "no structure deposited" and "UniProt was unreachable" need
 *  different copy and only one of them is retryable. */
export type ProteinStructureState =
  | "resolved"
  | "no_structure"
  | "no_reference"
  | "lookup_failed";

export interface ProteinStructure {
  identifier: string;
  state: ProteinStructureState;
  accession: string | null;
  protein_name: string | null;
  pdb_ids: string[];
}
