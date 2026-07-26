import { api } from "../api/client";
import type { UploadSessionInfo } from "../api/types";

export const CHUNK_CONCURRENCY = 4;
const MAX_CHUNK_RETRIES = 3;

/** SHA-256 of a chunk, hex encoded. Used for per-chunk integrity checking. */
export async function sha256Hex(data: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export interface ChunkedUploadCallbacks {
  onProgress?: (uploadedBytes: number) => void;
  onSession?: (session: UploadSessionInfo) => void;
  onPhase?: (phase: string) => void;
}

/**
 * Upload a File in chunks, resuming if a session already exists.
 *
 * Only the chunks the server reports as missing are sent, so an interrupted
 * transfer resumes where it stopped rather than restarting — the difference
 * between losing seconds and losing an hour on a 30 GB file.
 */
export async function uploadFileChunked(
  projectId: string,
  file: File,
  signal: AbortSignal,
  cb: ChunkedUploadCallbacks = {},
  existingSessionId?: string,
): Promise<{ objectId: string; dedup: boolean }> {
  let session: UploadSessionInfo;

  if (existingSessionId) {
    session = await api.getUpload(existingSessionId);
  } else {
    cb.onPhase?.("preparing");
    const created = await api.createUpload({
      project_id: projectId,
      filename: file.name,
      total_size: file.size,
    });
    // The server already had these exact bytes; nothing to transfer.
    if (created.dedup_hit && created.object) {
      cb.onProgress?.(file.size);
      return { objectId: created.object.id, dedup: true };
    }
    session = created.session!;
  }

  cb.onSession?.(session);
  cb.onPhase?.("uploading");

  const { chunk_size: chunkSize, total_chunks: totalChunks } = session;
  const missing = new Set(
    session.missing_chunks.length > 0
      ? session.missing_chunks
      : Array.from({ length: totalChunks }, (_, i) => i),
  );

  // Bytes already on the server count toward progress immediately, so a resumed
  // upload does not appear to start over.
  let uploaded = (totalChunks - missing.size) * chunkSize;
  uploaded = Math.min(uploaded, file.size);
  cb.onProgress?.(uploaded);

  const queue = Array.from(missing).sort((a, b) => a - b);
  let cursor = 0;

  const worker = async () => {
    while (cursor < queue.length) {
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      const index = queue[cursor++];

      const start = index * chunkSize;
      const end = Math.min(start + chunkSize, file.size);
      const blob = file.slice(start, end);
      const buffer = await blob.arrayBuffer();
      const digest = await sha256Hex(buffer);

      let attempt = 0;
      for (;;) {
        try {
          await api.putChunk(session.id, index, buffer, digest, signal);
          break;
        } catch (e) {
          if (signal.aborted) throw e;
          attempt += 1;
          // Transient network faults are expected on long transfers; only give
          // up after several tries, with backoff.
          if (attempt >= MAX_CHUNK_RETRIES) throw e;
          await new Promise((r) => setTimeout(r, 500 * attempt));
        }
      }

      uploaded += end - start;
      cb.onProgress?.(Math.min(uploaded, file.size));
    }
  };

  await Promise.all(
    Array.from({ length: Math.min(CHUNK_CONCURRENCY, queue.length || 1) }, worker),
  );

  cb.onPhase?.("assembling");
  const done = await api.completeUpload(session.id);
  return { objectId: done.object_id, dedup: false };
}
