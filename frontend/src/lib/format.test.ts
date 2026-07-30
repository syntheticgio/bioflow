import { describe, expect, it } from "vitest";
import { formatDate, isIsoTimestamp } from "./format";

describe("isIsoTimestamp", () => {
  it("accepts the instants the backend actually stores", () => {
    expect(isIsoTimestamp("2026-07-29T19:03:23.276489+00:00")).toBe(true);
    expect(isIsoTimestamp("2026-07-29T19:03:23Z")).toBe(true);
    expect(isIsoTimestamp("2026-07-29 19:03:23+0000")).toBe(true);
  });

  it("leaves fact values that merely look numeric alone", () => {
    // These share the facts blob with timestamps; reformatting one as a date
    // would corrupt a value people copy into methods sections.
    expect(isIsoTimestamp("GCA_000001405.29")).toBe(false);
    expect(isIsoTimestamp("SRR12345678")).toBe(false);
    expect(isIsoTimestamp("1.21")).toBe(false);
    expect(isIsoTimestamp(1753815803)).toBe(false);
  });

  it("rejects strings with no zone, which have no single correct rendering", () => {
    expect(isIsoTimestamp("2026-07-29")).toBe(false);
    expect(isIsoTimestamp("2026-07-29T19:03:23")).toBe(false);
  });
});

describe("formatDate", () => {
  it("keeps second-level resolution", () => {
    expect(formatDate("2026-07-29T19:03:23Z")).toMatch(/23/);
  });

  it("renders missing and unparseable values as an em dash", () => {
    expect(formatDate(null)).toBe("—");
    expect(formatDate(undefined)).toBe("—");
    expect(formatDate("not a date")).toBe("—");
  });
});
