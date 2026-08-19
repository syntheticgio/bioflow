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
      'Whether the sample was taken from the start of the file ("head") or spread across it in blocks ("strided"). Strided sampling needs an index; without one, reading from the start is the only option.',
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
    description:
      "The shortest read observed. A wide spread between minimum and maximum means the file has already been trimmed, or holds long-read data.",
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
    description:
      "How many records were read to produce the statistics on this file.",
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
    computed:
      "Read from the duplicate flag already set in the file, not re-detected here.",
  },
  mean_mapping_quality: {
    term: "Mean MAPQ",
    description:
      "The average confidence that reads are placed at the right locus, on a Phred-like scale. Repetitive regions drive it down, because a read that fits several places well cannot be confidently assigned to one.",
    computed:
      'Absent for STAR alignments, which do not use this scale — those report "Uniquely mapped" instead.',
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
    computed:
      "The share of reads carrying MAPQ 255, STAR's unique-alignment code.",
  },
  insert_size_histogram: {
    term: "Insert size",
    description:
      "The distribution of distances between the two ends of a read pair. A single clean peak is a healthy library; a peak near zero means the mates overlap, and a long tail suggests chimeric fragments.",
    computed:
      "Binned at 10 bp up to 2 kb. Absent entirely for unpaired data rather than shown as zeros.",
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
    computed:
      "The sum of squared contig lengths divided by the total assembly length.",
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
    description:
      "How many sequence records the file holds, extrapolated rather than counted in full.",
  },
  total_bases: {
    term: "Total bases",
    description:
      "The sum of all sequence lengths. For an assembly, this is its total size — compare it against the expected genome size.",
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
    description:
      "How many reference sequences the alignment header declares. This is the reference's contig count, not this file's read count.",
  },
  reference_names: {
    term: "Contig names",
    description:
      "The reference sequences this file was aligned against, from its header.",
  },
  reference_lengths: {
    term: "Contig lengths",
    description:
      "The length of each reference sequence, from the alignment header.",
  },
  reference_total_length: {
    term: "Reference length",
    description:
      "The summed length of every reference sequence in the header — the size of the genome this file was aligned to.",
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
    description:
      "The average read length measured across the whole file by QC.",
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
  "ui.qc_insert_size_peak": {
    term: "Insert size",
    description:
      "The most common distance between the two ends of a read pair. A peak shorter than twice the read length means the mates overlap, which some callers exploit and others cannot handle.",
  },
  "ui.qc_adapter": {
    term: "Adapter detected",
    description:
      "Adapter sequence QC found in the reads. Adapter left in place is sequence that came from the library kit rather than the sample, so it misaligns or blocks assembly — trimming removes it.",
  },
  "ui.qc_median_read_length": {
    term: "Median length",
    description:
      "The middle read length, which for long-read data describes the run better than the mean. A median far below the mean means a few very long reads are pulling the average up.",
  },
  "ui.qc_read_length_stdev": {
    term: "Length std. dev.",
    description:
      "How widely read lengths are spread around the mean. A wide spread is the normal shape of a Nanopore run rather than a fault, which is why it is reported beside the averages rather than as a warning.",
  },
  "ui.qc_mean_quality": {
    term: "Mean quality",
    description:
      "The average Phred score across reads, where Q20 is 99% base-call accuracy. Long-read platforms sit well below the Q30 expected of short reads, so read this against the platform rather than against an absolute bar.",
  },
  "ui.qc_median_quality": {
    term: "Median quality",
    description:
      "The middle read quality. It is the more robust of the two averages when a run has a tail of poor reads, which is common for Nanopore.",
  },
  "ui.qc_read_chemistry": {
    term: "Chemistry",
    description:
      "Which sequencing chemistry the reads appear to come from — HiFi or CLR for PacBio, duplex or simplex for Nanopore. It decides the aligner preset, so the row shows the reason alongside the answer.",
    computed:
      "Inferred from mean read length and mean quality, because neither the SAM nor the SRA platform tag distinguishes these modes. Ambiguous evidence reports as unknown rather than as a guess.",
  },
  "ui.qc_platform": {
    term: "Platform",
    description:
      "The sequencing instrument family the file came from. It decides which QC tool runs: a per-base quality curve makes no sense for reads running from 200 bp to 100 kb, so long-read files take a different path.",
  },
  "ui.qc_status": {
    term: "Status",
    description:
      'Shown only when QC did not finish cleanly. An ordinary successful run has no status row, because a row reading "ok" on every file never says anything.',
  },
  "ui.qc_read_length_n50": {
    term: "Read length N50",
    description:
      "The read length at which half the sequenced bases sit in reads that long or longer. For long-read data this describes the run far better than a mean, which a tail of short reads drags down.",
  },

  "ui.chart_length_bases_histogram": {
    term: "Bases by read length",
    description:
      "Total bases in each read-length bin — not the number of reads. The distinction changes the conclusion: a long-read run's reads are mostly short while its bases are mostly long, and it is the bases that decide whether a repeat gets spanned during assembly. The dashed line marks N50, the length at which half the bases sit in reads that long or longer.",
  },
  "ui.chart_length_quality_density": {
    term: "Length vs quality",
    description:
      "How many reads sit at each combination of length and mean quality; darker is denser. Two separate clouds mean two populations — a HiFi run with incomplete consensus shows a second, lower-quality group, and a mass of short poor reads dragging the averages down looks quite different from a run that is uniformly mediocre, though both give the same mean.",
  },

  // ---- Quality-tab charts ------------------------------------------------
  "ui.chart_quality_per_position": {
    term: "Quality per position",
    description:
      "Mean Phred score at each position along the read, averaged across the sample. Look for the point where the curve falls below Q20 — that is where trimming should cut.",
  },
  "ui.chart_base_composition": {
    term: "Base composition",
    description:
      "How often each base appears at each read position. The lines should settle roughly flat and parallel after the first few positions; sustained divergence at the start is usually adapter or primer sequence.",
  },
  "ui.chart_gc_per_read": {
    term: "GC distribution",
    description:
      "The distribution of GC content across individual reads. A single peak at the organism's expected GC is healthy; a second peak generally means a second organism is present.",
  },
  "ui.chart_trim_quality_overlay": {
    term: "Quality per position, before and after trimming",
    description:
      "Mean Phred score at each read position, measured on the same reads before and after the trim. Where the two curves separate is where trimming acted: a lifted 3\u2019 tail means quality decay was clipped, a raised start means adapter was removed, and two curves that sit on top of each other mean the trim did almost nothing \u2014 usually a sign the parameters were wrong for this file.",
    computed:
      "Both sides come from one fastp pass over the same file, binned onto identical positions and downsampled to at most 100 points. The \u201cafter\u201d line stops where trimming shortened the read rather than dropping to zero.",
  },
  "ui.chart_n_per_position": {
    term: "N content per position",
    description:
      "The share of reads with an uncalled (N) base at each position. A spike at one position means that sequencing cycle failed outright, which is a different problem from a gradual quality decline.",
  },
  "ui.chart_read_length": {
    term: "Read length distribution",
    description:
      "How many reads fall in each length bin. Untrimmed short-read data is a single spike at the run length; a spread means the file has been trimmed, or holds long reads.",
    computed:
      "Binned at 10 bp with no upper limit, so long-read distributions keep their shape.",
  },
  "ui.chart_tile_quality": {
    term: "Quality per tile",
    description:
      "Mean quality for each physical tile of the flowcell, against read position. Uniform colour is healthy; a dark band on one tile is a localised flowcell defect such as a bubble, affecting only the reads that came from it.",
  },
  "ui.chart_adapter_content": {
    term: "Adapter content",
    description:
      "The percentage of reads carrying adapter sequence at each position. Adapter rises toward the read end when fragments were shorter than the read length, so the sequencer ran off the end of the insert and into the kit's own sequence.",
    computed:
      "From the whole-file QC scan. Files QC'd before that scan existed show no chart rather than an empty one.",
  },
  "ui.chart_duplication_levels": {
    term: "Sequence duplication levels",
    description:
      "How many reads appear once, twice, and so on. A large bar at high duplication levels means the library was over-amplified from little starting material, so extra sequencing depth would add copies rather than new information.",
    computed:
      "From a full-file scan with FastQC's frozen-dictionary correction, not a sampled estimate.",
    learnMore: "/help/calculations",
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

  // ---- Remaining standalone surfaces ------------------------------------
  "ui.project_metadata": {
    term: "Project metadata",
    description:
      "Free-form key/value fields inherited by every file ingested into this project. Values are stored with their JSON type when possible, so metadata can be searched and reused by downstream work.",
  },
  "ui.file_metadata": {
    term: "Record metadata",
    description:
      "Metadata attached to this file and carried into pipelines that use it. Schema suggestions are conveniences rather than a restriction; custom fields remain valid alongside them.",
  },
  "ui.provenance": {
    term: "File provenance",
    description:
      "The recorded lineage of this file, oldest input first. Missing facts are shown as unrecorded rather than inferred, so the narrative remains citable without overstating what BioFlow knows.",
  },
  "ui.computation_history": {
    term: "Computation history",
    description:
      "Individual jobs recorded against this file, including their outcome, duration, threads, and peak memory when available. These are observations of runs, not estimates of future jobs.",
  },
  "ui.metrics_overview": {
    term: "Metrics overview",
    description:
      "A project-wide view of computation records. Run counts include failures, while duration, input size, read counts, and resource summaries use the successful outcome-filtered rows used by the predictive models.",
    computed:
      "Peak memory is absent for jobs shorter than 60 seconds because resource sampling has a minimum duration floor; the dash means no measurement, not zero.",
  },
  "ui.metrics_estimates": {
    term: "Job-type estimates",
    description:
      "Median and p90 measurements grouped by job type. They describe the recent successful sample used for modelling, not every recorded run and not a guarantee for the next run.",
  },
  "ui.metrics_recent_runs": {
    term: "Recent runs",
    description:
      "The newest recorded runs for each job type, with failures included. This list is the audit trail behind the summaries and may therefore disagree with their successful-run-only medians.",
  },
  "ui.chart_busco": {
    term: "BUSCO completeness",
    description:
      "The percentage of lineage-specific single-copy orthologs found in the assembly, split into single-copy, duplicated, fragmented, and missing markers. Higher complete content is useful, but duplication can indicate retained haplotypes rather than extra genes.",
  },
  "ui.chart_gc_tracks": {
    term: "GC tracks",
    description:
      "GC content, GC skew, and any available repeat or gene-density tracks along the assembly. Local peaks and troughs show composition or annotation changes by sequence position; they are not whole-assembly averages.",
  },
  "ui.chart_synteny": {
    term: "Synteny plot",
    description:
      "The order and orientation of assembly contigs against the selected reference. Coloured blocks show matching intervals; reversals and breaks reveal rearrangements or mis-assembly candidates.",
  },
  "ui.chart_assembly_graph": {
    term: "Assembly graph",
    description:
      "The assembler's segment and link topology. Branches and cycles represent unresolved alternative paths or repeats, so this graph is structural evidence rather than a finished sequence.",
  },
  "ui.chart_transcript_gene_body": {
    term: "Gene body coverage",
    description:
      "Where sampled reads fall from the 5′ to 3′ end of annotated transcripts. A strong slope indicates transcript-end bias, which can make expression estimates less comparable across genes.",
  },
  "ui.chart_transcript_distribution": {
    term: "Read distribution",
    description:
      "The share of sampled reads assigned to exons, introns, and intergenic sequence. It is interpreted against the annotation and library type; it is not a measure of expression abundance.",
  },

  // ---- Results tab: alignment coverage ------------------------------------
  "ui.bam_mean_depth": {
    term: "Mean depth",
    description:
      "How many reads cover the average reference base. It is the headline number for whether a call set can be trusted, but it says nothing about evenness — a genome half-covered at 60× and half at 0× reports the same mean as one evenly covered at 30×, which is what the ≥1×/≥10×/≥30× figures beside it exist to separate.",
    computed:
      "A length-weighted mean of the per-contig mean depths, so a long chromosome counts for more than a short scaffold rather than each contig counting once.",
  },
  "ui.bam_pct_covered": {
    term: "Breadth of coverage",
    description:
      "The percentage of the reference covered to at least the stated depth. 1× is \"sequenced at all\"; 10× and 30× are the conventional floors for calling a heterozygous and a somatic variant. A high mean depth next to a low ≥10× means the reads piled up somewhere rather than spreading out.",
    computed:
      "Measured over the 1,000 bins the coverage strip is drawn from, not per base — each bin holds the mean depth of its slice of the reference, so this is the fraction of bins whose average clears the threshold. A short, deeply covered region inside a mostly empty bin does not lift it over.",
  },
  "ui.bam_total_contigs": {
    term: "Contigs",
    description:
      "How many sequences the reference this BAM was aligned against declares. It is a property of the reference rather than of the alignment: a chromosome-level genome shows tens, a fragmented draft shows thousands, and the per-contig table below is that many rows long.",
    computed: "Counted from the BAM's own index, which lists every reference sequence in the header whether or not any read aligned to it.",
  },
  "ui.bam_contig_table": {
    term: "Per-contig detail",
    description:
      "Every reference sequence with its length, aligned read count, breadth, mean depth, and mean mapping quality. This is where an alignment that looks healthy overall comes apart — a single contig at a fraction of the genome's mean depth is a deletion, a mis-assembly, or a contig the sample simply does not have.",
    computed:
      "Coverage and mean depth here are per base from samtools coverage, unlike the binned ≥N× figures in the headline row, so the two can disagree slightly on a very unevenly covered contig.",
  },
  "ui.chart_birds_eye_coverage": {
    term: "Coverage across the reference",
    description:
      "Mean depth along the whole reference, contigs laid end to end. Flat is healthy; a spike is a repeat or a high-copy contaminant collecting reads from elsewhere, and a gap is a region the library never reached.",
    computed:
      "1,000 bins regardless of genome size, so each bar is 3 Mb of a human genome and 5 bp of a plasmid. Any contig shorter than one bin still gets its own bin rather than disappearing.",
  },
  "ui.chart_cumulative_coverage": {
    term: "Cumulative coverage",
    description:
      "The fraction of the reference at or above each depth. It answers \"did I sequence deeply enough\" directly, which a mean cannot: the curve's shape distinguishes even coverage from a mix of very deep and uncovered regions that average to the same number.",
    computed: "Computed over the same 1,000 bins as the coverage strip.",
  },
  "ui.chart_contig_depth": {
    term: "Depth by contig",
    description:
      "Mean depth per contig against the genome-wide mean. Contigs far below the line have less material in the sample than the reference expects; far above usually means repeat content or a plasmid present in many copies.",
  },
  "ui.chart_contig_depth_strip": {
    term: "Depth by chromosome",
    description:
      "Each contig drawn to scale and shaded by its depth against a typical contig. Cool means below typical, warm means above, and a neutral bar is ordinary depth. The readings it exists for are a chromosome at half the depth of its neighbours (aneuploidy, or a sex chromosome at the expected dosage), one at zero (a dropout or a reference/sample mismatch), and one at double depth (a duplication).",
    computed:
      "From the same per-contig `samtools coverage` pass as the contig table, so the lengths are the BAM header's own rather than a reference file's. The baseline is the length-weighted median depth, deliberately not the mean reported above: one small very deep sequence moves a mean arbitrarily far, and a yeast mitochondrion at 8,157\u00d7 over 86 kb is enough to pull the genome mean from 26\u00d7 to 80\u00d7 and make all sixteen nuclear chromosomes look like a dropout. The shade saturates at half and double the baseline. Bar height is sequence length, not depth.",
  },
  "ui.chart_depth_histogram": {
    term: "Depth distribution",
    description:
      "How many reference positions sit at each depth. A single tight peak is a uniform library; a long right tail is coverage bias, and a second peak flags contamination or a large copy-number change. None of that survives the averaging in the coverage strip, which is why this chart is separate.",
    computed:
      "60 buckets spanning zero to three times the mean depth, so an amplicon panel and a 30× genome both get a readable curve. Everything beyond that span lands in one overflow bucket at the right rather than being dropped.",
  },
  "ui.chart_insert_size": {
    term: "Insert size",
    description:
      "The distribution of distances between paired reads, which is the fragment length the library preparation produced. A peak below the combined read length means the pairs overlap, and adapter read-through follows.",
  },
  "ui.chart_mapq": {
    term: "Mapping quality",
    description:
      "How confidently each read was placed. A large bar at zero is reads the aligner could not place uniquely — repeats and multi-mapping — and those are what most downstream tools discard first.",
    computed:
      "The scale is the aligner's, not a shared one. Under STAR the value encodes the number of loci a read mapped to rather than a phred-scaled probability, so a STAR BAM's bars are not comparable with a BWA BAM's; the chart says so when it detects that scale.",
  },
  "ui.feature_coverage_table": {
    term: "Per-feature coverage",
    description:
      "Every feature in the annotation with its read count and breadth of coverage — the fraction of the feature's own length any read touched. Sorted worst first, so a gene with reads but almost no breadth (a handful of reads piling onto one end) surfaces before healthier ones.",
    computed:
      "Computed with `bedtools coverage` between the BAM and the project's annotation (GFF or BED). Breadth is bases covered divided by feature length, not depth — a feature can have many reads and still show low breadth if they cluster.",
  },

  // ---- Results tab: variants ---------------------------------------------
  "ui.vcf_variants": {
    term: "Variants",
    description:
      "How many records the VCF holds — sites, not alleles. A multiallelic site with three alternates counts once here and once again under Multiallelic, which is why SNPs plus Indels need not add up to this total.",
    computed:
      "bcftools stats' record count. Sites the caller emitted with no ALT are included in it.",
  },
  "ui.vcf_snps": {
    term: "SNPs",
    description:
      "Single-base substitutions. In a whole-genome call set these are the large majority; a set where indels approach them in number usually points at an alignment or filtering problem rather than at biology.",
    computed:
      "bcftools' own SNP site count, which classifies by site and not by allele.",
  },
  "ui.vcf_indels": {
    term: "Indels",
    description:
      "Sites where an allele's length differs from the reference. They are harder to call than substitutions and are where callers disagree most, so a large indel count relative to SNPs is worth checking against the caller's own filters before trusting it.",
  },
  "ui.vcf_ti_tv": {
    term: "Ti/Tv",
    description:
      "Transitions (A↔G, C↔T) divided by transversions. Transitions are chemically more likely, so a real call set is not near 0.5 — the value expected by chance. Around 2.0–2.1 is normal for whole-genome data and around 3.0 for exome, where the coding sequence is enriched for them. A number drifting toward 0.5 means false positives are diluting the real calls, which makes this the fastest read on whether the filtering was strict enough.",
    computed:
      "From bcftools stats over substitutions in the whole file. The expected value depends on the assay, so compare it against runs of the same kind rather than against a single fixed target.",
  },
  "ui.vcf_pass_pct": {
    term: "PASS",
    description:
      "The share of records the caller's own filters passed. Shown only when the file uses FILTER at all: bcftools call stamps every record with '.', and reporting that as either 0% or 100% would assert a filtering result that never happened.",
    computed: "PASS records over total records, from the FILTER tally of the whole file.",
  },
  "ui.vcf_multiallelic": {
    term: "Multiallelic",
    description:
      "Sites carrying more than one alternate allele. They are one record here but several events biologically, and many downstream tools need them split first — a high count is a signal to normalise before doing anything else with the file.",
  },
  "ui.chart_variant_density": {
    term: "Variant density",
    description:
      "Where variants sit across the reference, contigs laid end to end. Clusters are worth a look: a dense block is often a repetitive or poorly-aligned region generating calls rather than a genuinely variable one.",
    computed:
      "Bar heights use a square-root scale, not a straight count. Variant density is severely long-tailed, and a linear scale renders everything but the peaks as a flat line. Read it as where the variants are, not as an exact ratio between bars; hover for the real count.",
  },
  "ui.chart_vcf_qual": {
    term: "QUAL distribution",
    description:
      "The caller's confidence score across the call set. A large mass at low QUAL is the population most filters remove, and where it sits relative to the filter threshold shows how much the call set would change if that threshold moved.",
    computed: "From bcftools stats, which tallies this over SNPs.",
  },
  "ui.chart_vcf_depth": {
    term: "Depth distribution",
    description:
      "Read depth at called sites. The peak should sit near the alignment's mean depth; calls far above it are usually in collapsed repeats, where reads from several loci pile onto one.",
    computed:
      "Counted per site rather than per genotype, so a file the caller never genotyped still draws a real distribution instead of an empty one.",
  },
  "ui.vcf_substitutions": {
    term: "Substitution types",
    description:
      "Counts for each of the twelve possible base changes. The pairs that make up Ti/Tv dominate a healthy set; an unusual excess of C→A is the classic signature of oxidative damage during library preparation rather than of real variation.",
  },
  "ui.vcf_filters": {
    term: "Filters",
    description:
      "How many records carry each FILTER value the file uses. \"No filter applied\" means the record carries '.', which is what a caller that never filters emits — different from a record that was assessed and passed.",
  },
  "ui.vcf_per_contig": {
    term: "Per-contig counts",
    description:
      "Variants per contig, with a per-kb rate that makes contigs of different lengths comparable. An outlier rate is usually a difficult region rather than a variable one.",
    computed:
      "SNPs and indels here are classified by site the same way bcftools classifies them, so these columns sum to the headline row above rather than drifting from it on files with multiallelic sites. A site whose alleles are all the same length but longer than one base is an MNP and appears only in the total.",
  },

  // ---- Results tab: differential expression --------------------------------
  "ui.de_contrast": {
    term: "Contrast",
    description:
      "Which two conditions were compared, and how many samples each contributed. Every fold change below is the test condition relative to the reference, so reading the direction of a result depends on this row.",
  },
  "ui.de_genes_tested": {
    term: "Genes tested",
    description:
      "How many genes carry an adjusted p-value, out of all the genes in the count matrix. The difference is not a failure: DESeq2 drops genes with too few reads to say anything about, and testing them anyway would cost statistical power across every gene that remains.",
    computed:
      "Counted as the rows where padj is set. Genes filtered out show an em dash in the table rather than a p-value of 1.0, which would claim they were tested and found unremarkable.",
  },
  "ui.de_significant": {
    term: "Significant genes",
    description:
      "Genes whose adjusted p-value clears the run's alpha, split by direction. The adjustment is what makes this count meaningful — an unadjusted list of 20,000 tests contains a thousand results at p < 0.05 by chance alone.",
    computed:
      "Split by the sign of the log₂ fold change, so \"up\" means up in the test condition relative to the reference.",
  },
  "ui.chart_sample_pca": {
    term: "Sample clustering",
    description:
      "Samples projected onto their first two principal components. This is the plot that can invalidate everything below it: a replicate sitting with the wrong group means the contrast tested a design the samples do not match, and no amount of reading the p-value table reveals that.",
    computed:
      "Computed on log₂(normalised count + 1) over the most variable genes, then projected by SVD. The axis percentages say how much of the total variance each component carries — a low pair means the samples do not separate cleanly in two dimensions, not that the plot is wrong.",
  },
  "ui.chart_sample_correlation": {
    term: "Sample correlation",
    description:
      "How strongly every pair of samples agrees, as an N x N shaded matrix with samples grouped by condition. It answers what the projection above cannot: two replicates can sit adjacent on PC1/PC2 while correlating poorly, since those two components may carry only a modest share of the variance, and a batch effect orthogonal to both is invisible in the scatter but obvious as a block here.",
    computed:
      "Spearman's rho over the same log-normalised top-variance genes the projection uses, so the two plots always describe the same gene set. Spearman rather than Pearson because even after log\u2082 a handful of very highly expressed genes carry most of the remaining spread, and ranking bounds each gene's contribution. The colour scale spans the observed off-diagonal range rather than a fixed \u22121 to 1 \u2014 real samples in one experiment correlate somewhere in the 0.9s, and a fixed scale flattens exactly the differences worth seeing. Not drawn below three samples, where there is no structure to show.",
  },
  "ui.chart_volcano": {
    term: "Volcano plot",
    description:
      "Significance against effect size, one point per gene. The interesting corners are top-left and top-right: large change and strong evidence. A gene high on the y-axis with a small fold change is a reliable but tiny difference.",
    computed:
      "Coloured points clear both the run's alpha and a two-fold change. The y-axis is −log₁₀(padj), clamped to the smallest non-zero p-value present, because padj underflows to zero on strong results and an infinity would blank the plot.",
  },
  "ui.chart_ma": {
    term: "MA plot",
    description:
      "Fold change against expression level. A funnel widening to the left is the expected shape and the point of the plot: at low counts a ratio is mostly noise, so the largest apparent changes there are the least trustworthy.",
    computed: "Base mean on a log₁₀ axis, since expression spans several orders of magnitude.",
  },
  "ui.de_gene_table": {
    term: "Gene results",
    description:
      "Every tested gene with its mean count, log₂ fold change, standard error, and adjusted p-value. Sorted by significance by default. A large fold change with a large standard error beside it is one or two samples driving the result.",
    computed:
      "Mean count is DESeq2's baseMean — the average of normalised counts across all samples in both conditions, not raw reads.",
  },

  // ---- Results tab: annotation ---------------------------------------------
  "ui.annotation_type_counts": {
    term: "Features by type",
    description:
      "How many features of each type the file declares, in its own vocabulary. A GFF3 gene model nests gene, mRNA, exon and CDS rows describing the same locus, so these counts overlap by design and do not sum to a number of genes.",
  },
  "ui.annotation_biotype_counts": {
    term: "Features by biotype",
    description:
      "The biological classification each feature carries, where the file records one — protein_coding, lncRNA, pseudogene and the rest. A BED file or a peak call has none, and the block simply does not appear rather than showing zeros.",
  },
  "ui.chart_feature_density": {
    term: "Feature density",
    description:
      "Features per megabase for each sequence, which makes sequences of very different lengths comparable. An unannotated scaffold and a densely annotated chromosome are both normal; a chromosome far below its neighbours is usually an incomplete annotation rather than a sparse one.",
    computed: "Only sequences whose length is known from the reference get a rate; the rest are shown by count.",
  },
  "ui.chart_annotation_coverage": {
    term: "Annotated coverage",
    description:
      "The fraction of each sequence lying under at least one feature. Overlapping features are counted once, so a densely nested gene model does not report more than 100%.",
    computed:
      "Needs sequence lengths, which come from the annotation's reference rather than from the annotation itself — except for GenBank, which states them on its own LOCUS lines. With no matching reference in the project the chart is replaced by a note rather than drawn empty.",
  },
  "ui.chart_feature_lengths": {
    term: "Feature lengths",
    description:
      "How many features fall in each length band. The shape identifies the file as much as its name does: exon-scale peaks near a few hundred bases, gene-scale spans running into tens of kilobases.",
    computed:
      "Ten bands with edges at 100, 250, 500 bp and so on up to 100 kb, plus an overflow band above that. Length is the feature's own span, so a gene row's introns are included in it.",
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
