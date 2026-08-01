import { useState } from "react";
import { api, ApiRequestError } from "../api/client";
import type { Profile } from "../stores/profileStore";

interface Props {
  /** True only when `listProfiles()` came back empty, and passed straight
   *  through to `is_first_boot`. The picker owns this decision because the
   *  picker is what made the list call; this modal must not infer it, since
   *  the only thing it could infer from is its own emptiness. */
  isFirstBoot: boolean;
  /** The password comes back with the profile because the response cannot
   *  carry it -- `ProfileOut` returns `has_password`, never the value. The
   *  picker needs it to enter the profile it just created without prompting
   *  for something the user typed moments ago. */
  onCreated: (profile: Profile, password?: string) => void;
  onClose: () => void;
}

/**
 * The form that creates a profile.
 *
 * Errors are shown inline rather than through `notify`: the picker renders
 * instead of the shell, so the toast host is not mounted, and a duplicate
 * username needs to land next to the field that caused it anyway.
 *
 * The plan asked for an expandable Details section (name, institution,
 * research areas) posting as a `details` object. `ProfileCreate` in
 * `backend/app/api/v1/schemas.py` takes only `username`, `password`, `email`
 * and `is_first_boot` -- there is no `details` on the create route, and
 * FastAPI would drop the key without complaint. Rather than ship three inputs
 * whose values silently disappear, they are left out until the backend grows
 * somewhere to put them.
 */
export function AddProfileModal({ isFirstBoot, onCreated, onClose }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!username.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const profile = await api.createProfile({
        username: username.trim(),
        // Omitted rather than sent empty: the backend treats a falsy password
        // as "no password", but an empty string in the payload reads like a
        // password was chosen and is one refactor away from being hashed.
        ...(password ? { password } : {}),
        ...(email.trim() ? { email: email.trim() } : {}),
        ...(isFirstBoot ? { is_first_boot: true } : {}),
      });
      onCreated(profile, password || undefined);
    } catch (err) {
      // 409 (duplicate username) and 422 (validation, including a second
      // first-boot claim) both carry a usable `message` from the backend, so
      // there is nothing to improve on by branching on the code here.
      setError(
        err instanceof ApiRequestError ? err.message : "Could not create the profile",
      );
      setBusy(false);
    }
  };

  return (
    <div
      className="modal-backdrop"
      onClick={isFirstBoot ? undefined : onClose}
      onKeyDown={(e) => e.key === "Escape" && !isFirstBoot && onClose()}
    >
      <div className="modal profile-modal" onClick={(e) => e.stopPropagation()}>
        <h2>{isFirstBoot ? "Create your profile" : "New profile"}</h2>

        <form onSubmit={submit}>
          <div className="modal-body">
            {isFirstBoot && (
              <p className="profile-modal-note">
                This first profile takes ownership of everything already in
                your library — nothing is moved or copied.
              </p>
            )}

            <label htmlFor="ap-username">Username</label>
            <input
              id="ap-username"
              autoFocus
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="e.g. jtorcivia"
            />

            <label htmlFor="ap-password">Password</label>
            <input
              id="ap-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Optional"
            />
            <p className="profile-modal-hint">
              A speed bump, not security — it stops you opening the wrong
              profile by accident. The API stays unauthenticated either way.
            </p>

            <label htmlFor="ap-email">Email</label>
            <input
              id="ap-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="Optional"
            />

            {error && <div className="error-box">{error}</div>}
          </div>

          <div className="modal-actions">
            {/* Cancelling out of first boot would leave the picker with
                nothing to show and no way back, so the only exit from that
                state is creating the profile. */}
            {!isFirstBoot && (
              <button type="button" className="btn" onClick={onClose}>
                Cancel
              </button>
            )}
            <button
              type="submit"
              className="btn primary"
              disabled={!username.trim() || busy}
            >
              {busy ? "Creating…" : "Create profile"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
