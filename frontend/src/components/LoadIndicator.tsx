import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

/** Copy only. The dot's colour for each state lives in CSS (`.dot.state-*`),
 *  so a theme can retune one state without touching the shared status tokens. */
const STATE_COPY: Record<string, { label: string; help: string }> = {
  OPEN: {
    label: "Idle",
    help: "All job classes are being admitted.",
  },
  THROTTLED: {
    label: "Busy",
    help: "System under load — maintenance and bulk jobs are deferred. Your own actions still run.",
  },
  CLOSED: {
    label: "Loaded",
    help: "System heavily loaded — background work is paused. Your own actions still run.",
  },
};

export function LoadIndicator() {
  const { data: load } = useQuery({
    queryKey: ["system", "load"],
    queryFn: api.systemLoad,
    refetchInterval: 5000,
  });

  const { data: overdue } = useQuery({
    queryKey: ["schedules", "overdue"],
    queryFn: api.overdueSchedules,
    refetchInterval: 60000,
  });

  // Already cached by the header and footer, so this shares their data rather
  // than adding a third poll of the same endpoint.
  const { data: stats } = useQuery({
    queryKey: ["system", "stats"],
    queryFn: api.systemStats,
    refetchInterval: 15000,
  });

  if (!load) return null;

  const state = STATE_COPY[load.state] ?? STATE_COPY.OPEN;
  const cpu = load.cpu?.percent ?? 0;
  const mem = load.memory?.percent ?? 0;
  const lateCount = overdue?.overdue?.length ?? 0;
  const queue = stats?.queue;

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
      {lateCount > 0 && (
        <span
          className="badge error"
          title={
            "Maintenance has not run recently:\n" +
            overdue!.overdue
              .map((o) => `${o.name}: ${Math.round(o.seconds_overdue / 60)} min overdue`)
              .join("\n")
          }
        >
          maintenance overdue
        </span>
      )}

      <span
        className="load-indicator"
        title={
          `${state.help}\n` +
          `CPU ${cpu.toFixed(0)}%  ·  Memory ${mem.toFixed(0)}%` +
          (load.ramping ? "\nRamping back up after high load." : "") +
          (load.governor_active ? "" : "\nGovernor not yet reporting.")
        }
      >
        {/* The state rides as a class and CSS owns the colour, so a theme can
            retune one state without an !important and without moving
            --success/--warn/--error, which the quality chart and badges read.
            An inactive governor reports no state and keeps the default. */}
        <span
          className={`dot ${
            load.governor_active ? `state-${load.state.toLowerCase()}` : ""
          }`}
        />
        <span>{state.label}</span>
        {/* .load-metric rather than an inline colour: the state label leads and
            the measurements sit a step quieter behind it, but a theme that sets
            the whole strip in one ink needs to be able to say so. */}
        <span className="load-metric">{cpu.toFixed(0)}% cpu</span>
        {/* Queue depth belongs beside the load state: together they say
            whether the system is busy and whether there is work waiting. */}
        {queue && (
          <>
            <span className="load-metric">
              {queue.ready + queue.delayed} queued
            </span>
            <span className="load-metric">{queue.running} running</span>
          </>
        )}
      </span>
    </div>
  );
}
