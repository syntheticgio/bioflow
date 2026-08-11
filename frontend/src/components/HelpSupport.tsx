import { Link } from "react-router-dom";

/**
 * Where to get help, and what to expect when you do.
 *
 * The boundary is deliberate: BioFlow is a personal project, so this page
 * says so plainly -- help is offered when possible, not guaranteed -- while
 * still pointing every bug report and feature request at the one channel
 * (GitHub Issues) that can actually act on them.
 */
export function HelpSupport() {
  return (
    <div className="help-page">
      <h1>Support</h1>
      <p className="help-intro">
        BioFlow is a personal project that others are welcome to use. Bugs,
        feature requests, and questions all belong on the project's GitHub
        issue tracker.
      </p>

      <div className="software-prose">
        <h2>Reporting a bug or requesting a feature</h2>
        <p>
          Open an issue at{" "}
          <a
            href="https://github.com/syntheticgio/bioflow/issues"
            target="_blank"
            rel="noreferrer"
          >
            github.com/syntheticgio/bioflow/issues
          </a>
          . The repository itself lives at{" "}
          <a
            href="https://github.com/syntheticgio/bioflow"
            target="_blank"
            rel="noreferrer"
          >
            github.com/syntheticgio/bioflow
          </a>
          .
        </p>
        <p>
          A good bug report says what you did, what you expected, and what
          happened instead — including the file type and pipeline step if the
          problem is with a computation. Screenshots and the relevant log
          output help more than prose alone.
        </p>

        <h2>What to expect</h2>
        <p>
          BioFlow is maintained by one person in their spare time, so
          support is offered when possible but is not guaranteed. Issues are
          read and triaged, but there is no response-time commitment: a
          feature request may sit unanswered for a long while, and a bug may
          be fixed promptly or not at all. Please don't treat the tracker as
          a support hotline.
        </p>
        <p>
          Before opening an issue, check the existing ones — your bug or
          request may already be there, and a duplicate just buries the
          original. If you're unsure whether something is a bug or a usage
          question, open it anyway; the tracker is also where questions get
          answered, when they do.
        </p>

        <h2>Other pages</h2>
        <p>
          For how BioFlow works, see{" "}
          <Link to="/help/about">About BioFlow</Link> — and for what's
          installed and where the data comes from,{" "}
          <Link to="/help/software">Software</Link> and{" "}
          <Link to="/help/sources">Data Sources</Link>.
        </p>
      </div>
    </div>
  );
}
