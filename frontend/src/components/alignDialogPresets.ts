import type { AlignParams, AlignerName, AlignerPreset } from "../api/types";

export const ADVANCED_PRESET_VALUE = "advanced";
export const BOWTIE2_CUSTOM_PRESET_VALUE = "custom";
export const BOWTIE2_DEFAULT_PRESET_ID = "standard_short_read";

const BOWTIE2_PAIR_ONLY_KEYS = new Set([
  "minins",
  "maxins",
  "orientation",
  "dovetail",
  "no_contain",
  "no_overlap",
  "no_mixed",
  "no_discordant",
]);

function isNamedPreset(
  presets: Record<string, AlignerPreset> | undefined,
  value: unknown,
): value is string {
  return typeof value === "string" && value.length > 0 && presets?.[value] != null;
}

function paramsMatchPreset(
  params: Partial<AlignParams>,
  preset: AlignerPreset | undefined,
): boolean {
  if (!preset) return false;
  return Object.entries(preset.values).every(
    ([key, value]) => (params as Record<string, unknown>)[key] === value,
  );
}

export function initialPresetSelection({
  aligner,
  params,
  presets,
}: {
  aligner: AlignerName | undefined;
  params: Partial<AlignParams>;
  presets?: Record<string, AlignerPreset>;
}): string | null {
  if (!presets) return null;
  if (isNamedPreset(presets, params.preset)) return params.preset;
  if (aligner === "bowtie2") {
    return paramsMatchPreset(params, presets[BOWTIE2_DEFAULT_PRESET_ID])
      ? BOWTIE2_DEFAULT_PRESET_ID
      : BOWTIE2_CUSTOM_PRESET_VALUE;
  }
  return ADVANCED_PRESET_VALUE;
}

export function shouldClearPresetOnFieldEdit({
  aligner,
  activeSelection,
  presets,
  key,
}: {
  aligner: AlignerName | undefined;
  activeSelection: string | null;
  presets?: Record<string, AlignerPreset>;
  key: string;
}): boolean {
  const preset =
    aligner === "bowtie2" && isNamedPreset(presets, activeSelection)
      ? presets?.[activeSelection] ?? null
      : null;
  return (
    preset != null &&
    Object.prototype.hasOwnProperty.call(preset.values, key)
  );
}

export function hasInsertRangeError(params: Partial<AlignParams>): boolean {
  return params.minins != null && params.maxins != null && params.minins > params.maxins;
}

export function hasReportingError(params: Partial<AlignParams>): boolean {
  return Boolean(params.report_all) && Number(params.report_k ?? 0) > 0;
}

export function isBowtie2PairOnlyField(key: string): boolean {
  return BOWTIE2_PAIR_ONLY_KEYS.has(key);
}
