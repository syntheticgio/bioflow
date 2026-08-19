/** One taxon in the `taxonomy` fact's abundance table, already sorted
 *  descending by `pct` -- see `kraken_runner.top_taxa` on the backend. */
export interface TaxonEntry {
  name: string;
  rank: string;
  taxid: number;
  pct: number;
}

/**
 * Facts written onto a reads object by a completed `classify_reads` job.
 *
 * Bracken's refined abundances are preferred over Kraken2's raw species
 * rows when Bracken ran (`bracken_used`); `bracken_skipped` carries why it
 * didn't, verbatim from the tool, when it's absent instead. See
 * `kraken_runner.top_taxa` and `queue/kraken_handlers.py`.
 */
export interface TaxonomyFactsData {
  taxa: TaxonEntry[];
  unclassified_pct: number;
  db_key: string;
  bracken_used: boolean;
  bracken_skipped?: string;
}

/**
 * Present only when the reads' dominant genus-level classification
 * disagrees with `metadata.organism` -- see `kraken_runner.organism_mismatch`.
 * Absent metadata means no check ran, not that it passed.
 */
export interface TaxonomyMismatchData {
  claimed: string;
  dominant: { name: string; pct: number }[];
}
