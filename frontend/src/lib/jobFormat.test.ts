import { describe, it, expect } from "vitest";
import { RUNNING, WAITING, BLOCKED, waitingReason, jobLabel } from "./runFormat";
import type { JobSummary, SystemLoad } from "../api/types";

/**
 * These moved out of ActivityView.tsx so the mobile feed could reuse them
 * rather than growing a second copy. jobLabel is the one that most needed
 * pinning: it reads untyped payload keys, so a divergent copy would fail
 * silently the day a handler renames one.
 */

const job = (over: Partial<JobSummary> = {}) =>
  ({
    id: "j1",
    type: "align_reads",
    job_class: "compute",
    state: "queued",
    payload: {},
    attempts: 1,
    max_attempts: 3,
    cancel_requested: false,
    ...over,
  }) as unknown as JobSummary;

const load = (over: Partial<SystemLoad> = {}) =>
  ({
    state: "OPEN",
    admitted_classes: ["compute", "user_interactive"],
    ...over,
  }) as unknown as SystemLoad;

describe("state sets", () => {
  it("treats blocked as in-flight, not as finished", () => {
    // The desktop view derives "recent" by negation, which is safe there
    // because a blocked job is shown inside its run's card. A flat feed has
    // no such grouping, so blocked must be positively claimed or it lands
    // under "Recent" looking like it succeeded.
    expect(BLOCKED.has("blocked")).toBe(true);
    expect(RUNNING.has("blocked")).toBe(false);
    expect(WAITING.has("blocked")).toBe(false);
  });

  it("covers every non-terminal state between the three sets", () => {
    const inFlight = ["running", "pending", "queued", "delayed", "blocked"];
    for (const s of inFlight) {
      const claimed =
        RUNNING.has(s) || WAITING.has(s) || BLOCKED.has(s);
      expect(claimed, `${s} is claimed by no set`).toBe(true);
    }
  });
});

describe("waitingReason", () => {
  it("reports cancelling ahead of everything else", () => {
    expect(waitingReason(job({ cancel_requested: true }), load())).toBe(
      "cancelling",
    );
  });

  it("names a retry rather than calling it a queue wait", () => {
    expect(waitingReason(job({ state: "delayed" }), load())).toBe(
      "retrying after a failure",
    );
  });

  it("degrades to a bare wait when the governor is unknown", () => {
    expect(waitingReason(job(), undefined)).toBe("waiting");
  });

  it("blames the governor when this job's class is not admitted", () => {
    expect(
      waitingReason(job(), load({ state: "CLOSED", admitted_classes: [] })),
    ).toBe("waiting: system loaded");
    expect(
      waitingReason(job(), load({ state: "THROTTLED", admitted_classes: [] })),
    ).toBe("waiting: system busy");
  });

  it("says it is only queueing when the class is admitted", () => {
    expect(waitingReason(job(), load())).toBe("waiting for a free slot");
  });

  it("explains a blocked job by its dependency, not by load", () => {
    // A blocked job is not waiting on the machine -- it is waiting on
    // another job. Saying "system busy" here would be a lie that sends
    // someone looking at their CPU.
    expect(waitingReason(job({ state: "blocked" }), load())).toBe(
      "waiting on an earlier step",
    );
  });
});

describe("jobLabel", () => {
  it("prefers the paired read names", () => {
    expect(
      jobLabel(job({ payload: { r1_name: "a.fastq", r2_name: "b.fastq" } })),
    ).toBe("a.fastq + b.fastq");
  });

  it("uses a single read name when there is no mate", () => {
    expect(jobLabel(job({ payload: { r1_name: "a.fastq" } }))).toBe("a.fastq");
  });

  it("falls back to the generic name key", () => {
    expect(jobLabel(job({ payload: { name: "ref.fna" } }))).toBe("ref.fna");
  });

  it("falls back to the job type when the payload names nothing", () => {
    expect(jobLabel(job({ type: "run_qc", payload: {} }))).toBe("run_qc");
  });

  it("ignores a non-string name rather than rendering it", () => {
    expect(jobLabel(job({ type: "run_qc", payload: { name: 42 } }))).toBe(
      "run_qc",
    );
  });
});
