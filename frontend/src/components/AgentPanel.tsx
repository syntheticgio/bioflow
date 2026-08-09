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
  const [resumed, setResumed] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);

  // Track the current streaming message content
  const streamingContentRef = useRef<string>("");
  const currentToolCallsRef = useRef<ToolCallInfo[]>([]);

  // useAgentSSE wires this component's callbacks into whichever EventSource
  // is live at the time; because onConnectionChange is a fresh closure each
  // render, a callback firing asynchronously (agent_status arrives after a
  // network round trip) can run against a closure captured at an earlier
  // render than the one active when it fires. messagesRef always reflects
  // the current messages, so onConnectionChange below reads live state
  // instead of whatever `messages` looked like when its closure was made.
  const messagesRef = useRef<Message[]>(messages);
  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  // useAgentSSE reconnects its EventSource far more often than a real
  // network drop would suggest -- effectively on every render, since its
  // `connect` callback is rebuilt from fresh inline handlers each time (see
  // useAgentSSE.ts). Every reconnect re-emits agent_status as its first
  // event, so onConnectionChange's "did we land on a running agent with an
  // empty local transcript" check would otherwise also fire right after we
  // ourselves just cleared messages (New session), immediately relabeling a
  // brand-new, empty conversation as "resumed". suppressResumedRef is set
  // whenever we locally reset the conversation and cleared once messages
  // are confirmed empty, so only a connection that finds messages already
  // empty for a reason OTHER than our own reset -- i.e. a fresh page
  // load/drawer reopen -- counts as a resume.
  const suppressResumedRef = useRef(false);

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
        // Connected to an agent that was already running: it has a
        // conversation we are not showing (scrollback is not restored --
        // see issue #97), so say so rather than implying a blank agent.
        // Read messagesRef rather than `messages` -- this callback can run
        // against a closure from an earlier render than the current one.
        // Skip it if we ourselves just cleared the conversation: the SSE
        // hook reconnects aggressively (see comment on suppressResumedRef
        // above), and the reconnect that follows New session/restart would
        // otherwise see the empty transcript and relabel it as "resumed".
        if (messagesRef.current.length === 0 && !suppressResumedRef.current) {
          setResumed(true);
        }
      }
    },
  });

  const ask = useMutation({
    mutationFn: (q: string) => api.askAgent(projectId, q),
    onSuccess: (_data, q) => {
      // Optimistic: the user's message, plus an empty assistant bubble for
      // the stream to fill. The delta/tool handlers all target the last
      // message when it is isStreaming, so without this placeholder every
      // token is silently dropped.
      const userMsg: Message = { id: crypto.randomUUID(), role: "user", content: q };
      const assistantMsg: Message = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: "",
        isStreaming: true,
      };
      setMessages((prev) => [...prev, userMsg, assistantMsg]);
      setIsStreaming(true);
      setResumed(false);
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

  const newSession = useMutation({
    mutationFn: () => api.newAgentSession(projectId),
    onSuccess: () => {
      setMessages([]);
      setError(null);
      setIsStreaming(false);
      setResumed(false);
      streamingContentRef.current = "";
      currentToolCallsRef.current = [];
      // Deleting the session means any agent_status the SSE hook's next
      // (near-immediate) reconnect reports is describing the fresh, empty
      // session we just created, not an old one -- suppress relabeling it
      // "resumed" for a few seconds while that reconnect settles. Unlike
      // restart(), which keeps history server-side and should still say
      // "resumed" once it reconnects, this is the one action that makes an
      // empty transcript truly mean an empty conversation.
      suppressResumedRef.current = true;
      window.setTimeout(() => {
        suppressResumedRef.current = false;
      }, 3000);
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
            onClick={() => {
              if (confirm("Start a new session? This clears the agent's conversation history and can't be undone.")) {
                newSession.mutate();
              }
            }}
            title="New session (clears the agent's memory)"
            style={{ marginLeft: "auto" }}
            disabled={isStreaming}
          >
            🗑
          </button>
          <button
            type="button"
            className="icon-btn"
            onClick={() => restart.mutate()}
            title="Restart agent (keeps the conversation)"
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
              {resumed ? (
                <>
                  Resuming an earlier conversation — the agent still remembers it,
                  but the messages above are not shown. Ask a follow-up, or start
                  over with New session.
                </>
              ) : (
                <>
                  Ask the AI agent about your project data. It can run QC, trim, align,
                  and assemble pipelines, inspect jobs, and answer questions about your
                  files.
                </>
              )}
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
