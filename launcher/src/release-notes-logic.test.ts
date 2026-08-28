import { describe, expect, it } from "vitest";
import {
  formatPublished,
  initialRelease,
  parseReleaseBody,
  parseSpans,
  releaseLabel,
  shortenUrl,
} from "./release-notes-logic";
import type { Release } from "./types";

/** A verbatim excerpt of the real v0.6.0-beta body -- the shape
 *  `.github/release.yml` actually generates, not an invented sample. */
const REAL_BODY = `Container images published for this release:

\`\`\`
ghcr.io/syntheticgio/bioflow-backend:0.6.0-beta
ghcr.io/syntheticgio/bioflow-web:0.6.0-beta
\`\`\`

Both serve linux/amd64 and linux/arm64. \`:latest\` now points here.

<!-- Release notes generated using configuration in .github/release.yml at v0.6.0-beta -->

## What's Changed
### New features
* feat(ui): draw the agent panel's header controls, not emoji by @syntheticgio in https://github.com/syntheticgio/bioflow/pull/833
### Bug fixes
* fix(launcher): report the version it actually is, on every screen by @syntheticgio in https://github.com/syntheticgio/bioflow/pull/816
`;

function release(tag: string, over: Partial<Release> = {}): Release {
  return {
    tag,
    name: `BioFlow ${tag.replace(/^v/, "")}`,
    publishedAt: "2026-08-26T14:42:58Z",
    body: "notes",
    prerelease: tag.includes("-"),
    ...over,
  };
}

describe("parseReleaseBody", () => {
  it("parses a real generated release body into the expected blocks", () => {
    const blocks = parseReleaseBody(REAL_BODY);
    const kinds = blocks.map((b) => b.kind);
    expect(kinds).toEqual([
      "paragraph", // Container images published...
      "code", // the ghcr.io fence
      "paragraph", // Both serve linux/amd64...
      "heading", // ## What's Changed
      "heading", // ### New features
      "bullet", // the feat line
      "heading", // ### Bug fixes
      "bullet", // the fix line
    ]);
  });

  it("strips the generator's provenance comment", () => {
    const text = JSON.stringify(parseReleaseBody(REAL_BODY));
    expect(text).not.toContain("Release notes generated using configuration");
  });

  it("keeps a fenced block verbatim, including both its lines", () => {
    const code = parseReleaseBody(REAL_BODY).find((b) => b.kind === "code");
    expect(code).toEqual({
      kind: "code",
      text: "ghcr.io/syntheticgio/bioflow-backend:0.6.0-beta\nghcr.io/syntheticgio/bioflow-web:0.6.0-beta",
    });
  });

  it("distinguishes ## from ### so the two nest visually", () => {
    const headings = parseReleaseBody(REAL_BODY).filter((b) => b.kind === "heading");
    expect(headings.map((h) => (h.kind === "heading" ? h.level : null))).toEqual([2, 3, 3]);
  });

  it("accepts - bullets as well as *", () => {
    const blocks = parseReleaseBody("- a dash bullet");
    expect(blocks[0].kind).toBe("bullet");
  });

  it("clamps a deeper heading rather than dropping it", () => {
    const blocks = parseReleaseBody("#### deep");
    expect(blocks[0]).toEqual({
      kind: "heading",
      level: 3,
      spans: [{ kind: "text", text: "deep" }],
    });
  });

  it("shows the content of an unterminated fence rather than swallowing it", () => {
    // A hand-edited body can leave a fence open; dropping the remainder
    // would silently hide the rest of the release's notes.
    const blocks = parseReleaseBody("intro\n```\nstranded content");
    expect(blocks).toContainEqual({ kind: "code", text: "stranded content" });
  });

  it("treats a body with no notes as nothing to render, not a crash", () => {
    expect(parseReleaseBody("")).toEqual([]);
  });
});

describe("parseSpans", () => {
  it("shortens the trailing PR URL the generator emits", () => {
    const spans = parseSpans(
      "* feat(ui): a thing by @syntheticgio in https://github.com/syntheticgio/bioflow/pull/833",
    );
    expect(spans).toContainEqual({
      kind: "link",
      text: "#833",
      href: "https://github.com/syntheticgio/bioflow/pull/833",
    });
  });

  it("renders inline backticks as code", () => {
    const spans = parseSpans("Both serve arm64. `:latest` now points here.");
    expect(spans).toContainEqual({ kind: "code", text: ":latest" });
  });

  it("keeps a URL inside backticks literal instead of linking it", () => {
    // Alternation order in the pattern decides this; a linked URL inside a
    // code span would render a clickable fragment of a literal string.
    const spans = parseSpans("run `curl https://example.com/x` first");
    expect(spans).toContainEqual({ kind: "code", text: "curl https://example.com/x" });
    expect(spans.some((s) => s.kind === "link")).toBe(false);
  });

  it("handles a markdown link with its own label", () => {
    const spans = parseSpans("see [the docs](https://example.com/docs) for more");
    expect(spans).toContainEqual({
      kind: "link",
      text: "the docs",
      href: "https://example.com/docs",
    });
  });

  it("does not swallow text after the last match", () => {
    const spans = parseSpans("`code` and then trailing words");
    expect(spans[spans.length - 1]).toEqual({ kind: "text", text: " and then trailing words" });
  });

  it("passes a plain line through as one text span", () => {
    expect(parseSpans("nothing special here")).toEqual([
      { kind: "text", text: "nothing special here" },
    ]);
  });
});

describe("shortenUrl", () => {
  it("shortens pull and issue URLs", () => {
    expect(shortenUrl("https://github.com/syntheticgio/bioflow/pull/833")).toBe("#833");
    expect(shortenUrl("https://github.com/syntheticgio/bioflow/issues/842")).toBe("#842");
  });

  it("leaves an unrelated URL intact", () => {
    expect(shortenUrl("https://example.com/page")).toBe("https://example.com/page");
  });
});

describe("initialRelease", () => {
  const releases = [release("v0.6.0-beta"), release("v0.6.0-alpha"), release("v0.5.1")];

  it("opens on the running version when the backend resolved one", () => {
    expect(initialRelease(releases, "v0.5.1")?.tag).toBe("v0.5.1");
  });

  it("falls back to the newest when the running version has no release yet", () => {
    // A stage image can reach GHCR before its GitHub release is published.
    expect(initialRelease(releases, "v0.9.0-alpha")?.tag).toBe("v0.6.0-beta");
  });

  it("falls back to the newest when the backend resolved nothing", () => {
    expect(initialRelease(releases, null)?.tag).toBe("v0.6.0-beta");
  });

  it("has nothing to open when nothing is published", () => {
    expect(initialRelease([], "v0.5.1")).toBeNull();
  });
});

describe("releaseLabel", () => {
  it("marks the running version so it is distinguishable in the dropdown", () => {
    expect(releaseLabel(release("v0.5.1"), "v0.5.1")).toBe("BioFlow 0.5.1 (running)");
  });

  it("leaves other versions unmarked", () => {
    expect(releaseLabel(release("v0.4.0"), "v0.5.1")).toBe("BioFlow 0.4.0");
  });
});

describe("formatPublished", () => {
  it("renders a readable date from the API's timestamp", () => {
    expect(formatPublished("2026-08-26T14:42:58Z")).toMatch(/2026/);
  });

  it("renders nothing rather than 'Invalid Date'", () => {
    expect(formatPublished("")).toBeNull();
    expect(formatPublished("not a date")).toBeNull();
  });
});
