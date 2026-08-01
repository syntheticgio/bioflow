import { useEffect, useRef, useState } from "react";
import { api, ApiRequestError } from "../api/client";
import type { Profile } from "../stores/profileStore";
import { useProfileStore } from "../stores/profileStore";
import { AddProfileModal } from "./AddProfileModal";

/**
 * The startup screen: choose a profile, or create the first one.
 *
 * This renders *instead of* the shell rather than over it, because every view
 * in the shell fetches on mount and every one of those routes 400s without a
 * profile. Nothing here uses react-query: these are the only calls that work
 * before a profile exists, and caching a profile list that the very next
 * action invalidates buys nothing.
 */
export function ProfilePicker() {
  const [profiles, setProfiles] = useState<Profile[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  /** The profile whose password prompt is open. Only one at a time: the
   *  prompt replaces that tile's label, so two open at once would be two
   *  fields competing for the same Enter key. */
  const [pending, setPending] = useState<Profile | null>(null);
  const [password, setPassword] = useState("");
  const [authError, setAuthError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const passwordRef = useRef<HTMLInputElement>(null);

  const setCurrent = useProfileStore((s) => s.setCurrent);
  const autoLogin = useProfileStore((s) => s.autoLogin);
  const setAutoLogin = useProfileStore((s) => s.setAutoLogin);

  useEffect(() => {
    api
      .listProfiles()
      .then(setProfiles)
      .catch((e: Error) => setLoadError(e.message));
  }, []);

  const enter = async (profile: Profile, pw?: string) => {
    setBusy(true);
    setAuthError(null);
    try {
      // `selectProfile` returns the profile with `last_used_at` freshly
      // stamped, so the response is preferred over the tile's copy.
      const entered = await api.selectProfile(profile.id, pw);
      setCurrent(entered);
    } catch (err) {
      if (err instanceof ApiRequestError && err.code === "wrong_profile_password") {
        // Stay exactly where we are. Tearing the prompt down would read as
        // the click having failed rather than the password being wrong, and
        // it would cost the user the tile they had already picked.
        setAuthError(err.message);
        setPassword("");
        passwordRef.current?.focus();
      } else {
        setAuthError(err instanceof Error ? err.message : "Could not enter that profile");
      }
      setBusy(false);
    }
  };

  const click = (profile: Profile) => {
    if (profile.has_password) {
      setPending(profile);
      setPassword("");
      setAuthError(null);
    } else {
      void enter(profile);
    }
  };

  /**
   * `is_first_boot` is legitimate only while the list is genuinely empty.
   * Deriving it from the list we just fetched -- rather than from a flag set
   * once and carried -- means a stale tab that sat on this screen while
   * another created the first profile cannot claim adoption a second time.
   * The backend refuses that anyway; this stops us asking.
   */
  const firstBoot = profiles !== null && profiles.length === 0;

  useEffect(() => {
    if (firstBoot) setAdding(true);
  }, [firstBoot]);

  const created = (profile: Profile, password?: string) => {
    setAdding(false);
    setProfiles((prev) => (prev ? [...prev, profile] : [profile]));
    // Enter with the password the form just collected, not without one.
    // `selectProfile` checks it like any other entry, so dropping it here
    // sends the empty string and bounces the user off a "wrong password" for
    // the password they typed thirty seconds ago -- on first boot that is the
    // very first thing the app would ever say to them.
    void enter(profile, password);
  };

  if (loadError) {
    return (
      <div className="picker">
        <div className="picker-inner">
          <h1>BioFlow</h1>
          <div className="error-box">Could not load profiles: {loadError}</div>
        </div>
      </div>
    );
  }

  if (profiles === null) {
    return (
      <div className="picker">
        <div className="picker-inner">
          <div className="picker-loading">Loading profiles…</div>
        </div>
      </div>
    );
  }

  return (
    <div className="picker">
      <div className="picker-inner">
        <h1>BioFlow</h1>

        {firstBoot ? (
          /* A lone `+` on an empty grid does not say what a profile is or why
             one is needed, and this is the only screen a brand-new install
             ever sees. The modal opens over this text, which stays as the
             explanation behind it. */
          <p className="picker-welcome">
            A profile keeps one person's projects, uploads and pipeline runs
            separate from another's on this machine. Create the first one to
            get started — it will take ownership of anything already in your
            library.
          </p>
        ) : (
          <p className="picker-subtitle">Choose a profile</p>
        )}

        {!firstBoot && (
          <div className="picker-grid">
            {profiles.map((p) => (
              <div key={p.id} className="picker-tile-wrap">
                <button
                  type="button"
                  className={`picker-tile${pending?.id === p.id ? " active" : ""}`}
                  style={{ ["--tile-accent" as string]: p.display.colour }}
                  onClick={() => click(p)}
                  disabled={busy}
                >
                  <span className="picker-emoji">{p.display.emoji}</span>
                  <span className="picker-name">{p.username}</span>
                  {p.has_password && <span className="picker-lock">🔒</span>}
                </button>

                {pending?.id === p.id && (
                  <form
                    className="picker-password"
                    onSubmit={(e) => {
                      e.preventDefault();
                      if (!busy) void enter(p, password);
                    }}
                  >
                    <input
                      ref={passwordRef}
                      type="password"
                      autoFocus
                      value={password}
                      placeholder="Password"
                      onChange={(e) => setPassword(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Escape") {
                          setPending(null);
                          setAuthError(null);
                        }
                      }}
                    />
                    <button type="submit" className="btn primary" disabled={busy}>
                      {busy ? "…" : "Enter"}
                    </button>
                  </form>
                )}
              </div>
            ))}

            <button
              type="button"
              className="picker-tile picker-add"
              onClick={() => setAdding(true)}
            >
              <span className="picker-emoji">+</span>
              <span className="picker-name">New profile</span>
            </button>
          </div>
        )}

        {authError && <div className="error-box picker-error">{authError}</div>}

        {!firstBoot && (
          <label className="picker-autologin">
            <input
              type="checkbox"
              checked={autoLogin}
              onChange={(e) => setAutoLogin(e.target.checked)}
            />
            Skip this screen next time
          </label>
        )}
      </div>

      {adding && (
        <AddProfileModal
          isFirstBoot={firstBoot}
          onCreated={created}
          onClose={() => setAdding(false)}
        />
      )}
    </div>
  );
}
