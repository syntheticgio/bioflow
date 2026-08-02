import { api } from "../api/client";
import type { UploadSessionInfo } from "../api/types";
import type { HashWorkerRequest, HashWorkerResponse } from "./hashWorker";
import HashWorker from "./hashWorker?worker";

export const CHUNK_CONCURRENCY = 4;
const MAX_CHUNK_RETRIES = 3;

/** SHA-256 of a chunk, hex encoded. Used for per-chunk integrity checking. */
export async function sha256Hex(data: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * Whole-file SHA-256, computed off the main thread.
 *
 * `crypto.subtle.digest` takes one complete buffer, which is unusable for a
 * file this app expects to run to 30 GB -- so this hashes incrementally in a
 * worker via hash-wasm instead, feeding it fixed-size slices the same way
 * chunks are already read for upload.
 */
export function hashFile(
  file: File,
  signal: AbortSignal,
  onProgress?: (bytesHashed: number) => void,
): Promise<string> {
  return new Promise((resolve, reject) => {
    const worker = new HashWorker();
    const cleanup = () => worker.terminate();

    const onAbort = () => {
      cleanup();
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal.addEventListener("abort", onAbort);

    worker.onmessage = (e: MessageEvent<HashWorkerResponse>) => {
      const msg = e.data;
      if (msg.type === "progress") {
        onProgress?.(msg.bytesHashed);
      } else if (msg.type === "done") {
        signal.removeEventListener("abort", onAbort);
        cleanup();
        resolve(msg.digest);
      } else {
        signal.removeEventListener("abort", onAbort);
        cleanup();
        reject(new Error(msg.message));
      }
    };
    worker.onerror = (e) => {
      signal.removeEventListener("abort", onAbort);
      cleanup();
      reject(new Error(e.message || "Hashing failed"));
    };

    worker.postMessage({ file } satisfies HashWorkerRequest);
  });
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
    cb.onPhase?.("hashing");
    const clientSha256 = await hashFile(file, signal, (bytesHashed) =>
      cb.onProgress?.(bytesHashed),
    );

    cb.onPhase?.("preparing");
    const created = await api.createUpload({
      project_id: projectId,
      filename: file.name,
      total_size: file.size,
      client_sha256: clientSha256,
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
