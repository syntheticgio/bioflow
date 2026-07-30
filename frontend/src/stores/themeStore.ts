import { create } from "zustand";

export type Theme = "classic" | "broadsheet";

const STORAGE_KEY = "bioflow.theme";

/** Read the saved choice. Anything unrecognized (or a browser that refuses
 *  localStorage) falls back to the theme the app has always had. */
export function readStoredTheme(): Theme {
  try {
    return localStorage.getItem(STORAGE_KEY) === "broadsheet"
      ? "broadsheet"
      : "classic";
  } catch {
    return "classic";
  }
}

/** Broadsheet is a whole-page skin, so it hangs off <html> rather than a
 *  React root: CSS can then reach body and the scroll container, which live
 *  outside the tree. Exported because main.tsx calls it before first paint,
 *  ahead of React, to avoid a flash of the wrong theme. */
export function applyTheme(theme: Theme) {
  document.documentElement.classList.toggle(
    "theme-broadsheet",
    theme === "broadsheet",
  );
}

interface ThemeState {
  theme: Theme;
  setTheme: (t: Theme) => void;
  toggleTheme: () => void;
}

export const useThemeStore = create<ThemeState>((set, get) => ({
  theme: readStoredTheme(),

  setTheme: (theme) => {
    applyTheme(theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // Private-mode browsers reject writes. The theme still applies for
      // this session; only persistence is lost, which is not worth failing on.
    }
    set({ theme });
  },

  toggleTheme: () =>
    get().setTheme(get().theme === "broadsheet" ? "classic" : "broadsheet"),
}));
