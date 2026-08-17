/** One row of the annotation feature table. */
export interface AnnotationFeature {
  contig: string;
  start: number;
  end: number;
  type: string | null;
  strand: string | null;
  score: number | null;
  name: string | null;
  feature_id: string | null;
  parent: string | null;
  parent_status: string;
  depth: number;
  biotype: string | null;
  attributes: string | null;
  has_children: boolean;
  /** 1-based source line number (null for GenBank features). Added for
   *  annotation editing (#297 / #369 design). */
  line: number | null;
}

/** A page of the feature table. `total` is null when skip_count was set. */
export interface AnnotationFeaturePage {
  total: number | null;
  rows: AnnotationFeature[];
}

/** One row of the Genes view -- a per-gene summary, not a flat feature row.
 *  Stored at compute time rather than derived on read: see
 *  AnnotationFeatureTable's genes query. */
export interface AnnotationGene {
  feature_id: string | null;
  contig: string;
  start: number;
  end: number;
  type: string | null;
  strand: string | null;
  name: string | null;
  biotype: string | null;
  child_count: number;
  descendant_count: number;
  span_start: number;
  span_end: number;
}

/** A page of the Genes view. `mode` says which rule built the table --
 *  "fallback" means the file had no gene-typed records, so these are
 *  top-level features instead, and the UI must say so. */
export interface AnnotationGenePage {
  total: number | null;
  rows: AnnotationGene[];
  mode: "typed" | "fallback";
}

/** A feature drawn in the track, with its children and packed row. */
export interface AnnotationWindowFeature extends AnnotationFeature {
  children: AnnotationFeature[];
  row: number;
}

/** Individual features, below the density threshold. */
export interface AnnotationWindowFeatures {
  mode: "features";
  contig: string;
  start: number;
  end: number;
  total: number;
  features: AnnotationWindowFeature[];
  /** Features dropped because the row cap was reached. */
  truncated_rows: number;
}

/** Per-bin counts, at or above the density threshold. */
export interface AnnotationWindowBinned {
  mode: "binned";
  contig: string;
  start: number;
  end: number;
  total: number;
  bin_bases: number;
  counts: number[];
}

/** Discriminated on `mode`, so an empty region cannot read as a dense one. */
export type AnnotationWindow =
  | AnnotationWindowFeatures
  | AnnotationWindowBinned;

export interface AnnotationContigStat {
  name: string;
  length: number | null;
  count: number;
  covered_bases: number;
  /** Null when the contig's length is unknown -- not zero coverage. */
  covered_fraction: number | null;
  per_mb: number | null;
}

// --- Annotation edits (issue #297) ---

/** One pending column edit on an annotation source line. */
export interface AnnotationEditRow {
  line: number;
  field: string;
  old_value: string | null;
  new_value: string;
}

export interface AnnotationLengthBin {
  min: number;
  max: number | null;
  count: number;
}

/** Facts written by run_annotation_stats. Every optional field is absent
 *  rather than empty when it has nothing to say, so a block renders only
 *  when there is something in it. */
export interface AnnotationStatsFacts extends Record<string, unknown> {
  annotation_stats_status?: "ok";
  annotation_feature_count?: number;
  annotation_top_level_count?: number;
  annotation_contig_count?: number;
  annotation_per_contig?: AnnotationContigStat[];
  annotation_length_histogram?: AnnotationLengthBin[];
  annotation_type_counts?: Record<string, number>;
  annotation_biotype_counts?: Record<string, number>;
  annotation_attribute_keys?: Record<string, number>;
  annotation_malformed_lines?: number;
  /** False when no reference resolved, so per-contig coverage is unavailable. */
  annotation_contig_lengths_known?: boolean;
  /** Set only for GenBank-format annotations -- absent for GFF/GTF/BED. Its
   *  presence is the frontend's GenBank signal, since a GenBank record spans
   *  several lines and cannot be re-emitted by line number, so the export
   *  control hides entirely rather than offering a launch that can't work. */
  genbank_record_count?: number;
  genbank_has_sequence?: boolean;
  gff_version?: string;
  genome_build?: string;
  annotation_source?: string;
  source_version?: string;
  annotation_parent_status_counts?: Record<string, number>;
  annotation_unresolved_count?: number;
  annotation_max_depth?: number;
  annotation_gene_mode?: "typed" | "fallback";
  annotation_gene_count?: number;
}

export interface FeatureQuery {
  offset: number;
  limit: number;
  contig?: string;
  startMin?: number;
  startMax?: number;
  featureType?: string;
  biotype?: string;
  nameQuery?: string;
  strand?: string;
  skipCount?: boolean;
  view?: "all" | "unresolved";
}
