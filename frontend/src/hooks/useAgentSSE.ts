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
}

export function useAgentSSE({
  projectId,
  onMessageDelta,
  onToolCall,
  onToolResult,
  onDone,
  onError,
  onConnectionChange,
}: AgentSSEOptions) {
  const profileId = useProfileStore((s) => s.current?.id);
  const [connected, setConnected] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectAttempts = useRef(0);
  const maxReconnectDelay = 30000; // 30 seconds max

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
    onConnectionChange(false);
  }, [onConnectionChange]);

  const connect = useCallback(() => {
    if (!profileId) return;
    if (sourceRef.current) return; // already connected or connecting

    const source = new EventSource(
      `/api/v1/projects/${projectId}/agent/events?profile=${encodeURIComponent(profileId)}`,
    );

    source.onopen = () => {
      setConnected(true);
      onConnectionChange(true);
      reconnectAttempts.current = 0;
    };

    source.onerror = () => {
      setConnected(false);
      onConnectionChange(false);
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
        onConnectionChange(data.running);
      } catch {
        // Ignore malformed data
      }
    });

    source.addEventListener("message_delta", (event: Event) => {
      try {
        const msgEvent = event as MessageEvent;
        const data = JSON.parse(msgEvent.data);
        onMessageDelta(data.kind, data.contentIndex, data.delta);
      } catch {
        // Ignore malformed data
      }
    });

    source.addEventListener("tool_call", (event: Event) => {
      try {
        const msgEvent = event as MessageEvent;
        const data = JSON.parse(msgEvent.data);
        onToolCall(data.id, data.name, data.args);
      } catch {
        // Ignore malformed data
      }
    });

    source.addEventListener("tool_result", (event: Event) => {
      try {
        const msgEvent = event as MessageEvent;
        const data = JSON.parse(msgEvent.data);
        onToolResult(data.id, data.name, data.ok, data.summary);
      } catch {
        // Ignore malformed data
      }
    });

    source.addEventListener("done", () => {
      onDone();
    });

    source.addEventListener("error", (event: Event) => {
      try {
        const msgEvent = event as MessageEvent;
        const data = JSON.parse(msgEvent.data);
        onError(data.message);
      } catch {
        onError("An unknown error occurred");
      }
    });

    sourceRef.current = source;
  }, [profileId, projectId, onMessageDelta, onToolCall, onToolResult, onDone, onError, onConnectionChange]);

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
