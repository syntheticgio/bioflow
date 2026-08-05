// Pure logic pulled out of SetupWizard.tsx and Settings.tsx so it can be
// unit tested without rendering anything -- see the components themselves
// for how these are wired to state.

import type { PortValidation, StoragePathValidation } from "./commands";

export function canInstall(args: {
  loaded: boolean;
  storageLocation: string;
  storageValidation: StoragePathValidation;
  portValidation: PortValidation;
}): boolean {
  return (
    args.loaded &&
    args.storageLocation.length > 0 &&
    args.storageValidation.kind === "Ok" &&
    args.portValidation.kind === "Ok"
  );
}

// The setup wizard's status-line text when one or both fields have a
// problem -- "1 thing to fix" / "2 things to fix", not "1 things" or a
// count that silently drifts if a third validated field is ever added.
export function setupStatusText(args: { storageProblem: boolean; portProblem: boolean }): string {
  const count = [args.storageProblem, args.portProblem].filter(Boolean).length;
  if (count === 0) return "First run · not yet installed";
  return `First run · ${count} thing${count === 1 ? "" : "s"} to fix`;
}

// Settings.tsx's "you're pointing at a different folder now" warning --
// storage location is the only setting where "changed" needs a distinct
// warning (unlike port or the network toggle) because a changed storage
// location does not carry old data over automatically.
export function storageLocationChanged(current: string, next: string): boolean {
  return current !== next;
}
