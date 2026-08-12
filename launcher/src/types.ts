// Mirrors src-tauri/src/commands.rs's LauncherStateDto. Kept as a plain
// union rather than a class so it round-trips through Tauri's IPC exactly
// as serde_json wrote it.
export type LauncherState =
  | { kind: "NotInstalled" }
  | { kind: "DockerUnavailable"; installed: boolean }
  | { kind: "Stopped" }
  | { kind: "Running" }
  | { kind: "NodeRunning" }
  | { kind: "NodeStopped" };

/** A running BioFlow stack that is not the one this launcher manages --
 *  in practice a worktree stack started by ops/worktree-up.sh, whose Compose
 *  project is named biopipe-wt-<slug>. Mirrors commands.rs OtherStackDto.
 *
 *  Read-only: the launcher owns the `biopipe` project and offers no way to
 *  act on these. See issue #320. */
export interface OtherStack {
  project: string;
  /** The branch slug the project name encodes -- which branch this serves. */
  slug: string;
  /** Null when no web port could be read (no web container, or it publishes
   *  nothing). Shown as a plain entry with no link in that case, rather than
   *  guessing a port. */
  webPort: number | null;
}

export interface Settings {
  storageLocation: string;
  port: number;
  networkExposed: boolean;
  /** GB as typed, or "" for no hard cap. Converted to MB at the IPC edge. */
  hardMemGb: string;
  /** Tag the stack is pinned to in release mode (e.g. "latest", "0.3.0-alpha").
   *  Mirrors BIOFLOW_TAG in .env. */
  bioflowTag: string;
  /** When non-null, the stack runs in developer mode using locally built images
   *  from this checkout path. Mirrors BIOFLOW_DEVELOPER_REPO in .env. */
  developerRepo: string | null;
}

/** Available version choices for the Settings dropdown, fetched from GHCR.
 *  Mirrors update_check.rs VersionOptions. */
export interface VersionOptions {
  release: string;
  alpha: string | null;
  beta: string | null;
}
