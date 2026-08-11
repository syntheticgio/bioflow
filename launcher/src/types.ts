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
