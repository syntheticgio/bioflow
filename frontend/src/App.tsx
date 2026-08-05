import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { api } from "./api/client";
import { ActivityView } from "./components/ActivityView";
import { DetailPanel } from "./components/DetailPanel";
import { Footer } from "./components/Footer";
import { Header } from "./components/Header";
import { HelpAbout } from "./components/HelpAbout";
import { HelpCalculations } from "./components/HelpCalculations";
import { HelpDatabases } from "./components/HelpDatabases";
import { HelpFeedback } from "./components/HelpFeedback";
import { HelpGenomeAnalysisReview } from "./components/HelpGenomeAnalysisReview";
import { HelpPlaceholder } from "./components/HelpPlaceholder";
import { HelpSoftware } from "./components/HelpSoftware";
import { HelpSources } from "./components/HelpSources";
import { HelpWorkflowDiagrams } from "./components/HelpWorkflowDiagrams";
import { ProfilePicker } from "./components/ProfilePicker";
import { ProjectExplorer } from "./components/ProjectExplorer";
import { SearchView } from "./components/SearchView";
import { SettingsTools } from "./components/SettingsTools";
import { SettingsView } from "./components/SettingsView";
import { UploadTray } from "./components/UploadTray";
import { useEvents } from "./hooks/useEvents";
import { MobileActivity } from "./mobile/MobileActivity";
import { MobileConfirm } from "./mobile/MobileConfirm";
import { MobileDownload } from "./mobile/MobileDownload";
import { MobileShell } from "./mobile/MobileShell";
import { forceDesktop, useIsMobile } from "./mobile/useIsMobile";
import { useProfileStore } from "./stores/profileStore";
import { useUiStore } from "./stores/uiStore";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

function Shell() {
  const panelWidth = useUiStore((s) => s.panelWidth);
  const setPanelWidth = useUiStore((s) => s.setPanelWidth);
  // Live updates from workers; falls back to per-query polling if it drops.
  const { connected } = useEvents();

  const startResize = (e: React.MouseEvent) => {
    e.preventDefault();
    const move = (ev: MouseEvent) =>
      setPanelWidth(Math.min(Math.max(ev.clientX, 220), 640));
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  // All three are single full-width views with no left-hand tree to sit
  // beside: /activity is one long list, the help pages are prose, and
  // /settings has its own master-detail layout that a stray DetailPanel and
  // splitter would only crowd.
  const pathname = useLocation().pathname;
  const singleColumn =
    pathname === "/activity" ||
    pathname.startsWith("/help/") ||
    pathname.startsWith("/settings");

  // Themes that scroll the window rather than the panes (Broadsheet) would
  // otherwise land mid-page on every route change, carrying the previous
  // view's offset with them. Harmless where the panes scroll instead.
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [pathname]);

  return (
    <div className="shell">
      <Header />
      <div
        className={`main${singleColumn ? " main-single" : ""}`}
        style={{ ["--left-w" as string]: `${panelWidth}px` }}
      >
        <Routes>
          <Route path="/" element={<ProjectExplorer />} />
          <Route path="/p/:projectId" element={<ProjectExplorer />} />
          <Route path="/search" element={<SearchView />} />
          <Route path="/activity" element={<ActivityView />} />
          <Route path="/help/about" element={<HelpAbout />} />
          <Route path="/settings" element={<SettingsView />} />
          <Route path="/settings/ai" element={<SettingsView />} />
          <Route path="/settings/tools" element={<SettingsTools />} />
          <Route path="/help/calculations" element={<HelpCalculations />} />
          <Route path="/help/software" element={<HelpSoftware />} />
          <Route path="/help/sources" element={<HelpSources />} />
          <Route path="/help/databases" element={<HelpDatabases />} />
          <Route
            path="/help/workflow-diagrams"
            element={<HelpWorkflowDiagrams />}
          />
          <Route
            path="/help/genome-analysis-review"
            element={<HelpGenomeAnalysisReview />}
          />
          <Route path="/help/feedback" element={<HelpFeedback />} />
          <Route path="/help/placeholder" element={<HelpPlaceholder />} />
        </Routes>
        {!singleColumn && <DetailPanel />}
      </div>
      {!singleColumn && (
        <div
          className="splitter"
          style={{ left: panelWidth - 2 }}
          onMouseDown={startResize}
        />
      )}
      <Footer streamConnected={connected} />
      <UploadTray />
    </div>
  );
}

/**
 * The two things that can make a persisted profile wrong, settled once before
 * anything renders.
 *
 * 1. Auto-login is off. The store persists, so `current` is already populated
 *    on reload and the gate would skip the picker on its own. "Show me the
 *    picker every time" is therefore not the absence of an action -- it has
 *    to actively clear what was restored.
 * 2. The remembered profile was deleted. Its id then names nothing and every
 *    request 404s, so it is validated against the profile list, which is the
 *    one call that works without a profile.
 *
 * Module-scoped rather than a hook because the answer is a property of the
 * page load, not of any component: memoising the promise means React can
 * mount `Gate` as many times as it likes -- StrictMode mounts it twice in dev
 * -- without a second round of clearing or a second list request.
 */
let startupPromise: Promise<void> | null = null;

function startupCheck(): Promise<void> {
  if (startupPromise) return startupPromise;

  startupPromise = (async () => {
    const store = useProfileStore.getState();
    const remembered = store.current;
    if (!remembered) return;

    if (!store.autoLogin) {
      store.logout();
      return;
    }

    try {
      const profiles = await api.listProfiles();
      // Cleared, never substituted. Silently entering a different profile
      // would put someone in the wrong library without a single screen saying
      // so, which is the exact failure the partition exists to prevent.
      if (!profiles.some((p) => p.id === remembered.id)) {
        useProfileStore.getState().logout();
      }
    } catch {
      // The API is unreachable, which says nothing about whether the
      // remembered profile still exists. Keep it: dropping the user at the
      // picker over a transient failure would look like their profile had
      // been deleted, and the picker cannot load its list either.
    }
  })();

  return startupPromise;
}

/**
 * Decides between the picker and the shell, and waits for the startup checks
 * above before rendering either.
 *
 * It sits inside `QueryClientProvider` -- the picker itself needs no query
 * client, but this is also where `Shell` mounts, and hoisting the gate above
 * the provider would only move the boundary without gaining anything. What
 * matters is that it is *outside* `Shell`: `Shell`'s children fetch on mount
 * and every user-data route 400s with no profile, so rendering it first would
 * produce a burst of failed requests before the picker ever appeared.
 */
/**
 * Sends a narrow viewport to the mobile routes, once.
 *
 * One-directional on purpose. Redirecting a wide viewport back off /m/*
 * would make the "use desktop version" escape hatch impossible to use --
 * the moment it navigated to /, a still-narrow window would bounce it
 * straight back -- and would throw a tablet user out of the screen they
 * were reading the instant they rotated.
 */
function MobileRedirect() {
  const isMobile = useIsMobile();
  const { pathname } = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    if (!isMobile) return;
    if (forceDesktop()) return;
    if (pathname.startsWith("/m/")) return;
    navigate("/m/activity", { replace: true });
  }, [isMobile, pathname, navigate]);

  return null;
}

function Gate() {
  const current = useProfileStore((s) => s.current);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let live = true;
    // Every mount awaits the same promise, so the work happens once per page
    // load while each mount still gets its own `setReady`. A `useRef` guard
    // cannot do this: StrictMode remounts on the same fiber, so the ref stays
    // set while `ready` resets to false, and the second mount returns early
    // without ever flipping it -- a blank screen with no error anywhere.
    startupCheck().then(() => {
      if (live) setReady(true);
    });
    return () => {
      live = false;
    };
  }, []);

  if (!ready) return null;
  if (!current) return <ProfilePicker />;
  return (
    <>
      <MobileRedirect />
      <Routes>
        <Route path="/m" element={<MobileShell />}>
          <Route index element={<Navigate to="/m/activity" replace />} />
          <Route path="activity" element={<MobileActivity />} />
          <Route path="download" element={<MobileDownload />} />
          <Route path="download/:accession" element={<MobileConfirm />} />
        </Route>
        <Route path="*" element={<Shell />} />
      </Routes>
    </>
  );
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Gate />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
