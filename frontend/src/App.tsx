import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
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

  return (
    <div className="shell">
      <Header />
      <div
        className="main"
        style={{ ["--left-w" as string]: `${panelWidth}px` }}
      >
        <Routes>
          <Route path="/" element={<ProjectExplorer />} />
          <Route path="/p/:projectId" element={<ProjectExplorer />} />
          <Route path="/search" element={<SearchView />} />
        </Routes>
        <DetailPanel />
      </div>
      <div
        className="splitter"
        style={{ left: panelWidth - 2 }}
        onMouseDown={startResize}
      />
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
