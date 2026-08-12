import { describe, expect, it } from "vitest";

import { updateAffordance } from "./nodeStaleness";

const base = { imageDigest: "sha256:a", updatable: true, primaryDigest: "sha256:a" };

describe("updateAffordance", () => {
  it("hides the control when the node matches the primary", () => {
    expect(updateAffordance(base).kind).toBe("current");
  });

  it("offers an update when the digests differ", () => {
    expect(
      updateAffordance({ ...base, imageDigest: "sha256:old" }).kind,
    ).toBe("available");
  });

  it("offers an update when the node reports no version but has a key", () => {
    // A node whose worker is down reports nothing; it is exactly the node
    // most in need of the button.
    expect(
      updateAffordance({ ...base, imageDigest: null }).kind,
    ).toBe("available");
  });

  // NU-30: disabled and self-explaining, never a button that cannot work.
  it("disables the control, with a reason, on a node with no stored key", () => {
    const result = updateAffordance({
      ...base,
      imageDigest: "sha256:old",
      updatable: false,
    });
    expect(result.kind).toBe("unavailable");
    if (result.kind === "unavailable") {
      expect(result.reason).toMatch(/provision/i);
    }
  });

  it("does not claim staleness when the primary's digest is unknown", () => {
    expect(
      updateAffordance({ ...base, imageDigest: "sha256:old", primaryDigest: null }).kind,
    ).toBe("current");
  });
});
