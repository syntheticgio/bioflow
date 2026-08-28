import { create } from "zustand";

export type MessageLevel = "info" | "success" | "warn" | "error";

export interface Message {
  id: string;
  level: MessageLevel;
  text: string;
  at: number;
}

interface MessageState {
  messages: Message[];
  latest: Message | null;
  /**
   * The most recent error that has not been dismissed. The footer pins this
   * above whatever `latest` is, so a background info/success notification
   * cannot overwrite the only on-screen report of a failure (#890). A newer
   * error replaces it; `dismissError` (or `clear`) drops it. It always stays
   * in `messages`, so a dismissed error remains retrievable in the log.
   */
  error: Message | null;
  push: (level: MessageLevel, text: string) => void;
  clear: () => void;
  dismissError: () => void;
}

const MAX_MESSAGES = 100;

/**
 * Footer message log. The footer bar shows the pinned error if there is one,
 * otherwise the newest entry. The full history lives in `messages`, rendered
 * by the Messages panel.
 */
export const useMessageStore = create<MessageState>((set) => ({
  messages: [],
  latest: null,
  error: null,
  push: (level, text) =>
    set((state) => {
      const message: Message = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        level,
        text,
        at: Date.now(),
      };
      return {
        messages: [message, ...state.messages].slice(0, MAX_MESSAGES),
        latest: message,
        error: level === "error" ? message : state.error,
      };
    }),
  clear: () => set({ messages: [], latest: null, error: null }),
  dismissError: () => set({ error: null }),
}));

export const notify = {
  info: (t: string) => useMessageStore.getState().push("info", t),
  success: (t: string) => useMessageStore.getState().push("success", t),
  warn: (t: string) => useMessageStore.getState().push("warn", t),
  error: (t: string) => useMessageStore.getState().push("error", t),
};
