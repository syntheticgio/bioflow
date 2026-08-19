import { describe, expect, it } from "vitest";
import {
  SHADE_MAX_RATIO,
  SHADE_MIN_RATIO,
  baselineDepth,
  depthRatio,
  formatRatio,
  shadeFor,
} from "./depthShade";

describe("baselineDepth", () => {
  it("is the depth at which half the reference's bases sit below", () => {
    const contigs = [
      { length: 100, mean_depth: 10 },
      { length: 100, mean_depth: 30 },
      { length: 100, mean_depth: 20 },
    ];
    expect(baselineDepth(contigs)).toBe(20);
  });

  it("is not moved by one small very deep sequence", () => {
    // The real failure this function exists for, from a yeast BAM in this
    // app: NC_001224.1 is the 86 kb mitochondrion at 8157x against sixteen
    // nuclear chromosomes near 26x. The length-weighted *mean* of exactly
    // this data is 80.19x, which would put every nuclear chromosome at
    // 0.2-0.5x and paint the whole strip as a dropout.
    const nuclear = Array.from({ length: 16 }, (_, i) => ({
      length: 700_000 + i * 10_000,
      mean_depth: 26,
    }));
    const mitochondrion = { length: 85_779, mean_depth: 8157.34 };

    const baseline = baselineDepth([...nuclear, mitochondrion]);
    expect(baseline).toBe(26);

    // and so the nuclear chromosomes read as ordinary, not as a dropout
    expect(shadeFor(depthRatio(26, baseline)).kind).toBe("neutral");
    // while the organelle reads as what it is
    expect(shadeFor(depthRatio(8157.34, baseline)).kind).toBe("high");
  });

  it("weights by length so short scaffolds cannot outvote real chromosomes", () => {
    // A draft assembly: two megabase chromosomes at the true depth, plus a
    // pile of tiny low-depth scaffolds that outnumber them.
    const chromosomes = [
      { length: 5_000_000, mean_depth: 30 },
      { length: 4_000_000, mean_depth: 30 },
    ];
    const scaffolds = Array.from({ length: 40 }, () => ({
      length: 2_000,
      mean_depth: 1,
    }));
    expect(baselineDepth([...scaffolds, ...chromosomes])).toBe(30);
  });

  it("returns null when nothing aligned", () => {
    expect(baselineDepth([{ length: 100, mean_depth: 0 }])).toBeNull();
    expect(baselineDepth([])).toBeNull();
  });

  it("treats a trace of stray reads as nothing aligned, not as a baseline", () => {
    // From a real BAM in this app (DRR1078403.bam, genome mean 0.0): depths
    // are not a clean column of zeros but a scatter of ~0.0006x. Dividing by
    // that spread those ratios over 0.7x-1.6x and painted eight chromosomes
    // as structurally high-depth on a BAM where nothing mapped.
    const strays = [
      { length: 5_261_801, mean_depth: 0.00045 },
      { length: 4_054_025, mean_depth: 0.00054 },
      { length: 3_057_547, mean_depth: 0.00042 },
      { length: 2_481_190, mean_depth: 0.00097 },
      { length: 1_653_225, mean_depth: 0.01 },
    ];
    expect(baselineDepth(strays)).toBeNull();
    // and so every bar falls back to the unshaded reading
    for (const c of strays) {
      expect(shadeFor(depthRatio(c.mean_depth, baselineDepth(strays))).kind).toBe(
        "unknown",
      );
    }
  });

  it("ignores zero-length entries rather than dividing by them", () => {
    expect(
      baselineDepth([
        { length: 0, mean_depth: 999 },
        { length: 100, mean_depth: 12 },
      ]),
    ).toBe(12);
  });
});

describe("depthRatio", () => {
  it("divides a contig's depth by the baseline", () => {
    expect(depthRatio(15, 30)).toBeCloseTo(0.5);
    expect(depthRatio(60, 30)).toBeCloseTo(2);
  });

  it("returns null rather than infinity when nothing aligned", () => {
    // A zero baseline is a BAM with no alignment, not a genome of infinite
    // depth; a ratio here would paint the whole strip saturated.
    expect(depthRatio(0, 0)).toBeNull();
    expect(depthRatio(10, 0)).toBeNull();
  });

  it("returns null when the baseline is absent", () => {
    expect(depthRatio(10, undefined)).toBeNull();
    expect(depthRatio(10, null)).toBeNull();
  });
});

describe("shadeFor", () => {
  it("says unknown, not neutral, when there is no ratio", () => {
    // The distinction the chart depends on: "ordinary depth" and "we cannot
    // tell" must not render the same way.
    expect(shadeFor(null)).toEqual({ kind: "unknown" });
  });

  it("treats ordinary variation around the mean as neutral", () => {
    expect(shadeFor(1).kind).toBe("neutral");
    expect(shadeFor(1.05).kind).toBe("neutral");
    expect(shadeFor(0.95).kind).toBe("neutral");
  });

  it("saturates a haploid chromosome in a diploid sample", () => {
    // The sex-chromosome dosage reading from the ticket's first success
    // criterion: half depth is at the end of the scale, not partway along.
    const shade = shadeFor(SHADE_MIN_RATIO);
    expect(shade.kind).toBe("low");
    expect(shade.kind === "low" && shade.t).toBe(1);
  });

  it("saturates a duplication", () => {
    const shade = shadeFor(SHADE_MAX_RATIO);
    expect(shade.kind).toBe("high");
    expect(shade.kind === "high" && shade.t).toBe(1);
  });

  it("clamps past the ends rather than running off the scale", () => {
    // A mitochondrion at 400x the nuclear mean reads as "very high", the same
    // as a duplication -- the alternative is every chromosome collapsing to
    // one shade to leave room for it.
    const mito = shadeFor(400);
    expect(mito.kind).toBe("high");
    expect(mito.kind === "high" && mito.t).toBe(1);

    const dropout = shadeFor(0);
    expect(dropout.kind).toBe("low");
    expect(dropout.kind === "low" && dropout.t).toBe(1);
  });

  it("tints gently just outside the neutral band", () => {
    const justLow = shadeFor(0.88);
    expect(justLow.kind).toBe("low");
    expect(justLow.kind === "low" && justLow.t).toBeLessThan(0.2);

    const justHigh = shadeFor(1.12);
    expect(justHigh.kind).toBe("high");
    expect(justHigh.kind === "high" && justHigh.t).toBeLessThan(0.2);
  });

  it("increases intensity monotonically away from the mean", () => {
    const ts = [0.9, 0.8, 0.7, 0.6, 0.5].map((r) => {
      const s = shadeFor(r);
      return s.kind === "low" ? s.t : -1;
    });
    for (let i = 1; i < ts.length; i++) {
      expect(ts[i]).toBeGreaterThanOrEqual(ts[i - 1]);
    }
  });
});

describe("formatRatio", () => {
  it("reads as a multiple of typical depth, not of the mean", () => {
    // "mean" here would name a number this scale deliberately does not use.
    expect(formatRatio(0.5)).toBe("0.50× typical depth");
    expect(formatRatio(2)).toBe("2.00× typical depth");
  });
});
