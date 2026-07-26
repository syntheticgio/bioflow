import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { formatBytes } from "../lib/format";
import { LoadIndicator } from "./LoadIndicator";

/** Boilerplate menu. Real actions arrive as the feature set grows. */
const MENUS = ["File", "View", "Jobs", "Help"];

export function Header() {
  const { data } = useQuery({
    queryKey: ["system", "stats"],
    queryFn: api.systemStats,
    refetchInterval: 15000,
  });

  const disk = data?.storage.disk;

  return (
    <header className="header">
      <div className="header-brand">
        <span className="header-logo">B</span>
        <span>BioinfoHelper</span>
      </div>

      <nav className="header-menu">
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
