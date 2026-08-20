/* --- Expression: counting and differential testing ----------------------- */

/** featureCounts' -s. 0 unstranded, 1 forward, 2 reverse. */
export type Strandedness = 0 | 1 | 2;

/** Mirrors counts_runner.CountsParams. */
export interface CountsParams {
  threads: number;
  strandedness: Strandedness;
  strandedness_label: string;
  /** Counts fragments rather than reads. Wrong on paired data doubles
   * every count, which is why the dialog shows where the value came from. */
  paired: boolean;
  feature_type: string;
  attribute: string;
  count_multi_mapping: boolean;
}

export interface QuantifyDefaults {
  params: CountsParams | Record<string, never>;
  annotation_id: string | null;
  annotation_name: string | null;
  /** True when the project has several annotations and none could be picked. */
  needs_annotation: boolean;
  annotations: { id: string; name: string; kind: string }[];
  /** "alignment" when read off the aligner's --rna-strandness, else
   * "default" -- the dialog says which, because a guess and a derived value
   * deserve different confidence. */
  strandedness_source: "alignment" | "default";
  paired_source: "flagstat" | "alignment";
  available: boolean;
  max_threads: number;
}

export interface QuantifyRequest {
  bam_id: string;
  annotation_id?: string | null;
  params?: Partial<CountsParams>;
  resource_override?: boolean;
}

/** One counts file, as the DE dialog sees it before a design is chosen. */
export interface DeSample {
  object_id: string;
  name: string;
  sample: string;
  /** From the `condition` metadata field. Empty when never tagged. */
  condition: string;
  assigned_pct: number | null;
  genes_detected: number | null;
  /** Samples counted against different annotations cannot be merged. */
  annotation_sha256: string | null;
  annotation_name: string | null;
}

export interface DeDefaults {
  samples: DeSample[];
  conditions: string[];
  /** Pre-filled only when exactly two conditions are present. */
  contrast: { test: string; reference: string } | null;
  min_replicates: number;
  available: boolean;
}

export interface DeRequest {
  project_id: string;
  /** counts object id -> condition name. */
  design: Record<string, string>;
  contrast: { test: string; reference: string };
  threads?: number | null;
  resource_override?: boolean;
}

/** One assembled-transcript GTF, as the merge dialog lists it. */
export interface MergeTranscript {
  object_id: string;
  name: string;
  transcript_count: number | null;
  novel_transcript_count: number | null;
  gene_count: number | null;
}

export interface MergeTranscriptsDefaults {
  assemblies: MergeTranscript[];
  available: boolean;
}

export interface MergeTranscriptsRequest {
  project_id: string;
  /** The N assembled-transcript GTFs to merge. */
  gtf_object_ids: string[];
  reference_id?: string | null;
  output_name?: string | null;
  resource_override?: boolean;
}


/** One gene's result. Nulls are real: DESeq2 leaves padj unset for genes it
 * filtered out of multiple-testing correction. */
export interface DeRow {
  gene: string;
  base_mean: number | null;
  log2_fold_change: number | null;
  lfc_std_error: number | null;
  stat: number | null;
  p_value: number | null;
  padj: number | null;
}

export interface DeResultsPage {
  rows: DeRow[];
  total: number;
  offset: number;
  limit: number;
}
