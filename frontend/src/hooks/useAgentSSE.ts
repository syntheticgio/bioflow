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
   * Fired only from the backend's `agent_status` event -- the actual signal
   * for whether a pi agent process exists, as opposed to onConnectionChange
   * which also fires from the SSE connection merely opening (onopen), before
   * any real status is known. Use this, not onConnectionChange, for anything
   * that needs to know whether the agent process itself is running (e.g.
   * deciding whether a conversation is being "resumed").
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

    source.addEventListener("agent_status", (event: Event) => {
      try {
        const msgEvent = event as MessageEvent;
        const data = JSON.parse(msgEvent.data);
        setConnected(data.running);
        onConnectionChangeRef.current(data.running);
        onAgentStatusRef.current?.(data.running);
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

  return { connected, connect, disconnect };
}
