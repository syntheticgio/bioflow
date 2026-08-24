/**
 * What the agent drawer's status dot and banner should say.
 *
 * Two independent facts drive this, and the panel used to conflate them:
 *
 *  - `streamOpen`  -- is the SSE stream to /agent/events alive? This is the
 *    only one that can be "broken"; if it is false the drawer is genuinely
 *    cut off and reconnecting is the right advice.
 *  - `processRunning` -- does a pi process exist for this (profile, project)?
 *    It is false for perfectly healthy reasons: the process spawns lazily on
 *    the first /ask and is reaped once idle. A configured, working provider
 *    still reports false until the user sends something.
 *
 * Collapsing the second into the first is what made a live, verified
 * connection read as "Disconnected from agent" the moment nobody had asked
 * anything yet (issue #814).
 */
export type AgentConnectionState = "disconnected" | "idle" | "running";

export function agentConnectionState(
  streamOpen: boolean,
  processRunning: boolean,
): AgentConnectionState {
  if (!streamOpen) return "disconnected";
  return processRunning ? "running" : "idle";
}

/** Short text beside the status dot. */
export function agentStatusLabel(state: AgentConnectionState): string {
  switch (state) {
    case "disconnected":
      return "Disconnected";
    case "idle":
      return "Ready";
    case "running":
      return "Connected";
  }
}

/** CSS modifier for the status dot. "idle" is not an error state. */
export function agentStatusDotClass(state: AgentConnectionState): string {
  switch (state) {
    case "disconnected":
      return "agent-status-disconnected";
    case "idle":
      return "agent-status-idle";
    case "running":
      return "agent-status-connected";
  }
}

/**
 * The error banner text, or null when there is nothing wrong. Only a dead
 * stream earns one -- an idle agent is the normal resting state.
 */
export function agentStatusError(state: AgentConnectionState): string | null {
  return state === "disconnected"
    ? "Disconnected from agent. Click restart to reconnect."
    : null;
}
