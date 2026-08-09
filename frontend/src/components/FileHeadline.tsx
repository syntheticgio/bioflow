import type { DataObject, QcFacts, ReferenceOption } from "../api/types";
import { readQuality } from "../lib/readQuality";

/**
 * The assembly name to put on the Align button, or null to leave it as the
 * bare verb.
 *
 * Keyed on the assembly rather than the file: a project commonly holds the
 * same assembly more than once (the genomic FASTA registered twice, or
 * alongside its own index), and those are not a choice between references --
 * they all name ASM244v1. What must be unambiguous is the *answer*, so this
 * asks whether every candidate resolves to one assembly name.
 *
 * Null when they disagree, since the align dialog would then ask and a named
 * button would promise something it does not do; and null when nothing parses
 * as an assembly, since a truncated filename on a button is worse than the
 * plain word "Align".
 */
export function assemblyLabel(references: ReferenceOption[]): string | null {
  // Mirrors the dialog's own preference: an explicitly marked reference wins,
  // and only then does a plain FASTA stand in for one.
  const marked = references.filter((r) => r.role === "reference");
  const candidates = marked.length > 0 ? marked : references;

  const names = new Set<string>();
  for (const c of candidates) {
    const name = parseAssemblyName(c.name);
    // One unparseable candidate means the set is not known to be uniform.
    if (!name) return null;
    names.add(name);
  }

  return names.size === 1 ? [...names][0] : null;
}

/**
 * Pull the assembly name out of an NCBI-style filename:
 * `GCF_000002445.2_ASM244v1_genomic.fna` -> `ASM244v1`.
 *
 * Returns null rather than a guess when the name does not follow the
 * convention, since the caller can fall back to a label that is always right.
 */
export function parseAssemblyName(filename: string): string | null {
  const stem = filename.replace(/\.(fa|fna|fasta)(\.gz)?$/i, "");

  // GCF_<digits>.<ver>_<assembly>[_genomic]
  const ncbi = stem.match(/^GC[AF]_\d+\.\d+_(.+?)(?:_genomic)?$/i);
  if (ncbi) return ncbi[1];

  return null;
}

/**
 * The headline metrics beside a file's name.
 *
 * Two shapes of QC facts land on an object, chosen by which tool ran rather
 * than by platform: NanoPlot writes N50 and the read-length distribution for
 * long reads, fastp/FastQC write Q20/Q30 rates for short ones. They measure
 * genuinely different things, so this picks a lead metric per shape instead of
 * forcing one set of numbers into the other's layout.
 *
 * Self-suppressing: a file nobody has run QC on renders nothing here, and the
 * QC tab's own empty state is what tells the user why.
 */

export interface Stat {
  label: string;
  value: string;
  /** The one number that leads, set larger and in the accent. */
  lead?: boolean;
  /** Sits under the lead metric as its qualifier. */
  note?: string;
  /** Hover text, for a value that needs its reasoning attached. */
  title?: string;
}

const int = (n: number) => Math.round(n).toLocaleString();

/** Q-scores and percentages read better with a decimal; counts do not. */
const dec1 = (n: number) => n.toFixed(1);

export function fileStats(obj: DataObject): Stat[] {
  const qc = obj.facts as QcFacts;
  const stats: Stat[] = [];

  // GC is measured at ingest and lives at the top level of facts, not under
  // either QC tool's block -- so it is read from there and is available even
  // on a file nobody has run QC on. Already a percentage, unlike the 0-1
  // rates fastp reports.
  const gcPercent =
    typeof obj.facts.gc_content_percent === "number"
      ? obj.facts.gc_content_percent
      : null;

  // Long-read (NanoPlot): N50 leads, the way it does in the QC tab.
  if (qc.qc_read_length_n50 != null) {
    const reads = qc.qc_total_reads;
    const bases = qc.qc_total_bases;
    stats.push({
      label: "Read N50",
      value: `${int(qc.qc_read_length_n50)} bp`,
      lead: true,
      note:
        bases != null && reads != null
          ? `${int(bases)} bases over ${int(reads)} reads`
          : undefined,
    });
    if (qc.qc_mean_quality != null) {
      stats.push({ label: "Mean quality", value: `Q${dec1(qc.qc_mean_quality)}` });
    }
    if (qc.qc_median_quality != null) {
      stats.push({
        label: "Median quality",
        value: `Q${dec1(qc.qc_median_quality)}`,
      });
    }
    if (qc.qc_mean_read_length != null) {
      stats.push({
        label: "Mean length",
        value: `${int(qc.qc_mean_read_length)} bp`,
      });
    }
    if (gcPercent != null) {
      stats.push({ label: "GC content", value: `${gcPercent.toFixed(2)}%` });
    }
    pushGrade(stats, obj);
    return stats;
  }

  // Short-read (fastp/FastQC): Q30 is the number people judge a run by.
  const m = qc.qc_before_filtering;
  if (m) {
    if (m.q30_rate != null) {
      stats.push({
        label: "Q30 rate",
        value: `${(m.q30_rate * 100).toFixed(1)}%`,
        lead: true,
        note:
          m.total_bases != null && m.total_reads != null
            ? `${int(m.total_bases)} bases over ${int(m.total_reads)} reads`
            : undefined,
      });
    }
    if (m.q20_rate != null) {
      stats.push({ label: "Q20 rate", value: `${(m.q20_rate * 100).toFixed(1)}%` });
    }
    if (m.read1_mean_length != null) {
      stats.push({
        label: "Mean length",
        value: `${int(m.read1_mean_length)} bp`,
      });
    }
    if (gcPercent != null) {
      stats.push({ label: "GC content", value: `${gcPercent.toFixed(2)}%` });
    }
    // The whole-file scan's number wins over fastp's sampled one when it
    // exists, same preference QcReport gives it -- otherwise this headline
    // and the Quality-control table a few rows below it show two different
    // duplication rates for the same file.
    if (qc.qc_percent_unique != null || qc.qc_duplication_rate != null) {
      const rate =
        qc.qc_percent_unique != null
          ? 1 - qc.qc_percent_unique / 100
          : qc.qc_duplication_rate!;
      stats.push({
        label: "Duplication",
        value: `${(rate * 100).toFixed(1)}%`,
      });
    }
  } else {
    // No QC has run. Ingest still measured this file when it was added --
    // read length, count, quality and GC from a sampled scan -- so the panel
    // reports those rather than standing empty until someone runs QC. Marked
    // as sampled so the numbers are not mistaken for whole-file figures.
    pushIngestStats(stats, obj.facts, gcPercent);
  }

  pushGrade(stats, obj);
  return stats;
}

/**
 * What ingest measured, for a file nobody has run QC on.
 *
 * These come from a sampled scan at ingest rather than a QC tool over the
 * whole file, so they are coarser than anything in the branches above -- but
 * they are real measurements of this file, and showing them beats showing
 * nothing. Every label says "sampled" so the distinction survives.
 */
function pushIngestStats(
  stats: Stat[],
  facts: Record<string, unknown>,
  gcPercent: number | null,
) {
  const n = (k: string) => (typeof facts[k] === "number" ? (facts[k] as number) : null);

  const meanQ = n("mean_quality");
  const readLen = n("read_length");
  const readLenMin = n("read_length_min");
  const readLenMax = n("read_length_max");
  const count = n("read_count_estimate");
  const minPosQ = n("min_position_quality");
  const sampled = n("stats_sampled_reads") ?? n("sampled_records");

  if (meanQ != null) {
    stats.push({
      label: "Mean quality",
      value: `Q${dec1(meanQ)}`,
      lead: true,
      note:
        count != null
          ? `~${int(count)} reads` +
            (sampled != null ? `, sampled ${int(sampled)}` : "")
          : sampled != null
            ? `sampled ${int(sampled)} reads`
            : undefined,
      title: "Measured by sampling at ingest. Run QC for whole-file figures.",
    });
  }

  if (minPosQ != null) {
    stats.push({
      label: "Lowest position Q",
      value: `Q${dec1(minPosQ)}`,
      title: "The weakest per-cycle mean quality seen in the sample.",
    });
  }

  // A fixed-length run says more as one number than as an identical min/max
  // pair; a variable one needs the range.
  if (readLen != null) {
    stats.push({ label: "Read length", value: `${int(readLen)} bp` });
  } else if (readLenMin != null && readLenMax != null) {
    stats.push({
      label: "Read length",
      value: `${int(readLenMin)}–${int(readLenMax)} bp`,
    });
  }

  if (gcPercent != null) {
    stats.push({ label: "GC content", value: `${gcPercent.toFixed(2)}%` });
  }
}

/**
 * BioFlow's own 1-5 read grade, reported as the number rather than only the
 * word. The word already appears as a badge in the kicker; here the tier is
 * the value and what produced it is the qualifier, so the grade can be
 * compared between files instead of just read.
 *
 * Appended last: it is a judgement derived from the measurements above it,
 * not another measurement.
 */
function pushGrade(stats: Stat[], obj: DataObject) {
  const q = readQuality(obj);
  if (!q) return;

  // The basis names the number behind the grade -- "Q30 92.1%", or the mean Q
  // it falls back to. Without it the tier is an opinion with no workings.
  //
  // That fallback reads facts.mean_quality, ingest's 200k-read sample, which
  // is a different measurement from the whole-file qc_mean_quality tile above
  // and can differ from it substantially. Saying "sampled" keeps the two from
  // looking like the same number disagreeing with itself.
  const note = q.basis.startsWith("mean Q") ? `${q.basis} (sampled)` : q.basis;

  stats.push({
    label: "BioFlow grade",
    value: `${q.tier}/5 ${q.word}`,
    note,
    title: q.tooltip,
  });
}

export function FileHeadlineStats({ stats }: { stats: Stat[] }) {
  if (stats.length === 0) return null;

  const lead = stats.find((s) => s.lead);
  const rest = stats.filter((s) => !s.lead);

  return (
    <div className="headline-stats">
      {lead && (
        <div className="headline-lead">
          <div className="stat-label">{lead.label}</div>
          <div className="stat-lead-value">{lead.value}</div>
          {lead.note && <div className="stat-note">{lead.note}</div>}
        </div>
      )}
      <div className="headline-grid">
        {rest.map((s) => (
          <div
            key={s.label}
            className="stat"
            title={s.title}
            style={s.title ? { cursor: "help" } : undefined}
          >
            <div className="stat-label">{s.label}</div>
            <div className="stat-value">{s.value}</div>
            {s.note && <div className="stat-note">{s.note}</div>}
          </div>
        ))}
      </div>
    </div>
  );
}
