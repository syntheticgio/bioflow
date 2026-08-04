import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { DataObject, PipelineTool, PipelineType } from "../api/types";
import { ModalBackdrop } from "./ModalBackdrop";
import { ToolDetailPane } from "./ToolDetailPane";

interface Props {
  pipeline: PipelineType;
  selected: string | null;
  onSelect: (toolName: string) => void;
  onContinue: () => void;
  onClose: () => void;
  /**
   * The object being launched against, when known. Only used to warn on
   * `align`: aligners that cannot handle this file's chemistry are still
   * choosable (CLAUDE.md's position is that the dialog can afford a looser
   * filter than the suggestion engine, since a human is choosing), but the
   * choice should be an informed one rather than a FATAL ERROR forty seconds
   * into a queued job.
   */
  object?: DataObject;
}

// Chemistries qc_stats.infer_chemistry can report for a long-read file.
// Kept in sync with ReadChemistry on the backend rather than imported, since
// the frontend has no access to backend enums -- same duplication TrimDialog
// already carries for the same reason.
const LONG_READ_CHEMISTRIES = new Set(["hifi", "clr", "ont_simplex", "ont_duplex"]);

// Aligners this app registers that only handle short reads. minimap2 is the
// omission -- it covers every chemistry via its presets -- so it is never in
// this set.
const SHORT_READ_ONLY_ALIGNERS = new Set(["bwa-mem2", "bowtie2", "hisat2", "star"]);

function longReadChemistry(object: DataObject | undefined): string | null {
  const chemistry = object?.facts?.qc_read_chemistry;
  if (typeof chemistry === "string" && LONG_READ_CHEMISTRIES.has(chemistry)) {
    return chemistry;
  }
  return null;
}

// Exhaustive by type, deliberately: this is the one mirror of PipelineType
// that TypeScript can police, and it earned its keep on 2026-08-01 by being
// the only thing that noticed `expression` had reached the backend without
// reaching the frontend at all. Leave it as Record<PipelineType, string>
// rather than a partial map with a fallback -- the compile error is the
// feature.
const PIPELINE_LABEL: Record<PipelineType, string> = {
  trim: "a trimmer",
  align: "an aligner",
  qc: "a QC tool",
  utility: "a tool",
  download: "a download tool",
  variant: "a variant caller",
  expression: "an expression tool",
  assemble: "an assembler",
  reference_assembly: "a reference assembly tool",
  assembly_qc: "an assembly QC tool",
};

/**
 * A list-and-detail picker for one pipeline, between the panel button and the
 * parameter dialog: a compact rail of rows on the left, and everything about
 * whichever row is focused on the right.
 *
 * The rail replaces the older always-rendered card list. That list rendered
 * every tool's full summary and strengths inline so a disabled tool's
 * explanation -- "installed, but this application does not run it yet" --
 * stayed reachable without requiring the tool to be selectable. With 4-5
 * aligners that became a long vertical scroll of mostly-redundant detail.
 * This layout keeps the same property -- a disabled row's explanation is
 * always reachable -- but through *focus* instead of always-rendering:
 * `focused` tracks which row the detail pane describes, independent of
 * `selected` (which tracks the actual choice). A disabled row can be focused
 * and read, just not selected. `focusedTool` falls back to the selected tool
 * and then the first tool so the pane is never blank once options exist.
 *
 * The `listbox`/`option` roles below are a known, deliberate deviation from
 * the ARIA listbox pattern: that pattern has no state for "focus is here but
 * this option is not selected," since roving-tabindex focus on an option is
 * normally the selection act itself. A disabled row breaks that assumption
 * on purpose. `role="tablist"`/`tab`/`tabpanel` would map more precisely
 * (its `aria-selected` already tracks "which panel is showing" rather than
 * a value choice), but that is a larger rework than this redesign -- see
 * docs/superpowers/specs/2026-07-29-additional-aligners-design.md, section 6.
 */
export function PipelineToolSelector({
  pipeline,
  selected,
  onSelect,
  onContinue,
  onClose,
  object,
}: Props) {
  const listRef = useRef<HTMLDivElement>(null);
  const chemistry = pipeline === "align" ? longReadChemistry(object) : null;

  const { data, isLoading, isError } = useQuery({
    queryKey: ["pipelines", "tools"],
    queryFn: api.pipelineTools,
    staleTime: 60_000,
  });

  const tools = (data?.tools ?? []).filter((t) => t.pipelines.includes(pipeline));

  // A row is choosable only when the binary works *and* something in this
  // application actually calls it. `available` alone would offer cutadapt as
  // a choice that silently does nothing once Continue is pressed; `runnable`
  // alone would offer bwa-mem2 on a host where it cannot execute.
  const choosable = (t: PipelineTool) => t.available && t.runnable;

  // Focus is tracked separately from selection because a disabled row can be
  // focused but not selected -- that is what keeps its "not installed"
  // explanation reachable in the detail pane. The old card list skipped
  // disabled entries entirely, which was right for a plain radio group and
  // wrong once the pane carries the explanation.
  const [focused, setFocused] = useState<string | null>(null);
  const focusedTool =
    tools.find((t) => t.name === (focused ?? selected)) ?? tools[0] ?? null;

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (tools.length === 0) return;
    const current = tools.findIndex((t) => t.name === (focused ?? selected));

    let next: number;
    switch (e.key) {
      case "ArrowDown":
      case "ArrowRight":
        next = (current + 1 + tools.length) % tools.length;
        break;
      case "ArrowUp":
      case "ArrowLeft":
        next = (current - 1 + tools.length) % tools.length;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = tools.length - 1;
        break;
      default:
        return;
    }

    e.preventDefault();
    const tool = tools[next];
    setFocused(tool.name);
    // Selection follows focus only for choosable rows; a disabled row can be
    // read but not chosen.
    if (choosable(tool)) onSelect(tool.name);
    listRef.current
      ?.querySelector<HTMLDivElement>(`[data-tool="${tool.name}"]`)
      ?.focus();
  };

  const label = PIPELINE_LABEL[pipeline] ?? "a tool";

  return (
    <ModalBackdrop onClick={onClose}>
      <div className="modal tool-selector" onClick={(e) => e.stopPropagation()}>
        <h2>Select {label}</h2>

        {isLoading && (
          <div className="empty">
            <span className="spinner" /> Checking installed tools…
          </div>
        )}

        {isError && (
          <div className="error-box">
            Could not reach the tools API. Close this and try again.
          </div>
        )}

        {!isLoading && !isError && tools.length === 0 && (
          <div className="warn-box">
            No {label} tools are known to this pipeline. That is a
            configuration problem, not a missing binary — report it.
          </div>
        )}

        {chemistry && focusedTool && SHORT_READ_ONLY_ALIGNERS.has(focusedTool.name) && (
          <div className="warn-box" style={{ marginBottom: 12, fontSize: 12 }}>
            QC found this file's chemistry to be {chemistry.replace("_", " ")},
            which {focusedTool.name} does not align. minimap2 is the aligner
            that handles it — this file will likely fail if launched with{" "}
            {focusedTool.name} anyway.
          </div>
        )}

        {tools.length > 0 && (
          <div className="tool-picker">
            <div
              className="tool-rail"
              role="listbox"
              aria-label={`Select ${label}`}
              ref={listRef}
              onKeyDown={onKeyDown}
            >
              {tools.map((tool, i) => {
                const disabled = !choosable(tool);
                const isSelected = tool.name === selected;
                const isFocused = tool.name === (focused ?? selected);
                return (
                  <div
                    key={tool.name}
                    className={`tool-row${isSelected ? " selected" : ""}${
                      disabled ? " disabled" : ""
                    }${isFocused ? " focused" : ""}`}
                    role="option"
                    aria-selected={isSelected}
                    aria-disabled={disabled}
                    data-tool={tool.name}
                    tabIndex={isFocused || (!focused && !selected && i === 0) ? 0 : -1}
                    onFocus={() => setFocused(tool.name)}
                    onClick={() => {
                      setFocused(tool.name);
                      if (!disabled) onSelect(tool.name);
                    }}
                    onKeyDown={(e) => {
                      if ((e.key === "Enter" || e.key === " ") && !disabled) {
                        e.preventDefault();
                        onSelect(tool.name);
                      }
                    }}
                  >
                    <div className="tool-row-main">
                      <span className="tool-name">{tool.name}</span>
                      {tool.version && (
                        <span className="tool-version">v{tool.version}</span>
                      )}
                    </div>
                    <div className="tool-row-line">{tool.one_liner}</div>
                    {disabled && (
                      <span className="tool-row-badge">
                        {!tool.available ? "not installed" : "not supported yet"}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>

            <ToolDetailPane tool={focusedTool} />
          </div>
        )}

        <div className="modal-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={!selected}
            onClick={onContinue}
          >
            Continue
          </button>
        </div>
      </div>
    </ModalBackdrop>
  );
}
