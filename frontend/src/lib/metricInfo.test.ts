import { describe, expect, it } from "vitest";
import { LABELS } from "../components/FactsTable";
import { METRIC_INFO, NO_INFO_NEEDED, infoFor } from "./metricInfo";

/**
 * The registry is hand-maintained and keyed by something another module
 * already enumerates, which is the shape CLAUDE.md warns about: a key with no
 * entry is silently skipped rather than raised, so a new fact arrives with no
 * explanation and nothing fails. These tests are the partition check that
 * makes that a failure instead.
 */
describe("metric info coverage", () => {
  it("explains every labelled fact, or records why it needs none", () => {
    const missing = Object.keys(LABELS).filter(
      (key) => !(key in METRIC_INFO) && !NO_INFO_NEEDED.has(key),
    );
    expect(missing).toEqual([]);
  });

  it("does not both explain a fact and declare it needs no explanation", () => {
    const both = Object.keys(METRIC_INFO).filter((key) =>
      NO_INFO_NEEDED.has(key),
    );
    expect(both).toEqual([]);
  });

  it("declares no exemption for a fact the table never labels", () => {
    // A stale exemption is how coverage quietly shrinks: the fact is renamed,
    // its old key keeps the exemption, and the new one is missing an entry
    // that the first test would otherwise have caught.
    const stale = [...NO_INFO_NEEDED].filter((key) => !(key in LABELS));
    expect(stale).toEqual([]);
  });
});

describe("metric info content", () => {
  it("gives every entry a term and a description", () => {
    for (const [key, info] of Object.entries(METRIC_INFO)) {
      expect(info.term, `${key} term`).toBeTruthy();
      expect(info.description, `${key} description`).toBeTruthy();
    }
  });

  it("writes descriptions as sentences, not label restatements", () => {
    // A description no longer than its own term is a placeholder that passes
    // the truthiness check above while explaining nothing.
    for (const [key, info] of Object.entries(METRIC_INFO)) {
      expect(
        info.description.length,
        `${key} description is too short to explain anything`,
      ).toBeGreaterThan(40);
      expect(info.description.trimEnd(), `${key} description`).toMatch(
        /[.?!]$/,
      );
    }
  });

  it("points learnMore at a real help route", () => {
    for (const [key, info] of Object.entries(METRIC_INFO)) {
      if (!info.learnMore) continue;
      expect(info.learnMore, `${key} learnMore`).toMatch(/^\/help\//);
    }
  });

  it("returns undefined for a key it has no entry for", () => {
    // InfoMarker renders nothing on undefined -- a marker promising an
    // explanation and opening an empty card is worse than no marker.
    expect(infoFor("no_such_fact")).toBeUndefined();
  });
});

/**
 * The Results tab's headline numbers are written as `<Stat label=… metric=…>`
 * in component source rather than driven off a key map, so `LABELS` above
 * cannot see them. They are also the figures most likely to be copied into a
 * methods section -- Ti/Tv, mean depth, the breadth percentages -- and
 * several do not mean what their label alone suggests.
 *
 * Read from source for the same reason the LABELS partition exists: a `Stat`
 * added without a `metric` renders perfectly, just with no explanation, and
 * nothing else in the suite would notice. Scanning the tree catches one added
 * to a component this file has never heard of, which an enumerated list of
 * call sites would not.
 */
describe("Stat headline coverage", () => {
  // Read through Vite rather than node:fs: this project has no @types/node,
  // and `tsc --noEmit` runs over src/ in CI -- a readFileSync here compiles
  // nowhere even though vitest would happily run it. `query: "?raw"` hands
  // back each component's source text as a string, eagerly, at transform time.
  const sources = Object.entries(
    import.meta.glob("../components/*.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  )
    .map(([path, text]) => ({ name: path.split("/").pop() as string, text }))
    .filter(({ name }) => name !== "Stat.tsx");

  // Each `<Stat …>` opening tag, whether written on one line or across several.
  const statTags = (text: string) => text.match(/<Stat\b[^>]*/g) ?? [];

  it("gives every headline stat a metric key", () => {
    const missing: string[] = [];
    for (const { name, text } of sources) {
      for (const tag of statTags(text)) {
        if (/\bmetric=/.test(tag)) continue;
        const label = tag.match(/label="([^"]*)"/)?.[1] ?? tag;
        missing.push(`${name}: ${label}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it("points every stat's metric key at a real entry", () => {
    const unknown: string[] = [];
    for (const { name, text } of sources) {
      for (const tag of statTags(text)) {
        const key = tag.match(/metric="([^"]*)"/)?.[1];
        if (key && !(key in METRIC_INFO)) unknown.push(`${name}: ${key}`);
      }
    }
    expect(unknown).toEqual([]);
  });

  it("finds the stats it is meant to be checking", () => {
    // Without this the two tests above pass vacuously if the scan breaks --
    // a renamed directory or a changed tag shape would read as "all clear".
    const total = sources.reduce((n, s) => n + statTags(s.text).length, 0);
    expect(total).toBeGreaterThan(5);
  });
});
