// Pure logic for the Update button on App.tsx -- split out the same way
// wizard-logic.ts and settings-logic.ts separate their components' pure
// logic, so it can be unit tested without rendering anything. That matters
// more here than convention: this repo has no jsdom or testing-library
// setup and no .test.tsx files, so a pure module is the only testable seam.

/**
 * Mirrors `update_check::checkable_tag` in Rust. The backend is
 * authoritative -- it makes no network call in a suppressed mode -- but the
 * button must not depend on a poll result that could be stale across a mode
 * switch, and skipping the poll entirely avoids a pointless IPC round-trip
 * every five minutes.
 */
export type UpdateAffordance =
  /** Release mode, nothing newer published. No button. */
  | { kind: "hidden" }
  /** Release mode, a newer image exists. The clickable btn-warn. */
  | { kind: "available" }
  /** Developer or a pinned stage: visible, disabled, and self-explaining. */
  | { kind: "suppressed"; reason: string };

export interface UpdateInputs {
  /** Mirrors BIOFLOW_TAG in .env. */
  bioflowTag: string;
  /** Mirrors BIOFLOW_DEVELOPER_REPO in .env; null outside developer mode. */
  developerRepo: string | null;
  /** The latest result of the backend's check_for_update poll. */
  updateAvailable: boolean;
}

/**
 * Whether an update check means anything in this mode. Developer is checked
 * first so a hand-edited .env carrying both lines resolves the way
 * current_settings resolves it.
 */
export function shouldPollForUpdates(
  bioflowTag: string,
  developerRepo: string | null,
): boolean {
  if (developerRepo != null) return false;
  return bioflowTag === "latest";
}

export function updateAffordance({
  bioflowTag,
  developerRepo,
  updateAvailable,
}: UpdateInputs): UpdateAffordance {
  // Named before the pinned case: developer mode takes precedence, and the
  // hint names Rebuild because that genuinely is the update path for a
  // local build.
  if (developerRepo != null) {
    return { kind: "suppressed", reason: "Developer mode — use Rebuild in Settings." };
  }
  // Naming the tag keeps the reason concrete -- "pinned" alone does not tell
  // the user what they are pinned to.
  if (bioflowTag !== "latest") {
    return {
      kind: "suppressed",
      reason: `Pinned to ${bioflowTag} — change version in Settings.`,
    };
  }
  return updateAvailable ? { kind: "available" } : { kind: "hidden" };
}
