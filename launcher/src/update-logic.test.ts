import { describe, expect, it } from "vitest";
import { checkStageUpdate } from "./update-logic";
import type { VersionOptions } from "./types";

describe("checkStageUpdate", () => {
  const makeOptions = (alpha: string | null, beta: string | null): VersionOptions => ({
    release: "latest",
    alpha,
    beta,
  });

  it("returns higher version when both are same stage", () => {
    const result = checkStageUpdate("0.3.0-alpha", makeOptions("0.4.0-alpha", null));
    expect(result).toBe("0.4.0-alpha");
  });

  it("returns later stage when version is same", () => {
    const result = checkStageUpdate("0.3.0-alpha", makeOptions("0.3.0-alpha", "0.3.0-beta"));
    expect(result).toBe("0.3.0-beta");
  });

  it("returns null when only earlier stage is available", () => {
    const result = checkStageUpdate("0.4.0-beta", makeOptions("0.4.0-alpha", "0.4.0-beta"));
    expect(result).toBeNull();
  });

  it("returns null when nothing is available", () => {
    const result = checkStageUpdate("0.3.0-alpha", makeOptions(null, null));
    expect(result).toBeNull();
  });

  it("returns null for lower version", () => {
    const result = checkStageUpdate("0.3.0-alpha", makeOptions("0.2.0-alpha", null));
    expect(result).toBeNull();
  });

  it("returns null for release mode", () => {
    const result = checkStageUpdate("latest", makeOptions("0.4.0-alpha", null));
    expect(result).toBeNull();
  });

  it("picks the highest version when multiple are forward", () => {
    const result = checkStageUpdate("0.3.0-alpha", makeOptions("0.5.0-alpha", "0.4.0-beta"));
    expect(result).toBe("0.5.0-alpha");
  });

  it("returns beta over alpha at same higher version", () => {
    const result = checkStageUpdate("0.3.0-alpha", makeOptions("0.4.0-alpha", "0.4.0-beta"));
    expect(result).toBe("0.4.0-beta");
  });
});
