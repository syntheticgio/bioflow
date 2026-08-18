import { describe, expect, it } from "vitest";

import type { AlignParams, AlignerPreset } from "../api/types";
import {
  ADVANCED_PRESET_VALUE,
  BOWTIE2_CUSTOM_PRESET_VALUE,
  BOWTIE2_DEFAULT_PRESET_ID,
  INSERT_RANGE_ERROR_MESSAGE,
  REPORTING_ERROR_MESSAGE,
  launchValidationMessage,
  initialPresetSelection,
  isBowtie2PairOnlyField,
  reconcileParameterSetPreset,
  shouldClearPresetOnFieldEdit,
} from "./alignDialogPresets";

const bowtie2Presets: Record<string, AlignerPreset> = {
  standard_short_read: {
    id: "standard_short_read",
    label: "Standard short-read DNA",
    description: "Default",
    values: {
      sensitivity: "--sensitive",
      local: false,
      minins: 0,
      maxins: 500,
      orientation: "FR",
      no_mixed: false,
      no_discordant: false,
      dovetail: false,
      no_contain: false,
      no_overlap: false,
      report_k: 0,
      report_all: false,
    },
  },
  mate_pair: {
    id: "mate_pair",
    label: "Mate-pair",
    description: "RF preset",
    values: {
      sensitivity: "--sensitive",
      local: false,
      minins: 500,
      maxins: 20000,
      orientation: "RF",
      no_mixed: false,
      no_discordant: false,
      dovetail: false,
      no_contain: false,
      no_overlap: false,
      report_k: 0,
      report_all: false,
    },
  },
};

describe("initialPresetSelection", () => {
  it("uses an explicit named preset from params", () => {
    expect(
      initialPresetSelection({
        aligner: "bowtie2",
        params: { preset: "mate_pair" },
        presets: bowtie2Presets,
      }),
    ).toBe("mate_pair");
  });

  it("defaults bowtie2 to the standard short-read preset when params match it", () => {
    const params: Partial<AlignParams> = {
      aligner: "bowtie2",
      sensitivity: "--sensitive",
      local: false,
      minins: 0,
      maxins: 500,
      orientation: "FR",
      no_mixed: false,
      no_discordant: false,
      dovetail: false,
      no_contain: false,
      no_overlap: false,
      report_k: 0,
      report_all: false,
    };

    expect(
      initialPresetSelection({
        aligner: "bowtie2",
        params,
        presets: bowtie2Presets,
      }),
    ).toBe(BOWTIE2_DEFAULT_PRESET_ID);
  });

  it("falls back to custom when bowtie2 params do not match a named preset", () => {
    expect(
      initialPresetSelection({
        aligner: "bowtie2",
        params: {
          aligner: "bowtie2",
          sensitivity: "--very-sensitive",
          report_k: 3,
        },
        presets: bowtie2Presets,
      }),
    ).toBe(BOWTIE2_CUSTOM_PRESET_VALUE);
  });

  it("defaults a fresh bowtie2 seed to the standard preset before defaults arrive", () => {
    expect(
      initialPresetSelection({
        aligner: "bowtie2",
        params: { aligner: "bowtie2" },
        presets: bowtie2Presets,
      }),
    ).toBe(BOWTIE2_DEFAULT_PRESET_ID);

    expect(
      initialPresetSelection({
        aligner: "bowtie2",
        params: {
          aligner: "bowtie2",
          sensitivity: "--sensitive",
          local: false,
          minins: 0,
          maxins: 500,
          orientation: "FR",
          no_mixed: false,
          no_discordant: false,
          dovetail: false,
          no_contain: false,
          no_overlap: false,
          report_k: 0,
          report_all: false,
        },
        presets: bowtie2Presets,
      }),
    ).toBe(BOWTIE2_DEFAULT_PRESET_ID);
  });

  it("lets a supplied preset win once delayed defaults are available", () => {
    expect(
      initialPresetSelection({
        aligner: "bowtie2",
        params: { aligner: "bowtie2" },
        presets: bowtie2Presets,
      }),
    ).toBe(BOWTIE2_DEFAULT_PRESET_ID);

    expect(
      initialPresetSelection({
        aligner: "bowtie2",
        params: {
          aligner: "bowtie2",
          preset: "mate_pair",
          ...bowtie2Presets.mate_pair.values,
        },
        presets: bowtie2Presets,
      }),
    ).toBe("mate_pair");
  });

  it("clears an inherited built-in label when a saved set changes its values", () => {
    const currentParams: Partial<AlignParams> = {
      aligner: "bowtie2",
      preset: BOWTIE2_DEFAULT_PRESET_ID,
      ...bowtie2Presets.standard_short_read.values,
    };

    const result = reconcileParameterSetPreset({
      aligner: "bowtie2",
      currentParams,
      appliedValues: { sensitivity: "--very-sensitive", maxins: 750 },
      presets: bowtie2Presets,
    });

    expect(result.presetSelection).toBe(BOWTIE2_CUSTOM_PRESET_VALUE);
    expect(result.overrides).toEqual({
      sensitivity: "--very-sensitive",
      maxins: 750,
      preset: "",
    });
  });

  it("keeps a saved bowtie2 preset while schema presets are still loading", () => {
    const currentParams: Partial<AlignParams> = {
      aligner: "bowtie2",
      preset: "mate_pair",
      ...bowtie2Presets.mate_pair.values,
    };

    const result = reconcileParameterSetPreset({
      aligner: "bowtie2",
      currentParams,
      appliedValues: bowtie2Presets.mate_pair.values,
      presets: undefined,
    });

    expect(result.presetSelection).toBe("mate_pair");
    expect(result.overrides).toEqual({
      ...bowtie2Presets.mate_pair.values,
      preset: "mate_pair",
    });
  });

  it("keeps advanced as the non-bowtie2 free-form mode", () => {
    expect(
      initialPresetSelection({
        aligner: "bwa-mem2",
        params: {},
        presets: {
          human: {
            id: "human",
            label: "Human",
            description: "Human defaults",
            values: { preset: "human" },
          },
        },
      }),
    ).toBe(ADVANCED_PRESET_VALUE);
  });
});

describe("cross-field validation", () => {
  it("returns the insert-range correction when minins exceeds maxins", () => {
    expect(launchValidationMessage({ minins: 501, maxins: 500 })).toBe(
      INSERT_RANGE_ERROR_MESSAGE,
    );
    expect(launchValidationMessage({ minins: 500, maxins: 500 })).toBeNull();
  });

  it("returns the reporting correction when report_k conflicts with report_all", () => {
    expect(launchValidationMessage({ report_k: 10, report_all: true })).toBe(
      REPORTING_ERROR_MESSAGE,
    );
    expect(launchValidationMessage({ report_k: 0, report_all: true })).toBeNull();
  });
});

describe("Bowtie2 field helpers", () => {
  it("marks pair-only Bowtie2 controls", () => {
    expect(isBowtie2PairOnlyField("minins")).toBe(true);
    expect(isBowtie2PairOnlyField("orientation")).toBe(true);
    expect(isBowtie2PairOnlyField("report_all")).toBe(false);
  });

  it("clears a named bowtie2 preset when one of its managed fields is edited", () => {
    expect(
      shouldClearPresetOnFieldEdit({
        aligner: "bowtie2",
        activeSelection: "mate_pair",
        presets: bowtie2Presets,
        key: "orientation",
      }),
    ).toBe(true);
  });

  it("does not clear presets for non-bowtie2 advanced editing", () => {
    expect(
      shouldClearPresetOnFieldEdit({
        aligner: "bwa-mem2",
        activeSelection: "human",
        presets: {
          human: {
            id: "human",
            label: "Human",
            description: "Human defaults",
            values: { preset: "human" },
          },
        },
        key: "min_score",
      }),
    ).toBe(false);
  });
});
