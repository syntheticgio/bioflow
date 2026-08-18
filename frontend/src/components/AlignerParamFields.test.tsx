import { describe, expect, it, vi } from "vitest";

import { AlignerParamFields } from "./AlignerParamFields";

function changeSelect({
  field,
  params,
  nextValue,
}: {
  field: Record<string, unknown>;
  params: Record<string, unknown>;
  nextValue: string;
}) {
  const onChange = vi.fn();
  const tree = AlignerParamFields({
    fields: [field as never],
    params,
    onChange,
  }) as unknown as {
    props: { children: Array<{ props: { children: unknown[] } }> };
  };
  const label = tree.props.children[0];
  const select = label.props.children[1] as {
    props: { onChange: (event: { target: { value: string } }) => void };
  };
  select.props.onChange({ target: { value: nextValue } });
  return onChange;
}

describe("AlignerParamFields select normalization", () => {
  it("keeps an explicit minimap2 sr selection even though sr is the field default", () => {
    const onChange = changeSelect({
      field: {
        key: "preset",
        label: "Read type",
        kind: "select",
        default: "sr",
        help: "The wrong choice aligns long reads poorly rather than failing.",
        group: "biology",
        choices: [
          { value: "sr", label: "Short read (Illumina)" },
          { value: "map-ont", label: "Oxford Nanopore" },
        ],
      },
      params: { preset: "map-ont" },
      nextValue: "sr",
    });

    expect(onChange).toHaveBeenCalledWith("preset", "sr");
  });

  it("keeps an explicit winnowmap map-pb selection even though map-pb is the field default", () => {
    const onChange = changeSelect({
      field: {
        key: "preset",
        label: "Read type",
        kind: "select",
        default: "map-pb",
        help: "Long-read preset.",
        group: "biology",
        choices: [
          { value: "map-ont", label: "Oxford Nanopore" },
          { value: "map-pb", label: "PacBio (CLR)" },
        ],
      },
      params: { preset: "map-ont" },
      nextValue: "map-pb",
    });

    expect(onChange).toHaveBeenCalledWith("preset", "map-pb");
  });

  it("still normalizes the optional secondary-mode sentinel to undefined", () => {
    const onChange = changeSelect({
      field: {
        key: "secondary_mode",
        label: "Secondary alignment mode",
        kind: "select",
        default: "default",
        help: "Tool default leaves Minimap2 unchanged.",
        group: "performance",
        choices: [
          { value: "default", label: "Tool default" },
          { value: "enabled", label: "Enabled" },
          { value: "disabled", label: "Disabled" },
        ],
      },
      params: {},
      nextValue: "default",
    });

    expect(onChange).toHaveBeenCalledWith("secondary_mode", undefined);
  });

  it("still normalizes the optional cs-mode sentinel to undefined", () => {
    const onChange = changeSelect({
      field: {
        key: "cs_mode",
        label: "cs tag output",
        kind: "select",
        default: "none",
        help: "None leaves cs tags off.",
        group: "performance",
        choices: [
          { value: "none", label: "None" },
          { value: "short", label: "Short" },
          { value: "long", label: "Long" },
        ],
      },
      params: {},
      nextValue: "none",
    });

    expect(onChange).toHaveBeenCalledWith("cs_mode", undefined);
  });
});
