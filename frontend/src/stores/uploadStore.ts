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

/** Whether anything is still transferring. */
export function hasActiveUploads(items: UploadItem[]): boolean {
  return items.some((i) => i.status === "uploading" || i.status === "queued");
}

/**
 * Warn before the tab closes while an upload is in flight.
 *
 * Closing mid-transfer aborts a multi-gigabyte chunked upload silently. Resume
 * exists, but it needs the original File re-selected -- handles do not survive
 * a reload -- and the user may no longer have it to hand. Nothing warned them
 * (#893).
 *
 * Registered against the store rather than from a component effect, because
 * the upload keeps running whether or not any particular view is mounted, and
 * a guard that depends on the uploads panel being open would miss exactly the
 * case that matters. The listener is added only while something is active, so
 * an idle tab closes without a prompt -- browsers ignore the prompt entirely
 * unless the user has interacted with the page, which starting an upload
 * necessarily involves.
 *
 * `target` is injectable only so the attach/detach rule can be tested: this
 * repo has no jsdom, so a test cannot reach a real `window`.
 */
export interface UnloadTarget {
  addEventListener(type: "beforeunload", listener: () => void): void;
  removeEventListener(type: "beforeunload", listener: () => void): void;
}

export function installUnloadGuard(
  target: UnloadTarget = window as unknown as UnloadTarget,
): () => void {
  const onBeforeUnload = ((e: BeforeUnloadEvent) => {
    e.preventDefault();
    // Assigning returnValue is what actually triggers the prompt in some
    // browsers; the string itself is ignored and a generic message is shown.
    e.returnValue = "";
  }) as () => void;

  let attached = false;
  const sync = (items: UploadItem[]) => {
    const active = hasActiveUploads(items);
    if (active && !attached) {
      target.addEventListener("beforeunload", onBeforeUnload);
      attached = true;
    } else if (!active && attached) {
      target.removeEventListener("beforeunload", onBeforeUnload);
      attached = false;
    }
  };

  sync(useUploadStore.getState().items);
  const unsubscribe = useUploadStore.subscribe((s) => sync(s.items));

  return () => {
    unsubscribe();
    if (attached) target.removeEventListener("beforeunload", onBeforeUnload);
  };
}
