import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { formatBytes } from "../lib/format";
import { LoadIndicator } from "./LoadIndicator";

/** Still awaiting real actions. Help is implemented separately below. */
const MENUS = ["File", "View"];

/** Destinations that exist. Without these, /search and /activity are
 *  reachable only by typing the URL. */
const LINKS: { to: string; label: string; title: string }[] = [
  { to: "/search", label: "Search", title: "Search files by metadata" },
  { to: "/activity", label: "Activity", title: "Running and queued jobs" },
];

/** Help menu contents. One entry today; the shape is the point. */
const HELP_ITEMS: { to: string; label: string }[] = [
  { to: "/help/calculations", label: "BioFlow Calculations" },
];

export function Header() {
  const { data } = useQuery({
    queryKey: ["system", "stats"],
    queryFn: api.systemStats,
    refetchInterval: 15000,
  });

  const navigate = useNavigate();
  const [helpOpen, setHelpOpen] = useState(false);
  const helpRef = useRef<HTMLDivElement>(null);

  // A menu that only closes by re-clicking its button feels broken, so handle
  // the two things people actually do: click elsewhere, or press Escape.
  useEffect(() => {
    if (!helpOpen) return;

    function onPointerDown(e: MouseEvent) {
      if (helpRef.current && !helpRef.current.contains(e.target as Node)) {
        setHelpOpen(false);
      }
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setHelpOpen(false);
    }

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [helpOpen]);

  return (
    <header className="header">
      {/* The brand is the conventional way back to the file explorer, and it
          is the only one from a full-width view like /activity. */}
      <Link to="/" className="header-brand" title="Back to projects">
        <span className="header-logo">B</span>
        <span>BioFlow</span>
      </Link>

      <nav className="header-menu">
        {LINKS.map((l) => (
          <NavLink
            key={l.to}
            to={l.to}
            title={l.title}
            className={({ isActive }) => (isActive ? "active" : undefined)}
          >
            {l.label}
          </NavLink>
        ))}
        {MENUS.map((m) => (
          <button key={m} type="button" title={`${m} menu (not yet implemented)`}>
            {m}
          </button>
        ))}

        <div className="header-dropdown" ref={helpRef}>
          <button
            type="button"
            aria-haspopup="menu"
            aria-expanded={helpOpen}
            onClick={() => setHelpOpen((v) => !v)}
          >
            Help
          </button>
          {helpOpen && (
            <div className="header-dropdown-menu" role="menu">
              {HELP_ITEMS.map((item) => (
                <button
                  key={item.to}
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setHelpOpen(false);
                    navigate(item.to);
                  }}
                >
                  {item.label}
                </button>
              ))}
            </div>
          )}
        </div>
      </nav>

      <div className="header-right">
        <LoadIndicator />
        {/* Library size rather than free space: under Docker Desktop the
            container cannot see the external drive's real capacity, and a
            confidently wrong "192 GB free" is worse than not saying. This we
            can count exactly. */}
        {data && (
          <div
            className="load-indicator"
            title={`${data.counts.objects} files at ${data.storage.path}`}
          >
            <span>{formatBytes(data.storage.library_bytes)} stored</span>
          </div>
        )}
      </div>
    </header>
  );
}
