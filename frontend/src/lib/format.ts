export function formatBytes(bytes: number, decimals = 1): string {
  if (bytes === 0) return "0 B";
  if (!Number.isFinite(bytes)) return "—";
  const k = 1024;
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(k)), units.length - 1);
  const value = bytes / Math.pow(k, i);
  return `${value.toFixed(i === 0 ? 0 : decimals)} ${units[i]}`;
}

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
  });
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
  gtf: "GTF",
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
