import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { AgentConversationTurn } from "../api/types";
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

/** Map a saved API turn into the panel's internal Message type. */
function _mapTurn(t: AgentConversationTurn): Message {
  return {
    id: crypto.randomUUID(),
    role: t.role,
    content: t.content,
    toolCalls: t.tool_calls
      ? t.tool_calls.map((tc) => ({
          id: tc.id,
          name: tc.name,
          args: tc.args,
          result: tc.result ?? undefined,
          ok: tc.ok ?? undefined,
        }))
      : undefined,
  };
}

export function AgentPanel({
  projectId,
  onClose,
}: {
  projectId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [messages, setMessages] = useState<Message[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [initialized, setInitialized] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);

  // Load saved conversation on mount (issue #97). The query is keyed by
  // projectId, so TanStack Query caches it across close/reopen cycles --
  // a quick reopen reads from cache instantly, a slower one refetches.
  const { data: savedConversation } = useQuery({
    queryKey: ["agent-conversation", projectId],
    queryFn: () => api.getAgentConversation(projectId),
    staleTime: 5 * 60 * 1000,
  });

  // Initialize messages from the saved conversation once, on first load.
  useEffect(() => {
    if (!initialized && savedConversation?.turns) {
      setMessages(savedConversation.turns.map(_mapTurn));
      setInitialized(true);
    }
  }, [savedConversation, initialized]);

  const [showSettings, setShowSettings] = useState(false);
  const [draftPrompt, setDraftPrompt] = useState("");

  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId),
  });

  // Load the saved value into the draft whenever the editor is opened.
  useEffect(() => {
    if (showSettings) {
      setDraftPrompt(project.data?.agent_system_prompt ?? "");
    }
  }, [showSettings, project.data?.agent_system_prompt]);

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
      // The backend SSE generator persists the full assistant turn (text +
      // tool calls) to ProjectConversation. No frontend save needed here.
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
      // Optimistic: add the user message immediately. The backend's /ask
      // handler also persists it to ProjectConversation for durability.
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
      setInitialized(false);
      streamingContentRef.current = "";
      currentToolCallsRef.current = [];
    },
  });

  const clear = useMutation({
    mutationFn: () => api.clearAgentConversation(projectId),
    onSuccess: () => {
      setMessages([]);
      setError(null);
      setIsStreaming(false);
      setInitialized(false);
      streamingContentRef.current = "";
      currentToolCallsRef.current = [];
      // Invalidate so a reopen re-fetches the now-empty conversation.
      qc.invalidateQueries({ queryKey: ["agent-conversation", projectId] });
    },
  });

  const savePrompt = useMutation({
    mutationFn: (value: string) =>
      api.updateProject(projectId, { agent_system_prompt: value }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      setShowSettings(false);
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
            onClick={() => setShowSettings((v) => !v)}
            title="Agent instructions"
            style={{ marginLeft: "auto" }}
          >
            ⚙️
          </button>
          <button
            type="button"
            className="icon-btn"
            onClick={() => restart.mutate()}
            title="Restart agent"
          >
            🔄
          </button>
          <button
            type="button"
            className="icon-btn"
            onClick={() => clear.mutate()}
            title="Clear conversation"
            disabled={messages.length === 0 && !isStreaming}
          >
            🗑
          </button>
          <button type="button" className="icon-btn" onClick={onClose} title="Close">
            ×
          </button>
        </div>

        {showSettings ? (
          <div className="agent-drawer-body agent-prompt-editor">
            <label className="agent-prompt-label" htmlFor="agent-prompt">
              Extra instructions for this project
            </label>
            <p className="agent-prompt-help">
              Added on top of the agent's built-in project knowledge — it always
              knows which project it is in and which tools it has. Saving
              restarts the agent on your next message, which clears the current
              conversation.
            </p>
            <textarea
              id="agent-prompt"
              className="agent-prompt-textarea"
              value={draftPrompt}
              maxLength={4000}
              onChange={(e) => setDraftPrompt(e.target.value)}
              placeholder="e.g. Always say which tool you used. Assume paired-end Illumina reads."
            />
            <div className="agent-prompt-actions">
              <span className="agent-prompt-count">{draftPrompt.length} / 4000</span>
              <button
                type="button"
                className="btn"
                onClick={() => setDraftPrompt("")}
                disabled={draftPrompt.length === 0}
              >
                Reset to default
              </button>
              <button
                type="button"
                className="btn primary"
                onClick={() => savePrompt.mutate(draftPrompt)}
                disabled={savePrompt.isPending}
              >
                {savePrompt.isPending ? "Saving…" : "Save"}
              </button>
            </div>
            {savePrompt.isError && (
              <div className="agent-prompt-error">Could not save. Try again.</div>
            )}
          </div>
        ) : (
          <>
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
          </>
        )}
      </div>
    </>
  );
}
