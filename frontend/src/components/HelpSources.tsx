import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { DataSource } from "../api/types";

/**
 * The external services BioFlow reads from, and what it asks each one for.
 *
 * Deliberately the same ruled treatment as the Software page, reusing its
 * classes: to a reader these are two halves of one question -- what does this
 * application depend on -- and giving them different furniture would suggest a
 * distinction that is not there.
 *
 * What is different is what a source *has*. No version chip and no license
 * anywhere on this page: a source has no binary to probe and no build to pin,
 * so a version here could only ever be invented, and an invented one would
 * look like provenance while carrying none. The backend leaves the field out
 * of DataSource entirely for the same reason. What replaces it is `terms` --
 * the usage policy, which is the thing that actually governs calling someone
 * else's service.
 */

/** Grouped by what a source *is*. "api" is something this code calls;
 *  "database" is a record page handed to the user as a link. "reference" has
 *  no entries today and its section simply does not render -- a bare heading
 *  over nothing reads as a page that failed to load. */
const GROUPS: { kind: DataSource["kind"]; title: string }[] = [
  { kind: "api", title: "Programmatic interfaces" },
  { kind: "database", title: "Archives and databases" },
  { kind: "reference", title: "Reference data" },
];

function SourceEntry({ source }: { source: DataSource }) {
  return (
    <article className="software-entry">
      <div className="software-entry-head">
        <h3 className="software-name" id={`source-${source.kind}-${source.name}`}>
          {source.name}
        </h3>
      </div>

      <div className="software-entry-body">
        <div className="software-prose">
          {source.summary && <p>{source.summary}</p>}

          {source.usage && (
            <>
              <h4 className="software-label">How BioFlow uses it</h4>
              <p>{source.usage}</p>
            </>
          )}
        </div>

        <div className="software-facts">
          {source.citation && (
            <div className="software-fact">
              <span className="software-fact-label">Cite as</span>
              <span className="software-fact-value">
                {source.citation_url ? (
                  <a href={source.citation_url} target="_blank" rel="noreferrer">
                    {source.citation}
                  </a>
                ) : (
                  source.citation
                )}
              </span>
            </div>
          )}

          <div className="software-fact">
            <span className="software-fact-label">Links</span>
            <span className="software-fact-value">
              <span className="software-links">
                <a href={source.homepage} target="_blank" rel="noreferrer">
                  Homepage
                </a>
                {/* E-utilities has no landing page separate from its
                    documentation, so `docs` is empty there rather than
                    repeating the homepage URL under a second label. */}
                {source.docs && (
                  <a href={source.docs} target="_blank" rel="noreferrer">
                    Documentation
                  </a>
                )}
                {source.terms && (
                  <a href={source.terms} target="_blank" rel="noreferrer">
                    Usage policy
                  </a>
                )}
              </span>
            </span>
          </div>
        </div>
      </div>
    </article>
  );
}

export function HelpSources() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["system", "sources"],
    queryFn: api.sources,
  });

  const sources = data?.sources ?? [];

  return (
    <div className="help-page software-page">
      <h1>Data Sources</h1>
      <p className="help-intro">
        The external services BioFlow reads from, what it asks each one for,
        and where their terms of use are.
      </p>
      <p className="software-note">
        Every lookup here is best-effort. A source that is unreachable, rate
        limited, or has retired an accession skips the enrichment rather than
        failing your upload.
      </p>

      {isLoading && <p className="software-note">Loading data sources…</p>}
      {isError && (
        <p className="software-note">
          Could not reach the server to list data sources.
        </p>
      )}

      {GROUPS.map(({ kind, title }) => {
        const entries = sources.filter((s) => s.kind === kind);
        if (entries.length === 0) return null;
        return (
          <section key={kind} className="software-group">
            <h2 className="software-group-title">{title}</h2>
            {entries.map((source) => (
              <SourceEntry key={source.name} source={source} />
            ))}
          </section>
        );
      })}
    </div>
  );
}
