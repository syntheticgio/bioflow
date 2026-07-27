import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { useMessageStore } from "../stores/messageStore";
import { QueuePanel } from "./QueuePanel";

export function Footer({ streamConnected = false }: { streamConnected?: boolean }) {
  const latest = useMessageStore((s) => s.latest);
  const [queueOpen, setQueueOpen] = useState(false);

  const { data, isError } = useQuery({
    queryKey: ["system", "stats"],
    queryFn: api.systemStats,
    refetchInterval: 15000,
  });

  const storageOk = data?.storage.ok ?? false;
  const connected = !isError;
  const queue = data?.queue;

  return (
    <>
      {queueOpen && <QueuePanel onClose={() => setQueueOpen(false)} />}
    <footer className="footer">
      <span className={`footer-message ${latest?.level ?? ""}`}>
        {latest?.text ?? "Ready"}
      </span>

      {queue && (
        <button
          type="button"
          className="footer-link"
          title="Show running and queued jobs"
          onClick={() => setQueueOpen((o) => !o)}
        >
          {queue.ready + queue.delayed} queued · {queue.running} running
        </button>
      )}

      {data && (
        <span title={data.storage.detail}>
          {data.counts.projects} projects · {data.counts.objects} files
        </span>
      )}

      <span
        title={
          !connected
            ? "Cannot reach the API"
            : !storageOk
              ? data?.storage.detail
              : streamConnected
                ? `Live updates active. Storage: ${data?.storage.path}`
                : "Live updates disconnected — falling back to polling"
        }
        style={{ display: "flex", alignItems: "center", gap: 5 }}
      >
        <span className={`dot ${connected && storageOk ? "ok" : "bad"}`} />
        {!connected
          ? "Offline"
          : !storageOk
            ? "Storage error"
            : streamConnected
              ? "Live"
              : "Connected"}
      </span>
    </footer>
    </>
  );
}
