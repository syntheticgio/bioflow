/**
 * The API layer's types, split by the domain object each describes.
 *
 * This barrel re-exports every module, so `import { X } from "../api/types"`
 * resolves exactly as it did when this was one file -- no call site needs to
 * know which module a type moved into. Import from a specific module only
 * where the narrower dependency is worth stating.
 */

export * from "./ai";
export * from "./alignment";
export * from "./annotation";
export * from "./assembly";
export * from "./expression";
export * from "./job";
export * from "./ncbi";
export * from "./object";
export * from "./parameter-set";
export * from "./pipeline";
export * from "./project";
export * from "./protein";
export * from "./qc";
export * from "./run";
export * from "./share";
export * from "./system";
export * from "./taxonomy";
export * from "./variant";
export * from "./workflow";

/**
 * The profile shape is declared in `stores/profileStore.ts` and re-exported
 * here so `api/client.ts` can name a response type without a second copy
 * drifting from the first. The store is the home rather than this file
 * because the store is what has to keep the value alive across a reload --
 * everything else, including the API layer, only ever passes it through.
 */
export type { Profile } from "../../stores/profileStore";
