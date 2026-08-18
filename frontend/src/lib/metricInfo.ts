/**
 * What each reported number means, in the reader's terms.
 *
 * Every figure this app shows is one someone might copy into a methods
 * section, and several are not what their label implies. "Mean quality" is
 * the mean of the per-position means rather than the mean over every base;
 * it and most of its neighbours are measured on a 200,000-read sample rather
 * than the whole file. A number cannot say that about itself, so it is said
 * here.
 *
 * Descriptions are written from the code that produces the value
 * (`backend/app/storage/sequence_stats.py`, `storage/parsers.py`,
 * `pipelines/fastp_runner.py`), not from a textbook. Where BioFlow's
 * definition differs from the conventional one -- as it does for
 * `mean_quality`, and for MAPQ under STAR -- the difference is the most
 * useful sentence in the entry.
 *
 * Keys are the fact keys the backend emits, so `FactsTable` needs no
 * per-row wiring: it looks up the key it already has. Surfaces that write
 * their labels by hand (`QcReport`, the charts) pass a key from the same
 * namespace, prefixed `ui.` when there is no single backing fact.
 *
 * Coverage is enforced by `metricInfo.test.ts`, which fails when a labelled
 * fact has neither an entry here nor a documented place in `NO_INFO_NEEDED`.
 * That is the same companion-set pattern `schemas.FORMAT_DERIVED_ROLES`
 * uses, and it exists for the same reason: a registry that silently skips a
 * key it has no entry for is a registry that quietly stops covering things.
 */

export interface MetricInfo {
  /** Display name, repeated as the card's heading. */
  term: string;
  /** What it measures and how to read it. Two or three sentences. */
  description: string;
  /**
   * How BioFlow arrives at the number, when that is surprising or when a
   * caveat (sampling, a non-standard scale) changes how it should be read.
   */
  computed?: string;
  /** Anchor on the calculations help page, where longer treatment exists. */
  learnMore?: string;
}

const CALC = "/help/calculations";

export const METRIC_INFO: Record<string, MetricInfo> = {
  // ---- Sampling and provenance -------------------------------------------
  stats_sampled_reads: {
    term: "Records sampled",
    description:
      "How many reads the ingest statistics were measured on. Base composition and the quality curve converge well before this many reads, so a larger sample would shift these numbers only in the third decimal place.",
    computed:
      "Up to 200,000 reads from the start of the file. A 30 GB FASTQ costs the same as a 400 MB one because the scan stops either way.",
  },
  stats_sampling: {
    term: "Sampling method",
    description:
      "Whether the sample was taken from the start of the file (\"head\") or spread across it in blocks (\"strided\"). Strided sampling needs an index; without one, reading from the start is the only option.",
    computed:
      "A head sample of a coordinate-sorted BAM sees the first chromosome only, which is why the method is reported rather than assumed.",
  },
  quality_encoding: {
    term: "Quality encoding",
    description:
      "Which character-to-Phred-score mapping the file uses. Phred+33 is effectively universal; Phred+64 is old Illumina output, and reading it as Phred+33 would report implausibly high scores.",
    computed:
      "Detected from the observed score range rather than declared by the file: a minimum at or above 64 with a maximum above 74 means Phred+64.",
  },

  // ---- Read-level quality ------------------------------------------------
  mean_quality: {
    term: "Mean quality",
    description:
      "The average Phred score across the read, where a score of 30 means the base call is 99.9% likely to be right. Higher is better; most usable short-read data sits above Q30.",
    computed:
      "The mean of the per-position means, not the mean over every base — each read position contributes equally regardless of how many reads reached it. Measured on the ingest sample.",
    learnMore: CALC,
  },
  min_position_quality: {
    term: "Lowest position quality",
    description:
      "The worst any single read position scores once averaged across reads. A file can hold a healthy overall mean and still have a bad tail, and this is the number that exposes it — low values here are usually the last cycles of the run, which trimming removes.",
    computed:
      "The minimum of the per-position mean quality curve, so it is a position's average rather than the single worst base in the file.",
    learnMore: CALC,
  },
  quality_per_position: {
    term: "Quality by position",
    description:
      "Mean Phred score at each position along the read. A healthy curve starts high and declines gently; a sharp drop at the end is normal for Illumina and is what trimming is for.",
    computed:
      "Averaged across the ingest sample, over the first 1,000 positions. Long reads extend past that, but the useful detail is at the start.",
  },

  // ---- Composition -------------------------------------------------------
  gc_content_percent: {
    term: "GC content",
    description:
      "The percentage of called bases that are G or C. It is a fingerprint of the source organism, so a value far from the expected one for your species suggests contamination or a mix-up.",
    computed:
      "G+C over A+C+G+T across the sample. N bases are excluded from both sides rather than counted against the total.",
  },
  gc_per_read_mean: {
    term: "GC per read (mean)",
    description:
      "The average GC content of individual reads, rather than of the file as a whole. It sits alongside the per-read distribution, where a second peak is the usual sign that two organisms are present.",
    computed:
      "Averaged from the per-read GC histogram. Reads with no A/C/G/T at all are skipped rather than binned at 0%, which would invent a peak out of unsequenced bases.",
  },
  base_composition: {
    term: "Base composition",
    description:
      "How often each base occurs at each position along the read. The four lines should run roughly flat and parallel after the first few positions; a sustained divergence points to adapter or primer sequence.",
    computed: "Counted across the ingest sample.",
  },

  // ---- Read structure ----------------------------------------------------
  read_length: {
    term: "Read length",
    description:
      "The length of the reads in this file, in bases. A single value here means every read is the same length, which is what untrimmed short-read data looks like.",
  },
  read_length_min: {
    term: "Read length (min)",
    description: "The shortest read observed. A wide spread between minimum and maximum means the file has already been trimmed, or holds long-read data.",
  },
  read_length_max: {
    term: "Read length (max)",
    description:
      "The longest read observed. For short-read data this is normally the run's configured length, so a value below it means the file has already been trimmed.",
  },
  read_count_estimate: {
    term: "Read count",
    description:
      "How many reads the file holds. Shown as an estimate unless BioFlow counted every record — an extrapolation must never be mistaken for a measurement, because it ends up in someone's methods section.",
    computed:
      "Extrapolated from the average record size across the sample and the file's total size, when a full count would mean decompressing the whole file.",
  },
  sampled_records: {
    term: "Records sampled",
    description: "How many records were read to produce the statistics on this file.",
  },
  paired: {
    term: "Paired-end",
    description:
      "Whether this file is one half of a read pair. Paired data lets an aligner use the expected distance between mates to place ambiguous reads.",
  },
  paired_hint: {
    term: "Mate",
    description: "Which half of the pair this file holds — R1 or R2.",
  },
  first_read_ids: {
    term: "First read IDs",
    description:
      "The identifiers of the first few reads, as written by the sequencer. They are what BioFlow matches on to pair files, and on Illumina they also encode the flowcell and lane.",
  },

  // ---- Alignment ---------------------------------------------------------
  mapped_percent: {
    term: "Mapped",
    description:
      "The percentage of sampled reads that the aligner placed somewhere on the reference. A low value usually means the wrong reference, heavy contamination, or reads that still need trimming.",
  },
  duplicate_percent: {
    term: "Duplicates",
    description:
      "The percentage of reads flagged as PCR or optical duplicates. High duplication means the library had less unique material than the read count suggests, so the effective depth is lower than it looks.",
    computed: "Read from the duplicate flag already set in the file, not re-detected here.",
  },
  mean_mapping_quality: {
    term: "Mean MAPQ",
    description:
      "The average confidence that reads are placed at the right locus, on a Phred-like scale. Repetitive regions drive it down, because a read that fits several places well cannot be confidently assigned to one.",
    computed:
      "Absent for STAR alignments, which do not use this scale — those report \"Uniquely mapped\" instead.",
  },
  mapq_scale: {
    term: "MAPQ scale",
    description:
      "Which mapping-quality convention the file uses. Most aligners write a Phred-like score capped at 42 to 60; STAR instead writes 255 for a uniquely mapped read and small ordinal codes for multi-mapped ones.",
    computed:
      "Detected from the records rather than from provenance, so an imported STAR BAM with no run history is still recognised. Averaging STAR's codes would report roughly 247 where bwa-mem2 reports 50 on the same reads.",
  },
  uniquely_mapped_percent: {
    term: "Uniquely mapped",
    description:
      "The percentage of reads STAR placed at exactly one locus. This replaces mean MAPQ for STAR output, where the codes count loci rather than express a confidence.",
    computed: "The share of reads carrying MAPQ 255, STAR's unique-alignment code.",
  },
  insert_size_histogram: {
    term: "Insert size",
    description:
      "The distribution of distances between the two ends of a read pair. A single clean peak is a healthy library; a peak near zero means the mates overlap, and a long tail suggests chimeric fragments.",
    computed: "Binned at 10 bp up to 2 kb. Absent entirely for unpaired data rather than shown as zeros.",
  },
  mapq_histogram: {
    term: "MAPQ distribution",
    description:
      "How mapping confidence is spread across reads. A large spike at zero is the signature of a repetitive reference, where many reads cannot be placed uniquely.",
  },

  // ---- Assembly contiguity -----------------------------------------------
  sequence_n50: {
    term: "N50",
    description:
      "The contig length at which half the assembly sits in contigs that long or longer. It is the standard one-number summary of contiguity, and higher is better — but it says nothing about correctness, since a wrongly joined assembly scores well.",
    computed: "Computed over every contig, not the subset stored for display.",
    learnMore: CALC,
  },
  sequence_n90: {
    term: "N90",
    description:
      "The contig length at which 90% of the assembly sits in contigs that long or longer. It is far more sensitive than N50 to a long tail of short contigs, so a big gap between the two means the assembly is carried by a few large pieces.",
  },
  sequence_l50: {
    term: "L50",
    description:
      "How many contigs it takes to cover half the assembly. It is N50's counterpart — low is better, and a chromosome-level assembly approaches the chromosome count.",
  },
  sequence_auN: {
    term: "auN",
    description:
      "The area under the Nx curve: a contiguity summary that weights every base by the length of the contig it sits in. Unlike N50 it does not jump when a single contig crosses the halfway point, which is why two assemblies can share an N50 and still differ here.",
    computed: "The sum of squared contig lengths divided by the total assembly length.",
  },
  sequence_gap_count: {
    term: "Gaps",
    description:
      "How many runs of N appear inside contigs. Gaps are placeholders where scaffolding established order and orientation but no sequence — a scaffolded assembly has them, a raw contig set generally does not.",
  },
  sequence_gap_bases: {
    term: "Gap bases",
    description:
      "The total number of N bases inside gaps. Read against total length, this is how much of the assembly is inferred rather than sequenced.",
  },
  sequence_nx_curve: {
    term: "Nx curve",
    description:
      "Contig length against the percentage of the assembly held in contigs at least that long. N50 is this curve's value at x=50. A flat curve means evenly sized contigs; a steep one means a few large pieces carry the assembly.",
  },

  // ---- Sequence inventory ------------------------------------------------
  sequence_count: {
    term: "Sequences",
    description:
      "How many sequence records the file holds, counted in full. For an assembly this is the contig or scaffold count, where fewer generally means a more contiguous result.",
  },
  sequence_count_estimate: {
    term: "Sequences",
    description: "How many sequence records the file holds, extrapolated rather than counted in full.",
  },
  total_bases: {
    term: "Total bases",
    description: "The sum of all sequence lengths. For an assembly, this is its total size — compare it against the expected genome size.",
  },
  sequence_longest: {
    term: "Longest sequence",
    description:
      "The length of the longest record in the file. In an assembly, comparing this against the expected chromosome size says how close the result is to chromosome-level.",
  },
  sequence_shortest: {
    term: "Shortest sequence",
    description:
      "The length of the shortest record in the file. A very short minimum alongside a large N50 means a tail of small contigs that most downstream tools will filter out.",
  },
  sequence_names: {
    term: "Sequence names",
    description:
      "The identifiers of the records in this file, taken from their headers. These are the names an aligner or browser will use to refer to each sequence.",
  },
  reference_count: {
    term: "Reference contigs",
    description: "How many reference sequences the alignment header declares. This is the reference's contig count, not this file's read count.",
  },
  reference_names: { term: "Contig names", description: "The reference sequences this file was aligned against, from its header." },
  reference_lengths: { term: "Contig lengths", description: "The length of each reference sequence, from the alignment header." },
  reference_total_length: {
    term: "Reference length",
    description: "The summed length of every reference sequence in the header — the size of the genome this file was aligned to.",
  },

  // ---- QC report (fastp / FastQC) ----------------------------------------
  "ui.qc_total_reads": {
    term: "Reads",
    description:
      "How many reads QC measured. Unlike the ingest estimate, this is a full count — QC reads every record in the file.",
  },
  "ui.qc_total_bases": {
    term: "Bases",
    description:
      "The total sequenced bases in the file. Divided by your genome size, this is the theoretical maximum coverage depth available.",
  },
  "ui.qc_mean_length": {
    term: "Mean length",
    description: "The average read length measured across the whole file by QC.",
  },
  "ui.qc_q20": {
    term: "Q20",
    description:
      "The fraction of bases called with at least 99% confidence. It is the looser of the two standard thresholds; most usable data clears 90% here.",
  },
  "ui.qc_q30": {
    term: "Q30",
    description:
      "The fraction of bases called with at least 99.9% confidence. This is the standard Illumina yardstick and the number that sets a read file's quality grade in BioFlow.",
    learnMore: CALC,
  },
  "ui.qc_gc": {
    term: "GC",
    description:
      "The percentage of G and C bases across the whole file, measured by QC rather than estimated from a sample.",
  },
  "ui.qc_duplication": {
    term: "Duplication",
    description:
      "The share of reads that are copies of another read. High duplication means the library had less unique material than its read count suggests, so effective depth is lower than it appears.",
    computed:
      "Taken from a full-file scan with FastQC's frozen-dictionary correction when available, falling back to fastp's sampled estimate. Both are kept in the facts, but only one is shown — two methods disagreeing side by side is worse than one answer.",
    learnMore: CALC,
  },
  "ui.qc_read_length_n50": {
    term: "Read length N50",
    description:
      "The read length at which half the sequenced bases sit in reads that long or longer. For long-read data this describes the run far better than a mean, which a tail of short reads drags down.",
  },

  // ---- Quality-tab charts ------------------------------------------------
  "ui.chart_quality_per_position": {
    term: "Quality by position",
    description:
      "Mean Phred score at each position along the read, averaged across the sample. Look for the point where the curve falls below Q20 — that is where trimming should cut.",
  },
  "ui.chart_base_composition": {
    term: "Base composition by position",
    description:
      "How often each base appears at each read position. The lines should settle roughly flat and parallel after the first few positions; sustained divergence at the start is usually adapter or primer sequence.",
  },
  "ui.chart_gc_per_read": {
    term: "GC content per read",
    description:
      "The distribution of GC content across individual reads. A single peak at the organism's expected GC is healthy; a second peak generally means a second organism is present.",
  },
  "ui.chart_n_per_position": {
    term: "Uncalled bases by position",
    description:
      "The share of reads with an uncalled (N) base at each position. A spike at one position means that sequencing cycle failed outright, which is a different problem from a gradual quality decline.",
  },
  "ui.chart_read_length": {
    term: "Read length distribution",
    description:
      "How many reads fall in each length bin. Untrimmed short-read data is a single spike at the run length; a spread means the file has been trimmed, or holds long reads.",
    computed: "Binned at 10 bp with no upper limit, so long-read distributions keep their shape.",
  },
  "ui.chart_tile_quality": {
    term: "Quality by flowcell tile",
    description:
      "Mean quality for each physical tile of the flowcell, against read position. Uniform colour is healthy; a dark band on one tile is a localised flowcell defect such as a bubble, affecting only the reads that came from it.",
  },
  "ui.chart_chromosome_strip": {
    term: "Sequence lengths",
    description:
      "Each sequence in the reference drawn to scale. It shows at a glance whether the file is a chromosome-level assembly, a scaffold set, or a fragmented draft.",
  },
  "ui.chart_nx": {
    term: "Nx and NGx curves",
    description:
      "Contig length against the percentage of the assembly covered. Nx measures against the assembly's own size; NGx measures against the expected genome size, so an NGx curve that stops short of the axis is showing you the fraction of the genome the assembly never reached.",
  },
};

/**
 * Fact keys that are deliberately left without a card, with the reason.
 *
 * Not a backlog. An entry here is an assertion that a marker would add
 * nothing -- the label already says everything the value means, or the row
 * is provenance (a tool name, a version, a timestamp) rather than a
 * measurement someone has to interpret. The exhaustiveness test requires
 * every labelled fact to be in exactly one of this set or METRIC_INFO, so a
 * new fact cannot quietly arrive with no explanation and no decision.
 */
export const NO_INFO_NEEDED: ReadonlySet<string> = new Set([
  // Provenance: which tool ran, which version, and when. Self-describing.
  "bam_stats_computed_at",
  "bam_stats_tool_version",
  "bam_stats_status",
  "trimmed_by",
  "trim_tool_version",
  "aligned_by",
  "aligner",
  "aligner_version",
  "index_built_by",
  "index_tool_version",
  "index_status",
  "has_index",
  "sort_order",
  "sam_version",
  "vcf_version",
  "program_chain",
  "platforms",
  // File structure the label fully describes.
  "record_count",
  "header_lines",
  "column_counts",
  "first_contig",
  "sample_names",
  "sample_count",
  "read_group_count",
  // VCF schema rows: these name the fields the file declares, and what each
  // one means belongs to that file's own header, not to BioFlow.
  "info_fields",
  "info_field_count",
  "format_fields",
  "filters",
  "variant_types_sampled",
]);

export function infoFor(key: string): MetricInfo | undefined {
  return METRIC_INFO[key];
}
