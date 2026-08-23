import { describe, expect, it } from "vitest";
import type { DeletionPreview } from "../api/types";
import { describeContents } from "./deletionSummary";

function preview(over: Partial<DeletionPreview> = {}): DeletionPreview {
  return {
    project_ids: [],
    child_project_count: 0,
    object_count: 0,
    total_bytes: 0,
    run_count: 0,
    job_count: 0,
    upload_session_count: 0,
    active_jobs: [],
    blocked: false,
    ...over,
  };
}

describe("describeContents", () => {
  it("returns an empty string for an empty project, so the caller can fall back", () => {
    expect(describeContents(preview())).toBe("");
  });

  it("drops zero-valued clauses rather than saying '0 pipeline runs'", () => {
    expect(describeContents(preview({ object_count: 3, total_bytes: 1024 }))).toBe(
      "3 files (1.0 KB)",
    );
  });

  it("singularizes each count independently", () => {
    expect(
      describeContents(
        preview({ child_project_count: 1, object_count: 1, run_count: 1 }),
      ),
    ).toBe("1 sub-project, 1 file (0 B), and 1 pipeline run");
  });

  it("joins three clauses with a serial 'and'", () => {
    expect(
      describeContents(
        preview({
          child_project_count: 3,
          object_count: 47,
          total_bytes: 2_100_000_000,
          run_count: 12,
        }),
      ),
    ).toMatch(/^3 sub-projects, 47 files \(.+\), and 12 pipeline runs$/);
  });
});
