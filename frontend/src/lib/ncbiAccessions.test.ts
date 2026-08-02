import { describe, expect, it } from "vitest";
import { looksLikeAccessionPrefix } from "./ncbiAccessions";

describe("looksLikeAccessionPrefix", () => {
  it("recognizes SRA run/experiment/sample/study prefixes", () => {
    expect(looksLikeAccessionPrefix("SRR11768093")).toBe(true);
    expect(looksLikeAccessionPrefix("ERX8321150")).toBe(true);
    expect(looksLikeAccessionPrefix("DRS6640466")).toBe(true);
    expect(looksLikeAccessionPrefix("SRP261086")).toBe(true);
  });

  it("recognizes the first 3 letters of longer prefixes", () => {
    expect(looksLikeAccessionPrefix("PRJ")).toBe(true);
    expect(looksLikeAccessionPrefix("PRJNA631678")).toBe(true);
    expect(looksLikeAccessionPrefix("SAM")).toBe(true);
    expect(looksLikeAccessionPrefix("SAMN14886310")).toBe(true);
  });

  it("recognizes 3-letter assembly prefixes exactly", () => {
    expect(looksLikeAccessionPrefix("GCF_000002445.2")).toBe(true);
    expect(looksLikeAccessionPrefix("GCA_000001405.29")).toBe(true);
  });

  it("is case-insensitive", () => {
    expect(looksLikeAccessionPrefix("srr11768093")).toBe(true);
  });

  it("does not fire for organism names", () => {
    expect(looksLikeAccessionPrefix("Homo sapiens")).toBe(false);
    expect(looksLikeAccessionPrefix("Mus musculus")).toBe(false);
    expect(looksLikeAccessionPrefix("Escherichia coli")).toBe(false);
  });

  it("returns false for fewer than 3 characters", () => {
    expect(looksLikeAccessionPrefix("")).toBe(false);
    expect(looksLikeAccessionPrefix("SR")).toBe(false);
  });
});
