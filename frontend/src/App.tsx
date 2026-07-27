import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes, useLocation } from "react-router-dom";
import { ActivityView } from "./components/ActivityView";
import { DetailPanel } from "./components/DetailPanel";
import { Footer } from "./components/Footer";
import { Header } from "./components/Header";
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

  // The activity view is a single full-width list: there is no left-hand tree
  // to sit beside, and squeezing it into the explorer's column truncates every
  // filename it exists to show. Selecting a row navigates to the explorer,
  // which is where the detail panel lives.
  const singleColumn = useLocation().pathname === "/activity";

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
