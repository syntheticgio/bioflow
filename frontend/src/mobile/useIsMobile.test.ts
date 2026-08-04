import { describe, it, expect, beforeEach } from "vitest";
import { forceDesktop, setForceDesktop, MOBILE_QUERY } from "./useIsMobile";

/**
 * The hook itself needs a DOM to test and this repo has no jsdom setup, so
 * only the storage half is covered here. That is the half with a persistence
 * bug available to it; the matchMedia half is verified in a browser.
 */

class MemoryStorage {
  private m = new Map<string, string>();
  getItem(k: string) { return this.m.has(k) ? this.m.get(k)! : null; }
  setItem(k: string, v: string) { this.m.set(k, v); }
  removeItem(k: string) { this.m.delete(k); }
}

beforeEach(() => {
  (globalThis as { localStorage?: unknown }).localStorage = new MemoryStorage();
});

describe("MOBILE_QUERY", () => {
  it("matches the 600px breakpoint the design specifies", () => {
    expect(MOBILE_QUERY).toBe("(max-width: 600px)");
  });
});

describe("forceDesktop", () => {
  it("defaults to off", () => {
    expect(forceDesktop()).toBe(false);
  });

  it("round-trips through storage", () => {
    setForceDesktop(true);
    expect(forceDesktop()).toBe(true);
    setForceDesktop(false);
    expect(forceDesktop()).toBe(false);
  });

  it("clears the key rather than storing false", () => {
    // A stored "false" and an absent key must mean the same thing, so that
    // reading it can never depend on which one happens to be there.
    setForceDesktop(true);
    setForceDesktop(false);
    expect(localStorage.getItem("bioflow.forceDesktop")).toBe(null);
  });

  it("survives an unavailable localStorage rather than throwing", () => {
    // Safari private mode throws on setItem. A redirect helper that throws
    // takes the whole app down on first render.
    (globalThis as { localStorage?: unknown }).localStorage = {
      getItem() { throw new Error("denied"); },
      setItem() { throw new Error("denied"); },
      removeItem() { throw new Error("denied"); },
    };
    expect(() => setForceDesktop(true)).not.toThrow();
    expect(forceDesktop()).toBe(false);
  });
});
