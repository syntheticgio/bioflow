import { describe, expect, it } from "vitest";
import {
  assertDeletionPreview,
  assertEach,
  assertRunSummary,
  assertWorkflowRunSummary,
} from "./validators";

describe("assertDeletionPreview", () => {
  const wellFormed = {
    project_ids: ["p1"],
    child_project_count: 0,
    object_count: 3,
    total_bytes: 1024,
    run_count: 1,
    job_count: 0,
    upload_session_count: 0,
    active_jobs: [{ id: "j1", job_type: "align", state: "running" }],
    blocked: true,
  };

  it("accepts a well-formed response", () => {
    expect(() => assertDeletionPreview(wellFormed)).not.toThrow();
  });

  it("rejects a missing top-level field", () => {
    const { blocked: _blocked, ...malformed } = wellFormed;
    expect(() => assertDeletionPreview(malformed)).toThrow(/blocked/);
  });

  it("rejects a malformed active_jobs entry", () => {
    const malformed = { ...wellFormed, active_jobs: [{ id: "j1", job_type: "align" }] };
    expect(() => assertDeletionPreview(malformed)).toThrow(/state/);
  });

  it("rejects a non-object response", () => {
    expect(() => assertDeletionPreview(null)).toThrow();
    expect(() => assertDeletionPreview("nope")).toThrow();
  });
});

describe("assertWorkflowRunSummary", () => {
  const wellFormed = {
    id: "r1",
    definition_id: "d1",
    definition_version: 3,
    label: "My workflow",
    status: "running",
  };

  it("accepts a well-formed response", () => {
    expect(() => assertWorkflowRunSummary(wellFormed)).not.toThrow();
  });

  it("rejects a wrong-typed field", () => {
    const malformed = { ...wellFormed, label: undefined };
    expect(() => assertWorkflowRunSummary(malformed)).toThrow(/label/);
  });
});

describe("assertRunSummary", () => {
  const wellFormed = {
    id: "r1",
    kind: "alignment",
    project_id: "p1",
    label: "My run",
    status: "succeeded",
    inputs: [{ object_id: "o1", name: "reads.fastq", role: "reads" }],
    params: {},
    tool: "bwa-mem2",
    outputs: ["o2"],
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:05:00Z",
  };

  it("accepts a well-formed response", () => {
    expect(() => assertRunSummary(wellFormed)).not.toThrow();
  });

  it("accepts a null tool", () => {
    expect(() => assertRunSummary({ ...wellFormed, tool: null })).not.toThrow();
  });

  it("rejects a malformed input entry", () => {
    const malformed = { ...wellFormed, inputs: [{ object_id: "o1", role: "reads" }] };
    expect(() => assertRunSummary(malformed)).toThrow(/name/);
  });

  it("rejects a non-array inputs field", () => {
    const malformed = { ...wellFormed, inputs: undefined };
    expect(() => assertRunSummary(malformed)).toThrow(/inputs/);
  });
});

describe("assertEach", () => {
  it("validates every item in a list", () => {
    const list = [
      {
        id: "r1",
        kind: "alignment",
        project_id: "p1",
        label: "Run 1",
        status: "succeeded",
        inputs: [],
        params: {},
        tool: null,
        outputs: [],
        created_at: "2026-08-01T00:00:00Z",
        updated_at: "2026-08-01T00:05:00Z",
      },
    ];
    expect(() => assertEach(assertRunSummary, list)).not.toThrow();
  });

  it("throws naming the offending index", () => {
    const list = [{ id: "r1" }];
    expect(() => assertEach(assertRunSummary, list)).toThrow(/kind/);
  });
});
