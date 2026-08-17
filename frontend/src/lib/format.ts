export function formatBytes(bytes: number, decimals = 1): string {
  if (bytes === 0) return "0 B";
  if (!Number.isFinite(bytes)) return "—";
  const k = 1024;
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(k)), units.length - 1);
  const value = bytes / Math.pow(k, i);
  return `${value.toFixed(i === 0 ? 0 : decimals)} ${units[i]}`;
}

/**
 * A timestamp as a person reads it, in their own timezone.
 *
 * Seconds are kept: pipeline steps within one run land in the same minute, and
 * "which finished first" is a question people actually ask of these rows.
 */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/**
 * Just the clock time, for rows that are necessarily from the current session.
 *
 * This is right for the activity page's featured run and its step list, where
 * every line belongs to something happening now and the question is the order
 * steps finished in, not which day they were.
 *
 * It is not right for a list spanning days -- the finished-runs ledger used
 * this and gave every line a bare clock, so a run from last week was
 * indistinguishable from one an hour ago (#456). Those rows use
 * `formatRelative` and carry the exact time on hover.
 */
export function formatClock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/**
 * How long ago something happened, for a list that spans days.
 *
 * "How stale is this" is the question a column of finished runs is actually
 * asked, and an age answers it without the reader subtracting dates. Past a
 * week the age stops being readable as a duration -- nobody parses "63d ago"
 * -- so it gives the day instead, with the year once that is ambiguous too.
 *
 * Deliberately lossy: the exact instant lives in a `title` on the same row,
 * because a ledger line has room for one of the two and this is the one that
 * is read at a glance.
 */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";

  const now = new Date();
  const seconds = (now.getTime() - d.getTime()) / 1000;

  // A negative age means the clock behind the timestamp is ahead of the
  // browser's -- skew, not a run scheduled in the future. "-3m ago" reads as
  // a bug, so it clamps to the same thing a fresh run shows.
  if (seconds < 60) return "just now";

  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;

  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;

  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    ...(d.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
  });
}

// Matches an ISO-8601 instant: date, T, time, and a zone offset or Z. Anchored
// and zone-required on purpose -- a bare "2026-07-29" or an accession that
// happens to contain digits must not be silently reinterpreted as a date.
const ISO_TIMESTAMP = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$/;

/** True when a string is an ISO instant safe to hand to formatDate. */
export function isIsoTimestamp(v: unknown): v is string {
  return typeof v === "string" && ISO_TIMESTAMP.test(v);
}

export function shortHash(hash: string | null | undefined, chars = 12): string {
  if (!hash) return "—";
  return `${hash.slice(0, chars)}…`;
}

const FORMAT_LABELS: Record<string, string> = {
  fastq: "FASTQ",
  fasta: "FASTA",
  bam: "BAM",
  sam: "SAM",
  cram: "CRAM",
  vcf: "VCF",
  bcf: "BCF",
  bed: "BED",
  gff: "GFF",
  genbank: "GenBank",
  gtf: "GTF",
  gfa: "GFA",
  text: "Text",
  unknown: "Unknown",
};

export function formatKindLabel(kind: string): string {
  return FORMAT_LABELS[kind] ?? kind.toUpperCase();
}

export function compressionLabel(c: string): string | null {
  if (c === "none") return null;
  // BGZF is worth surfacing distinctly from plain gzip: it is block-compressed
  // and therefore indexable, which determines what tools can do with the file.
  return c === "bgzf" ? "BGZF" : c.toUpperCase();
}

/**
 * Public-archive accession fields that resolve to a canonical NCBI page.
 *
 * Each entry validates before building a URL: a half-typed or mistyped
 * accession should render as plain text rather than a link that 404s.
 */
const ACCESSION_LINKS: Record<
  string,
  { pattern: RegExp; url: (v: string) => string; label: string }
> = {
  // SRA run/experiment/sample/study all resolve through the same /sra/ path.
  sra_run: {
    pattern: /^[SED]RR\d{6,}$/i,
    url: (v) => `https://www.ncbi.nlm.nih.gov/sra/${v}`,
    label: "SRA run",
  },
  sra_experiment: {
    pattern: /^[SED]RX\d{6,}$/i,
    url: (v) => `https://www.ncbi.nlm.nih.gov/sra/${v}`,
    label: "SRA experiment",
  },
  sra_sample: {
    pattern: /^[SED]RS\d{6,}$/i,
    url: (v) => `https://www.ncbi.nlm.nih.gov/sra/${v}`,
    label: "SRA sample",
  },
  // Studies live under Trace's study browser, which shows the run table.
  sra_study: {
    pattern: /^[SED]RP\d{6,}$/i,
    url: (v) =>
      `https://trace.ncbi.nlm.nih.gov/Traces/study/?acc=${v}`,
    label: "SRA study",
  },
  bioproject: {
    pattern: /^PRJ[NED][A-Z]\d+$/i,
    url: (v) => `https://www.ncbi.nlm.nih.gov/bioproject/${v}`,
    label: "BioProject",
  },
  biosample: {
    pattern: /^SAM[NED][A-Z]?\d+$/i,
    url: (v) => `https://www.ncbi.nlm.nih.gov/biosample/${v}`,
    label: "BioSample",
  },
  assembly_accession: {
    // GCA_ (GenBank) or GCF_ (RefSeq), nine digits, dot, version.
    pattern: /^GC[AF]_\d{9}\.\d+$/i,
    url: (v) => `https://www.ncbi.nlm.nih.gov/datasets/genome/${v}`,
    label: "Assembly",
  },
  // A single chromosome or scaffold record, for the Sequence Viewer's
  // "View at NCBI" escape hatch. Separate from assembly_accession, which
  // points at a whole genome's Datasets page.
  nucleotide_accession: {
    pattern: /^[A-Z]{2}_?\d+(\.\d+)?$|^[A-Z]{4}\d{8,}(\.\d+)?$/i,
    url: (v) => `https://www.ncbi.nlm.nih.gov/nuccore/${v}`,
    label: "Sequence",
  },
};

/** External URL for an accession field, or null if it does not look valid. */
export function accessionUrl(key: string, value: unknown): string | null {
  const spec = ACCESSION_LINKS[key];
  if (!spec) return null;
  const v = String(value ?? "").trim();
  if (!v || !spec.pattern.test(v)) return null;
  return spec.url(v.toUpperCase());
}

export function isAccessionField(key: string): boolean {
  return key in ACCESSION_LINKS;
}

/**
 * Elapsed or remaining time, at a resolution that suits the magnitude.
 *
 * Carries hours because pipeline runs reach them; "184m 12s" is technically
 * correct and useless at a glance.
 */
export function formatDuration(ms: number): string {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}
