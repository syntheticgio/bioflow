import { formatDate, formatRelative } from "../lib/format";
import { useMessageStore, type Message, type MessageLevel } from "../stores/messageStore";

/**
 * The message log the footer has always maintained but never rendered (#890):
 * every `notify.*` call lands here, newest first, capped at 100. The footer
 * line shows the newest message (or a pinned error); this panel is where the
 * rest is retrieved. Dismissing or clearing never loses a message from this
 * list's perspective -- `clear` empties the log on purpose, and that is the
 * only way rows leave.
 */
export function MessagesPanel({ onClose }: { onClose: () => void }) {
  const messages = useMessageStore((s) => s.messages);
  const clear = useMessageStore((s) => s.clear);

  return (
    <>
      {/* Click-away, matching the Queue/Agent panel pattern. */}
      <div className="messages-backdrop" onClick={onClose} />
      <div className="messages-panel">
        <div className="messages-panel-head">
          <span className="panel-title">Messages</span>
          <span className="messages-panel-counts">
            {messages.length} in log
          </span>
          <button
            type="button"
            className="messages-clear"
            onClick={clear}
            disabled={messages.length === 0}
            title="Remove every message from the log"
          >
            Clear
          </button>
          <button
            type="button"
            className="icon-btn"
            onClick={onClose}
            title="Close"
          >
            ×
          </button>
        </div>

        {messages.length === 0 ? (
          <div className="messages-empty">No messages yet.</div>
        ) : (
          <div className="messages-rows">
            {messages.map((m) => (
              <MessageRow key={m.id} message={m} />
            ))}
          </div>
        )}
      </div>
    </>
  );
}

const LEVEL_MARK: Record<MessageLevel, string> = {
  error: "✕",
  warn: "!",
  success: "✓",
  info: "·",
};

function MessageRow({ message }: { message: Message }) {
  // `at` is epoch ms; the formatters take an ISO string.
  const at = new Date(message.at).toISOString();
  return (
    <div className={`message-row ${message.level}`}>
      <span className="message-row-marker" aria-hidden="true">
        {LEVEL_MARK[message.level]}
      </span>
      <span className="message-row-text">{message.text}</span>
      <span className="message-row-time" title={formatDate(at)}>
        {formatRelative(at)}
      </span>
    </div>
  );
}
