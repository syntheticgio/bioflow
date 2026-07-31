import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { PipelineTool, PipelineType } from "../api/types";

/**
 * Every tool BioFlow runs, with what it is for and how to cite it.
 *
 * A single scrolling index rather than a rail-and-detail browser, which the
 * app already has in PipelineToolSelector. That component exists to *choose* a
 * tool mid-job, so it shows one description at a time and hides the rest. This
 * page answers a different question -- "what is in here, and what do I put in
 * my methods section" -- which means every entry has to be visible at once, to
 * a reader scrolling and to ctrl-F. Each name carries an id so a specific tool
 * can be linked to directly.
 *
 * Versions come from the running container, not from this file: they are
 * probed per request, so the page reports what would actually execute rather
 * than what was true when it was written.
 */

/** Reading order, not the enum's order. Roughly the order a user meets these
 *  tools in a run -- retrieval is late because it precedes analysis in time
 *  but is the least interesting group to read about. */
const GROUPS: { type: PipelineType; title: string }[] = [
  { type: "qc", title: "Quality control" },
  { type: "trim", title: "Trimming" },
  { type: "align", title: "Alignment" },
  { type: "variant", title: "Variant calling" },
  { type: "download", title: "Data retrieval" },
  { type: "utility", title: "Utilities" },
];

const GROUP_TITLES: Record<PipelineType, string> = Object.fromEntries(
  GROUPS.map((g) => [g.type, g.title]),
) as Record<PipelineType, string>;

/**
 * The version chip, or the reason there isn't one.
 *
 * `available` is whether the binary is usable on this machine; `runnable` is
 * whether any handler in this application calls it. A tool can be installed
 * and still unwired, so the two get separate chips rather than one merged
 * "status": collapsing them would report a missing binary and an
 * unimplemented code path as the same problem, and only one of those is fixed
 * by installing something.
 *
 * Unavailable splits again, because "not installed" is a claim this page
 * cannot always support. `_probe` reports a binary it could not *run* the
 * same way it reports one it could not *find*, and the difference is visible
 * only in the error text. NanoPlot is the live example: it is installed and
 * works, but it is a Python entry point that takes ~13s to answer
 * `--version`, past the 10s probe timeout. Calling that "not installed"
 * would send a user to reinstall a package they already have.
 */
function VersionChips({ tool }: { tool: PipelineTool }) {
  if (!tool.available) {
    // A timeout means the probe gave up, not that the binary is absent.
    const timedOut = tool.error?.includes("timed out");
    return (
      <span
        className="software-version missing"
        title={tool.error || `${tool.name} is not installed`}
      >
        {timedOut ? "version check timed out" : "not installed"}
      </span>
    );
  }
  return (
    <>
      <span className="software-version">{tool.version || "installed"}</span>
      {!tool.runnable && (
        <span className="software-version pending">not yet wired up</span>
      )}
    </>
  );
}

/** A fact line, rendered only when there is a fact. Every field on ToolMeta
 *  below `summary` may legitimately be empty -- FastQC has never published a
 *  paper, so it has a citation but no DOI to link -- and an empty string must
 *  produce no row rather than a label with nothing after it. */
function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="software-fact">
      <span className="software-fact-label">{label}</span>
      <span className="software-fact-value">{children}</span>
    </div>
  );
}

function ToolEntry({ tool, alsoIn }: { tool: PipelineTool; alsoIn: string[] }) {
  return (
    <article className="software-entry">
      <div className="software-entry-head">
        <h3 className="software-name" id={`tool-${tool.name}`}>
          {tool.name}
        </h3>
        <VersionChips tool={tool} />
      </div>

      {alsoIn.length > 0 && (
        <p className="software-also">
          Also used for {alsoIn.join(" and ")}.
        </p>
      )}

      <div className="software-entry-body">
        <div className="software-prose">
          {tool.summary && <p>{tool.summary}</p>}

          {tool.usage && (
            <>
              <h4 className="software-label">How BioFlow uses it</h4>
              <p>{tool.usage}</p>
            </>
          )}

          {tool.strengths.length > 0 && (
            <>
              <h4 className="software-label">Strengths</h4>
              <ul className="software-strengths">
                {tool.strengths.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </>
          )}
        </div>

        <div className="software-facts">
          {tool.license && <Fact label="License">{tool.license}</Fact>}

          {tool.citation && (
            <Fact label="Cite as">
              {tool.citation_url ? (
                <a href={tool.citation_url} target="_blank" rel="noreferrer">
                  {tool.citation}
                </a>
              ) : (
                tool.citation
              )}
            </Fact>
          )}

          {(tool.homepage || tool.repository) && (
            <Fact label="Links">
              <span className="software-links">
                {tool.homepage && (
                  <a href={tool.homepage} target="_blank" rel="noreferrer">
                    Homepage
                  </a>
                )}
                {/* Skipped when it duplicates the homepage: several tools are
                    hosted on GitHub and have no separate site, and the same
                    URL under two labels reads as a broken link, not a
                    thorough one. */}
                {tool.repository && tool.repository !== tool.homepage && (
                  <a href={tool.repository} target="_blank" rel="noreferrer">
                    Source
                  </a>
                )}
              </span>
            </Fact>
          )}
        </div>
      </div>
    </article>
  );
}

export function HelpSoftware() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["pipelines", "tools"],
    queryFn: api.pipelineTools,
  });

  const tools = data?.tools ?? [];

  // A tool in two pipelines is placed once and cross-referenced, never
  // repeated: fastp is trim+qc and samtools is utility+qc, and rendering
  // either twice would duplicate one of the longest entries on the page and
  // make the index look half again as large as the toolchain really is.
  //
  // Its home is its *first listed* pipeline -- the primary role TOOL_META's
  // author picked -- not whichever heading comes first on this page. Those
  // differ, and the difference is not cosmetic: samtools is (utility, qc), so
  // keying off page order filed it under Quality control and left Utilities
  // with no entries at all. That reads samtools as a QC tool whose flagstat
  // is incidental, when it is a utility whose flagstat happens to serve QC.
  // The remaining pipelines become the cross-reference line.
  const grouped = GROUPS.map(({ type, title }) => ({
    type,
    title,
    entries: tools.filter((t) => t.pipelines[0] === type),
  }));

  return (
    <div className="help-page software-page">
      <h1>Software</h1>
      <p className="help-intro">
        The tools BioFlow runs on your data, what each is for, and how to cite
        it.
      </p>
      <p className="software-note">
        Versions are read from the running container, so this is what would
        execute now — not what was current when this page was written.
      </p>

      {isLoading && <p className="software-note">Checking installed tools…</p>}
      {isError && (
        <p className="software-note">
          Could not reach the server to list installed tools.
        </p>
      )}

      {grouped.map(({ type, title, entries }) =>
        entries.length === 0 ? null : (
          <section key={type} className="software-group">
            <h2 className="software-group-title">{title}</h2>
            {entries.map((tool) => (
              <ToolEntry
                key={tool.name}
                tool={tool}
                alsoIn={tool.pipelines
                  .slice(1)
                  .map((p) => GROUP_TITLES[p]?.toLowerCase())
                  .filter(Boolean)}
              />
            ))}
          </section>
        ),
      )}
    </div>
  );
}
