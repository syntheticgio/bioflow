/**
 * Recently-opened-project tracking for the header shortcut list.
 *
 * localStorage rather than a backend field: this is a single-user local
 * tool, so there is nothing to sync across devices, and adding a
 * last_opened_at field plus a write-on-view endpoint call would be plumbing
 * with no payoff here.
 */

export interface RecentProject {
  id: string;
  name: string;
  visitedAt: number;
}

const STORAGE_KEY = "bioflow.recentProjects";

// More than the max 3 ever rendered, so one stale/deleted entry doesn't
// shrink the usable pool below what RecentProjects needs to fill 3 slots.
const MAX_ENTRIES = 5;

/**
 * Storage access that cannot throw. Safari private-mode raises on
 * setItem/getItem, and this is a display convenience, not a feature anyone
 * should lose the whole page over.
 */
export function getRecentProjects(): RecentProject[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed;
  } catch {
    return [];
  }
}

export function recordProjectVisit(id: string, name: string): void {
  try {
    const existing = getRecentProjects().filter((p) => p.id !== id);
    const next = [{ id, name, visitedAt: Date.now() }, ...existing].slice(
      0,
      MAX_ENTRIES,
    );
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Preference lost on this reload; the rest of the page still works.
  }
}
