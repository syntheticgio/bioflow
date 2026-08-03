import { Link } from "react-router-dom";

/**
 * What BioFlow is, for anyone who lands on this page without the context
 * everyone building it already has.
 */
export function HelpAbout() {
  return (
    <div className="help-page">
      <h1>About BioFlow</h1>
      <p className="help-intro">
        A local, single-user web application for managing bioinformatics data
        files — projects, uploads, metadata, and a priority- and load-aware
        background job queue.
      </p>

      <div className="software-prose">
        <p>
          BioFlow runs entirely on your own machine: the file library, the
          job queue, and the database all live locally rather than in a
          hosted service. It's built as the foundation for assigning rich
          metadata to files and launching computations and pipelines —
          alignment, variant calling, and more — against them.
        </p>
        <p>
          See <Link to="/help/software">Software</Link> for what's installed
          and how each tool is used,{" "}
          <Link to="/help/sources">Data Sources</Link> for where reference
          data comes from, and{" "}
          <Link to="/help/calculations">BioFlow Calculations</Link> for what
          the numbers on a file mean.
        </p>
      </div>
    </div>
  );
}
