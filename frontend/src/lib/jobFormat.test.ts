import { describe, it, expect } from "vitest";
import {
  RUNNING,
  WAITING,
  BLOCKED,
  waitingReason,
  jobLabel,
  formatBlockedReason,
  isUnsatisfiable,
  maintenanceLabel,
  isMaintenance,
} from "./runFormat";
import type {
  BlockedReason,
  JobSummary,
  JobTypeInfo,
  SystemLoad,
} from "../api/types";

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
  it("still explains a blocked job when a reason or load is also passed", () => {
    // Regression test: waitingReason's `blocked` branch used to be dead code
    // because both call sites only invoked it when job.state was in WAITING
    // (which does not include "blocked"). A blocked job must reach this
    // function and say "waiting on an earlier step" regardless of what else
    // is passed in -- not "waiting on memory" or anything load-derived.
    const busyLoad = load({ state: "CLOSED", admitted_classes: [] });
    const memReason: BlockedReason = {
      gate: "mem",
      need: 32768,
      free: 8192,
      class: null,
      admitted: null,
    };
    expect(waitingReason(job({ state: "blocked" }), busyLoad, memReason)).toBe(
      "waiting on an earlier step",
    );
    expect(waitingReason(job({ state: "blocked" }), undefined, null)).toBe(
      "waiting on an earlier step",
    );
  });

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

  it("prefers a fresh recorded reason over inference from load", () => {
    // The recorded reason is what the queue actually decided; load-derived
    // inference is only a fallback for when it is missing or expired. Give
    // both, disagreeing, and confirm the recorded one wins.
    const cpuReason: BlockedReason = {
      gate: "cpu",
      need: 8,
      free: 2,
      class: null,
      admitted: null,
    };
    const closedLoad = load({ state: "CLOSED", admitted_classes: [] });
    expect(waitingReason(job(), closedLoad, cpuReason)).toBe(
      "waiting on CPU — needs 8, 2 free",
    );
  });
});

describe("formatBlockedReason", () => {
  it("formats the class gate", () => {
    expect(
      formatBlockedReason({
        gate: "class",
        need: null,
        free: null,
        class: "bulk",
        admitted: ["user_interactive"],
      }),
    ).toBe("waiting: system loaded");
  });

  it("formats the cpu gate with need and free", () => {
    expect(
      formatBlockedReason({
        gate: "cpu",
        need: 16,
        free: 4,
        class: null,
        admitted: null,
      }),
    ).toBe("waiting on CPU — needs 16, 4 free");
  });

  it("formats the mem gate in MB below the GB boundary", () => {
    expect(
      formatBlockedReason({
        gate: "mem",
        need: 512,
        free: 256,
        class: null,
        admitted: null,
      }),
    ).toBe("waiting on memory — needs 512 MB, 256 MB free");
  });

  it("formats the mem gate in GB at and above the 1024 MB boundary", () => {
    expect(
      formatBlockedReason({
        gate: "mem",
        need: 1024,
        free: 8192,
        class: null,
        admitted: null,
      }),
    ).toBe("waiting on memory — needs 1.0 GB, 8.0 GB free");
  });

  it("falls back to a bare memory message when need/free are unknown", () => {
    expect(
      formatBlockedReason({
        gate: "mem",
        need: null,
        free: null,
        class: null,
        admitted: null,
      }),
    ).toBe("waiting on memory");
  });

  it("formats the io gate", () => {
    expect(
      formatBlockedReason({
        gate: "io",
        need: null,
        free: null,
        class: null,
        admitted: null,
      }),
    ).toBe("waiting on disk — another heavy job is reading");
  });
});

describe("isUnsatisfiable", () => {
  it("is true when declared mem_mb comfortably exceeds the budget", () => {
    const l = load({ memory: { percent: 50, available_bytes: 0, budget_bytes: 1024 * 1024 * 1024 } });
    expect(isUnsatisfiable({ mem_mb: 32768 }, l)).toBe(true);
  });

  it("is false when declared mem_mb is well under the budget", () => {
    const l = load({ memory: { percent: 50, available_bytes: 0, budget_bytes: 64 * 1024 * 1024 * 1024 } });
    expect(isUnsatisfiable({ mem_mb: 4096 }, l)).toBe(false);
  });

  it("is false when load is undefined, without throwing", () => {
    expect(isUnsatisfiable({ mem_mb: 32768 }, undefined)).toBe(false);
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

/**
 * Maintenance work, told apart from the biological work and given a name a
 * person can read.
 *
 * #556: pressing "Clean up storage now" toasts "Progress is in Activity", and
 * the job really is in the list -- but it renders as the raw token `gc_blobs`
 * in "Other recent", under a wall of `verify_files` rows that the same sweep
 * posts once a minute. The user reasonably reads that as "it isn't shown".
 *
 * The split is keyed on the job *type* rather than on `job_class`, because
 * `scheduler.run_now` deliberately re-classes a hand-fired sweep as
 * `user_interactive` -- someone is watching it. Keying on the runtime class
 * would put the one sweep the user actually asked for in the other section.
 */
describe("maintenanceLabel", () => {
  it("names a storage sweep in words rather than its type token", () => {
    expect(maintenanceLabel("gc_blobs")).toBe("Storage cleanup");
  });

  it("names the file verification sweep", () => {
    expect(maintenanceLabel("verify_files")).toBe("File verification");
  });

  it("returns undefined for work that is not maintenance", () => {
    expect(maintenanceLabel("align_reads")).toBeUndefined();
  });
});

describe("isMaintenance", () => {
  const types: Record<string, JobTypeInfo> = {
    gc_blobs: { default_class: "maintenance" },
    verify_files: { default_class: "maintenance" },
    align_reads: { default_class: "compute" },
  };

  it("treats a sweep as maintenance even when fired by hand", () => {
    // run_now enqueues at user_interactive; the *type* is still maintenance.
    expect(
      isMaintenance(job({ type: "gc_blobs", job_class: "user_interactive" }), types),
    ).toBe(true);
  });

  it("leaves pipeline work out of maintenance", () => {
    expect(isMaintenance(job({ type: "align_reads" }), types)).toBe(false);
  });

  it("falls back to the built-in list when the type map has not loaded", () => {
    // The map is fetched; a cold cache must not reclassify every sweep as
    // biological work and flood the main list again.
    expect(isMaintenance(job({ type: "gc_blobs" }), undefined)).toBe(true);
  });

  it("does not guess about an unknown type with no map", () => {
    expect(isMaintenance(job({ type: "brand_new_thing" }), undefined)).toBe(false);
  });
});

describe("jobLabel on maintenance jobs", () => {
  it("uses the readable maintenance name instead of the type token", () => {
    expect(jobLabel(job({ type: "gc_blobs", payload: {} }))).toBe(
      "Storage cleanup",
    );
  });

  it("still prefers a payload file name when there is one", () => {
    expect(
      jobLabel(job({ type: "verify_blob", payload: { name: "reads.fastq" } })),
    ).toBe("reads.fastq");
  });
});
