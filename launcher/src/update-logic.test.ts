import { describe, expect, it } from "vitest";
import { shouldPollForUpdates, updateAffordance } from "./update-logic";

describe("updateAffordance", () => {
  it("hides the button in release mode when nothing is newer", () => {
    expect(
      updateAffordance({ bioflowTag: "latest", developerRepo: null, updateAvailable: false }),
    ).toEqual({ kind: "hidden" });
  });

  it("offers the update in release mode when something is newer", () => {
    expect(
      updateAffordance({ bioflowTag: "latest", developerRepo: null, updateAvailable: true }),
    ).toEqual({ kind: "available" });
  });

  it("suppresses in developer mode and points at Rebuild", () => {
    expect(
      updateAffordance({
        bioflowTag: "latest",
        developerRepo: "/home/me/bioflow",
        updateAvailable: false,
      }),
    ).toEqual({
      kind: "suppressed",
      reason: "Developer mode — use Rebuild in Settings.",
    });
  });

  it("suppresses in developer mode even if a check somehow reported true", () => {
    // The backend already returns false here, but the button must not depend
    // on that: a stale poll result from before a mode switch must not flash
    // an Update button at a local build.
    expect(
      updateAffordance({
        bioflowTag: "latest",
        developerRepo: "/home/me/bioflow",
        updateAvailable: true,
      }).kind,
    ).toBe("suppressed");
  });

  it("suppresses on a pinned alpha and names the tag", () => {
    expect(
      updateAffordance({ bioflowTag: "0.3.0-alpha", developerRepo: null, updateAvailable: false }),
    ).toEqual({
      kind: "suppressed",
      reason: "Pinned to 0.3.0-alpha — change version in Settings.",
    });
  });

  it("suppresses on a pinned beta and names the tag", () => {
    expect(
      updateAffordance({ bioflowTag: "0.4.0-beta", developerRepo: null, updateAvailable: false }),
    ).toEqual({
      kind: "suppressed",
      reason: "Pinned to 0.4.0-beta — change version in Settings.",
    });
  });

  it("developer mode wins over a pinned tag", () => {
    expect(
      updateAffordance({
        bioflowTag: "0.3.0-alpha",
        developerRepo: "/home/me/bioflow",
        updateAvailable: false,
      }).kind,
    ).toBe("suppressed");
    expect(
      updateAffordance({
        bioflowTag: "0.3.0-alpha",
        developerRepo: "/home/me/bioflow",
        updateAvailable: false,
      }),
    ).toEqual({
      kind: "suppressed",
      reason: "Developer mode — use Rebuild in Settings.",
    });
  });
});

describe("shouldPollForUpdates", () => {
  it("polls only in release mode", () => {
    expect(shouldPollForUpdates("latest", null)).toBe(true);
    expect(shouldPollForUpdates("latest", "/home/me/bioflow")).toBe(false);
    expect(shouldPollForUpdates("0.3.0-alpha", null)).toBe(false);
    expect(shouldPollForUpdates("0.4.0-beta", null)).toBe(false);
  });
});
