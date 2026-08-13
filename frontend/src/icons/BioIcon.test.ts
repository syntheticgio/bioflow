import { describe, expect, it } from "vitest";
import { readPlatformFor } from "../icons/BioIcon";

describe("readPlatformFor", () => {
  it("maps hifi/clr chemistry to PacBio", () => {
    expect(readPlatformFor("hifi", "Sequel II")).toBe("pacbio");
    expect(readPlatformFor("clr", "Sequel II")).toBe("pacbio");
  });

  it("maps ont chemistry to Nanopore", () => {
    expect(readPlatformFor("ont_simplex", "MinION")).toBe("nanopore");
    expect(readPlatformFor("ont_duplex", "PromethION")).toBe("nanopore");
  });

  it("maps short chemistry to Illumina", () => {
    expect(readPlatformFor("short", "NovaSeq")).toBe("illumina");
  });

  it("falls back to platform label matching for Nanopore", () => {
    expect(readPlatformFor(undefined, "OXFORD_NANOPORE")).toBe("nanopore");
    expect(readPlatformFor(undefined, "MinION")).toBe("nanopore");
  });

  it("falls back to platform label matching for PacBio", () => {
    expect(readPlatformFor(undefined, "Sequel II")).toBe("pacbio");
    expect(readPlatformFor(undefined, "Revio")).toBe("pacbio");
  });

  it("falls back to platform label matching for Illumina", () => {
    expect(readPlatformFor(undefined, "Illumina HiSeq")).toBe("illumina");
    expect(readPlatformFor(undefined, "NovaSeq X")).toBe("illumina");
  });

  it("returns null when neither chemistry nor label matches", () => {
    expect(readPlatformFor(undefined, "some-unknown-platform")).toBeNull();
    expect(readPlatformFor(undefined, undefined)).toBeNull();
    expect(readPlatformFor(undefined, "")).toBeNull();
  });
});
