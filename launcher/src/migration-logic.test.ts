import { describe, expect, it } from "vitest";
import { formatBytes, progressPercent } from "./migration-logic";

describe("formatBytes", () => {
  it("formats bytes as GB with one decimal place", () => {
    expect(formatBytes(5 * 1024 * 1024 * 1024)).toBe("5.0 GB");
  });

  it("formats a fractional GB amount", () => {
    expect(formatBytes(1.5 * 1024 * 1024 * 1024)).toBe("1.5 GB");
  });
});

describe("progressPercent", () => {
  it("computes a plain percentage, bytes copied over total times 100", () => {
    expect(progressPercent(50, 200)).toBe(25);
  });

  it("returns 0 when total is 0 rather than dividing by zero", () => {
    expect(progressPercent(0, 0)).toBe(0);
  });

  it("returns 100 when copying is complete", () => {
    expect(progressPercent(200, 200)).toBe(100);
  });
});
