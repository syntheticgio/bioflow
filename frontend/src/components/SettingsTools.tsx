import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import { formatBytes } from "../lib/format";
import { notify } from "../stores/messageStore";
import type { JobSummary, PipelineTool } from "../api/types";
import { SettingsNav } from "./SettingsNav";

const ACTIVE_JOB_STATES = new Set(["pending", "queued", "delayed", "running"]);

/**
 * Every tool BioFlow can run, with an action for the ones that need one.
 *
 * Deliberately lists BUNDLED tools too, not only ON_DEMAND_IMAGE ones. That is
 * the whole reason this exists as a page rather than a modal: it is meant to
 * answer "why is this card greyed out" for a tool baked into the image just as
 * much as "should I install DeepVariant" for one that is not, and a page that
 * only showed the installable handful would leave the first question
 * unanswered here too.
 *
 * Reads the same `GET /pipelines/tools` response `/help/software` does, built
 * from the backend's one `TOOL_META` registry -- there is deliberately no
 * second list of tools maintained here. `/help/software` stays documentation
 * and gains no buttons; this page is the only place install/uninstall live.
 */
export function SettingsTools() {
  const tools = useQuery({
    queryKey: ["pipelines", "tools"],
    queryFn: api.pipelineTools,
    // Poll while any on-demand tool has a job in flight (below), not on a
    // fixed timer -- see the jobs query for why this alone isn't enough.
  });

  // install_tool / uninstall_tool jobs, queried separately: the backend's
  // /jobs `type` filter takes exactly one value, and these are the only two
  // types this page cares about. Polls only while something is actually
  // running, the same conditional-refetchInterval shape JobList.tsx uses for
  // the Activity tab -- SSE covers the rest of the app, but this page has no
  // per-tool event channel to listen on.
  const installJobs = useQuery({
    queryKey: ["jobs", "install_tool"],
    queryFn: () => api.listJobs({ type: "install_tool", states: "active" }),
    refetchInterval: (q) => {
      const list = q.state.data as JobSummary[] | undefined;
      return list && list.length > 0 ? 1500 : false;
    },
  });
  const uninstallJobs = useQuery({
    queryKey: ["jobs", "uninstall_tool"],
    queryFn: () => api.listJobs({ type: "uninstall_tool", states: "active" }),
    refetchInterval: (q) => {
      const list = q.state.data as JobSummary[] | undefined;
      return list && list.length > 0 ? 1500 : false;
    },
  });

  if (tools.isLoading) {
    return (
      <div className="settings-page">
        <SettingsNav />
        <div>Loading…</div>
      </div>
    );
  }
  if (tools.isError || !tools.data) {
    return (
      <div className="settings-page">
        <SettingsNav />
        <div>Could not load tools.</div>
      </div>
    );
  }

  const jobByTool = new Map<string, JobSummary>();
  for (const job of [...(installJobs.data ?? []), ...(uninstallJobs.data ?? [])]) {
    const tool = job.payload?.tool;
    if (typeof tool === "string") jobByTool.set(tool, job);
  }

  const sorted = [...tools.data.tools].sort((a, b) => a.name.localeCompare(b.name));

  return (
    <div className="settings-page settings-page-wide">
      <SettingsNav />
      <h1>Settings · Tools</h1>
      <p className="settings-hint">
        Tools bundled in the image need no action. On-demand tools are pulled
        as a separate container image the first time you install them.
      </p>

      <div className="settings-tools-grid">
        {sorted.map((tool) => (
          <ToolRow key={tool.name} tool={tool} job={jobByTool.get(tool.name)} />
        ))}
      </div>
    </div>
  );
}

function ToolRow({ tool, job }: { tool: PipelineTool; job: JobSummary | undefined }) {
  const queryClient = useQueryClient();

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["pipelines", "tools"] });
    queryClient.invalidateQueries({ queryKey: ["jobs", "install_tool"] });
    queryClient.invalidateQueries({ queryKey: ["jobs", "uninstall_tool"] });
  };

  const install = useMutation({
    mutationFn: () => api.installTool(tool.name),
    onSuccess: invalidate,
    onError: (e: Error) => notify.error(e.message),
  });
  const uninstall = useMutation({
    mutationFn: () => api.uninstallTool(tool.name),
    onSuccess: invalidate,
    onError: (e: Error) => notify.error(e.message),
  });
  const cancel = useMutation({
    mutationFn: (id: string) => api.cancelJob(id),
    onSuccess: invalidate,
    onError: (e: Error) => notify.error(e.message),
  });
  const retry = useMutation({
    mutationFn: (id: string) => api.retryJob(id),
    onSuccess: invalidate,
    onError: (e: Error) => notify.error(e.message),
  });

  const confirmUninstall = () => {
    const size = tool.download_bytes ? ` (${formatBytes(tool.download_bytes)} on disk)` : "";
    if (confirm(`Uninstall ${tool.name}${size}? You can reinstall it later.`)) {
      uninstall.mutate();
    }
  };

  return (
    <div className="settings-tools-card">
      <div className="settings-tools-card-info">
        <span className="settings-tools-name">{tool.name}</span>
        <span className="settings-tools-one-liner">{tool.one_liner}</span>
      </div>
      <div className="settings-tools-card-state">
        <ToolState tool={tool} job={job} />
      </div>
      <div className="settings-tools-action">
        <ToolAction
          tool={tool}
          job={job}
          onInstall={() => install.mutate()}
          onUninstall={confirmUninstall}
          onCancel={(id) => cancel.mutate(id)}
          onRetry={(id) => retry.mutate(id)}
          busy={install.isPending || uninstall.isPending}
        />
      </div>
    </div>
  );
}

/**
 * The status column. `job` takes precedence over `tool.install_state` when
 * both exist -- a job in flight is more current than the probe result it
 * will eventually invalidate, and showing "not installed" while a pull is
 * already 80% done would read as the button not having worked.
 */
function ToolState({ tool, job }: { tool: PipelineTool; job: JobSummary | undefined }) {
  if (job && ACTIVE_JOB_STATES.has(job.state)) {
    const pct = job.progress.pct;
    const label =
      job.type === "uninstall_tool"
        ? job.progress.message || "removing…"
        : job.progress.message ||
          (typeof pct === "number" ? `${Math.round(pct * 100)}%` : "starting…");
    return <span className="settings-tools-state">{label}</span>;
  }

  if (job && job.state === "failed") {
    return (
      <span className="settings-tools-state settings-tools-state-failed">
        {job.error?.message || "failed"}
      </span>
    );
  }

  if (tool.delivery === "bundled") {
    return <span className="settings-tools-state">Included{tool.version ? ` — ${tool.version}` : ""}</span>;
  }

  if (tool.install_state === "installed") {
    return <span className="settings-tools-state">Installed{tool.version ? ` — ${tool.version}` : ""}</span>;
  }

  if (tool.install_state === "unknown") {
    return (
      <span className="settings-tools-state settings-tools-state-failed" title={tool.error ?? undefined}>
        Unavailable
      </span>
    );
  }

  return (
    <span className="settings-tools-state">
      Not installed
      {tool.download_bytes ? ` — ${formatBytes(tool.download_bytes)}` : ""}
    </span>
  );
}

function ToolAction({
  tool,
  job,
  onInstall,
  onUninstall,
  onCancel,
  onRetry,
  busy,
}: {
  tool: PipelineTool;
  job: JobSummary | undefined;
  onInstall: () => void;
  onUninstall: () => void;
  onCancel: (jobId: string) => void;
  onRetry: (jobId: string) => void;
  busy: boolean;
}) {
  if (job && ACTIVE_JOB_STATES.has(job.state)) {
    return (
      <button className="settings-link-button" onClick={() => onCancel(job.id)}>
        Cancel
      </button>
    );
  }

  if (job && job.state === "failed") {
    return (
      <button className="settings-link-button" onClick={() => onRetry(job.id)}>
        Retry
      </button>
    );
  }

  if (tool.delivery === "bundled") return null;

  if (tool.install_state === "installed") {
    return (
      <button className="settings-danger" onClick={onUninstall} disabled={busy}>
        Uninstall
      </button>
    );
  }

  // Also covers "unknown" (no docker client, daemon unreachable): offering
  // Install is more useful than nothing, and the handler's own probe will
  // report the real reason if it still fails.
  return (
    <button onClick={onInstall} disabled={busy}>
      Install
    </button>
  );
}
