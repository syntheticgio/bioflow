import { useState } from "react";
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
  // Before alignment: a de novo assembly is what you align *to* when there is
  // no published reference, so this is the earlier step for that workflow.
  { type: "assemble", title: "Assembly" },
  { type: "align", title: "Alignment" },
  // Added while merging the assembly work: `PipelineType.EXPRESSION` reached
  // the backend with featureCounts and pydeseq2 documented against it, but not
  // this list, so both tools were absent from /help/software with nothing
  // failing. That is the same silent omission `suggestion_service`'s
  // hand-maintained mapping is warned about in CLAUDE.md, in a second place.
  { type: "expression", title: "Expression" },
  { type: "variant", title: "Variant calling" },
  { type: "download", title: "Data retrieval" },
  { type: "utility", title: "Utilities" },
];

const GROUP_TITLES: Record<PipelineType, string> = Object.fromEntries(
  GROUPS.map((g) => [g.type, g.title]),
) as Record<PipelineType, string>;

/**
 * Every tool against every pipeline, as a grid.
 *
 * The entries below answer "what is this tool"; this answers the question they
 * cannot, which is "what have I got for job X" -- a reader who wants a variant
 * caller should not have to scroll six sections to learn there are two. It is
 * built from the same `tool.pipelines` the sections are built from, so a tool
 * added to TOOL_META appears here with no second edit and no mapping to rot.
 *
 * Primary and secondary roles are drawn differently rather than as one mark.
 * `pipelines[0]` is not incidental ordering -- it decides which section a tool
 * is filed under, and marking both cells identically would say samtools is a
 * QC tool as much as a utility, which is the exact reading the grouping code
 * below goes out of its way to avoid.
 *
 * Names link to the entries, so the matrix works as the page's index.
 * Unavailable tools are dimmed and marked, because a filled cell promising a
 * variant caller that isn't installed is worse than no cell at all.
 *
 * Collapsible, because fifteen rows is a lot of page to scroll past once you
 * have used it -- but it opens *expanded*, since a summary that must be
 * clicked open is not one you can take in at a glance. Folding it is for the
 * reader who has finished with it, not a default state to be discovered.
 * The toggle reuses .group-title from the explorer's category headers rather
 * than inventing a second disclosure idiom for one page.
 *
 * Column heads are buttons that filter the whole page to that pipeline. They
 * are `th > button` rather than a click handler on the `th` so the control is
 * focusable and announces itself; a bare clickable table header is invisible
 * to the keyboard.
 */
function ToolMatrix({
  tools,
  filter,
  onFilter,
}: {
  tools: PipelineTool[];
  filter: PipelineType | null;
  onFilter: (type: PipelineType | null) => void;
}) {
  const [expanded, setExpanded] = useState(true);

  if (tools.length === 0) return null;

  return (
    <section className="software-matrix-wrap" aria-labelledby="matrix-title">
      <button
        type="button"
        id="matrix-title"
        className="group-title software-matrix-toggle"
        aria-expanded={expanded}
        aria-controls="matrix-body"
        onClick={() => setExpanded((v) => !v)}
      >
        <span className="group-chevron">▶</span>
        <span>At a glance</span>
        <span className="group-count">{tools.length} tools</span>
      </button>

      {/* `hidden` rather than unmounting: it keeps the element `aria-controls`
          names present in the DOM, preserves the table's horizontal scroll
          across a fold/unfold, and is excluded from find-on-page, so ctrl-F
          cannot match a row the reader cannot see. */}
      <div id="matrix-body" hidden={!expanded}>
      {/* Its own scroll container: six columns plus names outruns a narrow
          panel, and the page body must not scroll sideways with it. */}
      <div className="software-matrix-scroll">
        <table className="software-matrix">
          <thead>
            <tr>
              <th scope="col" className="software-matrix-corner">
                Tool
              </th>
              {GROUPS.map(({ type, title }) => {
                const active = filter === type;
                return (
                  <th
                    key={type}
                    scope="col"
                    className={active ? "is-filtered" : undefined}
                    aria-sort={active ? "other" : undefined}
                  >
                    <button
                      type="button"
                      className="software-matrix-filter"
                      aria-pressed={active}
                      onClick={() => onFilter(active ? null : type)}
                      title={
                        active
                          ? `Show all tools again`
                          : `Show only ${title.toLowerCase()} tools`
                      }
                    >
                      {title}
                    </button>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {tools.map((tool) => (
              <tr
                key={tool.name}
                className={tool.available ? undefined : "is-missing"}
              >
                <th scope="row">
                  <a href={`#tool-${tool.name}`}>{tool.name}</a>
                  {!tool.available && (
                    <span className="software-matrix-flag" aria-hidden="true">
                      !
                    </span>
                  )}
                </th>
                {GROUPS.map(({ type, title }) => {
                  const rank = tool.pipelines.indexOf(type);
                  const role =
                    rank === 0 ? "primary" : rank > 0 ? "secondary" : null;
                  return (
                    <td key={type} className={role ? `is-${role}` : undefined}>
                      {role ? (
                        <>
                          <span aria-hidden="true" className="software-dot" />
                          <span className="visually-hidden">
                            {`${tool.name}: ${role} ${title.toLowerCase()} tool`}
                          </span>
                        </>
                      ) : null}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Each swatch and its label are one item, so the flex gap falls between
          pairs rather than between a swatch and the word it labels. */}
      <p className="software-matrix-key">
        <span className="software-matrix-key-item">
          <span className="software-dot is-key" /> primary role
        </span>
        <span className="software-matrix-key-item">
          <span className="software-dot is-key is-secondary-key" /> also used
          for
        </span>
        <span className="software-matrix-key-item">
          <span className="software-matrix-flag is-key">!</span> not installed
        </span>
      </p>
      </div>
    </section>
  );
}

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

  // Which pipeline the page is narrowed to, or null for everything. Held here
  // rather than inside ToolMatrix because it filters the entries below as well
  // -- narrowing the grid while leaving six sections of prose underneath would
  // answer "what aligners have I got" and then still make the reader scroll
  // past every QC tool to read about them.
  const [filter, setFilter] = useState<PipelineType | null>(null);

  const allTools = data?.tools ?? [];

  // Membership is `pipelines.includes`, not `pipelines[0] === type`: a filter
  // for Quality control that hid samtools would be lying about the toolchain,
  // since flagstat is exactly what a reader filtering for QC is looking for.
  // Uninstalled tools stay in for the same reason the matrix marks rather than
  // drops them -- "what could do this job" is the question being asked, and
  // omitting the answer because it needs installing is worse than dimming it.
  const tools = filter
    ? allTools.filter((t) => t.pipelines.includes(filter))
    : allTools;

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

  // The matrix reads top-to-bottom in the same order the sections read, so
  // scanning a column and then scrolling to the entry moves in one direction.
  // Flattening `grouped` rather than re-sorting `tools` keeps that guarantee
  // automatic: a tool the sections would drop cannot appear as a matrix row
  // linking to an anchor that was never rendered.
  const matrixRows = grouped.flatMap((g) => g.entries);

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

      <ToolMatrix tools={matrixRows} filter={filter} onFilter={setFilter} />

      {/* Says why the page is short, and gives the way back. Without it a
          filtered page is indistinguishable from a page that only ever had two
          tools on it -- the pressed column head is the only other clue, and it
          scrolls out of view. */}
      {filter && (
        <p className="software-filter-note">
          <span>
            Showing {tools.length}{" "}
            {tools.length === 1 ? "tool" : "tools"} for{" "}
            <strong>{GROUP_TITLES[filter].toLowerCase()}</strong>.
          </span>
          <button
            type="button"
            className="software-filter-clear"
            onClick={() => setFilter(null)}
          >
            Show all {allTools.length}
          </button>
        </p>
      )}

      {grouped.map(({ type, title, entries }) =>
        entries.length === 0 ? null : (
          <section key={type} className="software-group">
            <h2 className="software-group-title">{title}</h2>
            {/* Entries are wrapped rather than laid out by .software-group
                itself: the grid must contain only entries, so the heading is
                not pulled into a column beside the first tool. It also makes
                each section its own grid, which is what keeps a new section
                starting at the left column instead of continuing next to the
                previous section's last tool. */}
            <div className="software-group-entries">
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
            </div>
          </section>
        ),
      )}
    </div>
  );
}
