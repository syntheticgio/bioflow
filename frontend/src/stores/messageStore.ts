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
  push: (level: MessageLevel, text: string) => void;
  clear: () => void;
}

const MAX_MESSAGES = 100;

/** Footer message log. The newest entry is what the footer bar shows. */
export const useMessageStore = create<MessageState>((set) => ({
  messages: [],
  latest: null,
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
      };
    }),
  clear: () => set({ messages: [], latest: null }),
}));

export const notify = {
  info: (t: string) => useMessageStore.getState().push("info", t),
  success: (t: string) => useMessageStore.getState().push("success", t),
  warn: (t: string) => useMessageStore.getState().push("warn", t),
  error: (t: string) => useMessageStore.getState().push("error", t),
};
