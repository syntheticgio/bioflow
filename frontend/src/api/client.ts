import type {
  AnnotationEditRow,
  AiFetchModelsResult,
  AiPreset,
  AiProvider,
  AiProviderInput,
  AiRouting,
  AlignDefaults,
  AlignEnvelope,
  AlignerSchema,
  AnnotationFeature,
  AnnotationFeaturePage,
  AnnotationGenePage,
  AnnotationWindow,
  AssembleRequest,
  AssemblerSchema,
  AssemblyParams,
  AlignRequest,
  AssemblyAccepted,
  BulkResult,
  CompleteAccepted,
  CompletenessDefaults,
  CompletenessRequest,
  ContigsPage,
  DataObject,
  DataSources,
  DeDefaults,
  DeletionPreview,
  DeRequest,
  ExportArchive,
  DeResultsPage,
  ExtractedSequence,
  FacetValue,
  Facets,
  FeatureQuery,
  Feedback,
  FeedbackSubmission,
  GeneralSettings,
  JobLog,
  JobSummary,
  LineageDownloadRequest,
  LineageStatus,
  LocalDatabaseEntry,
  LocalDatabaseSubmission,
  MateSuggestion,
  MetadataSchema,
  NcbiResolveResponse,
  CurrentVersion,
  NodeInfo,
  NodeProvisionRequest,
  NodeProvisionStatus,
  NodeUpdateStatus,
  ObjectComputations,
  ObjectDetail,
  ObjectRole,
  OrganismSearchRequest,
  OrganismSearchResponse,
  OrganismSuggestResponse,
  OverdueSchedule,
  PipelineSuggestion,
  PipelineTools,
  Profile,
  Project,
  ProjectDetail,
  ProteinRecords,
  ProteinStructure,
  ProvenanceNarrative,
  ProvenanceProse,
  QuantifyDefaults,
  QuantifyRequest,
  ReferenceOption,
  RegisterAccepted,
  ReplanResult,
  ResourceLimits,
  ResourceLimitsIn,
  RunDetail,
  RunSummary,
  ScaffoldRequest,
  ScheduleInfo,
  SraAccepted,
  SraDownloadRequest,
  SraResolveResponse,
  TileMatrix,
  TimingEstimate,
  TrimDefaults,
  TrimRequest,
  SearchParams,
  SearchResults,
  SystemLoad,
  SystemStats,
  UniProtAccepted,
  UniProtResolveResponse,
  UploadCreated,
  UploadSessionInfo,
  VariantDefaults,
  VariantQuery,
  VariantRequest,
  VariantsPage,
  VariantStructure,
  DerivedGraph,
  MetricsStats,
  RecentRuns,
  JobTypeRuns,
  NodeTypeMeta,
  WorkflowRunDetail,
  WorkflowRunRow,
  VersionInfo,
  WorkflowDefinition,
  WorkflowDefinitionInput,
  WorkflowRunSummary,
} from "./types";

import { useProfileStore } from "../stores/profileStore";

const BASE = "/api/v1";

/**
 * The header every user-data route needs to know whose library to answer with.
 *
 * Read through `getState()` rather than the hook because these are plain
 * functions, not components -- there is nothing to re-render, and the value
 * only has to be correct at the moment the request is built.
 *
 * Returns an empty object when no profile is selected, so the header is absent
 * rather than empty. The backend rejects both, but an absent header means
 * `profile_unresolved`, which is the honest description of the state; sending
 * `X-BioFlow-Profile: ` would instead claim a profile was chosen and read as a
 * malformed id.
 */
function profileHeaders(): Record<string, string> {
  const id = useProfileStore.getState().current?.id;
  return id ? { "X-BioFlow-Profile": id } : {};
}

/**
 * `?profile=<id>` for URLs opened as a plain `<a href>` rather than fetched
 * through `request()`. A browser-native navigation never runs `profileHeaders`,
 * so these routes take the same profile id as a query param instead -- see
 * `get_current_owner_linkable` on the backend.
 */
function profileQuery(): string {
  const id = useProfileStore.getState().current?.id;
  return id ? `profile=${encodeURIComponent(id)}` : "";
}

export class ApiRequestError extends Error {
  code: string;
  status: number;
  details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details = {}) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.body && !(init.body instanceof Blob) && !(init.body instanceof ArrayBuffer)
        ? { "Content-Type": "application/json" }
        : {}),
      ...profileHeaders(),
      // Last, so an explicit per-call header can still override the profile --
      // which is how a call made on behalf of a profile other than the selected
      // one would have to work.
      ...init?.headers,
    },
  });

  if (!res.ok) {
    let code = "http_error";
    let message = `${res.status} ${res.statusText}`;
    let details = {};
    try {
      const body = await res.json();
      code = body.code ?? code;
      message = body.message ?? message;
      details = body.details ?? {};
    } catch {
      // Non-JSON error body (nginx, proxy). Keep the status text.
    }
    throw new ApiRequestError(res.status, code, message, details);
  }

  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export const api = {
  /**
   * The generic verb surface. Prefer these for new endpoints over adding
   * another named wrapper: `api.post<Project>("/projects", { name })` says the
   * same thing a one-line `createProject` would, without a second place to
   * look it up.
   *
   * The named methods below predate this and are being retired as their last
   * callers go (see #405). Not all of them can be: `putChunk` sends a raw
   * `ArrayBuffer` with an `X-Chunk-SHA256` header, and `objectDownloadUrl` /
   * `exportProject` build a URL for the browser to fetch rather than issuing
   * a request at all. Those keep their dedicated methods; anything else that
   * needs a header or a non-JSON body should pass `init` through rather than
   * grow a wrapper.
   *
   * `body` is `unknown` rather than a generic parameter because the payload is
   * whatever the route accepts, and the type that matters at a call site is
   * the response `T`. Passing `undefined` sends `JSON.stringify(undefined)`,
   * i.e. no body -- which is what the several POST routes that take no payload
   * want.
   */
  get: <T>(path: string, init?: RequestInit) => request<T>(path, init),

  post: <T>(path: string, body?: unknown, init?: RequestInit) =>
    request<T>(path, { ...init, method: "POST", body: JSON.stringify(body) }),

  patch: <T>(path: string, body: unknown, init?: RequestInit) =>
    request<T>(path, { ...init, method: "PATCH", body: JSON.stringify(body) }),

  delete: <T>(path: string, init?: RequestInit) =>
    request<T>(path, { ...init, method: "DELETE" }),

  /**
   * The four profile routes, and the only ones callable before a profile is
   * selected. They send the `X-BioFlow-Profile` header like everything else
   * -- `request<T>` adds it unconditionally -- but the backend ignores it
   * here, because requiring a profile to list the profiles would leave a
   * fresh install with no way in.
   */
  listProfiles: () => request<Profile[]>("/profiles"),

  /**
   * `is_first_boot` is the flag that makes a new profile adopt the library
   * that predates profiles rather than start empty.
   *
   * Every document written before this feature carries `owner: "local"`, and
   * an adopted profile's `owner_id()` returns that same `"local"` string --
   * so adoption moves nothing and touches nothing, it just makes one profile
   * answer to the name the existing data already uses. Sending it when
   * profiles already exist is a 422; the backend refuses a second adopter
   * because two profiles answering to `"local"` would both claim the same
   * library.
   *
   * Failing to send it on a populated install is the expensive mistake, and
   * it is silent: the new profile gets its own id as owner, sees nothing, and
   * the user's library reads as deleted. It is not -- it is still owned by
   * `"local"` -- but no profile can adopt it afterwards, because
   * `create_profile` only allows the flag while the collection is empty. So
   * pass it exactly when `listProfiles()` came back empty, and never on
   * assumption.
   */
  createProfile: (body: {
    username: string;
    password?: string;
    email?: string;
    is_first_boot?: boolean;
  }) => request<Profile>("/profiles", { method: "POST", body: JSON.stringify(body) }),

  /**
   * Returns no token and sets no cookie; the client just starts sending the
   * id. Calling it is still worth it for the password check and because the
   * backend stamps `last_used_at` here and nowhere else.
   */
  selectProfile: (id: string, password?: string) =>
    request<Profile>(`/profiles/${id}/select`, {
      method: "POST",
      body: JSON.stringify({ password: password ?? null }),
    }),

  deleteProfile: (id: string) => request<void>(`/profiles/${id}`, { method: "DELETE" }),

  listProjects: (parentId?: string) =>
    request<Project[]>(
      `/projects${parentId ? `?parent_id=${encodeURIComponent(parentId)}` : ""}`,
    ),

  getProject: (id: string) => request<ProjectDetail>(`/projects/${id}`),

  createProject: (body: { name: string; description?: string; parent_id?: string }) =>
    request<Project>("/projects", { method: "POST", body: JSON.stringify(body) }),

  updateProject: (id: string, body: Record<string, unknown>) =>
    request<Project>(`/projects/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  deleteProject: (id: string, cascade = false) =>
    request<void>(`/projects/${id}?cascade=${cascade}`, { method: "DELETE" }),

  deletionPreview: (id: string) =>
    request<DeletionPreview>(`/projects/${id}/deletion-preview`),

  /**
   * Queues a background job that writes one `.tar.gz` archive of the
   * project and its descendants. Returns as soon as the job is enqueued
   * (202) -- the archive itself is not ready yet, so this does not resolve
   * once the file exists, only once the queue has accepted the work.
   * `thresholdBytes` is a query param on the backend, not a body field
   * (see `backend/app/api/v1/exports.py`); omitted entirely when undefined
   * so the backend's own 100 MB default applies rather than this client
   * re-stating it.
   */
  createExport: (projectId: string, thresholdBytes?: number) =>
    request<{ job_id: string }>(
      `/projects/${projectId}/export${
        thresholdBytes != null ? `?threshold_bytes=${thresholdBytes}` : ""
      }`,
      { method: "POST" },
    ),

  listExports: () => request<ExportArchive[]>("/exports"),

  /**
   * Fetches an archive's bytes and returns a Blob, rather than the bare URL
   * `objectDownloadUrl` hands back for a plain `<a href>`. The download
   * route (`GET /exports/{name}/download`) resolves its owner from the
   * `X-BioFlow-Profile` header only -- unlike `/objects/{id}/download`,
   * it has no `?profile=` query fallback for a browser-native navigation
   * -- so a plain link would 401/422 for any profile but the legacy
   * "local" one. This attaches that header the same way `request()` does
   * for every other authenticated call, then hands back a Blob instead of
   * parsed JSON; the caller turns it into a download with
   * `URL.createObjectURL`.
   */
  downloadExport: async (name: string): Promise<Blob> => {
    const res = await fetch(`${BASE}/exports/${encodeURIComponent(name)}/download`, {
      headers: profileHeaders(),
    });
    if (!res.ok) {
      throw new ApiRequestError(res.status, "http_error", `${res.status} ${res.statusText}`);
    }
    return res.blob();
  },

  listObjects: (projectId: string) =>
    request<DataObject[]>(`/projects/${projectId}/objects`),

  getObject: (id: string) => request<ObjectDetail>(`/objects/${id}`),

  getObjectComputations: (id: string) =>
    request<ObjectComputations>(`/objects/${id}/computations`),

  getProvenanceNarrative: (id: string) =>
    request<ProvenanceNarrative>(`/objects/${id}/provenance-narrative`),

  generateProvenanceProse: (id: string) =>
    request<ProvenanceProse>(`/objects/${id}/provenance-narrative/prose`, {
      method: "POST",
    }),

  updateObject: (id: string, body: Record<string, unknown>) =>
    request<DataObject>(`/objects/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  pairObject: (id: string, body: { mate_object_id: string; read_number: number }) =>
    request<DataObject>(`/objects/${id}/pair`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  unpairObject: (id: string) =>
    request<DataObject>(`/objects/${id}/pair`, { method: "DELETE" }),

  deleteObject: (id: string) => request<void>(`/objects/${id}`, { method: "DELETE" }),

  reingestObject: (id: string) =>
    request<{ object_id: string; job_id: string }>(`/objects/${id}/reingest`, {
      method: "POST",
    }),

  inferMoleculeType: (id: string) =>
    request<{ molecule_type: string | null; basis: string }>(
      `/objects/${id}/infer-molecule-type`,
      { method: "POST" },
    ),

  /**
   * URL for downloading the object's raw stored bytes.
   *
   * A plain URL rather than a `request` call: these files run to gigabytes, so
   * the browser should stream it to disk itself instead of us buffering the
   * whole thing into memory as a Blob to hand back. Opened via a plain
   * `<a href>`, which never attaches `X-BioFlow-Profile`, so the profile
   * rides along as a query param instead -- see `get_current_owner_linkable`.
   */
  objectDownloadUrl: (id: string) =>
    `${BASE}/objects/${id}/download?${profileQuery()}`,

  // --- Chunked uploads ---
  createUpload: (body: {
    project_id: string;
    filename: string;
    total_size: number;
    client_sha256?: string;
  }) => request<UploadCreated>("/uploads", { method: "POST", body: JSON.stringify(body) }),

  getUpload: (id: string) => request<UploadSessionInfo>(`/uploads/${id}`),

  listUploads: (projectId?: string) =>
    request<UploadSessionInfo[]>(
      `/uploads${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
    ),

  /** Raw-body PUT. Multipart would spool and buffer an extra copy per chunk. */
  putChunk: (
    sessionId: string,
    index: number,
    data: ArrayBuffer,
    sha256: string,
    signal?: AbortSignal,
  ) =>
    request<{ index: number; missing_count: number }>(
      `/uploads/${sessionId}/chunks/${index}`,
      {
        method: "PUT",
        body: data,
        headers: {
          "Content-Type": "application/octet-stream",
          "X-Chunk-SHA256": sha256,
        },
        signal,
      },
    ),

  completeUpload: (sessionId: string) =>
    request<CompleteAccepted>(`/uploads/${sessionId}/complete`, { method: "POST" }),

  abortUpload: (sessionId: string) =>
    request<void>(`/uploads/${sessionId}`, { method: "DELETE" }),

  registerInPlace: (projectId: string, path: string, name?: string) =>
    request<RegisterAccepted>(`/projects/${projectId}/objects/register`, {
      method: "POST",
      body: JSON.stringify({ path, name }),
    }),

  // --- Project operations ---

  mergeFastq: (projectId: string, objectIds: string[], outputName: string) =>
    request<{ job_id: string }>(`/projects/${projectId}/operations/merge-fastq`, {
      method: "POST",
      body: JSON.stringify({ object_ids: objectIds, output_name: outputName }),
    }),

  batchRename: (projectId: string, renames: { id: string; name: string }[]) =>
    request<{ updated: number }>(`/projects/${projectId}/operations/batch-rename`, {
      method: "POST",
      body: JSON.stringify({ renames }),
    }),

  batchTags: (projectId: string, objectIds: string[], add: string[], remove: string[]) =>
    request<{ updated: number }>(`/projects/${projectId}/operations/batch-tags`, {
      method: "POST",
      body: JSON.stringify({ object_ids: objectIds, add, remove }),
    }),

  exportProject: (projectId: string) =>
    `${BASE}/projects/${projectId}/operations/export?${profileQuery()}`,

  qcAllReads: (projectId: string) =>
    request<{ job_ids: string[] }>(`/projects/${projectId}/operations/qc-all`, {
      method: "POST",
      body: JSON.stringify({}),
    }),

  // --- Search and metadata ---
  searchObjects: (p: SearchParams) => {
    const params = new URLSearchParams();
    if (p.q) params.set("q", p.q);
    if (p.projectId) params.set("project_id", p.projectId);
    for (const k of p.kind ?? []) params.append("kind", k);
    for (const s of p.status ?? []) params.append("status", s);
    for (const t of p.tag ?? []) params.append("tag", t);
    for (const m of p.meta ?? []) params.append("meta", m);
    params.set("limit", String(p.limit ?? 100));
    if (p.cursor) params.set("cursor", p.cursor);
    return request<SearchResults>(`/search/objects?${params}`);
  },

  searchFacets: (projectId?: string) =>
    request<Facets>(
      `/search/facets${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
    ),

  metadataValues: (key: string, projectId?: string) =>
    request<{ key: string; values: FacetValue[] }>(
      `/search/metadata-values/${encodeURIComponent(key)}` +
        (projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""),
    ),

  metadataSchema: (kind: string, role?: ObjectRole | null) =>
    request<MetadataSchema>(
      `/metadata/schemas/${encodeURIComponent(kind)}` +
        (role ? `?role=${encodeURIComponent(role)}` : ""),
    ),

  bulkMetadata: (objectIds: string[], set: Record<string, unknown>, unset: string[] = []) =>
    request<BulkResult>("/objects/bulk-metadata", {
      method: "POST",
      body: JSON.stringify({ object_ids: objectIds, set, unset }),
    }),

  bulkTags: (objectIds: string[], add: string[] = [], remove: string[] = []) =>
    request<BulkResult>("/objects/bulk-tags", {
      method: "POST",
      body: JSON.stringify({ object_ids: objectIds, add, remove }),
    }),

  systemStats: () => request<SystemStats>("/system/stats"),

  systemLoad: () => request<SystemLoad>("/system/load"),
  nodes: () => request<NodeInfo[]>("/nodes"),

  provisionNode: (body: NodeProvisionRequest) =>
    request<{ task_id: string; status: string }>("/nodes/provision", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getProvisionStatus: (taskId: string) =>
    request<NodeProvisionStatus>(`/nodes/provision/${encodeURIComponent(taskId)}`),

  /** The primary's own image digest and version, to compare each node's
   *  reported digest against. */
  currentVersion: () => request<CurrentVersion>("/nodes/current-version"),

  updateNode: (nodeId: string, drain: boolean) =>
    request<{ task_id: string; status: string }>(
      `/nodes/${encodeURIComponent(nodeId)}/update`,
      { method: "POST", body: JSON.stringify({ drain }) },
    ),

  getUpdateStatus: (taskId: string) =>
    request<NodeUpdateStatus>(`/nodes/update/${encodeURIComponent(taskId)}`),

  getVersion: () => request<VersionInfo>("/version"),

  sources: () => request<DataSources>("/system/sources"),

  submitFeedback: (body: FeedbackSubmission) =>
    request<Feedback>("/feedback", { method: "POST", body: JSON.stringify(body) }),

  listFeedback: () => request<Feedback[]>("/feedback"),

  submitLocalDatabase: (body: LocalDatabaseSubmission) =>
    request<LocalDatabaseEntry>("/local-databases", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listLocalDatabases: () => request<LocalDatabaseEntry[]>("/local-databases"),

  listSchedules: () => request<ScheduleInfo[]>("/schedules"),

  overdueSchedules: () => request<{ overdue: OverdueSchedule[] }>("/schedules/overdue"),

  updateSchedule: (name: string, body: Record<string, unknown>) =>
    request<ScheduleInfo>(`/schedules/${name}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  runScheduleNow: (name: string) =>
    request<{ name: string; job_id: string }>(`/schedules/${name}/run-now`, {
      method: "POST",
    }),

  listJobs: (
    opts: {
      projectId?: string;
      state?: string;
      /** Comma-separated states, or "active" for everything in flight. */
      states?: string;
      type?: string;
      /** Jobs launched against one file. */
      objectId?: string;
      limit?: number;
    } = {},
  ) => {
    const params = new URLSearchParams();
    if (opts.projectId) params.set("project_id", opts.projectId);
    if (opts.states) params.set("states", opts.states);
    else if (opts.state) params.set("state", opts.state);
    if (opts.type) params.set("type", opts.type);
    if (opts.objectId) params.set("object_id", opts.objectId);
    params.set("limit", String(opts.limit ?? 50));
    return request<JobSummary[]>(`/jobs?${params}`);
  },

  getJobLog: (id: string, tail = 200) =>
    request<JobLog>(`/jobs/${id}/log?tail=${tail}`),

  /** Pipeline runs -- one per user action -- newest first, with derived status. */
  listRuns: (opts: { projectId?: string; limit?: number } = {}) => {
    const params = new URLSearchParams();
    if (opts.projectId) params.set("project_id", opts.projectId);
    params.set("limit", String(opts.limit ?? 50));
    return request<RunSummary[]>(`/runs?${params}`);
  },

  getRun: (id: string) => request<RunDetail>(`/runs/${id}`),

  cancelRun: (id: string) =>
    request<{ run_id: string; jobs: Record<string, string> }>(
      `/runs/${id}/cancel`,
      { method: "POST" },
    ),

  getJob: (id: string) =>
    request<
      JobSummary & {
        timing_estimate?: TimingEstimate;
        eta_seconds?: number;
        pct_estimated?: number;
      }
    >(`/jobs/${id}`),

  cancelJob: (id: string) =>
    request<{ job_id: string; outcome: string }>(`/jobs/${id}/cancel`, {
      method: "POST",
    }),

  retryJob: (id: string) => request<JobSummary>(`/jobs/${id}/retry`, { method: "POST" }),

  jobTypes: () => request<Record<string, unknown>>("/jobs/types"),

  /** Aggregated computation cost, for the Reference → Metrics page. */
  metrics: () => request<MetricsStats>("/jobs/metrics"),
  metricsRuns: () => request<RecentRuns>("/jobs/metrics/runs"),
  metricsRunsFor: (jobType: string, limit: number, offset: number) =>
    request<JobTypeRuns>(
      `/jobs/metrics/runs?job_type=${encodeURIComponent(jobType)}&limit=${limit}&offset=${offset}`,
    ),

  // --- Pipelines ---

  pipelineTools: () => request<PipelineTools>("/pipelines/tools"),

  /** Queue a pull of an on-demand tool's image. Returns the existing job,
   *  not a duplicate, if one is already in flight for this tool. */
  installTool: (name: string) =>
    request<JobSummary>(`/pipelines/tools/${encodeURIComponent(name)}/install`, {
      method: "POST",
    }),

  /** Queue removal of an on-demand tool's image. Refused server-side for a
   *  bundled tool, one that was never installed, or one a running job is
   *  currently using. */
  uninstallTool: (name: string) =>
    request<JobSummary>(`/pipelines/tools/${encodeURIComponent(name)}/install`, {
      method: "DELETE",
    }),

  trimDefaults: (tool: string = "fastp") =>
    request<TrimDefaults>(`/pipelines/defaults?tool=${encodeURIComponent(tool)}`),

  /** The file this one would be trimmed alongside, or null. */
  detectMate: (objectId: string) =>
    request<MateSuggestion | null>(`/pipelines/mate/${objectId}`),

  launchTrim: (
    body: TrimRequest,
    targetNode?: string,
  ) =>
    request<JobSummary>(
      `/pipelines/trim${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  /** Queue a QC run. Read-only: produces a report, derives no files. */
  launchQC: (objectId: string, targetNode?: string) =>
    request<JobSummary>(
      `/pipelines/qc${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify({ object_id: objectId }),
      }),

  /**
   * Whether a local model is up and could write a summary right now.
   *
   * Probed rather than assumed: the model server is a process on the host that
   * the user starts and stops independently of this app, so the answer is only
   * good for as long as it takes to read it.
   */
  summaryStatus: () =>
    request<{ available: boolean; reason?: string; model?: string | null }>(
      "/pipelines/summary/status",
    ),

  deSummaryStatus: () =>
    request<{ available: boolean; reason?: string; model?: string | null; provider_name?: string }>(
      "/pipelines/de-summary/status"
    ),
  launchDeSummary: (objectId: string, targetNode?: string) =>
    request<JobSummary>(
      `/pipelines/de-summary${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify({ object_id: objectId }),
      }),
  variantSummaryStatus: () =>
    request<{ available: boolean; reason?: string; model?: string | null; provider_name?: string }>(
      "/pipelines/variant-summary/status"
    ),
  launchVariantSummary: (objectId: string, targetNode?: string) =>
    request<JobSummary>(
      `/pipelines/variant-summary${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify({ object_id: objectId }),
      }),

  /** The known-provider table. Static; safe to cache indefinitely. */
  aiPresets: () => request<AiPreset[]>("/settings/ai/presets"),

  aiProviders: () => request<AiProvider[]>("/settings/ai/providers"),

  createAiProvider: (body: AiProviderInput) =>
    request<AiProvider>("/settings/ai/providers", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /**
   * Update a provider. **Omit `api_key` to keep the stored key.**
   *
   * The backend distinguishes an absent field from an explicit null, so a form
   * that always sent `api_key` -- even as an empty string -- would wipe the
   * credential every time the user renamed a provider. Send the field only
   * when the user typed something, and send `null` only to deliberately clear.
   */
  updateAiProvider: (id: string, body: AiProviderInput) =>
    request<AiProvider>(`/settings/ai/providers/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  deleteAiProvider: (id: string) =>
    request<void>(`/settings/ai/providers/${id}`, { method: "DELETE" }),

  /** Fetch the model list, which is also the connection test. A provider
   *  failure comes back as a 200 with `status: "failed"`, not a thrown error --
   *  it renders as a badge, not a toast. */
  fetchAiModels: (id: string) =>
    request<AiFetchModelsResult>(`/settings/ai/providers/${id}/fetch-models`, {
      method: "POST",
    }),

  aiRouting: () => request<AiRouting>("/settings/ai/routing"),

  setAiRouting: (body: { default: string | null; slots: Record<string, string> }) =>
    request<AiRouting>("/settings/ai/routing", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  generalSettings: () => request<GeneralSettings>("/settings/general"),

  setGeneralSettings: (body: GeneralSettings) =>
    request<GeneralSettings>("/settings/general", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  resourceLimits: () => request<ResourceLimits>("/settings/resources"),

  setResourceLimits: (body: ResourceLimitsIn) =>
    request<ResourceLimits>("/settings/resources", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  /**
   * Background prose about a species. Null when there is nothing to say --
   * an unrecognized organism or no model server both land here, and neither is
   * an error for a decorative field.
   *
   * Cached server-side per species, so this is an indexed read for every file
   * after the first of a given organism.
   */
  organismBlurb: (organism: string) =>
    request<{ organism: string; text: string; model: string | null } | null>(
      `/pipelines/organism/${encodeURIComponent(organism)}`,
    ),

  /**
   * A plain-language explanation of a job error, from cache or freshly
   * generated. Returns null when there is no provider configured or the
   * model produced nothing -- both ordinary states, not an error for a
   * decorative field.
   *
   * Cached server-side per (code, message) pair, so re-explaining the same
   * underlying error on a different job is an indexed read.
   */
  failureExplanation: (code: string, message: string) =>
    request<{ text: string; model: string | null } | null>(
      `/pipelines/failure-explanation?code=${encodeURIComponent(code)}&message=${encodeURIComponent(message)}`,
    ),

  /** Queue a narrative summary of a file's QC data and metadata. */
  launchSummary: (objectId: string, targetNode?: string) =>
    request<JobSummary>(
      `/pipelines/summary${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify({ object_id: objectId, force: true }),
      }),

  /**
   * URL of a generated QC report.
   *
   * Not fetched through `request`: the report is an HTML page opened in a new
   * tab, not JSON. The server sandboxes it via CSP -- see `get_qc_report` --
   * because FastQC embeds sequence data taken straight from the reads. The
   * profile rides along as a query param for the same reason the CSP is
   * needed at all: this is a plain link, not a `fetch` call.
   */
  qcReportUrl: (objectId: string, reportPath: string) =>
    `${BASE}/pipelines/qc/report/${objectId}/${reportPath}?${profileQuery()}`,

  /**
   * The per-tile quality matrix. A `fetch`, not a link -- unlike
   * `qcReportUrl` above, which is a plain `<a href>` and therefore needs the
   * profile as a query param. This one rides the normal profile header.
   */
  qcTileMatrix: (objectId: string) =>
    request<TileMatrix>(`/pipelines/qc/tiles/${objectId}`),

  // --- NCBI SRA ---

  /**
   * Resolve an accession to its runs. Read-only; starts no download.
   *
   * `project_id` is optional and only marks which runs the project already
   * holds -- resolving is useful before a project is chosen.
   */
  sraResolve: (body: {
    accession: string;
    platform_filter?: string | null;
    project_id?: string | null;
  }) =>
    request<SraResolveResponse>("/sra/resolve", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  sraDownload: (body: SraDownloadRequest) =>
    request<SraAccepted>("/sra/download", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // --- NCBI unified resolve ---

  /**
   * Resolve an accession without knowing in advance whether it names an SRA
   * run/study or a GenBank/RefSeq assembly. `kind` on the response says which
   * branch (`sra` or `assembly`) is populated.
   */
  ncbiResolve: (body: {
    accession: string;
    platform_filter?: string | null;
    project_id?: string | null;
  }) =>
    request<NcbiResolveResponse>("/ncbi/resolve", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  ncbiDownloadAssembly: (body: {
    project_id: string;
    accession: string;
    components: string[];
  }) =>
    request<AssemblyAccepted>("/ncbi/download-assembly", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Autocomplete candidates for a partially typed organism name. */
  ncbiOrganismSuggest: (q: string) =>
    request<OrganismSuggestResponse>(
      `/ncbi/organism-suggest?q=${encodeURIComponent(q)}`,
    ),

  /** Paginated assemblies and sequencing runs for a resolved organism. */
  ncbiOrganismSearch: (body: OrganismSearchRequest) =>
    request<OrganismSearchResponse>("/ncbi/organism-search", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // --- UniProt ---

  /**
   * Resolve whatever was typed into the UniProt box without knowing in
   * advance what it names. `kind` says which branch is populated: a
   * `proteome` (with `candidates` when the organism has no reference one and
   * a choice must be made), `proteins`, or `empty`.
   */
  uniprotResolve: (body: { query: string; project_id?: string | null }) =>
    request<UniProtResolveResponse>("/uniprot/resolve", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  uniprotDownload: (body: {
    project_id: string;
    proteome_id?: string | null;
    accessions?: string[];
    reviewed_only: boolean;
    organism?: string | null;
    protein_count?: number | null;
  }) =>
    request<UniProtAccepted>("/uniprot/download", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Alignment defaults for one file, including a read group from its metadata. */
  alignDefaults: (objectId: string) =>
    request<AlignDefaults>(`/pipelines/align/defaults/${objectId}`),

  alignerSchema: (aligner: string) =>
    request<AlignerSchema>(
      `/pipelines/aligners/${encodeURIComponent(aligner)}/schema`,
    ),

  alignEnvelope: (objectId: string, referenceId: string) =>
    request<AlignEnvelope>(
      `/pipelines/align-envelope?object_id=${objectId}&reference_id=${referenceId}`,
    ),

  /** Candidate references in a project, each with its index status. */
  references: (projectId: string) =>
    request<{ references: ReferenceOption[] }>(`/pipelines/references/${projectId}`),

  /** A project's gene annotations, for the STAR annotated-index option. */
  annotations: (projectId: string) =>
    request<{ annotations: { object_id: string; name: string }[] }>(
      `/pipelines/annotations/${projectId}`,
    ),

  /** Which pipelines to offer for one file, each with the reason it can or
   * cannot run. Advisory: a failure here costs the Actions tab its cards, not
   * its manual controls. */
  suggestions: (objectId: string) =>
    request<{ suggestions: PipelineSuggestion[] }>(
      `/pipelines/suggestions/${objectId}`,
    ),

  launchSuggestion: (
    endpoint: string,
    body: Record<string, unknown>,
    targetNode?: string,
  ) =>
    request<JobSummary>(
      `${endpoint}${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  buildIndex: (body: {
    reference_id: string;
    aligner: string;
    annotation_id?: string | null;
  }) =>
    request<JobSummary>("/pipelines/index", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  launchAlignment: (body: AlignRequest, targetNode?: string) =>
    request<JobSummary>(
      `/pipelines/align${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      }),

  /** Propose a fitting configuration for a refused job, or say why there is
   * none. Feeds the resource-refusal card's Auto-adjust button. */
  replan: (jobType: string, params: Record<string, unknown>) =>
    request<ReplanResult>("/pipelines/replan", {
      method: "POST",
      body: JSON.stringify({ job_type: jobType, params }),
    }),

  /** Variant calling defaults for one BAM, including the inferred caller. */
  assemblerSchema: (assembler: string) =>
    request<AssemblerSchema>(
      `/pipelines/assemblers/${encodeURIComponent(assembler)}/schema`,
    ),

  assembleDefaults: (objectId: string) =>
    request<Partial<AssemblyParams>>(`/pipelines/assemble/defaults/${objectId}`),

  launchAssembly: (body: AssembleRequest, targetNode?: string) =>
    request<JobSummary>(
      `/pipelines/assemble${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      }),

  completenessDefaults: (objectId: string) =>
    request<CompletenessDefaults>(`/pipelines/completeness/defaults/${objectId}`),

  launchCompleteness: (body: CompletenessRequest, targetNode?: string) =>
    request<JobSummary>(
      `/pipelines/completeness${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      }),

  launchScaffold: (body: ScaffoldRequest, targetNode?: string) =>
    request<JobSummary>(
      `/pipelines/scaffold${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      }),

  downloadLineage: (body: LineageDownloadRequest) =>
    request<JobSummary>("/pipelines/completeness/lineage", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  lineageStatus: (lineage: string, odb?: string) =>
    request<LineageStatus>(
      `/pipelines/completeness/lineage-status?${new URLSearchParams({
        lineage,
        ...(odb ? { odb } : {}),
      })}`,
    ),

  variantDefaults: (bamId: string) =>
    request<VariantDefaults>(`/pipelines/variants/defaults/${bamId}`),

  launchVariantCalling: (body: VariantRequest, targetNode?: string) =>
    request<JobSummary>(
      `/pipelines/variants${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      }),

  /** Counting defaults for one BAM: the annotation it would use, and the
   * strandedness read off its alignment. */
  quantifyDefaults: (bamId: string) =>
    request<QuantifyDefaults>(`/pipelines/quantify/defaults/${bamId}`),

  launchQuantify: (body: QuantifyRequest, targetNode?: string) =>
    request<JobSummary>(
      `/pipelines/quantify${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      }),

  /** The samples, conditions and contrast the DE dialog opens with.
   * Project-scoped, unlike every other defaults route here -- differential
   * expression is an operation on a project, not on a file. */
  deDefaults: (projectId: string) =>
    request<DeDefaults>(`/pipelines/differential-expression/defaults/${projectId}`),

  launchDifferentialExpression: (body: DeRequest, targetNode?: string) =>
    request<JobSummary>(
      `/pipelines/differential-expression${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      }),

  /** A page of a DE results table, sorted and filtered server-side. */
  deResults: (
    objectId: string,
    q: {
      offset: number;
      limit: number;
      sort?: string;
      direction?: string;
      search?: string;
      max_padj?: number;
    }
  ) => {
    const params = new URLSearchParams({
      offset: String(q.offset),
      limit: String(q.limit),
    });
    if (q.sort) params.set("sort", q.sort);
    if (q.direction) params.set("direction", q.direction);
    if (q.search) params.set("search", q.search);
    if (q.max_padj != null) params.set("max_padj", String(q.max_padj));
    return request<DeResultsPage>(
      `/pipelines/de/results/${objectId}?${params.toString()}`
    );
  },

  /** Queue the Results computation for a BAM. Read-only: produces facts and
   * one TSV report, no derived objects. */
  launchBamStats: (objectId: string, targetNode?: string) =>
    request<JobSummary>(
      `/pipelines/bamstats${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify({ object_id: objectId }),
      }),

  /** Queue the RNA-seq transcript QC computation for a BAM against a GTF
   * annotation. Read-only: produces facts only, no derived objects. */
  launchTranscriptQc: (objectId: string, gtfObjectId: string) =>
    request<JobSummary>("/pipelines/transcript-qc", {
      method: "POST",
      body: JSON.stringify({ object_id: objectId, gtf_object_id: gtfObjectId }),
    }),

  /** A page of the per-contig table, sorted the same way the job wrote it
   * (mapped reads descending). */
  bamStatsContigs: (objectId: string, reportPath: string, offset: number, limit: number) =>
    request<ContigsPage>(
      `/pipelines/bamstats/report/${objectId}/${reportPath}?offset=${offset}&limit=${limit}`,
    ),

  /** URL for downloading the complete per-contig TSV. */
  bamStatsDownloadUrl: (objectId: string, reportPath: string) =>
    `${BASE}/pipelines/bamstats/report/${objectId}/${reportPath}?download=1&${profileQuery()}`,

  /** Queue the Results computation for a VCF/BCF. Read-only: produces facts
   * and a variants TSV, no derived objects. */
  launchVcfStats: (objectId: string, targetNode?: string) =>
    request<JobSummary>(
      `/pipelines/vcfstats${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify({ object_id: objectId }),
      }),

  /** A page of the variant table. Filters are applied server-side against
   *  the SQLite index rather than by slicing a TSV -- a plant VCF holds
   *  millions of rows, where reading the whole file costs ~440 MB per
   *  request. */
  vcfStatsVariants: (objectId: string, q: VariantQuery) => {
    const p = new URLSearchParams({
      offset: String(q.offset),
      limit: String(q.limit),
    });
    if (q.contig) p.set("contig", q.contig);
    if (q.posMin != null) p.set("pos_min", String(q.posMin));
    if (q.posMax != null) p.set("pos_max", String(q.posMax));
    if (q.filterValue) p.set("filter_value", q.filterValue);
    if (q.variantType) p.set("variant_type", q.variantType);
    if (q.minQual != null) p.set("min_qual", String(q.minQual));
    if (q.consequence) p.set("consequence", q.consequence);
    if (q.skipCount) p.set("skip_count", "true");
    return request<VariantsPage>(
      `/pipelines/vcfstats/variants/${objectId}?${p.toString()}`,
    );
  },

  /** The protein structure for one gene's variants, if there is one.
   *
   *  Requested on click rather than per row: most genes resolve to nothing,
   *  and asking for a whole page would spend dozens of round trips to decide
   *  the appearance of buttons that mostly never get pressed.
   *
   *  The residue is deliberately not a parameter. The server reads it from
   *  the variant database, because it is what picks between two proteins
   *  sharing a gene symbol -- a caller-supplied one could select a protein
   *  too short for a residue the callset actually contains. */
  variantStructure: (objectId: string, gene: string) =>
    request<VariantStructure>(
      `/pipelines/vcfstats/structure/${objectId}?gene=${encodeURIComponent(gene)}`,
    ),

  /** URL for downloading the complete variants TSV. */
  vcfStatsDownloadUrl: (objectId: string, reportPath: string) =>
    `${BASE}/pipelines/vcfstats/report/${objectId}/${reportPath}?${profileQuery()}`,

  /** One page of a protein FASTA's records, optionally filtered.
   *
   *  `q` matches identifier or description: a user does not know which field
   *  holds the text they remember. */
  proteinRecords: (
    objectId: string,
    opts: { offset?: number; limit?: number; q?: string } = {},
  ) => {
    const params = new URLSearchParams();
    if (opts.offset) params.set("offset", String(opts.offset));
    if (opts.limit) params.set("limit", String(opts.limit));
    if (opts.q) params.set("q", opts.q);
    const query = params.toString();
    return request<ProteinRecords>(
      `/objects/${objectId}/protein-records${query ? `?${query}` : ""}`,
    );
  },

  /** The structure for one record, resolved on selection.
   *
   *  Resolved per record rather than per page for the reason the variants
   *  viewer records: most records resolve to nothing, and pre-resolving would
   *  spend a round trip per row to decide how buttons look. */
  proteinRecordStructure: (objectId: string, ordinal: number) =>
    request<ProteinStructure>(
      `/objects/${objectId}/protein-records/${ordinal}/structure`,
    ),

  /** Queue the Results computation for a GFF/GTF/BED. Read-only: produces
   * facts and a SQLite feature index, no derived objects. */
  launchAnnotationStats: (objectId: string, targetNode?: string) =>
    request<JobSummary>(
      `/pipelines/annotationstats${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify({ object_id: objectId }),
      }),

  /** A page of the feature table. Rows are top-level features unless a type
   *  filter is set, in which case the server searches children too. */
  annotationFeatures: (objectId: string, q: FeatureQuery) => {
    const p = new URLSearchParams({
      offset: String(q.offset),
      limit: String(q.limit),
    });
    if (q.contig) p.set("contig", q.contig);
    if (q.startMin != null) p.set("start_min", String(q.startMin));
    if (q.startMax != null) p.set("start_max", String(q.startMax));
    if (q.featureType) p.set("feature_type", q.featureType);
    if (q.biotype) p.set("biotype", q.biotype);
    if (q.nameQuery) p.set("name_query", q.nameQuery);
    if (q.strand) p.set("strand", q.strand);
    if (q.skipCount) p.set("skip_count", "true");
    if (q.view) p.set("view", q.view);
    return request<AnnotationFeaturePage>(
      `/pipelines/annotationstats/features/${objectId}?${p.toString()}`,
    );
  },

  /** Matched-vs-exported feature counts for the current filter, before
   *  committing to an export -- the closure (exported) is routinely larger
   *  than the raw filter match, since it pulls in ancestors and descendants
   *  to keep the output structurally valid. */
  annotationExportCount: (objectId: string, q: FeatureQuery) => {
    const p = new URLSearchParams();
    if (q.contig) p.set("contig", q.contig);
    if (q.startMin != null) p.set("start_min", String(q.startMin));
    if (q.startMax != null) p.set("start_max", String(q.startMax));
    if (q.featureType) p.set("feature_type", q.featureType);
    if (q.biotype) p.set("biotype", q.biotype);
    if (q.nameQuery) p.set("name_query", q.nameQuery);
    if (q.strand) p.set("strand", q.strand);
    if (q.view === "unresolved") p.set("unresolved", "true");
    return request<{ matched: number; exported: number }>(
      `/pipelines/annotationstats/export-count/${objectId}?${p.toString()}`,
    );
  },

  /** Queue a subset export using the filters currently applied in the
   *  table. */
  launchAnnotationExport: (
    objectId: string,
    q: {
      contig?: string;
      startMin?: number;
      startMax?: number;
      featureType?: string;
      biotype?: string;
      nameQuery?: string;
      strand?: string;
      view?: "all" | "unresolved";
    },
  ) =>
    request<JobSummary>(`/pipelines/annotationstats/export`, {
      method: "POST",
      body: JSON.stringify({
        object_id: objectId,
        contig: q.contig || undefined,
        start_min: q.startMin ?? undefined,
        start_max: q.startMax ?? undefined,
        feature_type: q.featureType || undefined,
        biotype: q.biotype || undefined,
        name_query: q.nameQuery || undefined,
        strand: q.strand || undefined,
        unresolved: q.view === "unresolved",
      }),
    }),

  /** The reference already extracted from this GenBank, if any. The same
   *  query the launcher's guard runs, so the button and the launcher cannot
   *  disagree about whether extraction has already happened. */
  extractedGenBankSequence: (objectId: string) =>
    request<ExtractedSequence>(`/pipelines/genbanksequence/${objectId}`),

  /** Queue extraction of a GenBank's ORIGIN sequence into a FASTA reference. */
  launchExtractGenBankSequence: (objectId: string) =>
    request<JobSummary>(`/pipelines/genbanksequence`, {
      method: "POST",
      body: JSON.stringify({ object_id: objectId }),
    }),

  /** Every child of one feature, for an expanded row. `depth_cap` is the
   *  server's recursion bound, echoed back so the client doesn't hardcode
   *  a second copy of the same number. */
  annotationChildren: (objectId: string, parentId: string) =>
    request<{ rows: AnnotationFeature[]; depth_cap: number }>(
      `/pipelines/annotationstats/children/${objectId}?parent_id=${encodeURIComponent(parentId)}`,
    ),

  /** Pending annotation edits for one object (issue #297). */
  annotationEdits: (objectId: string) =>
    request<AnnotationEditRow[]>(
      `/pipelines/annotationstats/edits/${objectId}`,
    ),

  /** Save or update one column edit. */
  saveAnnotationEdit: (
    objectId: string,
    edit: { line: number; field: string; new_value: string },
  ) =>
    request<AnnotationEditRow>(`/pipelines/annotationstats/edits/${objectId}`, {
      method: "PUT",
      body: JSON.stringify(edit),
    }),

  /** Remove one pending edit. */
  deleteAnnotationEdit: (
    objectId: string,
    line: number,
    field: string,
  ) =>
    request<{ deleted: boolean }>(
      `/pipelines/annotationstats/edits/${objectId}?line=${line}&field=${encodeURIComponent(field)}`,
      { method: "DELETE" },
    ),

  /** Materialize pending edits into a derived annotation object. */
  materializeAnnotationEdits: (objectId: string) =>
    request<JobSummary>("/pipelines/annotationstats/materialize", {
      method: "POST",
      body: JSON.stringify({ object_id: objectId }),
    }),

  /** A page of the Genes view -- see AnnotationGenePage. */
  annotationGenes: (objectId: string, offset: number, limit: number, skipCount?: boolean) => {
    const p = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (skipCount) p.set("skip_count", "true");
    return request<AnnotationGenePage>(
      `/pipelines/annotationstats/genes/${objectId}?${p.toString()}`,
    );
  },

  /** Features overlapping a window, or their density when too dense to draw.
   *  The response echoes the window back so an out-of-order reply can be
   *  matched to the current viewport. */
  annotationWindow: (
    objectId: string,
    q: {
      contig: string;
      start: number;
      end: number;
      bins?: number;
      feature_type?: string;
      strand?: string;
    },
  ) => {
    const p = new URLSearchParams({
      contig: q.contig,
      start: String(Math.max(0, Math.floor(q.start))),
      end: String(Math.floor(q.end)),
    });
    if (q.bins) p.set("bins", String(q.bins));
    if (q.feature_type) p.set("feature_type", q.feature_type);
    if (q.strand) p.set("strand", q.strand);
    return request<AnnotationWindow>(
      `/pipelines/annotationstats/window/${objectId}?${p.toString()}`,
    );
  },

  /**
   * Upload via XHR rather than fetch: fetch exposes no upload progress events,
   * and progress is the whole point of the tray for large files.
   *
   * Because it does not go through `request<T>`, it has to set the profile
   * header itself -- an upload scoped to nobody would 400, and worse, a change
   * that only touched `request<T>` would look complete while leaving exactly
   * this path unscoped.
   */
  uploadObject: (
    projectId: string,
    file: File,
    onProgress?: (loaded: number, total: number) => void,
    signal?: AbortSignal,
  ): Promise<DataObject> =>
    new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${BASE}/projects/${projectId}/objects/upload`);
      xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      for (const [name, value] of Object.entries(profileHeaders())) {
        xhr.setRequestHeader(name, value);
      }

      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress?.(e.loaded, e.total);
      };

      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(JSON.parse(xhr.responseText));
        } else {
          let code = "http_error";
          let message = `${xhr.status} ${xhr.statusText}`;
          try {
            const body = JSON.parse(xhr.responseText);
            code = body.code ?? code;
            message = body.message ?? message;
          } catch {
            /* keep status text */
          }
          reject(new ApiRequestError(xhr.status, code, message));
        }
      };
      xhr.onerror = () => reject(new ApiRequestError(0, "network_error", "Upload failed"));
      xhr.onabort = () => reject(new ApiRequestError(0, "aborted", "Upload cancelled"));

      signal?.addEventListener("abort", () => xhr.abort());
      xhr.send(file);
    }),

  /* -------------------------------------------------------------- workflows */

  /** The canvas palette. Generated from the backend registry, so a tool added
   *  there appears here with no frontend change -- do not cache it across
   *  sessions or that property is lost. */
  listNodeTypes: () => request<NodeTypeMeta[]>("/workflows/node-types"),

  listWorkflows: () => request<WorkflowDefinition[]>("/workflows"),

  getWorkflow: (id: string) => request<WorkflowDefinition>(`/workflows/${id}`),

  /** A 422 carries every validation error under `details.errors`, not just the
   *  first, so the canvas can mark every bad wire at once. `ApiRequestError`
   *  already exposes `details`. */
  createWorkflow: (body: WorkflowDefinitionInput) =>
    request<WorkflowDefinition>("/workflows", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updateWorkflow: (id: string, body: WorkflowDefinitionInput) =>
    request<WorkflowDefinition>(`/workflows/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  /** Populate a canvas from runs the user already did. Returns an *unsaved*
   *  graph -- nothing is persisted until the user saves it. */
  deriveWorkflow: (runIds: string[]) =>
    request<DerivedGraph>("/workflows/derive", {
      method: "POST",
      body: JSON.stringify({ run_ids: runIds }),
    }),

  listWorkflowRuns: () => request<WorkflowRunRow[]>("/workflows/runs"),

  getWorkflowRun: (id: string) =>
    request<WorkflowRunDetail>(`/workflows/runs/${id}`),

  retryWorkflowNode: (runId: string, nodeId: string) =>
    request<{ retried: string }>(
      `/workflows/runs/${runId}/nodes/${encodeURIComponent(nodeId)}/retry`,
      { method: "POST" },
    ),

  retryFailedWorkflowNodes: (runId: string) =>
    request<{ retried: number }>(`/workflows/runs/${runId}/retry-failed`, {
      method: "POST",
    }),

  cancelWorkflowRun: (runId: string) =>
    request<{ cancelled: string }>(`/workflows/runs/${runId}/cancel`, {
      method: "POST",
    }),

  launchWorkflow: (
    id: string,
    body: {
      project_id: string;
      label: string;
      bindings: Record<string, string | string[]>;
    },
    targetNode?: string,
  ) =>
    request<WorkflowRunSummary>(
      `/workflows/${id}/runs${targetNode ? `?target_node=${encodeURIComponent(targetNode)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  // --- Agent chat ---
  askAgent: (projectId: string, message: string) =>
    request<{ status: string }>(`/projects/${projectId}/agent/ask`, {
      method: "POST",
      body: JSON.stringify({ message }),
    }),

  restartAgent: (projectId: string) =>
    request<{ status: string }>(`/projects/${projectId}/agent/restart`, {
      method: "POST",
    }),

  stopAgent: (projectId: string) =>
    request<void>(`/projects/${projectId}/agent`, { method: "DELETE" }),

  newAgentSession: (projectId: string) =>
    request<{ status: string }>(`/projects/${projectId}/agent/new-session`, {
      method: "POST",
    }),
};
