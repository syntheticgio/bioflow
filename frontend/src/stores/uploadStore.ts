import { create } from "zustand";

export type UploadStatus = "queued" | "uploading" | "done" | "error" | "cancelled";

export interface UploadItem {
  id: string;
  projectId: string;
  filename: string;
  size: number;
  loaded: number;
  status: UploadStatus;
  /** preparing | uploading | assembling | complete | deduplicated | failed */
  phase?: string;
  error?: string;
  controller?: AbortController;
  /** Server-side session, so a failed transfer can resume where it stopped. */
  sessionId?: string;
  /** Retained for resume. File handles do not survive a page reload. */
  file?: File;
}

interface UploadState {
  items: UploadItem[];
  add: (item: UploadItem) => void;
  update: (id: string, patch: Partial<UploadItem>) => void;
  remove: (id: string) => void;
  clearFinished: () => void;
}

/**
 * In-flight uploads live outside TanStack Query on purpose: File handles and
 * AbortControllers are non-serializable and have no server-side counterpart.
 */
export const useUploadStore = create<UploadState>((set) => ({
  items: [],
  add: (item) => set((s) => ({ items: [item, ...s.items] })),
  update: (id, patch) =>
    set((s) => ({
      items: s.items.map((i) => (i.id === id ? { ...i, ...patch } : i)),
    })),
  remove: (id) => set((s) => ({ items: s.items.filter((i) => i.id !== id) })),
  clearFinished: () =>
    set((s) => ({
      items: s.items.filter((i) => i.status === "uploading" || i.status === "queued"),
    })),
}));
