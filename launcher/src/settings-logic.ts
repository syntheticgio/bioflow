// Pure logic for the hard memory limit field on Settings.tsx -- split out
// the same way wizard-logic.ts separates SetupWizard.tsx's pure logic, so
// it can be unit tested without rendering anything.

/**
 * Parsing for the hard memory limit field.
 *
 * Blank is a real value meaning "no hard cap", not an error and not a
 * disabled state -- it is the default every fresh install has. The field is
 * shown in GB because that is what a human types; the backend wants MB.
 */

const MB_PER_GB = 1024;

/**
 * Below this, the worker cannot start at all: the container would be
 * OOM-killed before running any job, which is unrecoverable from the UI.
 * Rejecting it at the field is the only place a user gets told.
 */
export const MIN_HARD_MEM_GB = 1;

export type HardMemValue =
  | { kind: "none" }
  | { kind: "set"; mb: number }
  | { kind: "invalid" };

export function parseHardMemGb(raw: string): HardMemValue {
  if (raw.trim() === "") return { kind: "none" };

  const gb = Number(raw);
  if (!Number.isFinite(gb) || gb <= 0) return { kind: "invalid" };
  if (gb < MIN_HARD_MEM_GB) return { kind: "invalid" };

  return { kind: "set", mb: Math.round(gb * MB_PER_GB) };
}
