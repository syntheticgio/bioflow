import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import { formatBytes } from "../lib/format";
import { notify } from "../stores/messageStore";
import type { JobSummary, PipelineTool, PipelineType } from "../api/types";
import { SettingsNav } from "./SettingsNav";

const ACTIVE_JOB_STATES = new Set(["pending", "queued", "delayed", "running"]);

/**
 * The section a tool is filed under, in the order the page renders them.
 *
 * A presentation rollup over the backend's `pipelines`, not a second source of
 * truth: `PipelineType` is finer-grained than a settings page wants (ASSEMBLE,
 * ASSEMBLY_QC, and REFERENCE_ASSEMBLY are three families in the tool pickers,
 * where they only need to read as "Assembly" here), so this collapses them
 * rather than restating which tools exist. Adding a tool to `TOOL_META` files
 * it here automatically.
 */
const GROUPS = [
  "Reads & QC",
  "Alignment",
  "Variants",
  "Assembly",
  "Expression",
  "Archive",
  "Utilities",
] as const;

type Group = (typeof GROUPS)[number];

/**
 * First match wins, so the order here is what decides a tool with more than
 * one `PipelineType`.
 *
 * `utility` is deliberately first. The two tools that carry it also carry
 * something else -- samtools is UTILITY+QC, bcftools is VARIANT+UTILITY --
 * and in both cases the general-purpose toolkit is the better answer to
 * "where would someone look for this": samtools is a BAM/CRAM/SAM toolkit
 * whose flagstat output happens to be alignment QC, not a QC tool. `trim`
 * before `qc` puts fastp (TRIM+QC) under Reads & QC, which is the same
 * heading either way.
 */
const GROUP_BY_PIPELINE: readonly (readonly [PipelineType, Group])[] = [
  ["utility", "Utilities"],
  ["trim", "Reads & QC"],
  ["qc", "Reads & QC"],
  ["align", "Alignment"],
  ["variant", "Variants"],
  ["assemble", "Assembly"],
  ["assembly_qc", "Assembly"],
  ["reference_assembly", "Assembly"],
  ["expression", "Expression"],
  ["download", "Archive"],
];

/**
 * Tools whose section the rollup above gets wrong, and the reason each one is
 * here rather than fixed upstream.
 *
 * ivar is REFERENCE_ASSEMBLY because its consensus step builds a sequence
 * against a reference, which is the right family for the assembly tool
 * picker. On this page it reads as a variants tool -- it trims amplicon
 * primers and calls a viral consensus, and someone scanning for it is
 * thinking about variant calling, not about RagTag and Polypolish.
 *
 * bcftools is VARIANT+UTILITY, and `utility` winning the rollup is right for
 * samtools but wrong here -- bcftools is the pileup caller, and burying it
 * under Utilities puts it a heading away from clair3 and DeepVariant, which
 * are what someone comparing variant callers is scanning for. Its VCF-toolkit
 * half is real but secondary.
 *
 * An explicit list rather than reaching for a second backend field: two
 * entries do not justify one, and being wrong here costs a tool being one
 * heading away from where it was expected, not a tool disappearing.
 */
const GROUP_OVERRIDES: Readonly<Record<string, Group>> = {
  bcftools: "Variants",
  ivar: "Variants",
};

/**
 * Tools with no `pipelines` at all (bgzip today) land here rather than being
 * dropped. Silently skipping a tool the rollup has no rule for is the failure
 * this page can least afford: it exists to answer "why is this greyed out",
 * and a tool missing from it looks like a tool that does not exist.
 */
const FALLBACK_GROUP: Group = "Utilities";

function groupFor(tool: PipelineTool): Group {
  const override = GROUP_OVERRIDES[tool.name];
  if (override) return override;
  for (const [pipeline, group] of GROUP_BY_PIPELINE) {
    if (tool.pipelines.includes(pipeline)) return group;
  }
  return FALLBACK_GROUP;
}

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

  // Only sections that actually have tools get a heading -- an empty
  // "Expression" rule is a heading over nothing.
  const sections = GROUPS.map((group) => ({
    group,
    tools: sorted.filter((tool) => groupFor(tool) === group),
  })).filter((section) => section.tools.length > 0);

  return (
    <div className="settings-page">
      <SettingsNav />
      <h1>Settings · Tools</h1>
      <p className="settings-hint">
        Grouped by the step they belong to, so this page reads the same way the
        pipeline cards do. Versions are what you cite; the one on-demand tool
        carries its action.
      </p>

      <div className="settings-tools-groups">
        {sections.map((section) => (
          <section className="settings-tools-group" key={section.group}>
            <h2 className="settings-tools-group-heading">{section.group}</h2>
            {section.tools.map((tool) => (
              <ToolRow key={tool.name} tool={tool} job={jobByTool.get(tool.name)} />
            ))}
          </section>
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
    <div className="settings-tools-row">
      <span className="settings-tools-name">{tool.name}</span>
      <span className="settings-tools-one-liner" title={tool.one_liner}>
        {tool.one_liner}
      </span>
      <span className="settings-tools-card-state">
        <ToolState tool={tool} job={job} />
      </span>
      <span className="settings-tools-action">
        <ToolAction
          tool={tool}
          job={job}
          onInstall={() => install.mutate()}
          onUninstall={confirmUninstall}
          onCancel={(id) => cancel.mutate(id)}
          onRetry={(id) => retry.mutate(id)}
          busy={install.isPending || uninstall.isPending}
        />
      </span>
    </div>
  );
}

/**
 * The status column -- the version, in the ordinary case.
 *
 * It used to read "Included — 4.7" for a bundled tool and "Installed — 4.7"
 * for an on-demand one. Both words were carrying the delivery distinction in
 * prose, and neither earned its place once every row on the page is a tool
 * that is present: "Included" was true of all but one row, which makes it
 * noise rather than information. What is actually per-tool is the version
 * (the thing you cite in a methods section) and, for the on-demand handful,
 * the action beside it. Absence still gets words, because "Not installed" and
 * "Unavailable" are the states where a bare blank would be ambiguous.
 *
 * `job` takes precedence over `tool.install_state` when both exist -- a job in
 * flight is more current than the probe result it will eventually invalidate,
 * and showing "not installed" while a pull is already 80% done would read as
 * the button not having worked.
 */
/**
 * What to render in the version column, given that `version` is not always one.
 *
 * `_clean_version` in the backend falls back to the probe's entire first line
 * when it finds no digit-dot-digit anywhere in the output. merqury.sh prints a
 * usage banner carrying no version at all, so its "version" is the 90-character
 * line `Usage: merqury.sh [-c] <read-db.meryl> ...`. The old layout hid this in
 * a wide right-hand column; a narrow version column does not, and one tool's
 * banner was wrapping into a stack of single words that broke its whole row.
 *
 * Blanking it is right for *this page* regardless of the backend: a version
 * column should show a version or nothing, and a value with no digit in it is
 * not one. Guarding on shape rather than on the name "merqury" so the next
 * tool with an unparseable banner is handled too.
 *
 * The backend bug is real and outlives this -- merqury's own probe comment says
 * the version should come from the install directory, which it never does, so
 * `/help/software` still shows the banner. That is a separate fix.
 */
function displayVersion(version: string | null): string {
  if (!version) return "";
  return /\d+\.\d/.test(version) ? version : "";
}

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

  if (tool.delivery === "bundled" || tool.install_state === "installed") {
    return <span className="settings-tools-state">{displayVersion(tool.version)}</span>;
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

  // Both actions are text rather than filled buttons: on a page that is
  // mostly rows with no action at all, a button on the one or two rows that
  // have one pulls the eye away from the list it is meant to annotate.
  if (tool.install_state === "installed") {
    return (
      <button className="settings-tools-uninstall" onClick={onUninstall} disabled={busy}>
        Uninstall
      </button>
    );
  }

  // Also covers "unknown" (no docker client, daemon unreachable): offering
  // Install is more useful than nothing, and the handler's own probe will
  // report the real reason if it still fails.
  return (
    <button className="settings-tools-install" onClick={onInstall} disabled={busy}>
      Install
    </button>
  );
}
