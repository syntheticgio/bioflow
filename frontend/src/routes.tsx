/**
 * The route table, kept out of `App.tsx` so a routing change and a layout
 * change are edits to different files.
 *
 * `App.tsx` owns the shell -- chrome, providers, the splitter, the
 * single-vs-two-column decision -- and renders these two elements inside it.
 * Nothing here is lazy-loaded, so there is no `Suspense` boundary to keep on
 * one side or the other of the shell; a route added with `React.lazy()` would
 * need its boundary placed here, inside the same `<Routes>` it renders under,
 * rather than left to whatever happens to wrap the shell.
 */
import { useQuery } from "@tanstack/react-query";
import { Navigate, Route, Routes } from "react-router-dom";
import { api } from "./api/client";
import { ActivityView } from "./components/ActivityView";
import { HelpAbout } from "./components/HelpAbout";
import { HelpCalculations } from "./components/HelpCalculations";
import { HelpDatabases } from "./components/HelpDatabases";
import { HelpFeedback } from "./components/HelpFeedback";
import { HelpGenomeAnalysisReview } from "./components/HelpGenomeAnalysisReview";
import { HelpPlaceholder } from "./components/HelpPlaceholder";
import { HelpSoftware } from "./components/HelpSoftware";
import { HelpSources } from "./components/HelpSources";
import { HelpSupport } from "./components/HelpSupport";
import { HelpWorkflowDiagrams } from "./components/HelpWorkflowDiagrams";
import { Metrics } from "./components/Metrics";
import { MetricsJobType } from "./components/MetricsJobType";
import { ProjectExplorer } from "./components/ProjectExplorer";
import { SearchView } from "./components/SearchView";
import { SettingsGeneral } from "./components/SettingsGeneral";
import { SettingsMcp } from "./components/SettingsMcp";
import { SettingsNodes } from "./components/SettingsNodes";
import { SettingsResources } from "./components/SettingsResources";
import { SettingsStorage } from "./components/SettingsStorage";
import { SettingsTools } from "./components/SettingsTools";
import { SettingsView } from "./components/SettingsView";
import { SharesView } from "./components/SharesView";
import { WorkflowCanvas } from "./components/WorkflowCanvas";
import { MobileActivity } from "./mobile/MobileActivity";
import { MobileConfirm } from "./mobile/MobileConfirm";
import { MobileDownload } from "./mobile/MobileDownload";
import { MobileShell } from "./mobile/MobileShell";

/**
 * Guards direct navigation to /help/feedback: the Header hides the menu
 * entry when the setting is off, but a bookmarked or typed URL bypasses
 * that, so the route itself has to check the same flag.
 */
function FeedbackRoute() {
  const settings = useQuery({
    queryKey: ["settings", "general"],
    queryFn: api.generalSettings,
  });

  if (settings.isLoading) return null;
  if (!settings.data?.feedback_enabled) return <Navigate to="/help/about" replace />;
  return <HelpFeedback />;
}

/** The desktop route table, rendered inside the shell's main column. */
export function DesktopRoutes() {
  return (
    <Routes>
      <Route path="/" element={<ProjectExplorer />} />
      <Route path="/p/:projectId" element={<ProjectExplorer />} />
      <Route path="/search" element={<SearchView />} />
      <Route path="/activity" element={<ActivityView />} />
      <Route path="/workflows" element={<WorkflowCanvas />} />
      <Route path="/shares" element={<SharesView />} />
      <Route path="/help/about" element={<HelpAbout />} />
      <Route path="/settings" element={<SettingsView />} />
      <Route path="/settings/ai" element={<SettingsView />} />
      <Route path="/settings/tools" element={<SettingsTools />} />
      <Route path="/settings/resources" element={<SettingsResources />} />
      <Route path="/settings/storage" element={<SettingsStorage />} />
      <Route path="/settings/mcp" element={<SettingsMcp />} />
      <Route path="/settings/general" element={<SettingsGeneral />} />
      <Route path="/settings/nodes" element={<SettingsNodes />} />
      <Route path="/help/calculations" element={<HelpCalculations />} />
      <Route path="/metrics" element={<Metrics />} />
      <Route path="/metrics/:jobType" element={<MetricsJobType />} />
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
      <Route path="/help/feedback" element={<FeedbackRoute />} />
      <Route path="/help/support" element={<HelpSupport />} />
      <Route path="/help/placeholder" element={<HelpPlaceholder />} />
    </Routes>
  );
}

/**
 * The top-level split between the mobile routes and everything else.
 *
 * The catch-all is the desktop shell rather than the desktop route table:
 * every non-`/m/*` URL has to render the chrome around whatever it resolves
 * to, so the shell is passed in rather than imported, keeping the dependency
 * pointing one way -- `App.tsx` knows about the routes, not the reverse.
 */
export function TopLevelRoutes({ shell }: { shell: React.ReactNode }) {
  return (
    <Routes>
      <Route path="/m" element={<MobileShell />}>
        <Route index element={<Navigate to="/m/activity" replace />} />
        <Route path="activity" element={<MobileActivity />} />
        <Route path="download" element={<MobileDownload />} />
        <Route path="download/:accession" element={<MobileConfirm />} />
      </Route>
      <Route path="*" element={shell} />
    </Routes>
  );
}
