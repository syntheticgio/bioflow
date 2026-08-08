import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { useProfileStore } from "../stores/profileStore";

/**
 * Subscribes to the server's SSE stream and invalidates affected queries.
 *
 * Events are treated as advisory signals, not as data: on receipt we refetch
 * rather than patching the cache from the payload, so a dropped message costs a
 * short delay instead of leaving the UI subtly wrong.
 */
export function useEvents() {
  const qc = useQueryClient();
  // Subscribed via the hook rather than `getState()` so that switching profiles
  // re-renders and tears the stream down; see the effect below.
  const profileId = useProfileStore((s) => s.current?.id);
  const [connected, setConnected] = useState(false);
  // Progress events can arrive several times a second per job. Coalescing them
  // stops a busy queue from triggering a refetch storm.
  const pending = useRef<Set<string>>(new Set());
  const timer = useRef<number | null>(null);

  useEffect(() => {
    // No profile, no stream. `Shell` only mounts once one is selected, so this
    // guards a state that should not occur -- which is exactly when it matters:
    // an empty `profile=` is a 400 from the backend, and `EventSource`
    // reconnects on error automatically, so the visible symptom would be a
    // reconnect loop rather than one failed request.
    if (!profileId) {
      setConnected(false);
      return;
    }

    // The profile travels as a query parameter, not a header, because
    // `EventSource` has no way to send custom headers -- that is a limitation of
    // the browser API, not an oversight here, and it is why this one path
    // differs from the other two.
    //
    // The backend resolves it the same way it resolves the header, then
    // subscribes this stream to two Redis channels: this profile's, and the
    // system channel for events that belong to the installation rather than to
    // any one library (storage faults, missing blobs).
    const source = new EventSource(
      `/api/v1/events?profile=${encodeURIComponent(profileId)}`,
    );

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
          // A workflow node's state is derived from its jobs, so a job
          // reaching a terminal state is exactly when the aggregate changes.
          // The event cannot say *which* workflow -- `run_ids` carries
          // PipelineRun ids and 13 of 22 node types create no run -- so this
          // invalidates rather than targets. Cheap: the queries only refetch
          // if something is mounted and watching. See #80.
          qc.invalidateQueries({ queryKey: ["workflow-runs"] });
          qc.invalidateQueries({ queryKey: ["workflow-run"] });
        }
        if (key === "objects") {
          qc.invalidateQueries({ queryKey: ["objects"] });
          qc.invalidateQueries({ queryKey: ["object"] });
        }
        // A terminal job writes its JobRunTiming row in the executor's
        // `finally`, *after* it publishes this event -- so this can arrive
        // slightly ahead of the record it announces. The debounce below and
        // a refetch on next mount both cover it; see the History tab's plan.
        if (key === "computations") {
          qc.invalidateQueries({ queryKey: ["object-computations"] });
        }
        if (key === "stats") qc.invalidateQueries({ queryKey: ["system", "stats"] });
        if (key === "shares") qc.invalidateQueries({ queryKey: ["shares"] });
        if (key === "projects") qc.invalidateQueries({ queryKey: ["projects"] });
        // Not keyed to one project id: this is a low-frequency event, and a
        // wasted refetch of a conversation the user is not looking at costs
        // nothing visible.
        if (key === "project-conversation")
          qc.invalidateQueries({ queryKey: ["project-conversation"] });
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
    // A terminal outcome, not enqueue/progress/cancel-requested -- those
    // cannot have produced a JobRunTiming row yet.
    const terminalJobEvents = new Set([
      "job.succeeded",
      "job.failed",
      "job.cancelled",
      "job.dead",
    ]);
    for (const name of jobEvents) {
      source.addEventListener(name, () => {
        schedule("jobs");
        schedule("stats");
        if (name !== "job.progress") schedule("objects");
        if (terminalJobEvents.has(name)) schedule("computations");
      });
    }

    source.addEventListener("share.offered", () => schedule("shares"));
    source.addEventListener("share.declined", () => schedule("shares"));
    // Accepting materializes new objects, and may lazily create the "Shared
    // with me" project -- without invalidating those too, the accepted file
    // is invisible in the explorer until a manual reload, which is exactly
    // the symptom this whole feature exists to prevent.
    source.addEventListener("share.accepted", () => {
      schedule("shares");
      schedule("objects");
      schedule("projects");
    });

    source.addEventListener("qa.answered", () => schedule("project-conversation"));

    return () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
      // Drop whatever the old stream had queued but not yet flushed. The refs
      // outlive the effect, so without this a switch could carry the previous
      // profile's pending invalidations into the new subscription -- harmless
      // today, since invalidation only forces a refetch, but it would mean the
      // new profile's first refetch was triggered by the old one's events.
      pending.current.clear();
      timer.current = null;
      source.close();
    };
    // Re-subscribing on `profileId` is the point: without it, switching profiles
    // would leave the stream open as the previous one.
  }, [qc, profileId]);

  return { connected };
}
