import { describe, expect, it } from "vitest";

import {
  predictButtonAction,
  predictButtonLabel,
} from "./ProteinStructureTab";
import type { PredictionState } from "../api/types";

const STATES: (PredictionState | "loading")[] = [
  "loading",
  "not_started",
  "running",
  "completed",
  "failed",
];

describe("predictButtonAction", () => {
  it("shows the existing prediction rather than starting another", () => {
    // The bug: the label said "View prediction" while the handler called
    // startProteinPrediction unconditionally, so clicking it re-queued the
    // expensive job instead of showing the result already delivered (#884).
    expect(predictButtonAction("completed")).toBe("show");
  });

  it("starts a prediction when there is none, and on retry after failure", () => {
    expect(predictButtonAction("not_started")).toBe("start");
    expect(predictButtonAction("failed")).toBe("start");
  });

  it("does nothing while a prediction is in flight or being checked", () => {
    expect(predictButtonAction("running")).toBe("none");
    expect(predictButtonAction("loading")).toBe("none");
  });

  it("never starts a job from a state that already has one", () => {
    // The property, rather than the cases: any state that is not a fresh or
    // failed prediction must not enqueue work.
    for (const state of STATES) {
      if (state === "not_started" || state === "failed") continue;
      expect(predictButtonAction(state)).not.toBe("start");
    }
  });
});

describe("predictButtonLabel", () => {
  it("labels each state", () => {
    expect(predictButtonLabel("loading", null)).toBe("Checking…");
    expect(predictButtonLabel("not_started", null)).toBe("Predict structure");
    expect(predictButtonLabel("failed", null)).toBe("Retry prediction");
    expect(predictButtonLabel("completed", null)).toBe("View prediction");
  });

  it("shows rounded progress while running, and copes without it", () => {
    expect(predictButtonLabel("running", { pct: 42.4 })).toBe("Predicting… (42%)");
    expect(predictButtonLabel("running", null)).toBe("Predicting…");
  });

  it("promises 'view' only where the action actually shows something", () => {
    // The pairing is the point: a label saying "view" while the action is
    // "start" is exactly the defect this file exists for. Asserted across
    // every state so a new one cannot be added to only one of the two
    // functions.
    for (const state of STATES) {
      const label = predictButtonLabel(state, null);
      if (label === "View prediction") {
        expect(predictButtonAction(state)).toBe("show");
      }
      if (predictButtonAction(state) === "start") {
        expect(label).toMatch(/Predict structure|Retry prediction/);
      }
    }
  });
});
