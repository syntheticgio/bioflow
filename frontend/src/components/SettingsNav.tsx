import { Link, useLocation } from "react-router-dom";

/**
 * The section rail Settings did not have until Tools existed alongside AI.
 *
 * A plain nav rather than a `SettingsView`-owned tab state, because the two
 * pages are otherwise unrelated -- AI's own selection state (which provider is
 * open) has nothing to do with Tools', and forcing them through one parent's
 * `useState` would couple two screens that only share a shell. Routing already
 * *is* the state that matters here: which page is open.
 */
export function SettingsNav() {
  const { pathname } = useLocation();
  const onTools = pathname.startsWith("/settings/tools");

  return (
    <nav className="settings-section-nav">
      <Link
        to="/settings/ai"
        className={`settings-section-nav-item${onTools ? "" : " active"}`}
      >
        AI
      </Link>
      <Link
        to="/settings/tools"
        className={`settings-section-nav-item${onTools ? " active" : ""}`}
      >
        Tools
      </Link>
    </nav>
  );
}
