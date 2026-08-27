import { describe, expect, it } from "vitest";

import { withTargetNode } from "./client";

describe("withTargetNode", () => {
  it("leaves the path alone when no node is pinned", () => {
    // The common case by far: most launches do not target a node.
    expect(withTargetNode("/pipelines/qc")).toBe("/pipelines/qc");
    expect(withTargetNode("/pipelines/qc", undefined)).toBe("/pipelines/qc");
    expect(withTargetNode("/pipelines/qc", "")).toBe("/pipelines/qc");
  });

  it("appends the clause when one is", () => {
    expect(withTargetNode("/pipelines/qc", "gpu-1")).toBe(
      "/pipelines/qc?target_node=gpu-1",
    );
  });

  it("encodes the node name", () => {
    // The reason this is one function: twenty hand-written copies were twenty
    // chances to drop the encode.
    expect(withTargetNode("/pipelines/qc", "node one&x=1")).toBe(
      "/pipelines/qc?target_node=node%20one%26x%3D1",
    );
  });

  it("uses & when the path already carries a query", () => {
    // None of today's launch paths have one, but a caller that passes a
    // templated path with a query should not silently produce a second "?".
    expect(withTargetNode("/workflows/7/runs?dry=1", "gpu-1")).toBe(
      "/workflows/7/runs?dry=1&target_node=gpu-1",
    );
  });
});
