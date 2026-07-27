import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

/**
 * Subscribes to the server's SSE stream and invalidates affected queries.
 *
 * Events are treated as advisory signals, not as data: on receipt we refetch
 * rather than patching the cache from the payload, so a dropped message costs a
 * short delay instead of leaving the UI subtly wrong.
 */
export function useEvents() {
  const qc = useQueryClient();
  const [connected, setConnected] = useState(false);
  // Progress events can arrive several times a second per job. Coalescing them
  // stops a busy queue from triggering a refetch storm.
  const pending = useRef<Set<string>>(new Set());
  const timer = useRef<number | null>(null);

  useEffect(() => {
    const source = new EventSource("/api/v1/events");

    const flush = () => {
      timer.current = null;
      const keys = Array.from(pending.current);
      pending.current.clear();
      for (const key of keys) {
        if (key === "jobs") {
          qc.invalidateQueries({ queryKey: ["jobs"] });
          // Singular too: IngestProgress and the activity view watch one job
          // at a time, and without this a running job's detail never refreshes.
          qc.invalidateQueries({ queryKey: ["job"] });
        }
        if (key === "objects") {
          qc.invalidateQueries({ queryKey: ["objects"] });
          qc.invalidateQueries({ queryKey: ["object"] });
        }
        if (key === "stats") qc.invalidateQueries({ queryKey: ["system", "stats"] });
      }
    };

    const schedule = (key: string) => {
      pending.current.add(key);
      if (timer.current === null) {
        timer.current = window.setTimeout(flush, 500);
      }
    };

    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);

    const jobEvents = [
      "job.enqueued",
      "job.succeeded",
      "job.failed",
      "job.cancelled",
      "job.dead",
      "job.progress",
      "job.cancel_requested",
    ];
    for (const name of jobEvents) {
      source.addEventListener(name, () => {
        schedule("jobs");
        schedule("stats");
        if (name !== "job.progress") schedule("objects");
      });
    }

    return () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
      source.close();
    };
  }, [qc]);

  return { connected };
}
