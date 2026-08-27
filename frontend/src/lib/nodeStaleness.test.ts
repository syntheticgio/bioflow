import { describe, expect, it } from "vitest";

import { nodeStatusBadge, storageStatus, updateAffordance } from "./nodeStaleness";

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

describe("updateAffordance on a revoked node", () => {
  // A revoked node cannot claim jobs, so updating it is work nobody wants.
  // It rendered identically to an active one, which left the button live (#913).
  it("never offers the update, however stale the node is", () => {
    const result = updateAffordance({
      ...base,
      imageDigest: "sha256:old",
      enrollment: "revoked",
    });
    expect(result.kind).toBe("unavailable");
    if (result.kind === "unavailable") {
      expect(result.reason).toMatch(/revoked/i);
    }
  });

  it("wins over 'current' too, so the reason is always visible", () => {
    // Without this the badge would say Revoked while the Actions cell showed
    // nothing at all, which reads as "no action available yet".
    expect(updateAffordance({ ...base, enrollment: "revoked" }).kind).toBe(
      "unavailable",
    );
  });

  it("leaves every other enrollment value alone", () => {
    for (const enrollment of ["active", "unknown", null, undefined]) {
      expect(updateAffordance({ ...base, enrollment }).kind).toBe("current");
    }
  });
});

describe("nodeStatusBadge", () => {
  const node = { enrollment: "active", online: true, workers: 2, nodeId: "n1" };

  it("shows Online and Offline for an ordinary node", () => {
    expect(nodeStatusBadge(node).label).toBe("Online");
    expect(nodeStatusBadge({ ...node, online: false }).label).toBe("Offline");
  });

  it("shows Revoked, beating Online", () => {
    // The case that made revocation invisible: a revoked node can still be
    // heartbeating, and enumerate_nodes lists it deliberately for that reason.
    const badge = nodeStatusBadge({ ...node, enrollment: "revoked" });
    expect(badge.label).toBe("Revoked");
    expect(badge.modifier).toBe("revoked");
    expect(badge.title).toMatch(/still running/i);
  });

  it("shows Revoked for an offline revoked node, without the running caveat", () => {
    const badge = nodeStatusBadge({
      ...node,
      enrollment: "revoked",
      online: false,
    });
    expect(badge.label).toBe("Revoked");
    expect(badge.title).not.toMatch(/still running/i);
  });

  it("shows Revoked for a node revoked outside the UI", () => {
    // Derived from the server's `enrollment`, not from a local action, so a
    // direct API call displays the same way. That is an acceptance criterion.
    expect(
      nodeStatusBadge({ ...node, enrollment: "revoked", workers: 0 }).label,
    ).toBe("Revoked");
  });

  it("keeps the Unknown tri-state it was built alongside", () => {
    const badge = nodeStatusBadge({
      ...node,
      enrollment: "unknown",
      workers: 0,
      online: false,
    });
    expect(badge.label).toBe("Unknown");
    expect(badge.title).toMatch(/never enrolled|has ever enrolled/i);
  });

  it("does not call a node Unknown while it has workers", () => {
    expect(
      nodeStatusBadge({ ...node, enrollment: "unknown", workers: 1 }).label,
    ).toBe("Online");
  });
});
