import { create } from "zustand";
import { persist } from "zustand/middleware";

export interface Profile {
  id: string;
  username: string;
  email: string | null;
  display: { emoji: string; colour: string };
  details: Record<string, unknown>;
  has_password: boolean;
}

interface ProfileState {
  /** The profile whose data every request is scoped to, or null for "show the
   *  picker". This is the one piece of client state the API cannot recover
   *  from the URL: the backend issues no token and sets no cookie, so the id
   *  held here IS the session. */
  current: Profile | null;
  /** Whether to skip the picker on the next launch. Persisted alongside the
   *  profile because "stay logged in" is meaningless without remembering who. */
  autoLogin: boolean;
  setCurrent: (p: Profile) => void;
  logout: () => void;
  setAutoLogin: (v: boolean) => void;
}

/**
 * The selected profile, persisted to localStorage.
 *
 * Unlike `uiStore`, this one persists: the selection has to survive a reload,
 * because there is no server-side session to restore it from. Selecting a
 * profile returns no token and sets no cookie -- the client simply starts
 * sending the id, so losing it on refresh would drop the user back at the
 * picker every time.
 */
export const useProfileStore = create<ProfileState>()(
  persist(
    (set) => ({
      current: null,
      autoLogin: false,
      setCurrent: (current) => set({ current }),
      // Clears the profile but keeps `autoLogin` as the user set it: logging
      // out to switch profiles should not silently also turn the preference
      // off, since the next selection is what re-arms it.
      logout: () => set({ current: null }),
      setAutoLogin: (autoLogin) => set({ autoLogin }),
    }),
    { name: "bioflow-profile" },
  ),
);
