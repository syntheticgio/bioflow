// Pure logic behind the Release notes dialog -- split out the same way
// update-logic.ts and settings-logic.ts separate their components' pure
// logic. This repo has no jsdom or testing-library setup, so a pure module
// is the only testable seam (see update-logic.ts's header).

import type { Release } from "./types";

/** A parsed line of a release body, ready to render as one element. */
export type NoteBlock =
  /** `##`/`###` heading. `level` is 2 or 3; deeper levels clamp to 3. */
  | { kind: "heading"; level: 2 | 3; spans: Span[] }
  /** A `*`/`-` bullet. */
  | { kind: "bullet"; spans: Span[] }
  /** A fenced block, rendered verbatim in a <pre>. */
  | { kind: "code"; text: string }
  /** An ordinary paragraph line. */
  | { kind: "paragraph"; spans: Span[] };

/** An inline run within a block. */
export type Span =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "link"; text: string; href: string };

/**
 * The launcher has no markdown dependency, and the bodies it renders are not
 * arbitrary markdown: `.github/release.yml` generates them at tag time from
 * merged PR titles, so the vocabulary is closed and small -- `##`/`###`
 * headings, `*` bullets, fenced code, inline backticks and bare URLs. This
 * parses that vocabulary and nothing else; anything unrecognised falls
 * through as plain text rather than being dropped, so an unexpected
 * construct reads as slightly-flat prose instead of a hole in the notes.
 */
export function parseReleaseBody(body: string): NoteBlock[] {
  const blocks: NoteBlock[] = [];
  // Generator-inserted provenance comments ("Release notes generated
  // using configuration in .github/release.yml") are noise to a reader.
  const cleaned = body.replace(/<!--[\s\S]*?-->/g, "");
  const lines = cleaned.split(/\r?\n/);

  let fenced: string[] | null = null;
  for (const line of lines) {
    if (line.trimStart().startsWith("```")) {
      if (fenced === null) {
        fenced = [];
      } else {
        // A fence closing on an empty block would render as a stray empty
        // box; the generated bodies never do this, but a hand-edited one can.
        if (fenced.length > 0) blocks.push({ kind: "code", text: fenced.join("\n") });
        fenced = null;
      }
      continue;
    }
    if (fenced !== null) {
      fenced.push(line);
      continue;
    }

    const trimmed = line.trim();
    if (trimmed === "") continue;

    const heading = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      blocks.push({
        kind: "heading",
        level: heading[1].length <= 2 ? 2 : 3,
        spans: parseSpans(heading[2]),
      });
      continue;
    }

    const bullet = trimmed.match(/^[*-]\s+(.*)$/);
    if (bullet) {
      blocks.push({ kind: "bullet", spans: parseSpans(bullet[1]) });
      continue;
    }

    blocks.push({ kind: "paragraph", spans: parseSpans(trimmed) });
  }

  // An unterminated fence still holds real content -- show it rather than
  // silently swallowing the rest of the release.
  if (fenced !== null && fenced.length > 0) {
    blocks.push({ kind: "code", text: fenced.join("\n") });
  }

  return blocks;
}

/**
 * Splits one line into text, inline `code`, and link runs. Handles both
 * `[label](href)` and the bare URLs the PR-title generator emits ("... by
 * @user in https://github.com/.../pull/833").
 */
export function parseSpans(line: string): Span[] {
  const spans: Span[] = [];
  // Alternation order matters: inline code wins over a URL so a URL inside
  // backticks stays literal text.
  const pattern = /`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s<>)]+)/g;
  let last = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(line)) !== null) {
    if (match.index > last) {
      spans.push({ kind: "text", text: line.slice(last, match.index) });
    }
    if (match[1] !== undefined) {
      spans.push({ kind: "code", text: match[1] });
    } else if (match[2] !== undefined && match[3] !== undefined) {
      spans.push({ kind: "link", text: match[2], href: match[3] });
    } else if (match[4] !== undefined) {
      spans.push({ kind: "link", text: shortenUrl(match[4]), href: match[4] });
    }
    last = match.index + match[0].length;
  }
  if (last < line.length) {
    spans.push({ kind: "text", text: line.slice(last) });
  }
  return spans;
}

/**
 * A full PR URL is most of a bullet's width and says the same thing four
 * times over across a release. Renders as `#833`, keeping the href intact.
 * Anything that is not a PR/issue URL keeps its full text.
 */
export function shortenUrl(url: string): string {
  const pr = url.match(/\/(?:pull|issues)\/(\d+)\/?$/);
  return pr ? `#${pr[1]}` : url;
}

/**
 * The release the dialog opens on. The backend resolves `selectedTag` from
 * the running stack's `BIOFLOW_TAG` and is authoritative; this only covers
 * the case it cannot answer -- a version whose GitHub release is not
 * published yet -- by falling back to the newest entry rather than opening
 * empty.
 */
export function initialRelease(
  releases: Release[],
  selectedTag: string | null,
): Release | null {
  if (releases.length === 0) return null;
  if (selectedTag) {
    const exact = releases.find((r) => r.tag === selectedTag);
    if (exact) return exact;
  }
  return releases[0];
}

/**
 * The dropdown's label for one release: its name, marked when it is the
 * version currently running so the user can tell "what I have" from "what I
 * could have" without cross-referencing Settings.
 */
export function releaseLabel(release: Release, selectedTag: string | null): string {
  const suffix = release.tag === selectedTag ? " (running)" : "";
  return `${release.name}${suffix}`;
}

/**
 * The ISO timestamp GitHub returns, as a plain readable date. Returns null
 * for a missing or unparseable value so the caller renders nothing rather
 * than "Invalid Date".
 */
export function formatPublished(publishedAt: string): string | null {
  if (!publishedAt) return null;
  const date = new Date(publishedAt);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}
