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
