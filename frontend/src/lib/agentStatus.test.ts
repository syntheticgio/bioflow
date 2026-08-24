import { describe, expect, it } from "vitest";

import {
  agentConnectionState,
  agentStatusDotClass,
  agentStatusError,
  agentStatusLabel,
} from "./agentStatus";

describe("agentConnectionState", () => {
  it("is disconnected only when the SSE stream is down", () => {
    expect(agentConnectionState(false, false)).toBe("disconnected");
    expect(agentConnectionState(false, true)).toBe("disconnected");
  });

  it("is idle -- not disconnected -- when the stream is up but no process has spawned", () => {
    expect(agentConnectionState(true, false)).toBe("idle");
  });

  it("is running when the stream is up and a pi process exists", () => {
    expect(agentConnectionState(true, true)).toBe("running");
  });
});

describe("agentStatusError", () => {
  it("reports an error only for a dead stream", () => {
    expect(agentStatusError("disconnected")).toMatch(/Disconnected/);
  });

  it("says nothing for an agent that simply has not been asked anything yet", () => {
    expect(agentStatusError("idle")).toBeNull();
    expect(agentStatusError("running")).toBeNull();
  });
});

describe("agentStatusLabel", () => {
  it("never calls a live-but-idle stream disconnected", () => {
    expect(agentStatusLabel("idle")).toBe("Ready");
    expect(agentStatusLabel("running")).toBe("Connected");
    expect(agentStatusLabel("disconnected")).toBe("Disconnected");
  });
});

describe("agentStatusDotClass", () => {
  it("gives idle its own dot rather than the disconnected one", () => {
    expect(agentStatusDotClass("idle")).not.toBe(agentStatusDotClass("disconnected"));
    expect(agentStatusDotClass("running")).toBe("agent-status-connected");
  });
});
