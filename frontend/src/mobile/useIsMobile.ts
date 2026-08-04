import { useEffect, useState } from "react";

/**
 * Whether this is a phone-sized viewport, and whether the user has asked us
 * to stop caring.
 *
 * The breakpoint is a viewport query rather than a user-agent sniff: a
 * desktop browser dragged narrow is genuinely a narrow viewport, and the
 * escape hatch exists for anyone who disagrees.
 */
export const MOBILE_QUERY = "(max-width: 600px)";

const FORCE_DESKTOP_KEY = "bioflow.forceDesktop";

/**
 * Storage access that cannot throw. Safari's private mode raises on
 * setItem, and a redirect helper that throws takes down the first render of
 * the whole app rather than just losing a preference.
 */
export function forceDesktop(): boolean {
  try {
    return localStorage.getItem(FORCE_DESKTOP_KEY) === "1";
  } catch {
    return false;
  }
}

export function setForceDesktop(on: boolean): void {
  try {
    if (on) localStorage.setItem(FORCE_DESKTOP_KEY, "1");
    // Cleared rather than set to "false", so an absent key and a stored one
    // cannot come to mean different things.
    else localStorage.removeItem(FORCE_DESKTOP_KEY);
  } catch {
    // A browser that will not persist the preference still works; it just
    // forgets the choice on reload.
  }
}

/**
 * Live viewport match. Subscribes rather than reading once, so rotating a
 * phone or dragging a window re-evaluates instead of staying fixed at
 * whatever it was on first render.
 */
export function useIsMobile(): boolean {
  const [matches, setMatches] = useState(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia(MOBILE_QUERY).matches;
  });

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia(MOBILE_QUERY);
    const onChange = (e: MediaQueryListEvent) => setMatches(e.matches);
    mq.addEventListener("change", onChange);
    // Re-read on mount: the query can have changed between the initial
    // useState and the effect running.
    setMatches(mq.matches);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  return matches;
}
