import { describe, it, expect } from "vitest";
import { groupsFrom } from "./HelpSoftware";
import type { PipelineTool } from "../api/types";

/**
 * The help page's sections are derived from the tools rather than listed by
 * hand, and this pins the reason.
 *
 * On 2026-08-01 the hardcoded list lost tools twice in one day: the expression
 * vertical shipped featureCounts and pydeseq2 with no section to render them
 * in, and the assembly work would have done the same for Flye. Both times
 * every backend test passed -- TOOL_META was complete, and nothing checked
 * that the frontend agreed.
 *
 * The last case below is the one that matters most and is the easiest to
 * delete by accident: a pipeline type this build of the frontend has never
 * heard of must still render. That is what makes the page self-healing when
 * the backend adds one, which is precisely the situation that caused the bug.
 */
const tool = (name: string, pipelines: string[]) =>
  ({ name, pipelines }) as unknown as PipelineTool;

describe("groupsFrom", () => {
  it("puts sections in reading order, not the order tools arrive", () => {
    const groups = groupsFrom([
      tool("flye", ["assemble"]),
      tool("fastp", ["trim", "qc"]),
    ]);
    expect(groups.map((g) => g.type)).toEqual(["qc", "trim", "assemble"]);
  });

  it("drops types no tool declares, rather than leaving empty headings", () => {
    const groups = groupsFrom([tool("fastp", ["trim"])]);
    expect(groups.map((g) => g.type)).toEqual(["trim"]);
  });

  it("files a tool under every pipeline it declares, not just the first", () => {
    // samtools is (utility, qc): both columns must exist for it.
    const groups = groupsFrom([tool("samtools", ["utility", "qc"])]);
    expect(groups.map((g) => g.type)).toEqual(["qc", "utility"]);
  });

  it("renders expression, the type that was silently missing", () => {
    const groups = groupsFrom([tool("featurecounts", ["expression"])]);
    expect(groups).toEqual([{ type: "expression", title: "Expression" }]);
  });

  it("renders a pipeline type this build has never heard of", () => {
    // The self-healing branch. Without it, a backend that adds a type before
    // the frontend knows about it drops those tools off the page entirely --
    // which is the exact failure this whole approach exists to prevent, so a
    // title-cased fallback heading beats an invisible tool.
    const groups = groupsFrom([tool("mystery", ["proteomics"])]);
    expect(groups).toEqual([{ type: "proteomics", title: "Proteomics" }]);
  });

  it("keeps known types ahead of unknown ones", () => {
    const groups = groupsFrom([
      tool("mystery", ["proteomics"]),
      tool("fastp", ["qc"]),
    ]);
    expect(groups.map((g) => g.type)).toEqual(["qc", "proteomics"]);
  });
});
