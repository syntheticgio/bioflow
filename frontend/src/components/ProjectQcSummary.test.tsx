import { describe, expect, it } from "vitest";
import { panelState } from "./ProjectQcSummary";
import type { MultiqcStatus } from "../api/types";

/** A status with nothing going on, overridden per case. */
function status(over: Partial<MultiqcStatus> = {}): MultiqcStatus {
  return {
    summarizable: 0,
    generated_at: null,
    covered: null,
    stale: false,
    running: false,
    running_since: null,
    failed: false,
    failed_at: null,
    ...over,
  };
}

describe("panelState", () => {
  it("shows nothing to summarize when no file has QC output", () => {
    expect(panelState(status())).toBe("unavailable");
  });

  it("needs two files, not one", () => {
    expect(panelState(status({ summarizable: 1 }))).toBe("unavailable");
    expect(panelState(status({ summarizable: 2 }))).toBe("none");
  });

  it("offers a report once two files qualify", () => {
    expect(panelState(status({ summarizable: 14 }))).toBe("none");
  });

  it("shows a finished report", () => {
    expect(panelState(status({ summarizable: 14, generated_at: 100 }))).toBe(
      "ready",
    );
  });

  it("flags a report older than its inputs", () => {
    expect(
      panelState(status({ summarizable: 14, generated_at: 100, stale: true })),
    ).toBe("stale");
  });

  it("keeps offering an existing report when a refresh fails", () => {
    // The combination case: a failure that still has something to open
    // reads differently from a failure with nothing, and collapsing the
    // two would hide a report the user can still use.
    expect(
      panelState(status({ generated_at: 100, failed: true, failed_at: 200 })),
    ).toBe("failed-with-report");
  });

  it("reports a failure with no report to fall back on", () => {
    expect(panelState(status({ failed: true, failed_at: 200 }))).toBe("failed");
  });

  it("lets a running job outrank every other state", () => {
    // A run in flight is the most current fact about the panel. Without
    // this precedence a stale report would keep saying "stale" while its
    // own replacement was already building.
    expect(
      panelState(
        status({
          running: true,
          generated_at: 100,
          stale: true,
          failed: true,
          summarizable: 14,
        }),
      ),
    ).toBe("building");
  });

  it("prefers a usable report over a stale-and-failed muddle", () => {
    // failed wins over stale when both are set: the failure is the newer
    // event, and it explains why the report did not move.
    expect(
      panelState(status({ generated_at: 100, stale: true, failed: true })),
    ).toBe("failed-with-report");
  });
});
