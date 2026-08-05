import { invoke } from "@tauri-apps/api/core";
import type { LauncherState } from "./types";

export function status(): Promise<LauncherState> {
  return invoke("status");
}

// Direct Docker probe, independent of whether the stack is installed --
// status()'s underlying state machine never checks Docker before an install
// directory exists, so the setup wizard has no other way to show a real
// "Docker ready" indicator.
export function dockerReady(): Promise<boolean> {
  return invoke("docker_ready");
}

export interface SetupDefaults {
  storageLocation: string;
  installDir: string;
  port: number;
}

export function setupDefaults(): Promise<SetupDefaults> {
  return invoke<{ storage_location: string; install_dir: string; port: number }>(
    "setup_defaults",
  ).then((d) => ({
    storageLocation: d.storage_location,
    installDir: d.install_dir,
    port: d.port,
  }));
}

export type StoragePathValidation =
  | { kind: "Ok" }
  | { kind: "NotWritable" }
  | { kind: "NotDockerShared" };

export function validateStorage(path: string): Promise<StoragePathValidation> {
  return invoke("validate_storage", { path });
}

export type PortValidation = { kind: "Ok" } | { kind: "InUse" };

export function validateSetupPort(port: number): Promise<PortValidation> {
  return invoke("validate_setup_port", { port });
}

export function runStack(): Promise<void> {
  return invoke("run_stack");
}

export function stopStack(): Promise<void> {
  return invoke("stop_stack");
}

export function updateStack(): Promise<void> {
  return invoke("update_stack");
}

// Cheap, non-blocking registry manifest check -- decides only whether the
// Update button should render. Never triggers a pull on its own.
export function checkForUpdate(): Promise<boolean> {
  return invoke("check_for_update");
}

// No install_dir argument -- only one install is supported per machine, so
// the launcher always installs to its fixed default path (~/.bioflow) rather
// than a user-chosen one. See run_first_setup's own doc comment in
// commands.rs for why a user-chosen path was tried first and reverted.
export function runFirstSetup(args: {
  storageLocation: string;
  port: number;
}): Promise<void> {
  return invoke("run_first_setup", {
    args: {
      storage_location: args.storageLocation,
      port: args.port,
    },
  });
}

export function applySettings(args: {
  storageLocation: string;
  port: number;
  networkExposed: boolean;
}): Promise<void> {
  return invoke("apply_settings", {
    args: {
      storage_location: args.storageLocation,
      port: args.port,
      network_exposed: args.networkExposed,
    },
  });
}
