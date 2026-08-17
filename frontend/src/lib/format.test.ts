import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { formatDate, formatRelative, isIsoTimestamp } from "./format";

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

describe("formatRelative", () => {
  // A fixed "now" well clear of a month or year boundary, so the arithmetic
  // under test is the only thing the expectations depend on.
  const NOW = new Date("2026-08-17T12:00:00Z");

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  const ago = (ms: number) => new Date(NOW.getTime() - ms).toISOString();
  const SECOND = 1000;
  const MINUTE = 60 * SECOND;
  const HOUR = 60 * MINUTE;
  const DAY = 24 * HOUR;

  it("collapses the first minute to 'just now'", () => {
    expect(formatRelative(ago(0))).toBe("just now");
    expect(formatRelative(ago(59 * SECOND))).toBe("just now");
  });

  it("counts whole minutes, then whole hours, then whole days", () => {
    expect(formatRelative(ago(MINUTE))).toBe("1m ago");
    expect(formatRelative(ago(59 * MINUTE))).toBe("59m ago");
    expect(formatRelative(ago(HOUR))).toBe("1h ago");
    expect(formatRelative(ago(23 * HOUR))).toBe("23h ago");
    expect(formatRelative(ago(DAY))).toBe("1d ago");
    expect(formatRelative(ago(6 * DAY))).toBe("6d ago");
  });

  it("switches to an absolute date past a week, where '63d ago' is arithmetic", () => {
    // Beyond a week the age stops being the useful reading, so the row names
    // the day instead of asking the reader to subtract.
    expect(formatRelative(ago(7 * DAY))).toBe("Aug 10");
    expect(formatRelative(ago(30 * DAY))).toBe("Jul 18");
  });

  it("names the year once the run is from a different one", () => {
    // "Dec 20" alone is ambiguous by the following spring; the year is what
    // makes an old run legible.
    expect(formatRelative(ago(300 * DAY))).toBe("Oct 21, 2025");
  });

  it("renders a future timestamp as 'just now' rather than negative time", () => {
    // Clock skew between the container and the browser can put updated_at
    // slightly ahead; "-3m ago" would read as a bug.
    expect(formatRelative(new Date(NOW.getTime() + 5 * MINUTE).toISOString())).toBe(
      "just now",
    );
  });

  it("renders missing and unparseable values as an em dash", () => {
    expect(formatRelative(null)).toBe("—");
    expect(formatRelative(undefined)).toBe("—");
    expect(formatRelative("not a date")).toBe("—");
  });
});
