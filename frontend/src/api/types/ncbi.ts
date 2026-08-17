import type { ObjectRole } from "./object";

// --- NCBI SRA ---

/** One sequencing run: the unit that can actually be downloaded. */
export interface SraRunInfo {
  accession: string;
  experiment: string | null;
  sample: string | null;
  study: string | null;
  bioproject: string | null;
  biosample: string | null;
  platform: string | null;
  instrument: string | null;
  library_strategy: string | null;
  library_layout: string | null;
  library_source: string | null;
  spots: number | null;
  bases: number | null;
  /** Archive size from NCBI, not an estimate. Drives the size column. */
  bytes: number | null;
  organism: string | null;
  title: string | null;
  sample_attributes: Record<string, string>;
  /** Already in this project. Shown greyed out rather than hidden. */
  already_downloaded: boolean;
}

export interface SraHierarchyNode {
  accession: string;
  kind: string;
  title: string | null;
  platform: string | null;
  organism: string | null;
  child_count: number;
  total_bases: number | null;
}

export interface SraResolveResponse {
  accession: string;
  kind: string;
  title: string | null;
  organism: string | null;
  hierarchy: SraHierarchyNode[];
  runs: SraRunInfo[];
  total_run_count: number;
  total_bytes_estimate: number | null;
  /** The study holds more runs than the server will resolve in one go. */
  truncated: boolean;
  /** Set on "nothing found" and on a filter that excluded everything. */
  error: string | null;
}

export interface SraDownloadRequest {
  project_id: string;
  run_accessions: string[];
  run_qc?: boolean;
}

export interface SraAccepted {
  run_id: string;
  download_job_ids: string[];
  /** Runs already in flight, so no new job was created for them. */
  skipped: string[];
}

// --- NCBI unified resolve (assembly branch) ---

/** One downloadable part of an assembly. */
export interface AssemblyComponent {
  key: "genome" | "gff3" | "protein" | "cds";
  label: string;
  role: ObjectRole;
  available: boolean;
  size_bytes: number | null;
  /** Why it is unavailable. Present only when `available` is false. */
  reason: string | null;
}

export interface AssemblyResolveResponse {
  accession: string;
  organism: string | null;
  tax_id: number | null;
  strain: string | null;
  assembly_name: string | null;
  assembly_level: string | null;
  submitter: string | null;
  release_date: string | null;
  bioproject: string | null;
  paired_accession: string | null;
  total_length: number | null;
  scaffold_count: number | null;
  contig_count: number | null;
  gc_percent: number | null;
  scaffold_n50: number | null;
  components: AssemblyComponent[];
  already_downloaded: boolean;
  error: string | null;
}

/**
 * One accession, two possible answers. `kind` says which branch is populated
 * so the dialog never has to infer it from the shape.
 */
export interface NcbiResolveResponse {
  kind: string;
  sra: SraResolveResponse | null;
  assembly: AssemblyResolveResponse | null;
}

export interface AssemblyAccepted {
  run_id: string;
  download_job_ids: string[];
}

/** One candidate organism from NCBI's taxon_suggest, for autocomplete. */
export interface OrganismSuggestion {
  sci_name: string;
  tax_id: number;
  common_name: string | null;
  rank: string | null;
  group_name: string | null;
}

export interface OrganismSuggestResponse {
  suggestions: OrganismSuggestion[];
}

/**
 * A row in an organism's assembly list. Lighter than `AssemblyResolveResponse`:
 * no `components`, since that needs a CLI shellout per accession and this can
 * be a page of up to 20. Picking one assembly for its component picker goes
 * back through the existing single-accession `/ncbi/resolve` path.
 */
export interface OrganismAssemblySummary {
  accession: string | null;
  organism: string | null;
  tax_id: number | null;
  strain: string | null;
  assembly_name: string | null;
  assembly_level: string | null;
  submitter: string | null;
  release_date: string | null;
  /** NCBI's own pick for this organism: "reference genome" or
   *  "representative genome". Null for every other assembly. */
  refseq_category: string | null;
  total_length: number | null;
  scaffold_count: number | null;
  gc_percent: number | null;
  already_downloaded: boolean;
}

export interface OrganismSearchRequest {
  tax_id: number;
  sci_name: string;
  project_id?: string | null;
  assembly_page_token?: string | null;
  sra_offset?: number;
  page_size?: number;
  /** ILLUMINA | PACBIO_SMRT | OXFORD_NANOPORE, or null for everything. Only
   *  applies to sequencing runs -- an assembly has no platform of its own. */
  platform_filter?: string | null;
  /** NCBI's own assembly_level vocabulary, e.g. "Complete Genome". Only
   *  applies to the assembly list. */
  assembly_level?: string | null;
  /** Which table this request wants back. "both" is the initial search;
   *  paging either table's own pager narrows to that table alone. */
  section?: "both" | "assemblies" | "sra";
}

export interface OrganismSearchResponse {
  tax_id: number;
  sci_name: string | null;
  assemblies: OrganismAssemblySummary[];
  assemblies_next_page_token: string | null;
  sra_runs: SraRunInfo[];
  sra_total_count: number;
  sra_next_offset: number | null;
  error: string | null;
}

// --- UniProt ---

/** One proteome, as the download dialog's card and picker render it. */
export type UniProtProteome = {
  id: string;
  name: string;
  taxon_id: number | null;
  strain: string | null;
  protein_count: number | null;
  is_reference: boolean;
  /** Completeness, which is what makes choosing between strains possible. */
  busco_score: number | null;
  /** The NCBI assembly this proteome's genome came from, when UniProt names
   *  one. Offered as a link to the other dialog rather than a joint download. */
  genome_assembly: string | null;
  /** Both counts, so the reviewed/unreviewed difference is visible before the
   *  download rather than discovered after it -- roughly sevenfold for human,
   *  and identical for a fully curated organism like yeast. Null when the
   *  count request failed; the card then omits the choice rather than
   *  guessing. */
  reviewed_count: number | null;
  total_count: number | null;
};

export type UniProtProtein = {
  accession: string;
  entry_id: string | null;
  name: string | null;
  organism: string | null;
  length: number | null;
  reviewed: boolean;
};

export type UniProtResolveResponse = {
  kind: "proteome" | "proteins" | "empty";
  proteome: UniProtProteome | null;
  /** Other proteomes for the same organism. Populated on both branches: behind
   *  a disclosure when a reference proteome was found, and as the whole answer
   *  when it was not (taxon 4932 has none, but 360 sit behind it). */
  candidates: UniProtProteome[];
  needs_picker: boolean;
  proteins: UniProtProtein[];
  message: string | null;
};

export type UniProtAccepted = {
  run_id: string;
  job_ids: string[];
};
