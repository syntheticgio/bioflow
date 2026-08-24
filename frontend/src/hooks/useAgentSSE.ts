import { useCallback, useEffect, useRef, useState } from "react";

import { useProfileStore } from "../stores/profileStore";

export interface AgentSSEOptions {
  projectId: string;
  onMessageDelta: (kind: "text" | "thinking", contentIndex: number, delta: string) => void;
  onToolCall: (id: string, name: string, args: Record<string, unknown>) => void;
  onToolResult: (id: string, name: string, ok: boolean, summary?: string) => void;
  onDone: () => void;
  onError: (message: string) => void;
  onConnectionChange: (connected: boolean) => void;
  /**
   * Fired only from the backend's `agent_status` event -- the signal for
   * whether a pi agent process exists, as opposed to onConnectionChange,
   * which reports the SSE stream itself. A healthy stream with no process
   * yet (nobody has asked anything; the idle reaper took the last one) is
   * the normal resting state, so use this, not onConnectionChange, for
   * anything about the agent process (e.g. deciding whether a conversation
   * is being "resumed").
   */
  onAgentStatus?: (running: boolean) => void;
}

export function useAgentSSE({
  projectId,
  onMessageDelta,
  onToolCall,
  onToolResult,
  onDone,
  onError,
  onConnectionChange,
  onAgentStatus,
}: AgentSSEOptions) {
  const profileId = useProfileStore((s) => s.current?.id);
  const [connected, setConnected] = useState(false);
  // Whether a pi agent process currently exists for this (profile, project).
  // Distinct from `connected` -- see the agent_status listener below.
  const [running, setRunning] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectAttempts = useRef(0);
  const maxReconnectDelay = 30000; // 30 seconds max

  // Store callbacks in refs so the EventSource event handlers always read the
  // latest version without the connect() function changing on every render.
  const onMessageDeltaRef = useRef(onMessageDelta);
  const onToolCallRef = useRef(onToolCall);
  const onToolResultRef = useRef(onToolResult);
  const onDoneRef = useRef(onDone);
  const onErrorRef = useRef(onError);
  const onConnectionChangeRef = useRef(onConnectionChange);
  const onAgentStatusRef = useRef(onAgentStatus);

  useEffect(() => { onMessageDeltaRef.current = onMessageDelta; });
  useEffect(() => { onToolCallRef.current = onToolCall; });
  useEffect(() => { onToolResultRef.current = onToolResult; });
  useEffect(() => { onDoneRef.current = onDone; });
  useEffect(() => { onErrorRef.current = onError; });
  useEffect(() => { onConnectionChangeRef.current = onConnectionChange; });
  useEffect(() => { onAgentStatusRef.current = onAgentStatus; });

  const disconnect = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    reconnectAttempts.current = 0;
    if (sourceRef.current) {
      sourceRef.current.close();
      sourceRef.current = null;
    }
    setConnected(false);
    setRunning(false);
    onConnectionChangeRef.current(false);
  }, []);

  const connect = useCallback(() => {
    if (!profileId) return;
    if (sourceRef.current) return; // already connected or connecting

    const source = new EventSource(
      `/api/v1/projects/${projectId}/agent/events?profile=${encodeURIComponent(profileId)}`,
    );

    source.onopen = () => {
      setConnected(true);
      onConnectionChangeRef.current(true);
      reconnectAttempts.current = 0;
    };

    source.onerror = () => {
      setConnected(false);
      // A dropped stream tells us nothing about the process, but the next
      // agent_status will; until then, don't claim a running agent.
      setRunning(false);
      onConnectionChangeRef.current(false);
      source.close();
      sourceRef.current = null;

      // Exponential backoff: 1s, 2s, 4s, 8s, 16s, 30s max
      const delay = Math.min(1000 * Math.pow(2, reconnectAttempts.current), maxReconnectDelay);
      reconnectAttempts.current += 1;
      reconnectTimerRef.current = window.setTimeout(() => {
        connect();
      }, delay);
    };

    // agent_status answers "does a pi process exist?", which is NOT the same
    // question as "is this stream alive?". A process spawns lazily on the
    // first /ask and is reaped once idle, so running:false is the normal
    // resting state of a perfectly healthy connection. Reporting it through
    // setConnected/onConnectionChange is what made a live, verified provider
    // read as "Disconnected from agent" before anyone had asked anything
    // (issue #814). The stream's own onopen/onerror own `connected`; this
    // event only feeds onAgentStatus and the separate `running` flag.
    source.addEventListener("agent_status", (event: Event) => {
      try {
        const msgEvent = event as MessageEvent;
        const data = JSON.parse(msgEvent.data);
        setRunning(Boolean(data.running));
        onAgentStatusRef.current?.(Boolean(data.running));
      } catch {
        // Ignore malformed data
      }
    });

    source.addEventListener("message_delta", (event: Event) => {
      try {
        const msgEvent = event as MessageEvent;
        const data = JSON.parse(msgEvent.data);
        onMessageDeltaRef.current(data.kind, data.contentIndex, data.delta);
      } catch {
        // Ignore malformed data
      }
    });

    source.addEventListener("tool_call", (event: Event) => {
      try {
        const msgEvent = event as MessageEvent;
        const data = JSON.parse(msgEvent.data);
        onToolCallRef.current(data.id, data.name, data.args);
      } catch {
        // Ignore malformed data
      }
    });

    source.addEventListener("tool_result", (event: Event) => {
      try {
        const msgEvent = event as MessageEvent;
        const data = JSON.parse(msgEvent.data);
        onToolResultRef.current(data.id, data.name, data.ok, data.summary);
      } catch {
        // Ignore malformed data
      }
    });

    source.addEventListener("done", () => {
      onDoneRef.current();
    });

    source.addEventListener("error", (event: Event) => {
      try {
        const msgEvent = event as MessageEvent;
        const data = JSON.parse(msgEvent.data);
        onErrorRef.current(data.message);
      } catch {
        onErrorRef.current("An unknown error occurred");
      }
    });

    sourceRef.current = source;
  }, [
    profileId,
    projectId,
  ]);

  useEffect(() => {
    // Auto-connect when the component mounts and profileId is available
    if (profileId) {
      connect();
    }

    return () => {
      disconnect();
    };
  }, [profileId, connect, disconnect]);

  return { connected, running, connect, disconnect };
}
