import { create } from "zustand";
import type {
  AssemblyResolveResponse,
  SraResolveResponse,
} from "../api/types";

/**
 * The resolved accession, handed from the search screen to the confirm
 * screen.
 *
 * In a store rather than route state because `ncbiResolve` is a real network
 * call against NCBI: re-resolving on the confirm route would double every
 * lookup, and a reload there would fire a third. The confirm screen treats
 * an empty store as "go back" rather than resolving for itself.
 */
interface DownloadState {
  projectId: string | null;
  sra: SraResolveResponse | null;
  assembly: AssemblyResolveResponse | null;
  setProject: (id: string) => void;
  setResolved: (r: {
    sra: SraResolveResponse | null;
    assembly: AssemblyResolveResponse | null;
  }) => void;
}

const LAST_PROJECT_KEY = "bioflow.lastProject";

function rememberedProject(): string | null {
  try {
    return localStorage.getItem(LAST_PROJECT_KEY);
  } catch {
    return null;
  }
}

export const useDownloadStore = create<DownloadState>((set) => ({
  projectId: rememberedProject(),
  sra: null,
  assembly: null,
  setProject: (projectId) => {
    try {
      localStorage.setItem(LAST_PROJECT_KEY, projectId);
    } catch {
      // Not persisting the choice is survivable; it just asks again.
    }
    set({ projectId });
  },
  // Only ever one branch: ncbiResolve returns an assembly or a run list,
  // never both, and leaving a stale one beside a fresh one would show two
  // answers to one lookup.
  setResolved: ({ sra, assembly }) => set({ sra, assembly }),
}));
