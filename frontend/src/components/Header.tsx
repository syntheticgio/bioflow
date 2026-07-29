import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, NavLink } from "react-router-dom";
import { api } from "../api/client";
import { formatBytes } from "../lib/format";
import { notify } from "../stores/messageStore";
import { LoadIndicator } from "./LoadIndicator";
import { Menu } from "./Menu";

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

  const cleanUp = useMutation({
    mutationFn: () => api.runScheduleNow("gc_blobs"),
    onSuccess: () => {
      // The job is queued, not finished -- gc_blobs runs on the worker, so its
      // reclaim counts are not available here. Point at Activity instead of
      // inventing a number.
      notify.success("Storage cleanup started. Progress is in Activity.");
    },
    onError: (e: Error) => notify.error(e.message),
  });

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
        <Menu
          label="File"
          items={[
            {
              label: "Clean up storage now",
              onSelect: () => cleanUp.mutate(),
              disabled: cleanUp.isPending,
            },
          ]}
        />
        <Menu label="View" items={[]} />
        <Menu label="Help" items={[]} />
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
