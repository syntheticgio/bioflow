import { describe, expect, it } from "vitest";

import { overridesSurvivingAssemblerChange } from "./AssembleDialog";

describe("overridesSurvivingAssemblerChange", () => {
  it("keeps the two fields every assembler shares", () => {
    const kept = overridesSurvivingAssemblerChange({
      threads: 16,
      genome_size: 4_600_000,
    });
    expect(kept).toEqual({ threads: 16, genome_size: 4_600_000 });
  });

  it("drops a value the newly chosen assembler has no field for", () => {
    // ABySS -> MEGAHIT: `k` is ABySS's only real knob and MEGAHIT has no
    // k-mer field at all, so carrying it over would send a parameter the
    // launch would reject.
    const kept = overridesSurvivingAssemblerChange({ threads: 8, k: 71 });
    expect(kept).toEqual({ threads: 8 });
  });

  it("drops mode, which two assemblers spell with disjoint vocabularies", () => {
    // The value that would actively mislead rather than merely go stale:
    // Flye's `mode` is an accuracy grade (`nano-raw`), SPAdes' is a running
    // mode (`isolate`), and neither accepts the other's values.
    const kept = overridesSurvivingAssemblerChange({ mode: "nano-raw" });
    expect(kept).toEqual({});
  });

  it("drops Flye's meta checkbox, which only Flye has", () => {
    const kept = overridesSurvivingAssemblerChange({ meta: true, threads: 4 });
    expect(kept).toEqual({ threads: 4 });
  });

  it("keeps nothing when the user has edited nothing", () => {
    expect(overridesSurvivingAssemblerChange({})).toEqual({});
  });

  it("preserves a deliberate zero rather than treating it as unset", () => {
    // `iterations: 0` is a real choice (skip Flye's polishing), and a
    // truthiness check here would silently discard it. It is not a shared
    // field, so it is dropped -- but threads: 0 would be the same trap on a
    // field that is kept, which is why the guard is `!== undefined`.
    const kept = overridesSurvivingAssemblerChange({
      threads: 0,
      iterations: 0,
    });
    expect(kept).toEqual({ threads: 0 });
  });
});
