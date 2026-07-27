import { useQuery } from "@tanstack/react-query";
import { Link, NavLink } from "react-router-dom";
import { api } from "../api/client";
import { formatBytes } from "../lib/format";
import { LoadIndicator } from "./LoadIndicator";

/** Placeholders still awaiting real actions. */
const MENUS = ["File", "View", "Help"];

/** Destinations that exist. Without these, /search and /activity are
 *  reachable only by typing the URL. */
const LINKS: { to: string; label: string; title: string }[] = [
  { to: "/search", label: "Search", title: "Search files by metadata" },
  { to: "/activity", label: "Activity", title: "Running and queued jobs" },
];

export function Header() {
  const { data } = useQuery({
    queryKey: ["system", "stats"],
    queryFn: api.systemStats,
    refetchInterval: 15000,
  });

  const disk = data?.storage.disk;

  return (
    <header className="header">
      {/* The brand is the conventional way back to the file explorer, and it
          is the only one from a full-width view like /activity. */}
      <Link to="/" className="header-brand" title="Back to projects">
        <span className="header-logo">B</span>
        <span>BioinfoHelper</span>
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
      </nav>

      <div className="header-right">
        <LoadIndicator />
        {disk && (
          <div className="load-indicator" title={`Storage at ${data?.storage.path}`}>
            <span>{formatBytes(disk.free_bytes)} free</span>
            <span style={{ color: "var(--text-faint)" }}>·</span>
            <span>{disk.percent_used}% used</span>
          </div>
        )}
      </div>
    </header>
  );
}
