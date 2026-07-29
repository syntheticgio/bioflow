import type {
  AlignDefaults,
  AlignRequest,
  BulkResult,
  CompleteAccepted,
  DataObject,
  FacetValue,
  Facets,
  JobLog,
  JobSummary,
  MateSuggestion,
  MetadataSchema,
  ObjectDetail,
  ObjectRole,
  OverdueSchedule,
  PipelineTools,
  Project,
  ProjectDetail,
  ReferenceOption,
  RegisterAccepted,
  RunDetail,
  RunSummary,
  ScheduleInfo,
  SraAccepted,
  SraDownloadRequest,
  SraResolveResponse,
  TimingEstimate,
  TrimDefaults,
  TrimRequest,
  SearchParams,
  SearchResults,
  SystemLoad,
  SystemStats,
  UploadCreated,
  UploadSessionInfo,
  VariantDefaults,
  VariantRequest,
} from "./types";

const BASE = "/api/v1";

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

  listObjects: (projectId: string) =>
    request<DataObject[]>(`/projects/${projectId}/objects`),

  getObject: (id: string) => request<ObjectDetail>(`/objects/${id}`),

  updateObject: (id: string, body: Record<string, unknown>) =>
    request<DataObject>(`/objects/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  deleteObject: (id: string) => request<void>(`/objects/${id}`, { method: "DELETE" }),

  reingestObject: (id: string) =>
    request<{ object_id: string; job_id: string }>(`/objects/${id}/reingest`, {
      method: "POST",
    }),

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
    request<JobSummary & { timing_estimate?: TimingEstimate }>(`/jobs/${id}`),

  cancelJob: (id: string) =>
    request<{ job_id: string; outcome: string }>(`/jobs/${id}/cancel`, {
      method: "POST",
    }),

  retryJob: (id: string) => request<JobSummary>(`/jobs/${id}/retry`, { method: "POST" }),

  jobTypes: () => request<Record<string, unknown>>("/jobs/types"),

  // --- Pipelines ---

  pipelineTools: () => request<PipelineTools>("/pipelines/tools"),

  trimDefaults: (tool: string = "fastp") =>
    request<TrimDefaults>(`/pipelines/defaults?tool=${encodeURIComponent(tool)}`),

  /** The file this one would be trimmed alongside, or null. */
  detectMate: (objectId: string) =>
    request<MateSuggestion | null>(`/pipelines/mate/${objectId}`),

  launchTrim: (body: TrimRequest) =>
    request<JobSummary>("/pipelines/trim", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Queue a QC run. Read-only: produces a report, derives no files. */
  launchQC: (objectId: string) =>
    request<JobSummary>("/pipelines/qc", {
      method: "POST",
      body: JSON.stringify({ object_id: objectId }),
    }),

  /**
   * URL of a generated QC report.
   *
   * Not fetched through `request`: the report is an HTML page opened in a new
   * tab, not JSON. The server sandboxes it via CSP -- see `get_qc_report` --
   * because FastQC embeds sequence data taken straight from the reads.
   */
  qcReportUrl: (objectId: string, reportPath: string) =>
    `${BASE}/pipelines/qc/report/${objectId}/${reportPath}`,

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

  /** Alignment defaults for one file, including a read group from its metadata. */
  alignDefaults: (objectId: string) =>
    request<AlignDefaults>(`/pipelines/align/defaults/${objectId}`),

  /** Candidate references in a project, each with its index status. */
  references: (projectId: string) =>
    request<{ references: ReferenceOption[] }>(`/pipelines/references/${projectId}`),

  buildIndex: (body: { reference_id: string; aligner: string }) =>
    request<JobSummary>("/pipelines/index", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  launchAlignment: (body: AlignRequest) =>
    request<JobSummary>("/pipelines/align", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Variant calling defaults for one BAM, including the inferred caller. */
  variantDefaults: (bamId: string) =>
    request<VariantDefaults>(`/pipelines/variants/defaults/${bamId}`),

  launchVariantCalling: (body: VariantRequest) =>
    request<JobSummary>("/pipelines/variants", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /**
   * Upload via XHR rather than fetch: fetch exposes no upload progress events,
   * and progress is the whole point of the tray for large files.
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
};
