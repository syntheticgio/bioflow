import { invoke } from "@tauri-apps/api/core";
import type { LauncherState } from "./types";
import { parseHardMemGb } from "./settings-logic";

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

// One row of GET /pipelines/tools, reduced to what the prefetch screen
// needs -- mirrors optional_tools.rs's OptionalTool struct exactly, so this
// type is a straight pass-through of what Tauri's IPC already deserialized,
// not a second shape to keep in sync by hand.
export interface OptionalTool {
  name: string;
  image: string | null;
  download_bytes: number | null;
  available: boolean;
}

// Never hardcodes which tools are optional -- that's the whole point of
// task 9 (closing #40): the list lives in the stack's own TOOL_META and
// this just reads it back. Degrades to an empty array on any failure
// (offline stack, malformed response, timeout) rather than throwing --
// PrefetchStep.tsx treats an empty list as "nothing to offer" and skips
// itself, which is the correct behavior for a step that was always
// optional in the first place.
export function fetchOptionalTools(port: number): Promise<OptionalTool[]> {
  return invoke("fetch_optional_tools", { port });
}

// Pulls one tool's image directly with `docker pull`, bypassing
// POST /pipelines/tools/{name}/install -- see optional_tools.rs's
// module comment for why (no profile exists yet on a fresh install).
export function installOptionalTool(image: string): Promise<void> {
  return invoke("install_optional_tool", { image });
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

export interface CurrentSettings {
  hardMemMb: number | null;
}

// Reads back whatever settings can be recovered from .env on disk -- today
// just the hard memory limit, since it's the only field the UI could not
// otherwise reconstruct. See App.tsx's mount effect for why this exists.
export function currentSettings(): Promise<CurrentSettings> {
  return invoke<{ hard_mem_mb: number | null }>("current_settings").then((d) => ({
    hardMemMb: d.hard_mem_mb,
  }));
}

export function applySettings(args: {
  storageLocation: string;
  port: number;
  networkExposed: boolean;
  hardMemGb: string;
}): Promise<void> {
  const hard = parseHardMemGb(args.hardMemGb);
  return invoke("apply_settings", {
    args: {
      storage_location: args.storageLocation,
      port: args.port,
      network_exposed: args.networkExposed,
      hard_mem_mb: hard.kind === "set" ? hard.mb : null,
    },
  });
}

export interface MigrationProgress {
  phase: "Scanning" | "Copying" | "Validating" | "Removing" | "Complete";
  bytesCopied: number;
  totalBytes: number;
  error: string | null;
}

export async function startStorageMigration(args: {
  newLocation: string;
  keepOriginal: boolean;
  validateByHash: boolean;
}): Promise<void> {
  return invoke("start_storage_migration", {
    args: {
      new_location: args.newLocation,
      keep_original: args.keepOriginal,
      validate_by_hash: args.validateByHash,
    },
  });
}

export async function migrationProgress(): Promise<MigrationProgress | null> {
  const raw = await invoke<{
    phase: { phase: MigrationProgress["phase"] };
    bytes_copied: number;
    total_bytes: number;
    error: string | null;
  } | null>("migration_progress");
  if (!raw) return null;
  return {
    phase: raw.phase.phase,
    bytesCopied: raw.bytes_copied,
    totalBytes: raw.total_bytes,
    error: raw.error,
  };
}

export async function finishStorageMigration(args: {
  newLocation: string;
  port: number;
  networkExposed: boolean;
}): Promise<void> {
  return invoke("finish_storage_migration", {
    args: {
      new_location: args.newLocation,
      port: args.port,
      network_exposed: args.networkExposed,
    },
  });
}
