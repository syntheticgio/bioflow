import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect } from "react";
import { BrowserRouter, Route, Routes, useLocation } from "react-router-dom";
import { ActivityView } from "./components/ActivityView";
import { DetailPanel } from "./components/DetailPanel";
import { Footer } from "./components/Footer";
import { Header } from "./components/Header";
import { HelpCalculations } from "./components/HelpCalculations";
import { HelpSoftware } from "./components/HelpSoftware";
import { HelpSources } from "./components/HelpSources";
import { ProjectExplorer } from "./components/ProjectExplorer";
import { SearchView } from "./components/SearchView";
import { UploadTray } from "./components/UploadTray";
import { useEvents } from "./hooks/useEvents";
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

  // Both are single full-width views with no left-hand tree to sit beside:
  // /activity is one long list, and the help pages are prose.
  const pathname = useLocation().pathname;
  const singleColumn = pathname === "/activity" || pathname.startsWith("/help/");

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
          <Route path="/help/calculations" element={<HelpCalculations />} />
          <Route path="/help/software" element={<HelpSoftware />} />
          <Route path="/help/sources" element={<HelpSources />} />
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

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Shell />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
