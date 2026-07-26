import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { api } from "../api/client";
import { uploadFileChunked } from "../lib/upload";
import { notify } from "../stores/messageStore";
import { useUploadStore } from "../stores/uploadStore";

// Files upload one at a time; chunks within a file go 4-wide. More concurrent
// files would multiply heavy readers on the FUSE mount without improving
// aggregate throughput.
const FILE_CONCURRENCY = 1;

export function useUploads(projectId: string | undefined) {
  const qc = useQueryClient();
  const { add, update } = useUploadStore();

  const refresh = useCallback(() => {
    if (projectId) qc.invalidateQueries({ queryKey: ["objects", projectId] });
    qc.invalidateQueries({ queryKey: ["projects"] });
    qc.invalidateQueries({ queryKey: ["project", projectId] });
    qc.invalidateQueries({ queryKey: ["system", "stats"] });
  }, [projectId, qc]);

  const uploadOne = useCallback(
    async (file: File, sessionId?: string) => {
      if (!projectId) return;

      const id = `${Date.now()}-${file.name}-${Math.random().toString(36).slice(2, 7)}`;
      const controller = new AbortController();

      add({
        id,
        projectId,
        filename: file.name,
        size: file.size,
        loaded: 0,
        status: "uploading",
        phase: "preparing",
        controller,
        file,
      });

      try {
        const result = await uploadFileChunked(
          projectId,
          file,
          controller.signal,
          {
            onProgress: (loaded) => update(id, { loaded }),
            onPhase: (phase) => update(id, { phase }),
            onSession: (s) => update(id, { sessionId: s.id }),
          },
          sessionId,
        );

        update(id, {
          status: "done",
          loaded: file.size,
          phase: result.dedup ? "deduplicated" : "complete",
        });
        notify.success(
          result.dedup
            ? `${file.name} already stored — deduplicated, no transfer needed`
            : `Uploaded ${file.name}`,
        );
        refresh();
      } catch (e) {
        const aborted = e instanceof DOMException && e.name === "AbortError";
        const message = e instanceof Error ? e.message : "Upload failed";
        update(id, {
          status: aborted ? "cancelled" : "error",
          error: aborted ? undefined : message,
          // Keep the phase so a resumable failure is distinguishable from a
          // fatal one in the tray.
          phase: aborted ? "cancelled" : "failed",
        });
        if (aborted) notify.warn(`Cancelled ${file.name}`);
        else notify.error(`${file.name}: ${message}`);
      }
    },
    [projectId, add, update, refresh],
  );

  const uploadFiles = useCallback(
    async (files: File[]) => {
      for (let i = 0; i < files.length; i += FILE_CONCURRENCY) {
        await Promise.all(
          files.slice(i, i + FILE_CONCURRENCY).map((f) => uploadOne(f)),
        );
      }
    },
    [uploadOne],
  );

  /** Resume a previously interrupted transfer using the same File handle. */
  const resumeUpload = useCallback(
    async (itemId: string) => {
      const item = useUploadStore.getState().items.find((i) => i.id === itemId);
      if (!item?.file) {
        notify.warn("Re-select the file to resume this upload");
        return;
      }
      await uploadOne(item.file, item.sessionId);
    },
    [uploadOne],
  );

  const registerInPlace = useCallback(
    async (path: string) => {
      if (!projectId) return;
      try {
        const res = await api.registerInPlace(projectId, path);
        notify.success(`Registered ${res.object.name} — hashing in background`);
        refresh();
      } catch (e) {
        notify.error(e instanceof Error ? e.message : "Registration failed");
      }
    },
    [projectId, refresh],
  );

  return { uploadFiles, resumeUpload, registerInPlace };
}
