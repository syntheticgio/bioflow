import body from "../assets/genome-analysis-review/body.html?raw";

/**
 * "From Reads to Biology -- a field guide to genome assembly and downstream
 * analysis", embedded whole. The source document is a self-contained article
 * (own TOC sidebar, own typography scale, five embedded SVG figures shared
 * with HelpWorkflowDiagrams) with no external assets or scripts, so it's
 * rendered as one HTML blob rather than transcribed into JSX -- the source
 * has ~150 heading anchors and reproducing that by hand would be a second
 * copy to keep in sync with the original guide.
 *
 * Its CSS (originally global tag selectors: h1, p, a, table, ...) has been
 * scoped under .genome-review-doc in styles.css so it styles only this
 * document and doesn't leak into the rest of BioFlow.
 */
export function HelpGenomeAnalysisReview() {
  return (
    <div className="help-page genome-review-page">
      <div
        className="genome-review-doc"
        // eslint-disable-next-line react/no-danger -- static, locally
        // authored article shipped alongside this component; no user input.
        dangerouslySetInnerHTML={{ __html: body }}
      />
    </div>
  );
}
