import { useState } from "react";
import { api } from "../api/client";
import { accessionUrl } from "../lib/format";

interface Props {
  facts: Record<string, unknown>;
  objectId: string;
}

/**
 * Same link shape `QcReport.tsx`'s `ReportLink` uses -- new tab, noopener,
 * CSP-sandboxed on the server. Not imported from there: that component is
 * scoped to read QC, and duplicating four lines here is cheaper than adding
 * a cross-cutting export for one reuse.
 */
function ReportLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className="report-link">
      {children}
      <span className="report-link-icon" aria-hidden="true">
        ↗
      </span>
    </a>
  );
}

const MAX_VISIBLE_CONTIGS = 25;

/**
 * Curated facts for a reference assembly.
 *
 * The generic FactsTable dumps every parsed key, which buries the four numbers
 * that actually characterize a genome build. This shows those, and nothing
 * else.
 */
export function AssemblyFacts({ facts, objectId }: Props) {
  const [showAllContigs, setShowAllContigs] = useState(false);

  const exactCount = facts.sequence_count as number | undefined;
  const estimatedCount = facts.sequence_count_estimate as number | undefined;
  const isExact = facts.sequence_count_exact === true;
  const totalBases = facts.total_bases as number | undefined;
  const gc = facts.gc_content_percent as number | undefined;
  const sampledBases = facts.stats_sampled_bases as number | undefined;
  const sampling = facts.stats_sampling as string | undefined;
  const names = Array.isArray(facts.sequence_names)
    ? (facts.sequence_names as string[])
    : [];
  const namesTruncated = facts.sequence_names_truncated === true;

  type NamedLength = { name: string; length: number };
  const longest = facts.sequence_longest as NamedLength | undefined;
  const shortest = facts.sequence_shortest as NamedLength | undefined;
  const lengthsPartial = facts.sequence_lengths_partial === true;

  const count = isExact ? exactCount : estimatedCount;
  const hasAnything =
    count !== undefined || totalBases !== undefined || gc !== undefined;

  const ncbiTotal = facts.ncbi_total_length as number | undefined;
  // Sequences, not contigs: a FASTA's records are scaffolds, and the two
  // counts differ sharply for a chromosome-level assembly (12 vs 50 for
  // GCF_000002445.2). Comparing against contigs would invent a divergence.
  const ncbiSequences = facts.ncbi_sequence_count as number | undefined;
  const ncbiGc = facts.ncbi_gc_percent as number | undefined;
  const ncbiName = facts.ncbi_assembly_name as string | undefined;
  const ncbiAccession = facts.ncbi_assembly_accession as string | undefined;
  const ncbiUrl = ncbiAccession
    ? accessionUrl("assembly_accession", ncbiAccession)
    : null;
  const assemblyError = facts.assembly_error as string | undefined;
  const hasNcbi =
    ncbiTotal !== undefined || ncbiSequences !== undefined || ncbiGc !== undefined;

  // Contiguity: computed in the parser at ingest, not by a separate tool --
  // see docs/superpowers/specs/2026-08-02-post-assembly-qc-design.md.
  const n50 = facts.sequence_n50 as number | undefined;
  const n90 = facts.sequence_n90 as number | undefined;
  const l50 = facts.sequence_l50 as number | undefined;
  const auN = facts.sequence_auN as number | undefined;
  const gapCount = facts.sequence_gap_count as number | undefined;
  const hasContiguity = n50 !== undefined;

  // Completeness: compleasm, a separate job the user launches.
  const completenessTool = facts.assembly_completeness_tool as string | undefined;
  const completenessLineage = facts.assembly_completeness_lineage as
    | string
    | undefined;
  const completePct = facts.assembly_completeness_complete_pct as
    | number
    | undefined;
  const singlePct = facts.assembly_completeness_single_pct as number | undefined;
  const duplicatedPct = facts.assembly_completeness_duplicated_pct as
    | number
    | undefined;
  const fragmentedPct = facts.assembly_completeness_fragmented_pct as
    | number
    | undefined;
  const missingPct = facts.assembly_completeness_missing_pct as
    | number
    | undefined;
  const completenessTotal = facts.assembly_completeness_total as
    | number
    | undefined;
  const hasCompleteness = completenessTool !== undefined;

  // Misassembly QC: QUAST, a separate job the user launches against a
  // reference. Deliberately excludes N50/L50/total-length-shaped facts --
  // those are the contiguity block above, computed once at ingest by
  // `_parse_fasta`, not by QUAST's own report.tsv over a --min-contig
  // subset. See quast_runner.py's module docstring for why storing both
  // would eventually let two numbers meant to agree quietly disagree.
  const misassemblyTool = facts.assembly_misassembly_tool as string | undefined;
  const misassemblyTotal = facts.assembly_misassembly_total as number | undefined;
  const misassemblyRelocations = facts.assembly_misassembly_relocations as
    | number
    | undefined;
  const misassemblyTranslocations = facts.assembly_misassembly_translocations as
    | number
    | undefined;
  const misassemblyInversions = facts.assembly_misassembly_inversions as
    | number
    | undefined;
  const genomeFractionPct = facts.assembly_reference_genome_fraction_pct as
    | number
    | undefined;
  const referenceName = facts.assembly_reference_name as string | undefined;
  const misassemblyReportPath = facts.assembly_misassembly_report as
    | string
    | undefined;
  const hasMisassembly = misassemblyTool !== undefined;

  // A file named for a full assembly that holds one chromosome is a real and
  // easily-missed problem. Compare only when both sides are known.
  const countDiverges =
    count !== undefined && ncbiSequences !== undefined && count !== ncbiSequences;
  const lengthDiverges =
    totalBases !== undefined &&
    ncbiTotal !== undefined &&
    Math.abs(totalBases - ncbiTotal) / ncbiTotal > 0.01;
  const diverges = countDiverges || lengthDiverges;

  if (
    !hasAnything &&
    !hasNcbi &&
    !assemblyError &&
    !hasContiguity &&
    !hasCompleteness &&
    !hasMisassembly
  ) {
    return (
      <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
        No assembly facts extracted yet.
      </div>
    );
  }

  return (
    <div>
      {/* Suppressed entirely when nothing was measured, so a file that failed
          to parse shows the published block alone rather than an empty list. */}
      {hasAnything && (
      <dl className="kv">
        {count !== undefined && (
          <>
            <dt>Sequences</dt>
            <dd>
              {isExact ? count.toLocaleString() : `~${count.toLocaleString()}`}
              {!isExact && (
                <span style={{ color: "var(--text-faint)" }}> (estimated)</span>
              )}
            </dd>
          </>
        )}
        {totalBases !== undefined && (
          <>
            <dt>Total bases</dt>
            <dd>{formatBases(totalBases)}</dd>
          </>
        )}
        {longest !== undefined && shortest !== undefined && (
          <>
            <dt>Longest</dt>
            <dd>
              <span className="mono">{longest.name}</span> ·{" "}
              {formatBases(longest.length)}
            </dd>
            <dt>Shortest</dt>
            <dd>
              <span className="mono">{shortest.name}</span> ·{" "}
              {formatBases(shortest.length)}
            </dd>
          </>
        )}
        {n50 !== undefined && (
          <>
            <dt>N50</dt>
            <dd>
              {formatBases(n50)}
              {l50 !== undefined && (
                <span style={{ color: "var(--text-faint)" }}>
                  {" "}
                  ({l50.toLocaleString()} sequence{l50 === 1 ? "" : "s"})
                </span>
              )}
            </dd>
          </>
        )}
        {n90 !== undefined && (
          <>
            <dt>N90</dt>
            <dd>{formatBases(n90)}</dd>
          </>
        )}
        {auN !== undefined && (
          <>
            <dt>auN</dt>
            <dd>{formatBases(auN)}</dd>
          </>
        )}
        {gapCount !== undefined && gapCount > 0 && (
          <>
            <dt>Gaps</dt>
            <dd>{gapCount.toLocaleString()}</dd>
          </>
        )}
        {gc !== undefined && (
          <>
            {/* What this number means depends on how it was measured: a small
                file is counted exactly, a large uncompressed one is sampled
                across the whole file, and a compressed one is still a prefix
                read because gzip cannot seek cheaply. Objects ingested before
                strided sampling have no stats_sampling key and keep the
                original conservative label. */}
            <dt>
              {sampling === "complete"
                ? "GC content"
                : sampling === "strided"
                  ? "GC content (sampled across file)"
                  : "GC content (sampled)"}
            </dt>
            <dd>
              {gc}%
              {sampling !== "complete" && sampledBases !== undefined && (
                <span style={{ color: "var(--text-faint)" }}>
                  {" "}
                  from {formatBases(sampledBases)} sampled
                </span>
              )}
            </dd>
          </>
        )}
      </dl>
      )}

      {/* This note's visibility rides on hasAnything, which for a truncated
          parse depends on sequence_stats.fasta_stats having also set
          gc_content_percent -- an all-N sampled region or a decode error
          there leaves hasAnything false, and this caveat silently won't
          show even though sequence_longest/shortest and
          sequence_lengths_partial are set. Low-risk (a missing caveat, not
          wrong data), but worth knowing if this ever gets debugged. */}
      {hasAnything && lengthsPartial && longest !== undefined && (
        <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}>
          Longest and shortest are partial — the file was truncated during
          parsing, so later sequences were not measured.
        </div>
      )}

      {names.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div
            style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 6 }}
          >
            Sequences
          </div>
          <div
            className="mono"
            style={{
              fontSize: 12,
              display: "flex",
              flexWrap: "wrap",
              gap: "4px 10px",
            }}
          >
            {/* Indexed key: a malformed or concatenated FASTA can repeat a
                contig name, and duplicate keys would misrender on toggle. */}
            {(showAllContigs ? names : names.slice(0, MAX_VISIBLE_CONTIGS)).map(
              (n, i) => (
                <span key={`${i}-${n}`}>{n}</span>
              ),
            )}
          </div>
          {names.length > MAX_VISIBLE_CONTIGS && (
            <button
              type="button"
              className="btn"
              style={{ marginTop: 8 }}
              onClick={() => setShowAllContigs(!showAllContigs)}
            >
              {showAllContigs ? "Show fewer" : `Show all ${names.length}`}
            </button>
          )}
          {namesTruncated && (
            <div
              style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}
            >
              List truncated during parsing; the assembly has more sequences
              than are recorded here.
            </div>
          )}
        </div>
      )}

      {hasNcbi && (
        <div style={{ marginTop: 14 }}>
          <div
            style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 6 }}
          >
            Published assembly (NCBI){ncbiName ? ` · ${ncbiName}` : ""}
          </div>
          <dl className="kv">
            {ncbiSequences !== undefined && (
              <>
                <dt>Sequences</dt>
                <dd>{ncbiSequences.toLocaleString()}</dd>
              </>
            )}
            {ncbiTotal !== undefined && (
              <>
                <dt>Total bases</dt>
                <dd>{formatBases(ncbiTotal)}</dd>
              </>
            )}
            {ncbiGc !== undefined && (
              <>
                <dt>GC content</dt>
                <dd>{ncbiGc}%</dd>
              </>
            )}
          </dl>
          {diverges && (
            <div className="warn-box" style={{ marginTop: 8 }}>
              This file{" "}
              {count !== undefined && <>has {count.toLocaleString()} sequences</>}
              {count !== undefined && totalBases !== undefined && " "}
              {totalBases !== undefined && <>totalling {formatBases(totalBases)}</>};
              {" "}
              {ncbiUrl ? (
                <a href={ncbiUrl} target="_blank" rel="noreferrer">
                  the published assembly
                </a>
              ) : (
                "the published assembly"
              )}{" "}
              has{" "}
              {ncbiSequences !== undefined && (
                <>{ncbiSequences.toLocaleString()} sequences</>
              )}
              {ncbiSequences !== undefined && ncbiTotal !== undefined && " "}
              {ncbiTotal !== undefined && <>totalling {formatBases(ncbiTotal)}</>}. It
              may be a subset, or a different patch level.
            </div>
          )}
        </div>
      )}

      {hasCompleteness && (
        <div style={{ marginTop: 14 }}>
          <div
            style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 6 }}
          >
            Completeness ({completenessTool}
            {completenessLineage ? ` · ${completenessLineage}` : ""})
          </div>
          <dl className="kv">
            {completePct !== undefined && (
              <>
                <dt>Complete</dt>
                <dd>
                  {completePct}%
                  {completenessTotal !== undefined && (
                    <span style={{ color: "var(--text-faint)" }}>
                      {" "}
                      of {completenessTotal.toLocaleString()} markers
                    </span>
                  )}
                </dd>
              </>
            )}
            {singlePct !== undefined && (
              <>
                <dt>Single-copy</dt>
                <dd>{singlePct}%</dd>
              </>
            )}
            {duplicatedPct !== undefined && (
              <>
                <dt>Duplicated</dt>
                <dd>{duplicatedPct}%</dd>
              </>
            )}
            {fragmentedPct !== undefined && (
              <>
                <dt>Fragmented</dt>
                <dd>{fragmentedPct}%</dd>
              </>
            )}
            {missingPct !== undefined && (
              <>
                <dt>Missing</dt>
                <dd>{missingPct}%</dd>
              </>
            )}
          </dl>
          {/* Duplicated percentage is the haplotypic-duplication signal, and
              a single headline "complete" number throws it away -- worth
              flagging when it is large enough to matter rather than left to
              be noticed only by someone reading every row. */}
          {duplicatedPct !== undefined && duplicatedPct > 5 && (
            <div className="warn-box" style={{ marginTop: 8 }}>
              {duplicatedPct}% of markers are duplicated, which can mean
              retained haplotypes rather than real gene duplication.
            </div>
          )}
        </div>
      )}

      {hasMisassembly && (
        <div style={{ marginTop: 14 }}>
          <div
            style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 6 }}
          >
            Misassembly QC ({misassemblyTool}
            {referenceName ? ` vs. ${referenceName}` : ""})
          </div>
          <dl className="kv">
            {misassemblyTotal !== undefined && (
              <>
                <dt>Misassemblies</dt>
                <dd>{misassemblyTotal.toLocaleString()}</dd>
              </>
            )}
            {genomeFractionPct !== undefined && (
              <>
                <dt>Genome fraction</dt>
                <dd>{genomeFractionPct}%</dd>
              </>
            )}
          </dl>
          {/* Only when there is at least one to explain -- a project with
              zero misassemblies gains nothing from a 0/0/0 breakdown row. */}
          {misassemblyTotal !== undefined && misassemblyTotal > 0 && (
            <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 4 }}>
              {misassemblyRelocations !== undefined && (
                <>{misassemblyRelocations} relocation{misassemblyRelocations === 1 ? "" : "s"}, </>
              )}
              {misassemblyTranslocations !== undefined && (
                <>{misassemblyTranslocations} translocation{misassemblyTranslocations === 1 ? "" : "s"}, </>
              )}
              {misassemblyInversions !== undefined && (
                <>{misassemblyInversions} inversion{misassemblyInversions === 1 ? "" : "s"}</>
              )}
            </div>
          )}
          {misassemblyReportPath && (
            <div style={{ marginTop: 8 }}>
              <ReportLink href={api.qcReportUrl(objectId, misassemblyReportPath)}>
                QUAST report
              </ReportLink>
            </div>
          )}
        </div>
      )}

      {assemblyError && (
        <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 10 }}>
          NCBI lookup: {assemblyError}
        </div>
      )}
    </div>
  );
}

/** Base counts read better in Gb/Mb than as raw digits. */
function formatBases(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} Gb`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} Mb`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} kb`;
  return `${n} bp`;
}
