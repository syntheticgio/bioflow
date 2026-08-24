import { useState } from "react";

import {
  type AgentConnectionState,
  agentStatusDotClass,
  agentStatusLabel,
} from "../lib/agentStatus";

interface AgentPanelInputProps {
  onSend: (message: string) => void;
  disabled: boolean;
  state: AgentConnectionState;
}

export function AgentPanelInput({ onSend, disabled, state }: AgentPanelInputProps) {
  const [input, setInput] = useState("");

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (input.trim() && !disabled) {
        onSend(input);
        setInput("");
      }
    }
  };

  return (
    <div className="agent-input-area">
      <div className="agent-input-wrapper">
        <textarea
          className="agent-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask about your project..."
          disabled={disabled}
          rows={1}
        />
        <button
          className="agent-send-btn"
          onClick={() => {
            if (input.trim() && !disabled) {
              onSend(input);
              setInput("");
            }
          }}
          disabled={disabled || !input.trim()}
          title="Send message"
        >
          Send
        </button>
      </div>
      <div className="agent-input-status">
        <span className={`agent-status-dot ${agentStatusDotClass(state)}`} />
        <span className="agent-status-text">{agentStatusLabel(state)}</span>
      </div>
    </div>
  );
}
