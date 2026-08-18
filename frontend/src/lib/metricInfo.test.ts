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
      expect(info.description.trimEnd(), `${key} description`).toMatch(/[.?!]$/);
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
