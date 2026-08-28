// Pure logic for the Update control in Settings → Nodes, split out the way
// launcher/src/update-logic.ts is: this repo has no jsdom or testing-library
// setup and no .test.tsx files, so a pure module is the only testable seam.

export type UpdateAffordance =
  /** Node matches the primary, or there is nothing to compare against. */
  | { kind: "current" }
  /** Stale, or reporting nothing while updatable. Offer the button. */
  | { kind: "available" }
  /** Visible, disabled, self-explaining. */
  | { kind: "unavailable"; reason: string };

export interface StalenessInputs {
  /** The digest this node last reported; null if it never reported one. */
  imageDigest: string | null;
  /** Whether the primary holds an SSH key for this node. */
  updatable: boolean;
  /** The digest the primary is running; null if it cannot read its own. */
  primaryDigest: string | null;
  /**
   * The node's enrollment status. Optional so existing callers and tests are
   * unaffected; only "revoked" changes the outcome.
   */
  enrollment?: string | null;
}

export function updateAffordance({
  imageDigest,
  updatable,
  primaryDigest,
  enrollment,
}: StalenessInputs): UpdateAffordance {
  // Checked before staleness: a revoked node cannot claim jobs, so updating it
  // is work nobody wants. Rendering it identically to an active node is what
  // left the Update button live on one (#913).
  if (enrollment === "revoked") {
    return {
      kind: "unavailable",
      reason: "This node is revoked and cannot claim jobs.",
    };
  }

  // A node reporting no version is either offline or has no Docker socket.
  // Either way it is a candidate for an update, not a node known to be
  // current -- and a down worker is the case the button matters most for.
  const stale = imageDigest === null || (primaryDigest !== null && imageDigest !== primaryDigest);

  if (!stale) return { kind: "current" };

  if (!updatable) {
    return {
      kind: "unavailable",
      reason: "Not provisioned from BioFlow — no stored key to reach this node.",
    };
  }
  return { kind: "available" };
}

/** The version string to show in the table. */
export function versionLabel(version: string | null): string {
  return version ?? "Unknown";
}

export type StorageStatus =
  /** Probed, and the node reads the primary's storage. */
  | { kind: "shared"; label: string; title: string }
  /** Probed, and it does not. It is excluded from work that reads those files. */
  | { kind: "not-shared"; label: string; title: string }
  /** Never probed. Distinct from "not shared": nobody has asked yet. */
  | { kind: "unknown"; label: string; title: string };

export interface StorageInputs {
  /** Tri-state: null means never probed, not "no". */
  storageShared: boolean | null;
  /** The path probed, as the node sees it. */
  storageLocation: string | null;
}

/** What the Storage column says, and why.
 *
 * The tri-state survives all the way to the UI on purpose. "Never probed" and
 * "probed, cannot see it" call for different actions -- the first is a check
 * to run, the second is storage to fix -- and collapsing them into one
 * negative badge would hide which one a user is looking at.
 */
export function storageStatus({
  storageShared,
  storageLocation,
}: StorageInputs): StorageStatus {
  const where = storageLocation ? ` (${storageLocation})` : "";

  if (storageShared === true) {
    return {
      kind: "shared",
      label: "Shared",
      title: `This node reads the primary's storage${where}, proven by a round-trip check. It can run any job.`,
    };
  }
  if (storageShared === false) {
    return {
      kind: "not-shared",
      label: "Not shared",
      title: `This node cannot read the primary's storage${where}. It can still run jobs that fetch their own inputs, such as SRA downloads, but not work that reads the primary's files.`,
    };
  }
  return {
    kind: "unknown",
    label: "Unchecked",
    title:
      "Whether this node reads the primary's storage has never been checked. Until it is, BioFlow withholds work that reads the primary's files — the safe assumption, not a finding.",
  };
}

/** How one node's sweep result is presented, and whether it wants an action.
 *
 * `needsPath` is what makes the second sweep possible: the primary never
 * recorded where a pre-existing node's storage lives, and it cannot be
 * guessed -- probing the wrong directory would answer "not shared"
 * confidently and wrongly. So that outcome renders an input rather than a
 * verdict.
 *
 * `unreachable` and `not_probeable` are both "cannot check" and neither is a
 * finding, but they are kept apart because the remedies differ: an offline
 * machine needs powering on, a self-enrolled one can never be probed this way
 * and needs provisioning.
 */
export interface SweepOutcomeDisplay {
  /** Appended to "sweep-outcome". */
  kind: string;
  label: string;
  /** True when this row should offer a storage-path input. */
  needsPath: boolean;
}

export function sweepOutcome(outcome: string): SweepOutcomeDisplay {
  switch (outcome) {
    case "shared":
      return { kind: "shared", label: "Shared", needsPath: false };
    case "not_shared":
      return { kind: "not-shared", label: "Not shared", needsPath: false };
    case "no_recorded_path":
      return { kind: "needs-path", label: "Needs a path", needsPath: true };
    case "not_probeable":
      return { kind: "unknown", label: "Cannot check", needsPath: false };
    case "unreachable":
      return { kind: "unknown", label: "Cannot check", needsPath: false };
    default:
      // An outcome this UI does not know about is still not a finding, and
      // must never render as one.
      return { kind: "unknown", label: "Cannot check", needsPath: false };
  }
}

/** The Status cell's badge: its text, its modifier class, and its tooltip. */
export interface NodeStatusBadge {
  label: string;
  /** Appended to "nodes-status"; empty for the plain (offline) look. */
  modifier: string;
  title?: string;
}

export function nodeStatusBadge({
  enrollment,
  online,
  workers,
  nodeId,
}: {
  enrollment: string | null | undefined;
  online: boolean;
  workers: number;
  nodeId: string;
}): NodeStatusBadge {
  // Revoked wins over online/offline. A revoked node can still be heartbeating
  // -- enumerate_nodes lists it deliberately for exactly that reason -- and
  // showing it as "Online" is what made revocation invisible after the fact
  // (#913). This also covers a node revoked outside the UI, since the badge is
  // derived from the server's `enrollment` rather than from a local action.
  if (enrollment === "revoked") {
    return {
      label: "Revoked",
      modifier: "revoked",
      title: online
        ? `"${nodeId}" is revoked and cannot claim jobs, though its workers are still running.`
        : `"${nodeId}" is revoked and cannot claim jobs.`,
    };
  }

  if (enrollment === "unknown" && workers === 0) {
    return {
      label: "Unknown",
      modifier: "unknown",
      title: `Jobs are queued for "${nodeId}", but no node with that name has ever enrolled. Check the target node name used at launch.`,
    };
  }

  return online
    ? { label: "Online", modifier: "online" }
    : { label: "Offline", modifier: "" };
}
