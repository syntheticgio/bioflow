import { describe, it, expect } from "vitest";
import { mergeLedgerLines } from "./runFormat";
import type { RunSummary, WorkflowRunRow } from "../api/types";

/**
 * The ledger column carries two kinds of row since #93 folded workflows into
 * it. Interleaving is the whole point -- appending workflows would be a
 * one-line change that looks right on a screenshot where every workflow
 * happens to be the newest thing, and wrong the moment one is not.
 */

const run = (id: string, at: string) =>
  ({ id, updated_at: at }) as unknown as RunSummary;

const workflow = (id: string, at: string) =>
  ({ id, updated_at: at }) as unknown as WorkflowRunRow;

describe("mergeLedgerLines", () => {
  it("orders both kinds by recency, newest first", () => {
    const lines = mergeLedgerLines(
      [run("r-old", "2026-08-01T10:00:00Z"), run("r-new", "2026-08-03T10:00:00Z")],
      [workflow("w-mid", "2026-08-02T10:00:00Z")],
    );

    expect(lines.map((l) => l.run.id)).toEqual(["r-new", "w-mid", "r-old"]);
  });

  it("places a workflow between runs rather than at either end", () => {
    // The case a concatenation gets wrong: the workflow is neither the newest
    // nor the oldest, so appending or prepending both misorder it.
    const lines = mergeLedgerLines(
      [
        run("r1", "2026-08-05T00:00:00Z"),
        run("r2", "2026-08-03T00:00:00Z"),
        run("r3", "2026-08-01T00:00:00Z"),
      ],
      [workflow("w", "2026-08-04T00:00:00Z")],
    );

    expect(lines.map((l) => l.run.id)).toEqual(["r1", "w", "r2", "r3"]);
  });

  it("tags each line with its kind so the column can pick a component", () => {
    const lines = mergeLedgerLines(
      [run("r", "2026-08-02T00:00:00Z")],
      [workflow("w", "2026-08-01T00:00:00Z")],
    );

    expect(lines.map((l) => l.kind)).toEqual(["run", "workflow"]);
  });

  it("handles either side being empty", () => {
    expect(mergeLedgerLines([], [])).toEqual([]);
    expect(
      mergeLedgerLines([run("r", "2026-08-01T00:00:00Z")], []),
    ).toHaveLength(1);
    expect(
      mergeLedgerLines([], [workflow("w", "2026-08-01T00:00:00Z")]),
    ).toHaveLength(1);
  });

  it("compares timestamps chronologically across a month boundary", () => {
    // Guards the lexicographic shortcut: it is only equivalent to a date
    // compare because the API returns zero-padded ISO-8601 UTC. "2026-09-01"
    // sorting after "2026-08-31" is the property being relied on.
    const lines = mergeLedgerLines(
      [run("aug", "2026-08-31T23:59:59Z")],
      [workflow("sep", "2026-09-01T00:00:01Z")],
    );

    expect(lines.map((l) => l.run.id)).toEqual(["sep", "aug"]);
  });
});
