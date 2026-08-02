import { describe, expect, it } from "vitest";
import { isStarMapqScale, mapqBucketLabel } from "./mapq";

describe("isStarMapqScale", () => {
  it("reads the fact ingest recorded", () => {
    expect(isStarMapqScale({ mapq_scale: "star" })).toBe(true);
  });

  it("falls back to the histogram for BAMs aligned before that fact existed", () => {
    // The shape a real STAR run against the yeast genome produced.
    const facts = {
      mapq_histogram: [
        { mapq: 0, count: 886 },
        { mapq: 1, count: 1098 },
        { mapq: 3, count: 4668 },
        { mapq: 255, count: 193348 },
      ],
    };
    expect(isStarMapqScale(facts)).toBe(true);
  });

  it("leaves phred-scale aligners alone", () => {
    const facts = {
      mapq_histogram: [
        { mapq: 0, count: 100 },
        { mapq: 42, count: 200 },
        { mapq: 60, count: 9000 },
      ],
    };
    expect(isStarMapqScale(facts)).toBe(false);
  });

  it("is false when there is no MAPQ information at all", () => {
    expect(isStarMapqScale({})).toBe(false);
  });
});

describe("mapqBucketLabel", () => {
  it("labels STAR codes by what they mean", () => {
    expect(mapqBucketLabel(255, true)).toBe("unique");
    expect(mapqBucketLabel(3, true)).toBe("2 loci");
    expect(mapqBucketLabel(0, true)).toBe("5+ loci");
  });

  it("keeps a code it does not recognize legible", () => {
    expect(mapqBucketLabel(7, true)).toBe("MAPQ 7");
  });

  it("leaves phred scores as bare numbers", () => {
    expect(mapqBucketLabel(60, false)).toBe("60");
  });
});
