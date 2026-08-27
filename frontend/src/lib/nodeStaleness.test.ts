import { describe, expect, it } from "vitest";

import { storageStatus, updateAffordance } from "./nodeStaleness";

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

describe("storageStatus", () => {
  it("reports a probed, shared node as shared", () => {
    const result = storageStatus({ storageShared: true, storageLocation: "/mnt/x" });
    expect(result.kind).toBe("shared");
    expect(result.label).toBe("Shared");
  });

  it("reports a probed, unshared node as not shared", () => {
    const result = storageStatus({ storageShared: false, storageLocation: "/mnt/x" });
    expect(result.kind).toBe("not-shared");
    // Says what the node *can* still do, so it does not read as broken.
    expect(result.title).toContain("SRA downloads");
  });

  it("distinguishes never-probed from probed-and-negative", () => {
    const never = storageStatus({ storageShared: null, storageLocation: null });
    const negative = storageStatus({ storageShared: false, storageLocation: null });

    expect(never.kind).toBe("unknown");
    expect(negative.kind).toBe("not-shared");
    expect(never.label).not.toBe(negative.label);
  });

  it("says unchecked is an assumption, not a finding", () => {
    const result = storageStatus({ storageShared: null, storageLocation: null });
    expect(result.title).toContain("never been checked");
  });

  it("names the path it probed when there is one", () => {
    const result = storageStatus({ storageShared: false, storageLocation: "/srv/genomes" });
    expect(result.title).toContain("/srv/genomes");
  });

  it("omits the path cleanly when none was recorded", () => {
    const result = storageStatus({ storageShared: true, storageLocation: null });
    expect(result.title).not.toContain("()");
  });
});
