import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { formatBytes } from "../lib/format";
import { notify } from "../stores/messageStore";
import { useProfileStore } from "../stores/profileStore";
import { LoadIndicator } from "./LoadIndicator";
import { Menu } from "./Menu";

/** Destinations that exist. Without these, /search and /activity are
 *  reachable only by typing the URL. */
const LINKS: { to: string; label: string; title: string }[] = [
  { to: "/search", label: "Search", title: "Search files by metadata" },
  { to: "/activity", label: "Activity", title: "Running and queued jobs" },
  { to: "/settings", label: "Settings", title: "AI providers and task routing" },
];

/** Help menu contents, grouped so the dropdown stays scannable as it grows.
 *  Within "Reference", pages are in the order a user needs them: what the
 *  numbers mean, then what produced them, then where the inputs came from. */
const HELP_ITEMS: { to: string; label: string; section: string }[] = [
  { to: "/help/about", label: "About", section: "About" },
  { to: "/help/calculations", label: "BioFlow Calculations", section: "Reference" },
  { to: "/help/software", label: "Software", section: "Reference" },
  { to: "/help/sources", label: "Data Sources", section: "Reference" },
  { to: "/help/workflow-diagrams", label: "Workflow Diagrams", section: "Reference" },
  { to: "/help/genome-analysis-review", label: "Genome Analysis Review", section: "Reference" },
  { to: "/help/feedback", label: "Feedback", section: "Support" },
  { to: "/help/placeholder", label: "Placeholder", section: "Support" },
];

export function Header() {
  const { data } = useQuery({
    queryKey: ["system", "stats"],
    queryFn: api.systemStats,
    refetchInterval: 15000,
  });

  const navigate = useNavigate();

  const profile = useProfileStore((s) => s.current);
  const logout = useProfileStore((s) => s.logout);

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
        <Menu
          label="Help"
          items={HELP_ITEMS.map((item) => ({
            label: item.label,
            section: item.section,
            onSelect: () => navigate(item.to),
          }))}
        />
        {/* The label is the profile itself, not a generic "Profile", because
            the whole point of putting this in the header is that you can see
            which library you are working in without opening anything. Someone
            who switched an hour ago should not have to click to find out.

            One item, not two: "Switch profile" and "Logout" would both call
            logout() and both land on the picker, so offering both would be two
            names for one action. Selecting a profile issues no token and sets
            no cookie, so there is no session to end -- returning to the picker
            is the entirety of what either would do, and "Switch profile" is
            the honest name for it.

            There is deliberately no "Edit details": the backend exposes only
            GET/POST/select/DELETE on /profiles, so an edit control would be
            dead on arrival. */}
        {profile && (
          <Menu
            label={`${profile.display.emoji} ${profile.username}`}
            items={[{ label: "Switch profile", onSelect: logout }]}
          />
        )}
      </nav>

      <div className="header-right">
        {/* What the library holds, then what it is doing. Library size rather
            than free space: under Docker Desktop the container cannot see the
            external drive's real capacity, and a confidently wrong "192 GB
            free" is worse than not saying. These we can count exactly. */}
        {data && (
          <div
            className="header-stats"
            title={`${data.counts.objects} files at ${data.storage.path}`}
          >
            <span>{data.counts.objects} files</span>
            <span>{data.counts.projects} projects</span>
            <span>{formatBytes(data.storage.library_bytes)} stored</span>
          </div>
        )}
        <LoadIndicator />
      </div>
    </header>
  );
}
