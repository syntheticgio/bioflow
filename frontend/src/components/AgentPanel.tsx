import { useMutation } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useAgentSSE } from "../hooks/useAgentSSE";
import { AgentMessageBubble } from "./AgentMessageBubble";
import { AgentPanelInput } from "./AgentPanelInput";

interface ToolCallInfo {
  id: string;
  name: string;
  args: Record<string, unknown>;
  result?: string;
  ok?: boolean;
}

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  isStreaming?: boolean;
  toolCalls?: ToolCallInfo[];
}

export function AgentPanel({
  projectId,
  onClose,
}: {
  projectId: string;
  onClose: () => void;
}) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);

  // Track the current streaming message content
  const streamingContentRef = useRef<string>("");
  const currentToolCallsRef = useRef<ToolCallInfo[]>([]);

  const { connected } = useAgentSSE({
    projectId,
    onMessageDelta: (_kind, _contentIndex, delta) => {
      if (_kind === "text") {
        streamingContentRef.current += delta;
        setMessages((prev) => {
          const updated = [...prev];
          const lastIdx = updated.length - 1;
          if (lastIdx >= 0 && updated[lastIdx].isStreaming) {
            updated[lastIdx] = {
              ...updated[lastIdx],
              content: streamingContentRef.current,
            };
          }
          return updated;
        });
      }
    },
    onToolCall: (id, name, args) => {
      currentToolCallsRef.current.push({ id, name, args });
      setMessages((prev) => {
        const updated = [...prev];
        const lastIdx = updated.length - 1;
        if (lastIdx >= 0 && updated[lastIdx].isStreaming) {
          updated[lastIdx] = {
            ...updated[lastIdx],
            toolCalls: [...(updated[lastIdx].toolCalls ?? []), { id, name, args }],
          };
        }
        return updated;
      });
    },
    onToolResult: (_id, _name, ok, summary) => {
      setMessages((prev) => {
        const updated = [...prev];
        const lastIdx = updated.length - 1;
        if (lastIdx >= 0 && updated[lastIdx].isStreaming) {
          const toolCalls = (updated[lastIdx].toolCalls ?? []).map((tc) =>
            tc.id === _id ? { ...tc, result: summary, ok } : tc,
          );
          updated[lastIdx] = { ...updated[lastIdx], toolCalls };
        }
        return updated;
      });
    },
    onDone: () => {
      setIsStreaming(false);
      setMessages((prev) => {
        const updated = [...prev];
        const lastIdx = updated.length - 1;
        if (lastIdx >= 0 && updated[lastIdx].isStreaming) {
          updated[lastIdx] = { ...updated[lastIdx], isStreaming: false };
        }
        return updated;
      });
      streamingContentRef.current = "";
      currentToolCallsRef.current = [];
    },
    onError: (message) => {
      setIsStreaming(false);
      setError(message);
      streamingContentRef.current = "";
      currentToolCallsRef.current = [];
    },
    onConnectionChange: (isConnected) => {
      if (!isConnected) {
        setError("Disconnected from agent. Click restart to reconnect.");
      } else {
        setError(null);
      }
    },
  });

  const ask = useMutation({
    mutationFn: (q: string) => api.askAgent(projectId, q),
    onSuccess: () => {
      // Optimistic: add the user message immediately
      const userMsg: Message = { id: crypto.randomUUID(), role: "user", content: "" };
      setMessages((prev) => [...prev, userMsg]);
      setIsStreaming(true);
      streamingContentRef.current = "";
      currentToolCallsRef.current = [];
    },
  });

  const restart = useMutation({
    mutationFn: () => api.restartAgent(projectId),
    onSuccess: () => {
      setMessages([]);
      setError(null);
      setIsStreaming(false);
      streamingContentRef.current = "";
      currentToolCallsRef.current = [];
    },
  });

  useEffect(() => {
    bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight });
  }, [messages, isStreaming]);

  const submit = (message: string) => {
    const q = message.trim();
    if (!q || isStreaming) return;
    ask.mutate(q);
  };

  return (
    <>
      <div className="queue-backdrop" onClick={onClose} />
      <div className="agent-drawer">
        <div className="queue-panel-head">
          <span className="panel-title">AI Agent</span>
          {connected ? (
            <span className="agent-status-dot agent-status-connected" title="Connected" />
          ) : (
            <span className="agent-status-dot agent-status-disconnected" title="Disconnected" />
          )}
          {error && (
            <span className="agent-error-badge">{error}</span>
          )}
          <button
            type="button"
            className="icon-btn"
            onClick={() => restart.mutate()}
            title="Restart agent"
            style={{ marginLeft: "auto" }}
          >
            🔄
          </button>
          <button type="button" className="icon-btn" onClick={onClose} title="Close">
            ×
          </button>
        </div>

        <div className="agent-drawer-body" ref={bodyRef}>
          {messages.length === 0 && !isStreaming ? (
            <div className="queue-empty">
              Ask the AI agent about your project data. It can run QC, trim, align, and
              assemble pipelines, inspect jobs, and answer questions about your files.
            </div>
          ) : (
            messages.map((msg) => (
              <AgentMessageBubble
                key={msg.id}
                role={msg.role}
                content={msg.content}
                isStreaming={msg.isStreaming}
                toolCalls={msg.toolCalls}
              />
            ))
          )}
          {isStreaming && messages.length === 0 && (
            <div className="agent-loading">Starting agent...</div>
          )}
        </div>

        <AgentPanelInput
          onSend={submit}
          disabled={isStreaming}
          connected={connected}
        />
      </div>
    </>
  );
}
