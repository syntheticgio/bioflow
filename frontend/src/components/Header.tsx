import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import mastheadImg from "../assets/broadhead-masthead.png";
import { formatBytes } from "../lib/format";
import { useElementWidth } from "../lib/useElementWidth";
import { notify } from "../stores/messageStore";
import { useProfileStore } from "../stores/profileStore";
import { LoadIndicator } from "./LoadIndicator";
import { Menu } from "./Menu";
import { RecentProjects } from "./RecentProjects";

/** Same query the header badge and /shares itself both read, so opening the
 *  page costs no second request and the badge clears from the same
 *  invalidation `useEvents` schedules on `share.accepted`/`share.declined`. */
function useShareInboxCount() {
  const profileId = useProfileStore((s) => s.current?.id);
  const { data } = useQuery({
    queryKey: ["shares", "inbox", profileId],
    queryFn: api.shareInbox,
    enabled: Boolean(profileId),
  });
  return data?.length ?? 0;
}

/** Destinations that exist. Without these, /search and /activity are
 *  reachable only by typing the URL. */
const LINKS: { to: string; label: string; title: string }[] = [
  { to: "/search", label: "Search", title: "Search files by metadata" },
  { to: "/activity", label: "Activity", title: "Running and queued jobs" },
  { to: "/workflows", label: "Workflows", title: "Build and save reusable pipeline graphs" },
];

/** Reference menu contents: standalone reference documents, not tied to what
 *  BioFlow itself integrates with or computes. */
const REFERENCE_ITEMS: { to: string; label: string; section: string }[] = [
  { to: "/help/workflow-diagrams", label: "Workflow Diagrams", section: "Reference" },
  { to: "/help/genome-analysis-review", label: "Genome Analysis Review", section: "Reference" },
  { to: "/help/databases", label: "Databases", section: "Reference" },
  { to: "/metrics", label: "Metrics", section: "Reference" },
];

/** Help menu contents, grouped so the dropdown stays scannable as it grows.
 *  "About BioFlow" groups the pages that describe what this software does
 *  and integrates with -- calculations it runs, tools it wraps, sources it
 *  reads from -- as distinct from the standalone reference documents in the
 *  Reference menu. */
const HELP_ITEMS: { to: string; label: string; section: string }[] = [
  { to: "/help/about", label: "About", section: "About" },
  { to: "/help/calculations", label: "BioFlow Calculations", section: "About BioFlow" },
  { to: "/help/software", label: "Software", section: "About BioFlow" },
  { to: "/help/sources", label: "Data Sources", section: "About BioFlow" },
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

  const [headerRightRef, headerRightWidth] = useElementWidth<HTMLDivElement>();

  const profile = useProfileStore((s) => s.current);
  const logout = useProfileStore((s) => s.logout);
  const inboxCount = useShareInboxCount();

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
        <img src={mastheadImg} alt="BioFlow" className="header-masthead-img" />
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
          label="Reference"
          items={REFERENCE_ITEMS.map((item) => ({
            label: item.label,
            section: item.section,
            onSelect: () => navigate(item.to),
          }))}
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

            Settings lives here rather than as a top-level nav entry because
            it is profile-scoped (AI providers and task routing are per
            profile), so it belongs with the other profile-scoped action.

            "Switch profile" rather than "Logout": both would call logout()
            and land on the picker, so offering both would be two names for
            one action. Selecting a profile issues no token and sets no
            cookie, so there is no session to end -- returning to the picker
            is the entirety of what either would do.

            There is deliberately no "Edit details": the backend exposes only
            GET/POST/select/DELETE on /profiles, so an edit control would be
            dead on arrival.

            "Shared with me" sits here rather than under Activity: sharing is
            identity-shaped (something another profile did, to you), and
            Activity is about jobs and runs. The count badge is what makes a
            new offer visible without opening the menu at all. */}
        {profile && (
          <Menu
            label={
              <>
                {profile.username}
                {inboxCount > 0 && (
                  <span className="menu-badge" title={`${inboxCount} pending share offer(s)`}>
                    {inboxCount}
                  </span>
                )}
              </>
            }
            items={[
              { label: "Shared with me", onSelect: () => navigate("/shares") },
              { label: "Settings", onSelect: () => navigate("/settings") },
              { label: "Switch profile", onSelect: logout },
            ]}
          />
        )}
      </nav>

      <div className="header-right" ref={headerRightRef}>
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
        <RecentProjects availableWidth={headerRightWidth} />
        <LoadIndicator />
      </div>
    </header>
  );
}
