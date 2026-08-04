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

/** Display names and reading order. Reading order is roughly the order a user
 *  meets these tools in a run -- assembly before alignment because a de novo
 *  assembly is what you align *to* when there is no published reference, and
 *  retrieval late because it precedes analysis in time but is the least
 *  interesting group to read about.
 *
 *  This is presentation only. It is deliberately *not* the list of sections:
 *  see `groupsFrom` below for why. */
const TITLES: Record<string, string> = {
  qc: "Quality control",
  trim: "Trimming",
  assemble: "Assembly",
  reference_assembly: "Reference assembly",
  assembly_qc: "Assembly QC",
  align: "Alignment",
  expression: "Expression",
  variant: "Variant calling",
  download: "Data retrieval",
  utility: "Utilities",
};

const ORDER: PipelineType[] = [
  "qc", "trim", "assemble", "reference_assembly", "assembly_qc", "align",
  "expression", "variant", "download", "utility",
];

/**
 * The sections to render, derived from the tools themselves.
 *
 * This used to be a hardcoded list, and it silently lost tools twice in one
 * day: the expression vertical shipped featureCounts and pydeseq2 with no
 * section to render them in, and the assembly work nearly did the same for
 * Flye. Both times TOOL_META was complete and every backend test passed --
 * the page just quietly omitted them, because a list of sections maintained
 * by hand beside a list of tools maintained by hand is a mirror with nothing
 * holding it.
 *
 * Deriving removes the mirror rather than testing it. A new PipelineType now
 * renders the moment a tool declares it; an unknown type gets a title-cased
 * fallback heading, which is mildly ugly and infinitely better than the tool
 * being invisible. Types with no tools are dropped, so an enum member nothing
 * uses does not leave an empty heading.
 *
 * A cross-language test was the other option and was tried first. It cannot
 * work here: run-worktree-tests.sh mounts only backend/app and backend/tests,
 * so the .tsx is not visible to pytest at all.
 */
export function groupsFrom(
  tools: PipelineTool[],
): { type: PipelineType; title: string }[] {
  const present = new Set<PipelineType>();
  for (const tool of tools) for (const p of tool.pipelines ?? []) present.add(p);

  const ordered = ORDER.filter((t) => present.has(t));
  // Anything the backend has that ORDER does not know about, appended rather
  // than dropped. This is the branch that makes the page self-healing.
  // Cast, not a lie: these came off the wire, so the backend has a
  // PipelineType this build of the frontend predates. Rendering it under a
  // title-cased heading beats dropping the tools that declare it.
  const unknown = ([...present] as PipelineType[])
    .filter((t) => !ORDER.includes(t))
    .sort();

  return [...ordered, ...unknown].map((type) => ({
    type,
    title: TITLES[type] ?? type.charAt(0).toUpperCase() + type.slice(1),
  }));
}

const GROUP_TITLES: Record<string, string> = TITLES;

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
  allTools,
}: {
  tools: PipelineTool[];
  filter: PipelineType | null;
  onFilter: (type: PipelineType | null) => void;
  // The unfiltered set, for the columns. `tools` is already narrowed by the
  // active filter, and deriving columns from it would delete every other
  // column the moment one was clicked -- taking the way back out with them.
  allTools: PipelineTool[];
}) {
  const [expanded, setExpanded] = useState(true);
  const GROUPS = groupsFrom(allTools);

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

function ToolEntry({
  tool,
  alsoIn,
  solo,
}: {
  tool: PipelineTool;
  alsoIn: string[];
  // True when this entry has no row-mate in .software-group-entries -- an odd
  // tool out at the end of its section, sitting alone in a two-column grid
  // row. The facts card normally sinks to the bottom of the row to line up
  // with a neighbour's shorter entry (see .software-facts), but with no
  // neighbour to line up with, that just strands it below empty space. `solo`
  // switches the card back to following the prose instead.
  solo: boolean;
}) {
  return (
    <article className={`software-entry${solo ? " is-solo" : ""}`}>
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
  // Sections from the unfiltered set, so the same headings exist whether or
  // not a filter is on; `entries` below is what the filter narrows.
  const grouped = groupsFrom(allTools).map(({ type, title }) => ({
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

      <ToolMatrix
        tools={matrixRows}
        allTools={allTools}
        filter={filter}
        onFilter={setFilter}
      />

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
              {entries.map((tool, i) => (
                <ToolEntry
                  key={tool.name}
                  tool={tool}
                  alsoIn={tool.pipelines
                    .slice(1)
                    .map((p) => GROUP_TITLES[p]?.toLowerCase())
                    .filter(Boolean)}
                  solo={entries.length % 2 === 1 && i === entries.length - 1}
                />
              ))}
            </div>
          </section>
        ),
      )}
    </div>
  );
}
