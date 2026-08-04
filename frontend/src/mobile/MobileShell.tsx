import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { setForceDesktop } from "./useIsMobile";
import "../styles/mobile.css";

/**
 * The mobile chrome: a title bar and a two-item tab bar around whichever
 * route is showing.
 *
 * Separate from the desktop Shell rather than a variant of it, because that
 * shell carries a resizable splitter, the DetailPanel and the UploadTray --
 * none of which have a place on a phone, and all of which would need
 * conditionals threaded through them to pretend otherwise.
 */
export function MobileShell() {
  const navigate = useNavigate();

  const useDesktop = () => {
    setForceDesktop(true);
    navigate("/", { replace: true });
  };

  return (
    <div className="m-shell">
      <header className="m-header">
        <span className="m-title">BioFlow</span>
        <button className="m-desktop-link" onClick={useDesktop}>
          Use desktop version
        </button>
      </header>

      <main className="m-main">
        <Outlet />
      </main>

      <nav className="m-tabs">
        <NavLink
          to="/m/activity"
          className={({ isActive }) => `m-tab${isActive ? " active" : ""}`}
        >
          Activity
        </NavLink>
        <NavLink
          to="/m/download"
          className={({ isActive }) => `m-tab${isActive ? " active" : ""}`}
        >
          Download
        </NavLink>
      </nav>
    </div>
  );
}
