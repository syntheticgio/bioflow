import { useEffect, useMemo, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { VariantContigRow, VariantRow } from "../api/types";
import { isNcbiNucleotideAccession, markerLabel } from "../lib/chromosomes";
import { useDebounced } from "../lib/useDebounced";
import { canShowStructure } from "../lib/variants";
import { SequenceViewerModal } from "./SequenceViewerModal";
import { StructureViewerModal } from "./StructureViewerModal";

const PAGE_SIZE = 50;

/**
 * The complete variant table, paginated and filtered server-side against the
 * SQLite index built alongside the report -- see api.vcfStatsVariants.
 */
export function VariantTable({
  objectId,
  reportPath,
  contigs,
  filters,
  samples,
}: {
  objectId: string;
  reportPath?: string;
  contigs: VariantContigRow[];
  filters: { filter: string; count: number }[];
  samples: string[];
}) {
  const [page, setPage] = useState(0);
  const [contig, setContig] = useState("");
  const [filterValue, setFilterValue] = useState("");
  const [variantType, setVariantType] = useState("");
  const [minQualInput, setMinQualInput] = useState("");
  const [consequence, setConsequence] = useState("");
  const [sampleIdx, setSampleIdx] = useState(0);
  // Holds the last known total across a skip_count page turn, so the row
  // count does not flash to nothing while only the page changed.
  const [lastTotal, setLastTotal] = useState<number | null>(null);
  // The variant whose genomic context is open, or null for none.
  const [contextRow, setContextRow] = useState<VariantRow | null>(null);
  const [structureRow, setStructureRow] = useState<VariantRow | null>(null);

  // Contig -> length, for scaling the viewer's window to the sequence.
  // Memoised so the lookup is not rebuilt on every keystroke in the filters.
  const contigLengths = useMemo(
    () => new Map(contigs.map((c) => [c.contig, c.length])),
    [contigs],
  );

  const minQual = useDebounced(minQualInput, 300);

  // Any real filter change starts over at page 0; only the page itself
  // should leave it alone.
  useEffect(() => {
    setPage(0);
  }, [contig, filterValue, variantType, minQual, consequence]);

  const parsedMinQual = minQual.trim() === "" ? undefined : Number(minQual);
  const minQualValid = parsedMinQual == null || !Number.isNaN(parsedMinQual);

  const { data, isLoading } = useQuery({
    queryKey: [
      "vcfstats",
      "variants",
      objectId,
      page,
      contig,
      filterValue,
      variantType,
      minQualValid ? parsedMinQual : undefined,
      consequence,
    ],
    queryFn: () =>
      api.vcfStatsVariants(objectId, {
        offset: page * PAGE_SIZE,
        limit: PAGE_SIZE,
        contig: contig || undefined,
        filterValue: filterValue || undefined,
        variantType: variantType || undefined,
        minQual: minQualValid ? parsedMinQual : undefined,
        consequence: consequence || undefined,
        skipCount: page > 0,
      }),
    placeholderData: keepPreviousData,
    enabled: minQualValid,
  });

  useEffect(() => {
    if (data?.total != null) setLastTotal(data.total);
  }, [data?.total]);

  const total = data?.total ?? lastTotal;
  const rows = data?.rows ?? [];
  const hasNext = rows.length === PAGE_SIZE;

  // Populated from whatever consequences appear on the current page rather
  // than a hardcoded vocabulary -- bcftools csq emits terms this list should
  // not need to anticipate. This means the option list only ever reflects
  // what's visible right now, not the whole callset; an honest limitation
  // rather than a facet query this task doesn't need. The active selection
  // is kept in the list even when filtering has narrowed the page to other
  // values, so choosing an option never makes it vanish from the dropdown.
  const consequenceOptions = useMemo(() => {
    const seen = new Set<string>();
    if (consequence) seen.add(consequence);
    for (const row of rows) {
      if (row.consequence) seen.add(row.consequence);
    }
    return [...seen].sort();
  }, [rows, consequence]);

  // Whether this callset is annotated at all is a property of the file, not of
  // the page you happen to be on. Deciding it per page made the whole control
  // unmount on any page whose rows all fell outside a transcript -- common,
  // since csq only annotates variants inside one -- so the filter bar reflowed
  // as you paged. Latched instead: once a consequence has been seen, the
  // control stays.
  const [isAnnotated, setIsAnnotated] = useState(false);
  useEffect(() => {
    if (rows.some((r) => r.consequence)) setIsAnnotated(true);
  }, [rows]);

  return (
    <div className="section">
      <div
        className="section-title"
        style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}
      >
        <span>Variants</span>
        {reportPath && (
          <a
            href={api.vcfStatsDownloadUrl(objectId, reportPath)}
            style={{ marginLeft: "auto", fontSize: 11 }}
          >
            Download TSV
          </a>
        )}
      </div>

      <div
        style={{
          display: "flex",
          gap: 12,
          flexWrap: "wrap",
          alignItems: "flex-end",
          marginBottom: 10,
          fontSize: 12,
        }}
      >
        <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ color: "var(--text-faint)" }}>Contig</span>
          <select value={contig} onChange={(e) => setContig(e.target.value)}>
            <option value="">All contigs</option>
            {contigs.map((c) => (
              <option key={c.contig} value={c.contig}>
                {c.contig} ({c.variants.toLocaleString()})
              </option>
            ))}
          </select>
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ color: "var(--text-faint)" }}>Filter</span>
          <select value={filterValue} onChange={(e) => setFilterValue(e.target.value)}>
            <option value="">All</option>
            {filters.map((f) => (
              <option key={f.filter} value={f.filter}>
                {f.filter === "." ? "(none)" : f.filter} ({f.count.toLocaleString()})
              </option>
            ))}
          </select>
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ color: "var(--text-faint)" }}>Type</span>
          <select value={variantType} onChange={(e) => setVariantType(e.target.value)}>
            <option value="">All</option>
            <option value="snp">SNPs</option>
            <option value="indel">Indels</option>
          </select>
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <span style={{ color: "var(--text-faint)" }}>Min QUAL</span>
          <input
            type="text"
            inputMode="decimal"
            value={minQualInput}
            onChange={(e) => setMinQualInput(e.target.value)}
            placeholder="any"
            style={{ width: 80 }}
          />
        </label>

        {isAnnotated && (
          <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            {/* "(this page)" because the options come from the rows currently
                loaded, not the whole callset -- a consequence that only occurs
                on page 90 is not offered on page 1. Saying so turns a dropdown
                that looks unstable into one that is visibly scoped. */}
            <span style={{ color: "var(--text-faint)" }}>Consequence (this page)</span>
            <select value={consequence} onChange={(e) => setConsequence(e.target.value)}>
              <option value="">All</option>
              {consequenceOptions.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
        )}

        {samples.length > 1 && (
          <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <span style={{ color: "var(--text-faint)" }}>Sample</span>
            <select
              value={sampleIdx}
              onChange={(e) => setSampleIdx(Number(e.target.value))}
            >
              {samples.map((s, i) => (
                <option key={s} value={i}>
                  {s}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      {isLoading && !data ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>Loading…</div>
      ) : rows.length === 0 ? (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          No variants match these filters.
        </div>
      ) : (
        <>
          <table className="trim-table">
            <thead>
              <tr>
                <th>Chrom</th>
                <th style={{ textAlign: "right" }}>Pos</th>
                <th>Ref</th>
                <th>Alt</th>
                <th>Gene</th>
                <th>Consequence</th>
                <th>AA change</th>
                <th style={{ textAlign: "right" }}>Qual</th>
                <th>Filter</th>
                <th style={{ textAlign: "right" }}>Depth</th>
                <th>Genotype</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={`${row.chrom}:${row.pos}:${i}`}>
                  <td className="mono">{row.chrom}</td>
                  <td style={{ textAlign: "right" }} className="mono">
                    {row.pos.toLocaleString()}
                  </td>
                  <td className="mono">
                    <Truncated value={row.ref} />
                  </td>
                  <td className="mono">
                    <Truncated value={row.alt} />
                  </td>
                  {/* Empty on every row of an un-annotated VCF, which is the
                      common case -- so this renders as an ordinary dash
                      rather than anything that suggests something failed. */}
                  <td className="mono">{row.gene ?? "—"}</td>
                  <td>{row.consequence ?? "—"}</td>
                  <td className="mono">{row.aa_change ?? "—"}</td>
                  <td style={{ textAlign: "right" }}>
                    {row.qual == null ? "—" : row.qual.toFixed(1)}
                  </td>
                  <td>
                    {row.filter === "PASS" ? (
                      <span className="badge ready">PASS</span>
                    ) : (
                      row.filter
                    )}
                  </td>
                  <td style={{ textAlign: "right" }}>{row.dp == null ? "—" : row.dp}</td>
                  <td className="mono">{genotypeFor(row.gt, sampleIdx)}</td>
                  <td>
                    {/* Variants are called against whatever reference was
                        aligned to, often a local assembly whose contigs have
                        no page at NCBI. No button beats a button that opens a
                        viewer which then fails. */}
                    {isNcbiNucleotideAccession(row.chrom) && (
                      <button
                        type="button"
                        className="btn"
                        style={{ padding: "1px 8px", fontSize: 11 }}
                        onClick={() => setContextRow(row)}
                      >
                        Context
                      </button>
                    )}
                    {/* Optimistic: this says the row *could* have a
                        structure, not that one exists. Two thirds of genes
                        have none, and resolving every row to decide would
                        cost a page of requests to render buttons that
                        mostly never get pressed -- so the miss is reported
                        in the modal instead. */}
                    {canShowStructure(row) && (
                      <button
                        type="button"
                        className="btn"
                        style={{
                          padding: "1px 8px",
                          fontSize: 11,
                          marginLeft: 4,
                        }}
                        onClick={() => setStructureRow(row)}
                      >
                        3D
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginTop: 8,
              fontSize: 11,
              color: "var(--text-faint)",
            }}
          >
            <span>
              {total != null
                ? `${total.toLocaleString()} variant${total === 1 ? "" : "s"}`
                : `Showing ${rows.length}`}
            </span>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <button
                type="button"
                className="btn"
                style={{ padding: "1px 8px", fontSize: 11 }}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
              >
                Prev
              </button>
              <span>Page {page + 1}</span>
              <button
                type="button"
                className="btn"
                style={{ padding: "1px 8px", fontSize: 11 }}
                onClick={() => setPage((p) => p + 1)}
                disabled={!hasNext}
              >
                Next
              </button>
            </div>
          </div>
        </>
      )}

      {contextRow && (
        <SequenceViewerModal
          accession={contextRow.chrom}
          // A fresh object every render, which the modal tolerates by
          // depending on these three fields rather than on the object. That
          // only holds while each stays stable across a re-render:
          // `markerLabel` is pure, and `contigLengths` is memoised above. Make
          // either one churn and the NCBI viewer reloads on every keystroke in
          // the filters.
          focus={{
            position: contextRow.pos,
            label: markerLabel(contextRow.ref, contextRow.alt),
            sequenceLength: contigLengths.get(contextRow.chrom),
          }}
          onClose={() => setContextRow(null)}
        />
      )}

      {/* `gene` is non-null here: canShowStructure gated the button on it. */}
      {structureRow?.gene && (
        <StructureViewerModal
          objectId={objectId}
          gene={structureRow.gene}
          aaChange={structureRow.aa_change}
          aaPos={structureRow.aa_pos}
          onClose={() => setStructureRow(null)}
        />
      )}
    </div>
  );
}

/** REF/ALT can run 30+ bases for an indel; truncate so one long allele does
 *  not blow out the column. */
function Truncated({ value }: { value: string }) {
  if (value.length <= 12) return <>{value}</>;
  return <span title={value}>{value.slice(0, 12)}…</span>;
}

/** `gt` holds one genotype per sample, tab-separated. Falls back to the whole
 *  string when the index is out of range or there was never more than one. */
function genotypeFor(gt: string, sampleIdx: number): string {
  const parts = gt.split("\t");
  return parts[sampleIdx] ?? gt;
}
