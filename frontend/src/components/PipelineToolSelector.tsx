import { useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { PipelineTool, PipelineType } from "../api/types";

interface Props {
  pipeline: PipelineType;
  selected: string | null;
  onSelect: (toolName: string) => void;
  onContinue: () => void;
  onClose: () => void;
}

const PIPELINE_LABEL: Record<PipelineType, string> = {
  trim: "a trimmer",
  align: "an aligner",
  qc: "a QC tool",
  utility: "a tool",
  download: "a download tool",
};

/**
 * A radio group of tool cards for one pipeline, between the panel button and
 * the parameter dialog.
 *
 * Always shown, even when only one tool matches. An earlier draft of this
 * plan skipped the selector whenever exactly one tool was *available* --  but
 * on a host where bwa-mem2 cannot run, that rule hides the align selector
 * entirely (minimap2 is the only available aligner) while showing trim's
 * (fastp, cutadapt, trimmomatic all probe as available). That inverts the
 * plan's own goal of surfacing *why* an installed-but-unrunnable tool is
 * greyed out -- the one case that skip rule would hide it in is the one host
 * where seeing it matters most. Always rendering means the explanation is
 * always reachable, at the cost of one click for a pipeline that happens to
 * have no real choice today.
 */
export function PipelineToolSelector({
  pipeline,
  selected,
  onSelect,
  onContinue,
  onClose,
}: Props) {
  const listRef = useRef<HTMLDivElement>(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["pipelines", "tools"],
    queryFn: api.pipelineTools,
    staleTime: 60_000,
  });

  const tools = (data?.tools ?? []).filter((t) => t.pipelines.includes(pipeline));

  // A card is choosable only when the binary works *and* something in this
  // application actually calls it. `available` alone would offer cutadapt as
  // a choice that silently does nothing once Continue is pressed; `runnable`
  // alone would offer bwa-mem2 on a host where it cannot execute.
  const choosable = (t: PipelineTool) => t.available && t.runnable;

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (tools.length === 0) return;
    const current = tools.findIndex((t) => t.name === selected);

    // `start` is the index the search steps *from*; the loop below always
    // steps at least once, so Home (start just before 0, step forward) and
    // End (start just past the last, step backward) land correctly without a
    // special case.
    let start: number;
    let step: 1 | -1;
    switch (e.key) {
      case "ArrowDown":
      case "ArrowRight":
        start = current;
        step = 1;
        break;
      case "ArrowUp":
      case "ArrowLeft":
        start = current;
        step = -1;
        break;
      case "Home":
        start = -1;
        step = 1;
        break;
      case "End":
        start = tools.length;
        step = -1;
        break;
      default:
        return;
    }

    e.preventDefault();

    // Steps past disabled cards rather than landing on one: a radio group
    // where an arrow key can select an unusable option is worse than one
    // that skips it, the same reason a browser's own <input type=radio>
    // group skips disabled members.
    let candidate = start;
    for (let tries = 0; tries < tools.length; tries++) {
      candidate = (candidate + step + tools.length) % tools.length;
      if (choosable(tools[candidate])) break;
    }
    if (!choosable(tools[candidate])) return; // nothing choosable at all

    const tool = tools[candidate];
    onSelect(tool.name);
    listRef.current
      ?.querySelector<HTMLDivElement>(`[data-tool="${tool.name}"]`)
      ?.focus();
  };

  const label = PIPELINE_LABEL[pipeline] ?? "a tool";

  return (
    <div className="modal-backdrop" onClick={onClose}>
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

        {tools.length > 0 && (
          <div
            className="tool-card-list"
            role="radiogroup"
            aria-label={`Select ${label}`}
            ref={listRef}
            onKeyDown={onKeyDown}
          >
            {tools.map((tool) => {
              const disabled = !choosable(tool);
              const isSelected = tool.name === selected;
              // Roving tabindex, as in Tabs.tsx: the group is one stop in the
              // page's tab order. Before anything is selected, the stop is
              // the first choosable card rather than the first card outright
              // -- landing Tab on a disabled option would require an extra
              // arrow press just to reach something the group can select.
              const isTabStop =
                isSelected ||
                (!selected && !disabled && tools.findIndex(choosable) === tools.indexOf(tool));
              return (
                <ToolCard
                  key={tool.name}
                  tool={tool}
                  selected={isSelected}
                  disabled={disabled}
                  tabIndex={isTabStop ? 0 : -1}
                  onSelect={() => !disabled && onSelect(tool.name)}
                />
              );
            })}
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
    </div>
  );
}

function ToolCard({
  tool,
  selected,
  disabled,
  tabIndex,
  onSelect,
}: {
  tool: PipelineTool;
  selected: boolean;
  disabled: boolean;
  tabIndex: 0 | -1;
  onSelect: () => void;
}) {
  // Two different reasons a card can be unusable, and the message has to say
  // which: "not installed" points at the environment, "not yet supported"
  // points at this application. Telling a user to install a binary that
  // already works would send them chasing the wrong fix.
  const reason = !tool.available
    ? tool.error || `${tool.name} is not installed`
    : !tool.runnable
      ? `${tool.name} is installed, but this application does not run it yet.`
      : null;

  return (
    <div
      className={`tool-card${selected ? " selected" : ""}${disabled ? " disabled" : ""}`}
      role="radio"
      aria-checked={selected}
      aria-disabled={disabled}
      data-tool={tool.name}
      tabIndex={tabIndex}
      onClick={onSelect}
      onKeyDown={(e) => {
        if ((e.key === "Enter" || e.key === " ") && !disabled) {
          e.preventDefault();
          onSelect();
        }
      }}
    >
      <div className="tool-card-header">
        <span className="tool-radio">
          {selected && <span className="tool-radio-dot" />}
        </span>
        <span className="tool-name">{tool.name}</span>
        {tool.version && <span className="tool-version">v{tool.version}</span>}
      </div>

      {tool.summary && <div className="tool-card-summary">{tool.summary}</div>}

      {tool.strengths.length > 0 && (
        <ul className="tool-card-strengths">
          {tool.strengths.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      )}

      {reason && <div className="tool-card-error">{reason}</div>}
    </div>
  );
}
