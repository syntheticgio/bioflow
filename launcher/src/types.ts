// Mirrors src-tauri/src/commands.rs's LauncherStateDto. Kept as a plain
// union rather than a class so it round-trips through Tauri's IPC exactly
// as serde_json wrote it.
export type LauncherState =
  | { kind: "NotInstalled" }
  | { kind: "DockerUnavailable"; installed: boolean }
  | { kind: "Stopped" }
  | { kind: "Running" };

export interface Settings {
  storageLocation: string;
  port: number;
  networkExposed: boolean;
  /** GB as typed, or "" for no hard cap. Converted to MB at the IPC edge. */
  hardMemGb: string;
}
