import { Fragment, useState } from "react";
import { formatDate, isIsoTimestamp } from "../lib/format";
import { isStarMapqScale } from "../lib/mapq";
import { InfoMarker } from "./InfoMarker";
import { TruncatedValue } from "./TruncatedValue";

/**
 * Renders parser output with human labels and honest units.
 *
 * The important rule here: anything estimated is shown as estimated. A read
 * count that is really an extrapolation must never look like a measurement,
 * because it will end up in someone's methods section.
 */

/** Exported for the metric-info exhaustiveness test, which asserts every
 *  labelled fact either has an explanation or is listed as not needing one. */
export const LABELS: Record<string, string> = {
  read_length: "Read length",
  read_length_min: "Read length (min)",
  read_length_max: "Read length (max)",
  read_count_estimate: "Read count",
  sampled_records: "Records sampled",
  first_read_ids: "First read IDs",
  paired: "Paired-end",
  paired_hint: "Mate",
  sort_order: "Sort order",
  sam_version: "SAM version",
  vcf_version: "VCF version",
  reference_count: "Reference contigs",
  reference_names: "Contig names",
  reference_lengths: "Contig lengths",
  reference_total_length: "Reference length",
  sample_names: "Samples",
  sample_count: "Sample count",
  read_group_count: "Read groups",
  platforms: "Platform",
  program_chain: "Programs",
  has_index: "Indexed",
  info_fields: "INFO fields",
  info_field_count: "INFO field count",
  format_fields: "FORMAT fields",
  filters: "Filters",
  variant_types_sampled: "Variant types",
  record_count: "Records",
  first_contig: "First contig",
  sequence_count: "Sequences",
  sequence_count_estimate: "Sequences",
  sequence_names: "Sequence names",
  total_bases: "Total bases",
  column_counts: "Columns",
  header_lines: "Header lines",
  sequence_longest: "Longest sequence",
  sequence_shortest: "Shortest sequence",
  // Contiguity: the numbers QUAST would report, computed here instead since
  // QUAST is not packaged for this image. See docs/superpowers/specs/
  // 2026-08-02-post-assembly-qc-design.md.
  sequence_n50: "N50",
  sequence_n90: "N90",
  sequence_l50: "L50",
  sequence_auN: "auN",
  sequence_gap_count: "Gaps",
  sequence_gap_bases: "Gap bases",
  // Within a group the heading already says which tool wrote these, so the
  // labels drop the redundant prefix: "BAM statistics / Tool", not
  // "BAM statistics / BAM stats tool version".
  bam_stats_computed_at: "Computed",
  bam_stats_tool_version: "Tool",
  bam_stats_status: "Status",
  // No qc_* labels: those facts are rendered by QcReport and never reach this
  // table (see isSuppressed).
  trimmed_by: "Tool",
  trim_tool_version: "Version",
  aligned_by: "Tool",
  aligner: "Aligner",
  aligner_version: "Version",
  index_built_by: "Tool",
  index_tool_version: "Version",
  index_status: "Status",
  quality_encoding: "Quality encoding",
  gc_content_percent: "GC content",
  gc_per_read_mean: "GC per read (mean)",
  mean_quality: "Mean quality",
  min_position_quality: "Lowest position quality",
  mean_mapping_quality: "Mean MAPQ",
  uniquely_mapped_percent: "Uniquely mapped",
  mapq_scale: "MAPQ scale",
  mapped_percent: "Mapped",
  duplicate_percent: "Duplicates",
};

// Rendered as annotations on other rows, or in their own panel — not as rows
// of their own.
const SUPPRESSED = new Set([
  "read_count_exact",
  "record_count_exact",
  "sequence_count_exact",
  "estimate_note",
  "parse_warning",
  "parse_error",
  "reference_names_truncated",
  "sequence_names_truncated",
  "sample_names_truncated",
  // SRA provenance has its own section; see SraPanel.
  "sra_accession",
  "sra_source",
  "sra_fields_applied",
  "sra_conflicts",
  "sra_error",
  // Rendered as charts below the table, not as raw arrays.
  "base_composition",
  "quality_per_position",
  "stats_sampled_reads",
  "stats_sampled_bases",
  "stats_sampling",
  "sequence_lengths_partial",
  // Rendered as the Longest/Shortest rows in AssemblyFacts; a 50-entry dict
  // would swamp the generic table.
  "sequence_lengths",
  // Trimming has its own before/after section; see TrimReport. The raw report
  // is a nested object that the generic renderer would print as [object Object].
  "trim_report",
  "trim_outputs",
  "trim_params",
  "trimmed_by",
  "trim_tool_version",
  // Every qc_* key is suppressed by isSuppressed() below -- QcReport renders
  // them properly, with units and the report links. See the note there.
  // BAM results have their own charts, summary row and per-contig table; see
  // BamResults. These are arrays of objects that the generic renderer can only
  // print as a wall of key: value chips, duplicating what's drawn below.
  "bam_stats_summary",
  "bam_stats_coverage_bins",
  "bam_stats_coverage_boundaries",
  "bam_stats_cumulative",
  "bam_stats_contigs_top",
  "bam_stats_report",
  "insert_size_histogram",
  "mapq_histogram",
]);

/**
 * Facts, grouped by where they came from.
 *
 * A single flat list mixes three unrelated provenances -- what the file's own
 * header claims, what we measured by scanning records, and what a pipeline
 * tool wrote afterwards -- and alphabetical order scatters each tool's output
 * across the whole table. Grouping keeps "what did samtools say" answerable in
 * one glance, and makes the provenance of a number visible, which matters when
 * a header claim and a measurement disagree.
 *
 * `keys` fixes the order within a group. A group's `match` catches keys not
 * listed by name -- how each tool's own facts stay together as the pipelines
 * grow, without this list needing an edit per new fact.
 */
type FactGroup = {
  title: string;
  /** Explains what the group's numbers are, when the source isn't obvious. */
  note?: string;
  keys: string[];
  match?: (key: string) => boolean;
};

const GROUPS: FactGroup[] = [
  {
    title: "File contents",
    keys: [
      "sort_order", "paired", "paired_hint", "read_length", "read_length_min",
      "read_length_max", "read_count_estimate", "record_count", "sequence_count",
      "sequence_count_estimate", "total_bases", "sequence_longest",
      "sequence_shortest", "sequence_n50", "sequence_n90", "sequence_l50",
      "sequence_auN", "sequence_gap_count", "sequence_gap_bases",
      "sequence_names", "first_contig", "first_read_ids",
      "sampled_records", "column_counts", "header_lines",
    ],
  },
  {
    title: "Measured quality",
    note: "Computed by sampling records in this file.",
    keys: [
      "quality_encoding", "mean_quality", "min_position_quality",
      "gc_content_percent", "gc_per_read_mean", "mapped_percent",
      "duplicate_percent", "mean_mapping_quality", "uniquely_mapped_percent",
      "mapq_scale",
    ],
  },
  {
    title: "Header",
    note: "Declared by the file itself, not measured.",
    keys: [
      "sam_version", "vcf_version", "sample_names", "sample_count", "platforms",
      "read_group_count", "reference_count", "reference_total_length",
      "reference_names", "reference_lengths", "info_fields", "info_field_count",
      "format_fields", "filters", "variant_types_sampled", "program_chain",
      "has_index",
    ],
  },
  // There is deliberately no "Quality control" group here. QcReport renders
  // QC's facts with their units and report links, and isSuppressed() filters
  // every qc_* key before grouping -- a group here would collect nothing and
  // previously produced a second, worse copy of that section.
  {
    title: "Trimming",
    note: "Written by the trim step.",
    keys: ["trimmed_by", "trim_tool_version"],
    match: (k) => k.startsWith("trim_"),
  },
  {
    title: "Alignment",
    note: "Written by the align step.",
    keys: ["aligned_by", "aligner", "aligner_version", "align_params"],
    match: (k) => k.startsWith("align") || k === "aligned_by",
  },
  {
    title: "BAM statistics",
    note: "Written by samtools; see the charts below.",
    keys: ["bam_stats_status", "bam_stats_tool_version", "bam_stats_computed_at"],
    match: (k) => k.startsWith("bam_stats_"),
  },
  {
    title: "Indexing",
    note: "Written by the index step.",
    keys: ["index_status", "index_built_by", "index_tool_version"],
    match: (k) => k.startsWith("index_"),
  },
];

/**
 * Split keys into their groups, preserving each group's declared order.
 *
 * Anything unclaimed lands in a trailing "Other" group rather than being
 * dropped -- a new fact from a parser must still be visible before anyone
 * remembers to classify it here.
 */
function groupKeys(keys: string[]): { title: string; note?: string; keys: string[] }[] {
  const remaining = new Set(keys);
  const out: { title: string; note?: string; keys: string[] }[] = [];

  for (const group of GROUPS) {
    const named = group.keys.filter((k) => remaining.has(k));
    const matched = group.match
      ? [...remaining].filter((k) => !group.keys.includes(k) && group.match!(k)).sort()
      : [];
    const members = [...named, ...matched];
    if (members.length === 0) continue;
    members.forEach((k) => remaining.delete(k));
    out.push({ title: group.title, note: group.note, keys: members });
  }

  if (remaining.size > 0) {
    out.push({ title: "Other", keys: [...remaining].sort() });
  }
  return out;
}

function label(key: string): string {
  return (LABELS[key] ?? key.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()))
    .replace(/^Ai\b/, "AI");
}

function formatNumber(n: number): string {
  if (n === 0 || !Number.isFinite(n)) return n.toString();
  const abs = Math.abs(n);
  if (abs >= 1) return n.toLocaleString();
  // Leading zeros don't count as precision, so scale the budget by magnitude:
  // 0.87 keeps 3 digits, 0.00042 keeps 3 digits rather than rounding to 0.
  const sigFigs = Math.max(3, -Math.floor(Math.log10(abs)) + 2);
  const text = n.toLocaleString(undefined, { maximumSignificantDigits: sigFigs });
  // Rounding a fraction up to a bare "1" would claim a whole number the value
  // never reached; keep enough digits to stay visibly below it.
  if (Math.abs(Number(text.replace(/,/g, ""))) >= 1) {
    return n.toLocaleString(undefined, { maximumSignificantDigits: sigFigs + 3 });
  }
  return text;
}

// How many more entries each click of "+N more" reveals. Some facts (BAM
// coverage bins) run to a thousand entries, so revealing everything at once
// buries the rest of the table.
const PAGE = 20;

/**
 * Last-resort text for one value in a list or nested object.
 *
 * The generic table gets whatever a parser or pipeline decided to put in
 * `facts`, so it has to stay honest about values it has no dedicated renderer
 * for: a nested object printed via template literal or String() becomes
 * "[object Object]", which tells the reader nothing and hides real data. A
 * fact worth a good layout belongs in its own panel (see SUPPRESSED); until
 * then, showing its contents beats showing its type.
 */
function scalarText(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "number") return formatNumber(v);
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (isIsoTimestamp(v)) return formatDate(v);
  if (typeof v !== "object") return String(v);
  if (Array.isArray(v)) return v.map(scalarText).join(", ");
  return Object.entries(v as Record<string, unknown>)
    .map(([k, inner]) => `${k}: ${scalarText(inner)}`)
    .join(", ");
}

function CollapsibleList({ items, max = 8 }: { items: unknown[]; max?: number }) {
  const [visible, setVisible] = useState(max);
  const shown = items.slice(0, visible);
  const remaining = items.length - shown.length;
  return (
    <span>
      {shown.map((v, i) => (
        <span key={i} className="chip">
          {scalarText(v)}
        </span>
      ))}
      {remaining > 0 && (
        <button
          type="button"
          onClick={() => setVisible(visible + PAGE)}
          style={{
            color: "var(--accent)",
            fontSize: 11,
            marginLeft: 4,
            verticalAlign: "middle",
          }}
        >
          +{remaining} more
        </button>
      )}
      {remaining <= 0 && items.length > max && (
        <button
          type="button"
          onClick={() => setVisible(max)}
          style={{
            color: "var(--accent)",
            fontSize: 11,
            marginLeft: 4,
            verticalAlign: "middle",
          }}
        >
          show less
        </button>
      )}
    </span>
  );
}

function renderValue(key: string, value: unknown, facts: Record<string, unknown>) {
  if (typeof value === "boolean") return value ? "Yes" : "No";

  // Facts arrive as whatever a parser or pipeline stored, so timestamps land
  // here as raw ISO strings ("2026-07-29T19:03:23.276489+00:00") unless they
  // are recognised by shape rather than by key name.
  if (isIsoTimestamp(value)) return formatDate(value);

  if (key === "sequence_longest" || key === "sequence_shortest") {
    const v = value as { name: string; length: number };
    return (
      <span>
        <span className="mono">{v.name}</span> · {formatNumber(v.length)} bp
      </span>
    );
  }

  // Lengths in bases, same unit as sequence_longest/shortest above -- N50,
  // N90, auN and the gap-base total are all counts of bases, not of contigs
  // (sequence_l50 and sequence_gap_count are, and fall through to the plain
  // number case below).
  if (
    (key === "sequence_n50" ||
      key === "sequence_n90" ||
      key === "sequence_auN" ||
      key === "sequence_gap_bases") &&
    typeof value === "number"
  ) {
    return `${formatNumber(value)} bp`;
  }

  if (key === "read_count_estimate" || key === "sequence_count_estimate") {
    // "~" plus an explicit qualifier: two signals, because this number looks
    // authoritative and is not.
    return (
      <span>
        ~{formatNumber(value as number)}{" "}
        <span style={{ color: "var(--text-faint)", fontSize: 11 }}>(estimated)</span>
      </span>
    );
  }

  if (key === "record_count" || key === "sequence_count") {
    const exactKey = key === "record_count" ? "record_count_exact" : "sequence_count_exact";
    const exact = facts[exactKey] !== false;
    return (
      <span>
        {exact ? "" : "~"}
        {formatNumber(value as number)}
        {!exact && (
          <span style={{ color: "var(--text-faint)", fontSize: 11 }}> (estimated)</span>
        )}
      </span>
    );
  }

  // The label no longer carries "percent", so the value has to.
  if (key.endsWith("_percent") && typeof value === "number") {
    return `${formatNumber(value)}%`;
  }

  if (typeof value === "number") return formatNumber(value);

  if (Array.isArray(value)) {
    if (value.length === 0) return "—";
    // The program chain lists one entry per invocation, so a tool run several
    // times shows up several times. Which tools touched the file is the useful
    // part, not how many times each ran. Deduping here (not only in the parser)
    // also cleans up results stored before the parser started deduping.
    if (key === "program_chain") {
      const distinct = [...new Set(value.map(scalarText))];
      return <CollapsibleList items={distinct} />;
    }
    const truncated =
      facts[`${key}_truncated`] === true ||
      (key === "reference_names" &&
        typeof facts.reference_count === "number" &&
        facts.reference_count > value.length);
    return (
      <span>
        <CollapsibleList items={value} />
        {truncated && (
          <span style={{ color: "var(--text-faint)", fontSize: 11 }}>
            {" "}
            (first {value.length} shown)
          </span>
        )}
      </span>
    );
  }

  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    return (
      <CollapsibleList
        items={entries.map(([k, v]) => `${k}: ${scalarText(v)}`)}
        max={5}
      />
    );
  }

  const text = String(value);

  // Long unbroken strings -- hashes, paths, adapter sequences -- are truncated
  // with a control to see the rest. Prose is left alone: it wraps readably on
  // its spaces, and hiding the end of a sentence helps nobody. The test is
  // whether the value has whitespace to wrap on, not how long it is.
  if (text.length > 40 && !/\s/.test(text)) {
    return <TruncatedValue value={text} head={28} />;
  }

  return text;
}

/**
 * How many facts this table would actually render.
 *
 * Exported so a tab label can count them without duplicating the suppression
 * list -- a count that disagreed with the rows below it would be worse than
 * no count at all.
 */
/**
 * Whether a fact is rendered somewhere better than this generic table.
 *
 * The named entries are one-offs; the `qc_` prefix is a whole family. QcReport
 * renders those with their units ("17,230 bp", "Q16.8"), folds the chemistry
 * and its reasoning into one row, and links the tool's own HTML report -- so
 * repeating them here produced two "Quality control" sections saying the same
 * thing, one of them worse. Anything QC writes that QcReport does not show
 * should be added there rather than un-suppressed here.
 */
function isSuppressed(key: string, facts: Record<string, unknown>): boolean {
  // A mean over STAR's MAPQ codes is not a quality -- it lands near 250 for a
  // good run, against ~50 for the same reads through bwa-mem2. Ingest stopped
  // writing it, but BAMs aligned before that still carry the number, and this
  // table would print it beside every other aligner's as though comparable.
  if (key === "mean_mapping_quality" && isStarMapqScale(facts)) return true;
  return SUPPRESSED.has(key) || key.startsWith("qc_");
}

export function countVisibleFacts(facts: Record<string, unknown>): number {
  return Object.keys(facts).filter((k) => !isSuppressed(k, facts)).length;
}

export function FactsTable({
  facts,
  columns = false,
}: {
  facts: Record<string, unknown>;
  /** Lay the groups out in columns rather than one stack. Each group is a
   *  self-contained card, so they can flow into however many columns fit. */
  columns?: boolean;
}) {
  const keys = Object.keys(facts).filter((k) => !isSuppressed(k, facts));
  if (keys.length === 0 && !facts.parse_error && !facts.parse_warning) return null;

  const groups = groupKeys(keys);
  // One group is just a list; a heading over the whole table would be noise.
  // In column layout the titles are what make the groups legible as groups,
  // so they always show there.
  const showTitles = columns || groups.length > 1;

  // In column mode the caller owns the .facts-columns container, so its own
  // sections can flow in the same columns as these groups instead of sitting
  // full-width beneath them. That means emitting the groups as bare siblings:
  // any wrapper here would become a single column item and take the whole
  // width with it.
  const Wrapper = columns ? Fragment : "div";

  return (
    <Wrapper>
      {/* Parse problems are about the file as a whole, so they stay above the
          columns rather than flowing as one more card. */}
      {typeof facts.parse_error === "string" && (
        <div className="error-box">
          Could not parse this file: {facts.parse_error}
        </div>
      )}
      {typeof facts.parse_warning === "string" && (
        <div className="warn-box">{facts.parse_warning}</div>
      )}

      <ColumnHost columns={columns}>
        {groups.map((group, i) => (
          <div
            key={group.title}
            className={columns ? "facts-group" : undefined}
            style={columns ? undefined : { marginTop: i === 0 ? 0 : 14 }}
          >
            {showTitles &&
              (columns ? (
                /* A real heading in column layout: it is the only thing
                   telling one card from the next, so it carries the weight
                   the stacked layout got from position alone. */
                <div className="facts-group-title">
                  <span>{group.title}</span>
                  {group.note && (
                    <span className="facts-group-note">{group.note}</span>
                  )}
                </div>
              ) : (
                <div
                  style={{
                    fontSize: 11,
                    color: "var(--text-faint)",
                    textTransform: "uppercase",
                    letterSpacing: 0.5,
                    marginBottom: 6,
                  }}
                >
                  {group.title}
                  {group.note && (
                    <span style={{ textTransform: "none", letterSpacing: 0 }}>
                      {" · "}
                      {group.note}
                    </span>
                  )}
                </div>
              ))}
            <dl className="kv">
              {group.keys.map((k) => (
                <span key={k} style={{ display: "contents" }}>
                  <dt>
                    {label(k)}
                    <InfoMarker metric={k} />
                  </dt>
                  <dd>{renderValue(k, facts[k], facts)}</dd>
                </span>
              ))}
            </dl>

            {/* The note qualifies the estimated count, so it sits inside the
                group holding that key. As a sibling of the groups it would
                become its own column item and drift away from the row it
                explains. */}
            {typeof facts.estimate_note === "string" &&
              group.keys.some((k) => k.endsWith("_estimate")) && (
                <div
                  style={{
                    color: "var(--text-faint)",
                    fontSize: 11,
                    marginTop: 8,
                  }}
                >
                  {facts.estimate_note}
                </div>
              )}
          </div>
        ))}
      </ColumnHost>
    </Wrapper>
  );
}

/** Wraps the groups in the column container, or passes them straight through
 *  when the caller is providing one. */
function ColumnHost({
  columns,
  children,
}: {
  columns: boolean;
  children: React.ReactNode;
}) {
  return columns ? <>{children}</> : <div>{children}</div>;
}
