/** One side of the before/after comparison in a fastp report. */
export interface TrimSide {
  total_reads: number | null;
  total_bases: number | null;
  q20_rate: number | null;
  q30_rate: number | null;
  gc_content: number | null;
  read1_mean_length: number | null;
  read2_mean_length: number | null;
}

export interface QualityOverlayPoint {
  position: number;
  before: number | null;
  after: number | null;
}

export interface TrimReport {
  tool: string;
  tool_version: string | null;
  sequencing: string | null;
  before: TrimSide;
  after: TrimSide;
  filtering: {
    passed_reads: number | null;
    low_quality_reads: number | null;
    too_many_n_reads: number | null;
    too_short_reads: number | null;
  };
  duplication_rate: number | null;
  insert_size_peak: number | null;
  /**
   * Read1's mean Phred per cycle, measured before and after trimming in the
   * same fastp pass and binned onto one shared set of positions. Absent on
   * objects trimmed before #639 shipped, and on runs where fastp reported
   * only one side -- the chart self-suppresses either way.
   *
   * `after` runs out at the tail when trimming shortened the read; a null
   * there means "no longer a cycle", not "not measured".
   */
  quality_overlay?: QualityOverlayPoint[];
  adapters?: {
    trimmed_reads: number | null;
    trimmed_bases: number | null;
    read1_sequence: string | null;
    read2_sequence: string | null;
  };
}

/**
 * QC facts written onto an object by a `run_qc` job.
 *
 * Flat and `qc_`-prefixed rather than nested under one key, because they are
 * merged into the same `facts` dict as everything else the ingest and the
 * pipelines record. `TrimSide` is reused for the measurements: with filtering
 * disabled there is only the one state, but it is the same set of numbers.
 */
export interface QcFacts {
  qc_tool?: string;
  qc_tool_version?: string | null;
  qc_sequencing?: string | null;
  /** NCBI's platform spelling, e.g. OXFORD_NANOPORE. Written by the long-read
   *  QC path alongside the chemistry it inferred. */
  qc_platform?: string | null;
  qc_before_filtering?: TrimSide;
  qc_duplication_rate?: number | null;
  qc_insert_size_peak?: number | null;
  qc_adapters?: {
    read1_sequence: string | null;
    read2_sequence: string | null;
  };
  /**
   * Per-position cumulative adapter percentages, one series per probe.
   * Written by the whole-file contamination scan; absent on files QC'd
   * before it existed, which is why every consumer treats it as optional.
   */
  qc_adapter_content?: {
    positions: number[];
    series: { name: string; values: number[] }[];
  };
  /** FastQC's 16-slot duplication histogram, as percentages of the library. */
  qc_duplication_levels?: {
    labels: string[];
    percentages: number[];
  };
  /** Whole-file, correction-adjusted. Preferred over `qc_duplication_rate`. */
  qc_percent_unique?: number;
  qc_duplication_scanned_reads?: number;
  /** Paths relative to the report route, absent when the tool did not run. */
  qc_fastp_report?: string;
  qc_fastqc_report?: string;
  /** Whether the reads' headers carried flow-cell tile coordinates.
   *  "absent" covers SRA-stripped downloads and long reads -- an ordinary
   *  outcome, not a failure. The chart renders nothing unless "present". */
  qc_tile_source?: "present" | "absent";
  qc_tile_count?: number;
  qc_tile_sampled_reads?: number;
  qc_tile_sample_rate?: number;
  qc_tile_truncated?: boolean;
  /** Filename of the sidecar holding the matrix, which is too large to live
   *  in this document -- fetched separately via `api.qcTileMatrix`. */
  qc_tile_matrix?: string;
  qc_tile_worst?: {
    tile: number;
    mean_quality: number;
    /** How far below the run's overall mean this tile sits. */
    deficit: number;
  };
  qc_status?: string;

  // NanoPlot facts, written by the long-read QC path (Nanopore/PacBio)
  // instead of the fastp/FastQC pair above. N50 is this run's headline
  // number the way Q30 is for a short-read one.
  qc_total_reads?: number | null;
  qc_total_bases?: number | null;
  qc_mean_read_length?: number | null;
  qc_median_read_length?: number | null;
  qc_read_length_n50?: number | null;
  qc_read_length_stdev?: number | null;
  qc_mean_quality?: number | null;
  qc_median_quality?: number | null;
  qc_nanoplot_report?: string;
  /**
   * Total bases per log-spaced read-length bin -- bases, not reads. Written
   * by binning NanoPlot's `--raw` per-read TSV during the job; absent on
   * long-read files QC'd before that existed, which is why the chart treats
   * it as optional rather than assuming every NanoPlot object has one.
   */
  qc_length_bases_histogram?: {
    bins_per_decade: number;
    min_length: number;
    bins: {
      length_bin: number;
      length_bin_end: number;
      bases: number;
      reads: number;
    }[];
    total_bases: number;
    total_reads: number;
  };
  /**
   * Read count per (length bin, quality bin) cell, from the same pass.
   * Sparse: only occupied cells are present, as `[length_bin_start,
   * quality_bin, count]` triples -- named keys on a grid this size would be
   * most of the fact's document size.
   */
  qc_length_quality_density?: {
    bins_per_decade: number;
    min_length: number;
    quality_bins: number;
    cells: [number, number, number][];
    max_count: number;
    total_reads: number;
  };
  /** Inferred by qc_stats.infer_chemistry; see ReadChemistry on the backend. */
  qc_read_chemistry?: string;
  qc_read_chemistry_reason?: string;
}

/** The per-tile quality matrix, served from its own route because it is far
 *  too large for the object document. Rows are tiles in ascending (physical)
 *  order; columns are read positions. A null cell is a position no read on
 *  that tile reached -- distinct from a genuine quality of zero. */
export interface TileMatrix {
  tiles: number[];
  positions: number;
  matrix: (number | null)[][];
  sampled_reads: number;
  sample_rate: number;
  truncated: boolean;
}
