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
}

export function updateAffordance({
  imageDigest,
  updatable,
  primaryDigest,
}: StalenessInputs): UpdateAffordance {
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
