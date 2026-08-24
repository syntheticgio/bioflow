import { describe, expect, it } from "vitest";
import pkg from "../package.json";
import { LAUNCHER_VERSION, LAUNCHER_VERSION_LABEL } from "./version";

describe("LAUNCHER_VERSION", () => {
  // The regression this guards (#808): the displayed version was a literal
  // "0.1.0" that nothing bumped, so it sat four releases stale. Asserting
  // against package.json rather than a literal means the test does not itself
  // need editing on every release -- which is how the last one went stale.
  it("is the version ops/release.sh bumps in package.json", () => {
    expect(LAUNCHER_VERSION).toBe(pkg.version);
  });

  it("is a real version string, not a build-time placeholder", () => {
    expect(LAUNCHER_VERSION).toMatch(/^\d+\.\d+\.\d+(-(alpha|beta))?$/);
  });

  it("keeps the pre-release suffix tauri.conf.json has to strip", () => {
    // tauri.conf.json carries the core version only, so it cannot be the
    // source here: an alpha build would claim to be the release.
    if (pkg.version.includes("-")) {
      expect(LAUNCHER_VERSION).toContain("-");
    }
  });

  it("labels the version for a status line", () => {
    expect(LAUNCHER_VERSION_LABEL).toBe(`Launcher ${pkg.version}`);
  });
});
