import { createSHA256 } from "hash-wasm";

// Independent of the upload session's chunk size (chosen server-side, and
// unknown until a session exists) -- this only needs to keep memory bounded
// while feeding hash-wasm's incremental hasher.
const HASH_READ_CHUNK = 16 * 1024 * 1024;

export interface HashWorkerRequest {
  file: File;
}

export type HashWorkerResponse =
  | { type: "progress"; bytesHashed: number }
  | { type: "done"; digest: string }
  | { type: "error"; message: string };

self.onmessage = async (e: MessageEvent<HashWorkerRequest>) => {
  const { file } = e.data;
  try {
    const hasher = await createSHA256();
    hasher.init();

    let offset = 0;
    while (offset < file.size) {
      const end = Math.min(offset + HASH_READ_CHUNK, file.size);
      const buffer = await file.slice(offset, end).arrayBuffer();
      hasher.update(new Uint8Array(buffer));
      offset = end;
      postMessage({ type: "progress", bytesHashed: offset } satisfies HashWorkerResponse);
    }

    const digest = hasher.digest("hex");
    postMessage({ type: "done", digest } satisfies HashWorkerResponse);
  } catch (e) {
    const message = e instanceof Error ? e.message : "Hashing failed";
    postMessage({ type: "error", message } satisfies HashWorkerResponse);
  }
};
