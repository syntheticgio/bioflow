import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { accessionUrl } from "../lib/format";
import { BuscoChart } from "./BuscoChart";

interface Props {
  facts: Record<string, unknown>;
  objectId: string;
  projectId: string;
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
export function AssemblyFacts({ facts, objectId, projectId }: Props) {
  const [showAllContigs, setShowAllContigs] = useState(false);
  const navigate = useNavigate();

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

  // Assembly errors: CRAQ, reference-free. Structural facts (CSE, S-AQI,
  // AQI) are absent rather than zero on a short-read-only run -- the
  // handler drops them because CRAQ prints meaningless values for them
  // when no long reads were supplied. `undefined` here means "not
  // measured", and the block must not render a 0 in its place.
  const errorTool = facts.assembly_error_tool as string | undefined;
  const errorAqi = facts.assembly_error_aqi as number | undefined;
  const errorRAqi = facts.assembly_error_r_aqi as number | undefined;
  const errorSAqi = facts.assembly_error_s_aqi as number | undefined;
  const errorCre = facts.assembly_error_cre_count as number | undefined;
  const errorCse = facts.assembly_error_cse_count as number | undefined;
  const errorHasNgs = facts.assembly_error_has_ngs as boolean | undefined;
  const errorHasSms = facts.assembly_error_has_sms as boolean | undefined;
  const hasAssemblyErrors = errorTool !== undefined;

  // K-mer QV: Merqury, reference-free base-level accuracy measured against a
  // read set. Gated on the QV score itself (the primary number), not on the
  // completeness percentage, which may be absent if completeness.stats
  // didn't exist for this run.
  const assemblyQv = facts.assembly_qv as number | undefined;
  const assemblyQvErrorRate = facts.assembly_qv_error_rate as number | undefined;
  const assemblyQvCompletenessPct = facts.assembly_qv_completeness_pct as
    | number
    | undefined;
  const assemblyQvK = facts.assembly_qv_k as number | undefined;
  const assemblyQvReadObjectId = facts.assembly_qv_read_object_id as
    | string
    | undefined;
  const assemblyQvReadObjectName = facts.assembly_qv_read_object_name as
    | string
    | undefined;
  const assemblyQvToolVersion = facts.assembly_qv_tool_version as
    | string
    | undefined;
  const assemblyQvMerylVersion = facts.assembly_qv_meryl_version as
    | string
    | undefined;
  const hasAssemblyQv = assemblyQv !== undefined;

  // Continuity: GCI, long-read only. Score has no upstream-published quality
  // bands (unlike CRAQ's AQI) -- see aqiBand's docstring for the contrast --
  // so only the raw published benchmark range is shown as context, never a
  // classification.
  const continuityTool = facts.assembly_continuity_tool as string | undefined;
  const continuityGci = facts.assembly_continuity_gci as number | undefined;
  const continuityExpectedN50 = facts.assembly_continuity_expected_n50 as
    | number
    | undefined;
  const continuityObservedN50 = facts.assembly_continuity_observed_n50 as
    | number
    | undefined;
  const continuityExpectedContigs = facts.assembly_continuity_expected_contigs as
    | number
    | undefined;
  const continuityObservedContigs = facts.assembly_continuity_observed_contigs as
    | number
    | undefined;
  const continuityAligners = Array.isArray(facts.assembly_continuity_aligners)
    ? (facts.assembly_continuity_aligners as string[])
    : [];
  const continuityMapQual = facts.assembly_continuity_map_qual as number | undefined;
  const hasContinuity = continuityGci !== undefined;

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
    !hasMisassembly &&
    !hasAssemblyErrors &&
    !hasAssemblyQv &&
    !hasContinuity
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
          {singlePct !== undefined && duplicatedPct !== undefined
            && fragmentedPct !== undefined && missingPct !== undefined && (
            <BuscoChart
              singlePct={singlePct}
              duplicatedPct={duplicatedPct}
              fragmentedPct={fragmentedPct}
              missingPct={missingPct}
              total={completenessTotal}
            />
          )}
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

      {hasAssemblyErrors && (
        <div style={{ marginTop: 14 }}>
          <div
            style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 6 }}
          >
            Assembly errors ({errorTool}, reference-free)
          </div>
          <dl className="kv">
            {errorAqi !== undefined && (
              <>
                <dt>AQI</dt>
                <dd>
                  {errorAqi.toFixed(1)}{" "}
                  <span style={{ color: "var(--text-faint)" }}>
                    {aqiBand(errorAqi)}
                  </span>
                </dd>
              </>
            )}
            {errorRAqi !== undefined && (
              <>
                <dt>R-AQI (regional)</dt>
                <dd>{errorRAqi.toFixed(1)}</dd>
              </>
            )}
            {errorSAqi !== undefined && (
              <>
                <dt>S-AQI (structural)</dt>
                <dd>{errorSAqi.toFixed(1)}</dd>
              </>
            )}
            {errorCre !== undefined && (
              <>
                <dt>Regional errors (CRE)</dt>
                <dd>{errorCre.toLocaleString()}</dd>
              </>
            )}
            {errorCse !== undefined && (
              <>
                <dt>Structural errors (CSE)</dt>
                <dd>{errorCse.toLocaleString()}</dd>
              </>
            )}
          </dl>
          {errorHasSms === false && (
            <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 4 }}>
              Short reads only: structural errors are not reported, because
              CRAQ can barely detect them without long reads.
            </div>
          )}
          {errorHasNgs === false && (
            <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 4 }}>
              Long reads only: regional errors are undercounted, especially
              for ONT-based assemblies.
            </div>
          )}
        </div>
      )}

      {hasAssemblyQv && (
        <div style={{ marginTop: 14 }}>
          <div
            style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 6 }}
          >
            K-mer accuracy (Merqury
            {assemblyQvToolVersion ? ` ${assemblyQvToolVersion}` : ""}
            {assemblyQvMerylVersion ? `, meryl ${assemblyQvMerylVersion}` : ""}
            {assemblyQvK !== undefined ? `, k=${assemblyQvK}` : ""})
          </div>
          {/* The read set is not a footnote: a QV is a statement about this
              assembly against those specific reads, and reads from a
              different individual measure real biology as error. Shown
              beside the headline number, not tucked into a collapsed
              provenance section. */}
          {assemblyQvReadObjectName && (
            <div style={{ fontSize: 12, marginBottom: 8 }}>
              Measured against{" "}
              {assemblyQvReadObjectId ? (
                <button
                  type="button"
                  style={{
                    font: "inherit",
                    color: "var(--accent, #4a9eff)",
                    background: "none",
                    border: "none",
                    padding: 0,
                    cursor: "pointer",
                    textDecoration: "underline",
                  }}
                  onClick={() =>
                    navigate(
                      `/p/${projectId}?sel=object:${assemblyQvReadObjectId}`,
                    )
                  }
                >
                  {assemblyQvReadObjectName}
                </button>
              ) : (
                <span>{assemblyQvReadObjectName}</span>
              )}
            </div>
          )}
          <dl className="kv">
            {assemblyQv !== undefined && (
              <>
                <dt>QV</dt>
                <dd>
                  {assemblyQv.toFixed(1)}
                  {assemblyQvErrorRate !== undefined && (
                    <span style={{ color: "var(--text-faint)" }}>
                      {" "}
                      ({(assemblyQvErrorRate * 100).toPrecision(2)}% error rate)
                    </span>
                  )}
                </dd>
              </>
            )}
            {assemblyQvCompletenessPct !== undefined && (
              <>
                <dt>k-mer completeness</dt>
                <dd>{assemblyQvCompletenessPct}%</dd>
              </>
            )}
          </dl>
          <SpectraCnPlots objectId={objectId} />
        </div>
      )}

      {hasContinuity && (
        <div style={{ marginTop: 14 }}>
          <div
            style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 6 }}
          >
            Continuity ({continuityTool}, long reads)
          </div>
          <dl className="kv">
            {continuityGci !== undefined && (
              <>
                <dt>GCI</dt>
                <dd>
                  {continuityGci.toFixed(1)}{" "}
                  <span style={{ color: "var(--text-faint)" }}>
                    {continuityBenchmarkNote()}
                  </span>
                </dd>
              </>
            )}
            {continuityObservedN50 !== undefined &&
              continuityExpectedN50 !== undefined && (
                <>
                  <dt>N50</dt>
                  <dd>
                    {formatBases(continuityObservedN50)} observed /{" "}
                    {formatBases(continuityExpectedN50)} expected
                  </dd>
                </>
              )}
            {continuityObservedContigs !== undefined &&
              continuityExpectedContigs !== undefined && (
                <>
                  <dt>Contigs</dt>
                  <dd>
                    {continuityObservedContigs.toLocaleString()} observed /{" "}
                    {continuityExpectedContigs.toLocaleString()} expected
                  </dd>
                </>
              )}
            {continuityMapQual !== undefined && (
              <>
                <dt>Mapping quality threshold (-mq)</dt>
                <dd>{continuityMapQual}</dd>
              </>
            )}
          </dl>
          {continuityAligners.length > 0 && (
            <div style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 4 }}>
              Aligned with {continuityAligners.join(", ")}. A single aligner
              undercounts issues in repetitive regions; upstream recommends
              pairing two aligners (e.g. winnowmap + minimap2).
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

/**
 * Merqury's spectra-cn/QV plots, copied server-side into
 * qc_reports/<object_id>/. Filenames verified against a real run (2026-08-07,
 * S. cerevisiae R64 assembly + DRR1066343 reads) rather than guessed from
 * documentation -- the earlier guess (`qv.spectra-cn.*.png`, `qv.qv.png`)
 * did not match any file Merqury actually wrote.
 *
 * The handler always links the assembly as `assembly.fasta`, which
 * `_named_link` resolves to `in_assembly.fasta` on disk (see
 * `_MERQURY_ASSEMBLY_LINK` in `assembly_qc_handlers.py`) -- Merqury strips
 * the extension for its own naming, so every run's plots are prefixed
 * `qv.in_assembly.*` and `qv.spectra-asm.*`, not just this one's. Facts
 * carries no filename list, so each candidate is rendered optimistically and
 * hidden with onError if a particular run didn't produce it.
 */
function SpectraCnPlots({ objectId }: { objectId: string }) {
  const [hidden, setHidden] = useState<Record<string, boolean>>({});
  const candidates = [
    "qv.in_assembly.spectra-cn.fl.png",
    "qv.in_assembly.spectra-cn.ln.png",
    "qv.in_assembly.spectra-cn.st.png",
    "qv.spectra-asm.fl.png",
    "qv.spectra-asm.ln.png",
    "qv.spectra-asm.st.png",
  ];

  return (
    <div
      style={{
        marginTop: 8,
        display: "flex",
        flexWrap: "wrap",
        gap: 10,
      }}
    >
      {candidates.map((file) => (
        <img
          key={file}
          src={api.qcReportUrl(objectId, file)}
          alt={file}
          style={{
            display: hidden[file] ? "none" : "block",
            maxWidth: 280,
            width: "100%",
            border: "1px solid var(--border, #333)",
            borderRadius: 4,
          }}
          onError={() => setHidden((prev) => ({ ...prev, [file]: true }))}
        />
      ))}
    </div>
  );
}

/**
 * CRAQ's AQI quality bands, also documented in
 * backend/app/pipelines/tools.py's TOOL_META["craq"] -- keep the two in sync
 * if the thresholds ever need correcting. The top band is exclusive
 * ("reference quality" needs AQI > 90); the rest are inclusive at their
 * lower edge.
 */
function aqiBand(aqi: number): string {
  if (aqi > 90) return "reference quality";
  if (aqi >= 80) return "high quality";
  if (aqi >= 60) return "draft quality";
  return "low quality";
}

/**
 * GCI publishes no quality bands the way CRAQ's AQI does (see aqiBand
 * above), so this deliberately does not classify the score -- only states
 * the raw range GCI's own paper reports across real T2T assemblies, for
 * context.
 */
function continuityBenchmarkNote(): string {
  return "published range across T2T assemblies: 7.26–99.99";
}

/** Base counts read better in Gb/Mb than as raw digits. */
function formatBases(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} Gb`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} Mb`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} kb`;
  return `${n} bp`;
}
