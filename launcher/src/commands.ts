import { invoke } from "@tauri-apps/api/core";
import type { LauncherState } from "./types";

export function status(): Promise<LauncherState> {
  return invoke("status");
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

export function runFirstSetup(
  args: { storageLocation: string; installDir: string; port: number },
  bundledComposePath: string,
): Promise<void> {
  return invoke("run_first_setup", {
    args: {
      storage_location: args.storageLocation,
      install_dir: args.installDir,
      port: args.port,
    },
    bundledComposePath,
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
