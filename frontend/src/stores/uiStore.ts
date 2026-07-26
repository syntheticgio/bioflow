import { create } from "zustand";

export type Selection =
  | { kind: "none" }
  | { kind: "project"; id: string }
  | { kind: "object"; id: string };

interface UiState {
  selection: Selection;
  panelWidth: number;
  select: (s: Selection) => void;
  clearSelection: () => void;
  setPanelWidth: (w: number) => void;
}

/**
 * Client-only UI state. Navigation itself lives in the URL (see routes), not
 * here, so refresh and back behave correctly and breadcrumbs stay derivable.
 */
export const useUiStore = create<UiState>((set) => ({
  selection: { kind: "none" },
  panelWidth: 340,
  select: (selection) => set({ selection }),
  clearSelection: () => set({ selection: { kind: "none" } }),
  setPanelWidth: (panelWidth) => set({ panelWidth }),
}));
