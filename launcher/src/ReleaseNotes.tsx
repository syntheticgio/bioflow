import { useEffect, useState } from "react";
import { listReleaseNotes, openExternal } from "./commands";
import type { Release } from "./types";
import {
  formatPublished,
  initialRelease,
  parseReleaseBody,
  releaseLabel,
} from "./release-notes-logic";
import type { NoteBlock, Span } from "./release-notes-logic";

const RELEASES_URL = "https://github.com/syntheticgio/bioflow/releases";

interface Props {
  onClose: () => void;
}

/** Renders the closed markdown vocabulary `.github/release.yml` generates --
 *  see parseReleaseBody. Links open in the user's browser rather than
 *  navigating the webview, which would strand the launcher on a web page
 *  with no way back. */
function Spans({ spans }: { spans: Span[] }) {
  return (
    <>
      {spans.map((span, i) => {
        if (span.kind === "code") {
          return (
            <code key={i} className="notes-code">
              {span.text}
            </code>
          );
        }
        if (span.kind === "link") {
          return (
            <a
              key={i}
              href={span.href}
              className="notes-link"
              onClick={(e) => {
                e.preventDefault();
                openExternal(span.href);
              }}
            >
              {span.text}
            </a>
          );
        }
        return <span key={i}>{span.text}</span>;
      })}
    </>
  );
}

function Block({ block }: { block: NoteBlock }) {
  if (block.kind === "code") {
    return <pre className="notes-pre">{block.text}</pre>;
  }
  if (block.kind === "heading") {
    return block.level === 2 ? (
      <h3 className="notes-h2">
        <Spans spans={block.spans} />
      </h3>
    ) : (
      <h4 className="notes-h3">
        <Spans spans={block.spans} />
      </h4>
    );
  }
  if (block.kind === "bullet") {
    return (
      <li className="notes-bullet">
        <Spans spans={block.spans} />
      </li>
    );
  }
  return (
    <p className="notes-paragraph">
      <Spans spans={block.spans} />
    </p>
  );
}

/**
 * The Release notes dialog (#842). Opens on the release matching the running
 * stack -- the backend resolves that from `BIOFLOW_TAG`, so it agrees with
 * the Settings version dropdown -- and offers every other published release
 * in a dropdown, which is what makes it useful next to the Update button:
 * the user can read what a newer version contains before taking it.
 */
export function ReleaseNotes({ onClose }: Props) {
  const [releases, setReleases] = useState<Release[]>([]);
  const [runningTag, setRunningTag] = useState<string | null>(null);
  const [viewing, setViewing] = useState<Release | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    listReleaseNotes()
      .then((notes) => {
        if (cancelled) return;
        setReleases(notes.releases);
        setRunningTag(notes.selectedTag);
        setViewing(initialRelease(notes.releases, notes.selectedTag));
      })
      // The command already collapses every failure to an empty list, so
      // this only catches an IPC-level fault. Either way the empty state
      // below renders -- there is no error to show the user that is more
      // useful than the link to GitHub.
      .catch(() => {
        if (!cancelled) setReleases([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const published = viewing ? formatPublished(viewing.publishedAt) : null;
  const blocks = viewing ? parseReleaseBody(viewing.body) : [];

  return (
    <div className="dialog-backdrop">
      <section className="dialog dialog-wide" aria-label="Release notes">
        <h2 className="dialog-title">Release notes</h2>
        <div className="dialog-rule" />

        {releases.length > 0 && (
          <div className="notes-picker">
            <select
              className="field-value-input"
              value={viewing?.tag ?? ""}
              onChange={(e) =>
                setViewing(releases.find((r) => r.tag === e.target.value) ?? null)
              }
              aria-label="Version"
            >
              {releases.map((release) => (
                <option key={release.tag} value={release.tag}>
                  {releaseLabel(release, runningTag)}
                </option>
              ))}
            </select>
            {published && <span className="field-hint">{published}</span>}
          </div>
        )}

        <div className="dialog-body">
          {loading && <p className="field-hint">Loading…</p>}

          {!loading && releases.length === 0 && (
            <p className="notes-paragraph">
              Couldn't reach GitHub to load the release notes. Read them at{" "}
              <a
                href={RELEASES_URL}
                className="notes-link"
                onClick={(e) => {
                  e.preventDefault();
                  openExternal(RELEASES_URL);
                }}
              >
                github.com/syntheticgio/bioflow/releases
              </a>
              .
            </p>
          )}

          {!loading && viewing && blocks.length === 0 && (
            <p className="field-hint">This release was published without notes.</p>
          )}

          {!loading && viewing && blocks.length > 0 && (
            <div className="notes-body">
              {blocks.map((block, i) => (
                <Block key={i} block={block} />
              ))}
            </div>
          )}
        </div>

        <div className="dialog-actions">
          <button className="btn btn-secondary" onClick={onClose}>
            Close
          </button>
        </div>
      </section>
    </div>
  );
}
