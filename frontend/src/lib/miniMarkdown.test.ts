import { describe, expect, it } from "vitest";

import { parseInline, parseMarkdown } from "./miniMarkdown";

describe("parseInline", () => {
  it("returns a single text span for plain text", () => {
    expect(parseInline("just words")).toEqual([
      { kind: "text", text: "just words" },
    ]);
  });

  it("splits bold, code and italic out of surrounding text", () => {
    expect(parseInline("a **b** c `d` e _f_ g")).toEqual([
      { kind: "text", text: "a " },
      { kind: "bold", children: [{ kind: "text", text: "b" }] },
      { kind: "text", text: " c " },
      { kind: "code", text: "d" },
      { kind: "text", text: " e " },
      { kind: "italic", children: [{ kind: "text", text: "f" }] },
      { kind: "text", text: " g" },
    ]);
  });

  it("prefers bold over italic when both could match", () => {
    expect(parseInline("**parameters not recorded**")).toEqual([
      {
        kind: "bold",
        children: [{ kind: "text", text: "parameters not recorded" }],
      },
    ]);
  });

  // A filename is the realistic source of a stray marker, and mangling one
  // would corrupt the record the report exists to provide.
  it("leaves an unclosed marker as literal text", () => {
    expect(parseInline("sample_1 ** unclosed")).toEqual([
      { kind: "text", text: "sample_1 ** unclosed" },
    ]);
  });

  it("does not treat a single underscore inside a word as italic", () => {
    expect(parseInline("DRR1066343_1.fastq")).toEqual([
      { kind: "text", text: "DRR1066343_1.fastq" },
    ]);
  });

  // Regression: nearly every identifier in this report is snake_case, and a
  // rule that ignores word boundaries pairs the underscore in one name with
  // the underscore in the next. Real data rendered `download_sra_run` as
  // "download*sra*run" and scrambled whole parameter lines.
  it("leaves a snake_case identifier with two underscores intact", () => {
    expect(parseInline("(job: download_sra_run)")).toEqual([
      { kind: "text", text: "(job: download_sra_run)" },
    ]);
  });

  it("leaves a parameter list of snake_case names intact", () => {
    const line = "Parameters: min_length=36, sliding_window_size=4, threads=4";
    expect(parseInline(line)).toEqual([{ kind: "text", text: line }]);
  });

  it("still italicises a phrase delimited at word boundaries", () => {
    expect(parseInline("_All facts recorded._")).toEqual([
      {
        kind: "italic",
        children: [{ kind: "text", text: "All facts recorded." }],
      },
    ]);
  });

  it("italicises a phrase that itself contains snake_case names", () => {
    expect(parseInline("_combined `a_1.fastq` and `b_2.fastq`._")).toEqual([
      {
        kind: "italic",
        children: [
          { kind: "text", text: "combined " },
          { kind: "code", text: "a_1.fastq" },
          { kind: "text", text: " and " },
          { kind: "code", text: "b_2.fastq" },
          { kind: "text", text: "." },
        ],
      },
    ]);
  });
});

describe("parseMarkdown", () => {
  it("reads headings at both levels", () => {
    const blocks = parseMarkdown("## Provenance\n\n### Steps");
    expect(blocks).toEqual([
      { kind: "heading", level: 2, spans: [{ kind: "text", text: "Provenance" }] },
      { kind: "heading", level: 3, spans: [{ kind: "text", text: "Steps" }] },
    ]);
  });

  it("groups consecutive bullets into one list", () => {
    const blocks = parseMarkdown("- one\n- two");
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({ kind: "list" });
    if (blocks[0].kind !== "list") throw new Error("expected a list");
    expect(blocks[0].items).toHaveLength(2);
  });

  it("starts a new list after a blank line", () => {
    const blocks = parseMarkdown("- one\n\n- two");
    expect(blocks.filter((b) => b.kind === "list")).toHaveLength(2);
  });

  it("nests an indented bullet under the preceding item", () => {
    const blocks = parseMarkdown("- trimmed with trimmomatic\n  - Parameters: threads=4");
    if (blocks[0].kind !== "list") throw new Error("expected a list");
    expect(blocks[0].items).toHaveLength(1);
    expect(blocks[0].items[0].children).toEqual([
      { spans: [{ kind: "text", text: "Parameters: threads=4" }], children: [] },
    ]);
  });

  it("treats an indented bullet with no parent as top level", () => {
    const blocks = parseMarkdown("  - orphan");
    if (blocks[0].kind !== "list") throw new Error("expected a list");
    expect(blocks[0].items).toHaveLength(1);
    expect(blocks[0].items[0].children).toEqual([]);
  });

  it("reads a non-bullet, non-heading line as a paragraph", () => {
    expect(parseMarkdown("_2 facts not recorded._")).toEqual([
      {
        kind: "paragraph",
        spans: [
          {
            kind: "italic",
            children: [{ kind: "text", text: "2 facts not recorded." }],
          },
        ],
      },
    ]);
  });

  // The shape produced by render_markdown for a real file, end to end.
  it("parses a representative report", () => {
    const report = [
      "## Provenance",
      "",
      "_2 facts not recorded._",
      "",
      "### Steps",
      "",
      "- **DRR1066343_1.fastq** — downloaded from the SRA an unrecorded tool",
      "  - **parameters not recorded** (job: download_sra_run)",
      "- **DRR1066343_1.trimmed.fastq** — trimmed with trimmomatic 0.39",
      "  - Parameters: min_length=36, threads=4",
      "",
      "- _This step combined two inputs (a branch in the lineage): `a.fastq`, `b.fastq`._",
    ].join("\n");

    const blocks = parseMarkdown(report);
    const kinds = blocks.map((b) => b.kind);
    expect(kinds).toEqual(["heading", "paragraph", "heading", "list", "list"]);

    const steps = blocks[3];
    if (steps.kind !== "list") throw new Error("expected a list");
    expect(steps.items).toHaveLength(2);
    expect(steps.items[0].children).toHaveLength(1);
    expect(steps.items[0].spans[0]).toEqual({
      kind: "bold",
      children: [{ kind: "text", text: "DRR1066343_1.fastq" }],
    });
  });
});
