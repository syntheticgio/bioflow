/**
 * How BioFlow's derived numbers are computed.
 *
 * One section per topic. Read Quality Score is the first; the structure exists
 * so the next derived number is one more <section>, not a new page.
 */
export function HelpCalculations() {
  return (
    <div className="help-page">
      <h1>BioFlow Calculations</h1>
      <p className="help-intro">
        How the numbers BioFlow derives are computed, and what they do and do
        not tell you.
      </p>

      <section className="help-section">
        <h2>Read Quality Score</h2>
        <p>
          Every read file gets a 1–5 grade. Base quality sets the grade;
          specific problems can lower it. The grade appears as a colored dot on
          the file's icon, as a word in the file list, and in the detail panel
          header — hover any of them for the score and its reasoning.
        </p>

        <h3>Base quality sets the tier</h3>
        <p>
          When QC has run, the grade comes from <strong>Q30</strong> — the
          fraction of bases with a 99.9%-or-better confidence call, the standard
          Illumina yardstick. Thresholds follow Illumina convention:
        </p>
        <table className="help-table">
          <thead>
            <tr>
              <th>Grade</th>
              <th>Word</th>
              <th>Q30</th>
              <th>Without QC: mean quality</th>
            </tr>
          </thead>
          <tbody>
            <tr><td>5</td><td>Excellent</td><td>≥ 90%</td><td>≥ Q36</td></tr>
            <tr><td>4</td><td>Good</td><td>≥ 80%</td><td>≥ Q32</td></tr>
            <tr><td>3</td><td>Fair</td><td>≥ 70%</td><td>≥ Q28</td></tr>
            <tr><td>2</td><td>Poor</td><td>≥ 55%</td><td>≥ Q22</td></tr>
            <tr><td>1</td><td>Unsuitable</td><td>&lt; 55%</td><td>&lt; Q22</td></tr>
          </tbody>
        </table>
        <p>
          Before QC runs, the grade uses the mean quality measured from a
          200,000-read sample at ingest. That is a coarser signal, so the
          tooltip says which one it used.
        </p>

        <h3>What lowers the grade</h3>
        <p>Each of these drops the grade by one, never below 1:</p>
        <ul>
          <li>
            <strong>Duplication above 50%</strong> — but only when the assay is
            unset, WGS, or WES. See the caveat below.
          </li>
          <li>
            <strong>More than 1% ambiguous (N) bases</strong> — no library
            design wants these, so this always counts.
          </li>
          <li>
            <strong>Collapsed cycles</strong> — some read position averages
            below Q20 while the overall mean is Q30 or better. A healthy
            average can hide a bad read tail, which trimming fixes.
          </li>
          <li>
            <strong>A collapsed cycle</strong> — more than 5% of reads have an
            uncalled base at one specific position. This usually means a single
            sequencing cycle failed. When it applies it replaces the ambiguous-base
            penalty above rather than adding to it: both describe the same defect,
            and one problem should only cost one grade.
          </li>
        </ul>

        <h3>The duplication caveat</h3>
        <p>
          High duplication means something different depending on how the
          library was made. For whole-genome or exome sequencing it suggests
          over-amplification of too little input. For{" "}
          <strong>RNA-seq, amplicon, targeted panel, ChIP-seq, and ATAC-seq</strong>{" "}
          it is expected — those methods amplify on purpose, and abundant
          transcripts or enriched regions are genuinely sequenced many times.
        </p>
        <p>
          So the duplication penalty is skipped for those assays. When the assay
          is not recorded, the penalty is applied, because unlabeled data is
          most often whole-genome. If a file is RNA-seq or amplicon and looks
          unfairly marked down, set <strong>Assay</strong> under the file's
          Metadata tab and the grade will account for it.
        </p>

        <h3>What GC content does not do</h3>
        <p>
          GC content never changes the grade. Expected GC is a property of the
          organism — roughly 41% for human, under 20% for <em>Plasmodium</em> —
          so without knowing the source, an unusual GC is not evidence of a
          problem.
        </p>
        <p>
          The GC distribution chart will still show you what to expect when
          something can say what that is. It looks for an answer in two places,
          in order: a <strong>reference genome in the same project</strong>,
          whose GC is measured from the file itself, and then a small table of{" "}
          <strong>published figures for well-known genomes</strong>, used when
          the file's Organism metadata names one. The chart always says which of
          the two it used, and which file or assembly the number came from.
        </p>
        <p>
          When neither applies, no expected line is drawn. You can tick{" "}
          <strong>overlay normal distribution</strong> to see a bell curve fitted
          to the file's own data — useful for judging whether the distribution is
          one clean peak or two overlapping ones, but it is a description of this
          file, not an expectation of it. A second peak usually means
          contamination; a very broad one can mean amplification bias.
        </p>
        <p>
          The GC distribution is measured from a sample of up to 200,000 reads,
          while the N content chart is fastp's count across the whole file. Both
          say which under the chart.
        </p>

        <h3>When no grade is shown</h3>
        <p>
          Files with no grade are either not read files (alignments, references,
          indexes), still being ingested, or missing quality measurements. An
          empty space means the question does not apply — not that the file
          failed.
        </p>
      </section>
    </div>
  );
}
