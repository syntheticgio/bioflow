import { describe, expect, it } from "vitest";
import { parseHardMemGb } from "./settings-logic";

describe("parseHardMemGb", () => {
  it("treats blank as no hard cap", () => {
    expect(parseHardMemGb("")).toEqual({ kind: "none" });
    expect(parseHardMemGb("   ")).toEqual({ kind: "none" });
  });

  it("converts a valid GB value to MB", () => {
    expect(parseHardMemGb("16")).toEqual({ kind: "set", mb: 16384 });
    expect(parseHardMemGb("1.5")).toEqual({ kind: "set", mb: 1536 });
  });

  it("rejects values that are not a positive number", () => {
    expect(parseHardMemGb("abc").kind).toBe("invalid");
    expect(parseHardMemGb("0").kind).toBe("invalid");
    expect(parseHardMemGb("-4").kind).toBe("invalid");
  });

  it("rejects a limit too small to run anything", () => {
    // A 0.2 GB ceiling would OOM-kill the worker on startup, before any job
    // runs -- an unrecoverable state reached by a plausible typo.
    expect(parseHardMemGb("0.2").kind).toBe("invalid");
  });

  it("accepts the smallest sane limit", () => {
    expect(parseHardMemGb("2")).toEqual({ kind: "set", mb: 2048 });
  });
});
