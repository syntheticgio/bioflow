import { useState } from "react";

/**
 * Renders parser output with human labels and honest units.
 *
 * The important rule here: anything estimated is shown as estimated. A read
 * count that is really an extrapolation must never look like a measurement,
 * because it will end up in someone's methods section.
 */

const LABELS: Record<string, string> = {
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
]);

const ORDER = [
  "sort_order", "paired", "paired_hint", "read_length", "read_length_min",
  "read_length_max", "read_count_estimate", "record_count", "sequence_count",
  "sequence_count_estimate", "total_bases", "sample_names", "sample_count",
  "platforms", "read_group_count", "reference_count", "reference_total_length",
  "reference_names", "reference_lengths", "vcf_version", "sam_version",
  "info_fields", "format_fields", "filters", "variant_types_sampled",
  "has_index", "program_chain", "first_contig", "first_read_ids",
  "sampled_records", "column_counts", "header_lines",
];

function label(key: string): string {
  return LABELS[key] ?? key.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

function formatNumber(n: number): string {
  return n.toLocaleString();
}

// How many more entries each click of "+N more" reveals. Some facts (BAM
// coverage bins) run to a thousand entries, so revealing everything at once
// buries the rest of the table.
const PAGE = 20;

function CollapsibleList({ items, max = 8 }: { items: unknown[]; max?: number }) {
  const [visible, setVisible] = useState(max);
  const shown = items.slice(0, visible);
  const remaining = items.length - shown.length;
  return (
    <span>
      {shown.map((v, i) => (
        <span key={i} className="chip">
          {String(v)}
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

  if (key === "sequence_longest" || key === "sequence_shortest") {
    const v = value as { name: string; length: number };
    return (
      <span>
        <span className="mono">{v.name}</span> · {formatNumber(v.length)} bp
      </span>
    );
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

  if (typeof value === "number") return formatNumber(value);

  if (Array.isArray(value)) {
    if (value.length === 0) return "—";
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
        items={entries.map(([k, v]) =>
          typeof v === "number" ? `${k}: ${formatNumber(v)}` : `${k}: ${v}`,
        )}
        max={5}
      />
    );
  }

  return String(value);
}

export function FactsTable({ facts }: { facts: Record<string, unknown> }) {
  const keys = Object.keys(facts).filter((k) => !SUPPRESSED.has(k));
  if (keys.length === 0 && !facts.parse_error && !facts.parse_warning) return null;

  const ordered = [
    ...ORDER.filter((k) => keys.includes(k)),
    ...keys.filter((k) => !ORDER.includes(k)).sort(),
  ];

  return (
    <div>
      {typeof facts.parse_error === "string" && (
        <div className="error-box">
          Could not parse this file: {facts.parse_error}
        </div>
      )}
      {typeof facts.parse_warning === "string" && (
        <div className="warn-box">{facts.parse_warning}</div>
      )}

      {ordered.length > 0 && (
        <dl className="kv">
          {ordered.map((k) => (
            <span key={k} style={{ display: "contents" }}>
              <dt>{label(k)}</dt>
              <dd>{renderValue(k, facts[k], facts)}</dd>
            </span>
          ))}
        </dl>
      )}

      {typeof facts.estimate_note === "string" && (
        <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 8 }}>
          {facts.estimate_note}
        </div>
      )}
    </div>
  );
}
