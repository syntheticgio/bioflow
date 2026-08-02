interface Props {
  canPreprocess: boolean;
  canAlign: boolean;
  canCallVariants: boolean;
  canQuantify: boolean;
  canAssemble: boolean;
  canScoreCompleteness: boolean;
  canDifferentialExpression: boolean;
  canQC: boolean;
  hasQc: boolean;
  hasTrim: boolean;
  /** Named reference for the Align button, when the project has one. */
  alignTarget: string | null;
  onStart: (pipeline: "trim" | "align" | "variant") => void;
  /** Not part of `onStart`: counting has one tool, so it opens its dialog
   * directly rather than going through the tool selector first. */
  onQuantify: () => void;
  onAssemble: () => void;
  onScoreCompleteness: () => void;
  onDifferentialExpression: () => void;
  onRunQC: () => void;
  qcPending: boolean;
  onReingest: () => void;
  reingestPending: boolean;
  reingestDisabled: boolean;
}

/**
 * The tool pickers: pick an operation, then choose its settings in a dialog.
 *
 * Deliberately presentational -- every piece of state and every mutation lives
 * in DetailPanel, which owns the dialogs these buttons open. This component
 * decides only what is shown and what it is called.
 *
 * The distinction from the suggestion cards below is settings, not capability:
 * both run the same pipelines, but a card has already answered the questions
 * this route asks.
 */
export function Computations({
  canPreprocess,
  canAlign,
  canCallVariants,
  canQuantify,
  canAssemble,
  canScoreCompleteness,
  canDifferentialExpression,
  canQC,
  hasQc,
  hasTrim,
  alignTarget,
  onStart,
  onQuantify,
  onAssemble,
  onScoreCompleteness,
  onDifferentialExpression,
  onRunQC,
  qcPending,
  onReingest,
  reingestPending,
  reingestDisabled,
}: Props) {
  return (
    <div className="section">
      <div className="section-title">Computations</div>
      <div className="section-note" style={{ marginBottom: 10 }}>
        Run a tool on this file and choose its settings yourself. The cards
        below run these same operations with the settings already decided.
      </div>
      <div className="detail-actions">
        {canPreprocess && (
          <button
            type="button"
            className="btn primary"
            onClick={() => onStart("trim")}
            title="Adapter-trim and quality-filter these reads"
          >
            {/* "Preprocess", not "Trim": the operation also quality- and
                length-filters, which the narrower name hides. The pipeline key
                is still "trim". */}
            Preprocess
            {!hasTrim && (
              <span className="outstanding-badge" aria-label="Not yet run" />
            )}
          </button>
        )}
        {canAlign && (
          <button
            type="button"
            className="btn"
            onClick={() => onStart("align")}
            title={
              alignTarget
                ? `Align these reads against ${alignTarget}`
                : "Align these reads against a reference"
            }
          >
            {/* Names the reference when the project has one, so the button
                says what it will actually do. Falls back to the bare verb when
                there is nothing to name -- the dialog then handles picking, or
                explains that none exists. */}
            {alignTarget ? `Align to ${alignTarget}` : "Align"}
          </button>
        )}
        {canCallVariants && (
          <button
            type="button"
            className="btn"
            onClick={() => onStart("variant")}
            title="Call variants from this alignment"
          >
            Call variants
          </button>
        )}
        {canAssemble && (
          <button
            type="button"
            className="btn"
            onClick={onAssemble}
            title="Assemble these long reads into contigs, with no reference"
          >
            Assemble
          </button>
        )}
        {canScoreCompleteness && (
          <button
            type="button"
            className="btn"
            onClick={onScoreCompleteness}
            title="Score what fraction of a lineage-specific ortholog set can be found in this assembly"
          >
            Score completeness
          </button>
        )}
        {canQuantify && (
          <button
            type="button"
            className="btn"
            onClick={onQuantify}
            title="Count this alignment's reads against a gene annotation"
          >
            Count reads per gene
          </button>
        )}
        {canDifferentialExpression && (
          <button
            type="button"
            className="btn"
            onClick={onDifferentialExpression}
            title="Compare gene expression between groups of samples"
          >
            Differential expression…
          </button>
        )}
        {canQC && (
          <button
            type="button"
            className="btn"
            onClick={onRunQC}
            disabled={qcPending}
            title="Measure read quality with fastp and FastQC"
          >
            {qcPending ? "Running QC…" : "Run QC"}
            {!hasQc && !qcPending && (
              <span className="outstanding-badge" aria-label="Not yet run" />
            )}
          </button>
        )}
        {/* Set as text -- it is a repair action, not one of the pipeline
            steps. */}
        <button
          type="button"
          className="btn-text"
          onClick={onReingest}
          disabled={reingestPending || reingestDisabled}
          title="Re-run format detection and header parsing"
        >
          {reingestPending ? "Re-ingesting…" : "Re-ingest"}
        </button>
      </div>
    </div>
  );
}
