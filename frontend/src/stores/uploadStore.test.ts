import { describe, expect, it } from "vitest";

import {
  hasActiveUploads,
  installUnloadGuard,
  useUploadStore,
  type UnloadTarget,
  type UploadItem,
} from "./uploadStore";

function item(status: UploadItem["status"], id: string = status): UploadItem {
  return {
    id,
    projectId: "p1",
    filename: "reads.fastq",
    size: 100,
    loaded: 0,
    status,
  };
}

describe("hasActiveUploads", () => {
  it("is false with nothing in the list", () => {
    expect(hasActiveUploads([])).toBe(false);
  });

  it("counts uploading and queued as active", () => {
    expect(hasActiveUploads([item("uploading")])).toBe(true);
    expect(hasActiveUploads([item("queued")])).toBe(true);
  });

  it("does not count finished, failed or cancelled transfers", () => {
    // The guard must not warn about an upload that already stopped -- a prompt
    // on a tab with nothing in flight trains the user to dismiss it.
    for (const status of ["done", "error", "cancelled"] as const) {
      expect(hasActiveUploads([item(status)])).toBe(false);
    }
  });

  it("is true when one of several is still going", () => {
    expect(
      hasActiveUploads([item("done", "a"), item("uploading", "b"), item("error", "c")]),
    ).toBe(true);
  });
});

/** Records attach/detach instead of touching a real window (no jsdom here). */
function recordingTarget() {
  const listeners = new Set<() => void>();
  const target: UnloadTarget = {
    addEventListener: (_type, listener) => listeners.add(listener),
    removeEventListener: (_type, listener) => listeners.delete(listener),
  };
  return { target, count: () => listeners.size };
}

describe("installUnloadGuard", () => {
  it("attaches nothing while the tab is idle", () => {
    // The whole point is that closing an idle tab is not interrupted.
    useUploadStore.setState({ items: [] });
    const { target, count } = recordingTarget();
    const uninstall = installUnloadGuard(target);
    try {
      expect(count()).toBe(0);
    } finally {
      uninstall();
      useUploadStore.setState({ items: [] });
    }
  });

  it("attaches when an upload starts and detaches when it finishes", () => {
    useUploadStore.setState({ items: [] });
    const { target, count } = recordingTarget();
    const uninstall = installUnloadGuard(target);
    try {
      useUploadStore.setState({ items: [item("uploading")] });
      expect(count()).toBe(1);

      useUploadStore.setState({ items: [item("done")] });
      expect(count()).toBe(0);
    } finally {
      uninstall();
      useUploadStore.setState({ items: [] });
    }
  });

  it("does not stack a listener per upload", () => {
    // Every progress tick updates the store, so a naive attach-on-change would
    // add thousands of listeners over one multi-gigabyte transfer.
    useUploadStore.setState({ items: [] });
    const { target, count } = recordingTarget();
    const uninstall = installUnloadGuard(target);
    try {
      useUploadStore.setState({ items: [item("uploading", "a")] });
      useUploadStore.setState({
        items: [item("uploading", "a"), item("queued", "b")],
      });
      useUploadStore.setState({ items: [{ ...item("uploading", "a"), loaded: 50 }] });
      expect(count()).toBe(1);
    } finally {
      uninstall();
      useUploadStore.setState({ items: [] });
    }
  });

  it("attaches immediately when an upload is already running at install", () => {
    useUploadStore.setState({ items: [item("uploading")] });
    const { target, count } = recordingTarget();
    const uninstall = installUnloadGuard(target);
    try {
      expect(count()).toBe(1);
    } finally {
      uninstall();
      useUploadStore.setState({ items: [] });
    }
  });

  it("removes the listener when uninstalled mid-upload", () => {
    useUploadStore.setState({ items: [item("uploading")] });
    const { target, count } = recordingTarget();
    installUnloadGuard(target)();
    expect(count()).toBe(0);
    useUploadStore.setState({ items: [] });
  });
});
