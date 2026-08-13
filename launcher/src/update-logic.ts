// Pure logic for the Update button on App.tsx -- split out the same way
// wizard-logic.ts and settings-logic.ts separate their components' pure
// logic, so it can be unit tested without rendering anything. That matters
// more here than convention: this repo has no jsdom or testing-library
// setup and no .test.tsx files, so a pure module is the only testable seam.

import type { VersionOptions } from "./types";

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
  /** Pinned to a stage tag and a newer stage image exists. The clickable btn-warn. */
  | { kind: "stage-update"; targetTag: string }
  /** Developer or a pinned stage: visible, disabled, and self-explaining. */
  | { kind: "suppressed"; reason: string };

export interface UpdateInputs {
  /** Mirrors BIOFLOW_TAG in .env. */
  bioflowTag: string;
  /** Mirrors BIOFLOW_DEVELOPER_REPO in .env; null outside developer mode. */
  developerRepo: string | null;
  /** The latest result of the backend's check_for_update poll. */
  updateAvailable: boolean;
  /** Fetched from listVersionOptions; null while loading or on failure. */
  versionOptions: VersionOptions | null;
}

/**
 * Whether an update check means anything in this mode. Developer is checked
 * first so a hand-edited .env carrying both lines resolves the way
 * current_settings resolves it.
 */
/** Mirrors update_check::check_stage_update in Rust. Pure, side-effect-free. */
export function checkStageUpdate(
  currentTag: string,
  options: VersionOptions,
): string | null {
  if (currentTag === "latest") return null;

  const current = parseStageTag(currentTag);
  if (!current) return null;

  const candidates = [options.alpha, options.beta];
  let best: { tag: string; ver: [number, number, number]; rank: number } | null = null;

  for (const candidate of candidates) {
    if (!candidate) continue;
    const cv = parseStageTag(candidate);
    if (!cv) continue;

    const isForward =
      cv.ver[0] > current.ver[0] ||
      (cv.ver[0] === current.ver[0] && cv.ver[1] > current.ver[1]) ||
      (cv.ver[0] === current.ver[0] && cv.ver[1] === current.ver[1] && cv.ver[2] > current.ver[2]) ||
      (cv.ver[0] === current.ver[0] && cv.ver[1] === current.ver[1] && cv.ver[2] === current.ver[2] && cv.rank > current.rank);

    if (!isForward) continue;

    if (!best || cv.ver[0] > best.ver[0] ||
        (cv.ver[0] === best.ver[0] && cv.ver[1] > best.ver[1]) ||
        (cv.ver[0] === best.ver[0] && cv.ver[1] === best.ver[1] && cv.ver[2] > best.ver[2]) ||
        (cv.ver[0] === best.ver[0] && cv.ver[1] === best.ver[1] && cv.ver[2] === best.ver[2] && cv.rank > best.rank)) {
      best = { tag: candidate, ver: cv.ver, rank: cv.rank };
    }
  }

  return best?.tag ?? null;
}

function parseStageTag(tag: string): { ver: [number, number, number]; rank: number } | null {
  const alphaMatch = tag.match(/^(\d+)\.(\d+)\.(\d+)-alpha$/);
  if (alphaMatch) {
    return {
      ver: [parseInt(alphaMatch[1]), parseInt(alphaMatch[2]), parseInt(alphaMatch[3])],
      rank: 0,
    };
  }
  const betaMatch = tag.match(/^(\d+)\.(\d+)\.(\d+)-beta$/);
  if (betaMatch) {
    return {
      ver: [parseInt(betaMatch[1]), parseInt(betaMatch[2]), parseInt(betaMatch[3])],
      rank: 1,
    };
  }
  return null;
}

export function shouldPollForUpdates(
  _bioflowTag: string,
  developerRepo: string | null,
): boolean {
  if (developerRepo != null) return false;
  // Poll for both release (digest) and stage (tag list) updates
  return true;
}

export function updateAffordance({
  bioflowTag,
  developerRepo,
  updateAvailable,
  versionOptions,
}: UpdateInputs): UpdateAffordance {
  // Developer mode → suppressed (unchanged)
  if (developerRepo != null) {
    return { kind: "suppressed", reason: "Developer mode — use Rebuild in Settings." };
  }
  // Alpha/Beta mode → check for newer stage tag
  if (bioflowTag !== "latest" && versionOptions) {
    const target = checkStageUpdate(bioflowTag, versionOptions);
    if (target) {
      return { kind: "stage-update", targetTag: target };
    }
    return { kind: "suppressed", reason: `Pinned to ${bioflowTag} — change version in Settings.` };
  }
  // Release mode → existing digest-based check
  return updateAvailable ? { kind: "available" } : { kind: "hidden" };
}
