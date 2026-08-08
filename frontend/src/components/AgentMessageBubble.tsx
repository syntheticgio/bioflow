import { useMemo } from "react";

interface ToolCallInfo {
  id: string;
  name: string;
  args: Record<string, unknown>;
  result?: string;
  ok?: boolean;
}

interface AgentMessageBubbleProps {
  role: "user" | "assistant";
  content: string;
  isStreaming?: boolean;
  toolCalls?: ToolCallInfo[];
}

function formatToolName(name: string): string {
  // Convert bioflow_ prefix to a more readable format
  if (name.startsWith("bioflow_")) {
    return name.replace("bioflow_", "").replace(/_/g, " ");
  }
  return name;
}

function formatToolArgs(args: Record<string, unknown>): string {
  return Object.entries(args)
    .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
    .join(", ");
}

export function AgentMessageBubble({
  role,
  content,
  isStreaming,
  toolCalls,
}: AgentMessageBubbleProps) {
  const bubbleClass = `agent-bubble agent-bubble-${role}`;

  const toolCallElements = useMemo(() => {
    if (!toolCalls || toolCalls.length === 0) return null;

    return (
      <div className="agent-tool-calls">
        {toolCalls.map((tc) => (
          <div key={tc.id} className={`agent-tool-call ${tc.ok !== undefined ? (tc.ok ? "agent-tool-call-done" : "agent-tool-call-error") : "agent-tool-call-pending"}`}>
            <span className="agent-tool-call-name">
              {tc.ok === undefined ? "🔍" : tc.ok ? "✅" : "❌"}
              {" "}
              {formatToolName(tc.name)}({formatToolArgs(tc.args)})
            </span>
            {tc.result && <span className="agent-tool-call-result">{tc.result}</span>}
          </div>
        ))}
      </div>
    );
  }, [toolCalls]);

  return (
    <div className={bubbleClass}>
      {role === "user" ? (
        <div className="agent-bubble-content">
          <div className="agent-bubble-text">{content}</div>
        </div>
      ) : (
        <div className="agent-bubble-content">
          <div className="agent-bubble-text">
            {content || (isStreaming ? "..." : "")}
            {isStreaming && <span className="agent-cursor" />}
          </div>
          {toolCallElements}
        </div>
      )}
    </div>
  );
}
