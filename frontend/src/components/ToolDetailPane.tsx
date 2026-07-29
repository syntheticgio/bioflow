import type { PipelineTool } from "../api/types";

/**
 * The right-hand half of the selector: everything about the focused tool.
 *
 * Driven by *focus* rather than selection, which is the whole reason the
 * redesign works. A disabled tool cannot be selected, but its explanation --
 * "installed, but this application does not run it yet" -- is exactly what a
 * user needs to see, and that was the point of the original always-render
 * card list. Following focus keeps that reachable.
 */
export function ToolDetailPane({ tool }: { tool: PipelineTool | null }) {
  if (!tool) {
    return (
      <div className="tool-detail empty">
        Select a tool to see what it does.
      </div>
    );
  }

  const reason = !tool.available
    ? tool.error || `${tool.name} is not installed`
    : !tool.runnable
      ? `${tool.name} is installed, but this application does not run it yet.`
      : null;

  return (
    <div className="tool-detail">
      <div className="tool-detail-header">
        <h3>{tool.name}</h3>
        {tool.version && <span className="tool-version">v{tool.version}</span>}
      </div>

      {reason && <div className="tool-card-error">{reason}</div>}

      {tool.summary && <p className="tool-detail-summary">{tool.summary}</p>}

      {tool.strengths.length > 0 && (
        <ul className="tool-detail-strengths">
          {tool.strengths.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
