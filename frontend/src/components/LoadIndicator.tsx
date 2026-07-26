import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

const STATE_COPY: Record<string, { label: string; color: string; help: string }> = {
  OPEN: {
    label: "Idle",
    color: "var(--success)",
    help: "All job classes are being admitted.",
  },
  THROTTLED: {
    label: "Busy",
    color: "var(--warn)",
    help: "System under load — maintenance and bulk jobs are deferred. Your own actions still run.",
  },
  CLOSED: {
    label: "Loaded",
    color: "var(--error)",
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

  if (!load) return null;

  const state = STATE_COPY[load.state] ?? STATE_COPY.OPEN;
  const cpu = load.cpu?.percent ?? 0;
  const mem = load.memory?.percent ?? 0;
  const lateCount = overdue?.overdue?.length ?? 0;

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
        <span
          className="dot"
          style={{ background: load.governor_active ? state.color : "var(--text-faint)" }}
        />
        <span>{state.label}</span>
        <span style={{ color: "var(--text-faint)" }}>
          {cpu.toFixed(0)}% cpu
        </span>
      </span>
    </div>
  );
}
