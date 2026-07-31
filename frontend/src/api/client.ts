import type {
  AlignDefaults,
  AlignEnvelope,
  AlignerSchema,
  AlignRequest,
  AssemblyAccepted,
  BulkResult,
  CompleteAccepted,
  ContigsPage,
  DataObject,
  DeletionPreview,
  FacetValue,
  Facets,
  JobLog,
  JobSummary,
  MateSuggestion,
  MetadataSchema,
  NcbiResolveResponse,
  ObjectDetail,
  ObjectRole,
  OverdueSchedule,
  PipelineSuggestion,
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
  VariantQuery,
  VariantRequest,
  VariantsPage,
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

  deletionPreview: (id: string) =>
    request<DeletionPreview>(`/projects/${id}/deletion-preview`),

  listObjects: (projectId: string) =>
    request<DataObject[]>(`/projects/${projectId}/objects`),

  getObject: (id: string) => request<ObjectDetail>(`/objects/${id}`),

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

  /**
   * URL for downloading the object's raw stored bytes.
   *
   * A plain URL rather than a `request` call: these files run to gigabytes, so
   * the browser should stream it to disk itself instead of us buffering the
   * whole thing into memory as a Blob to hand back.
   */
  objectDownloadUrl: (id: string) => `${BASE}/objects/${id}/download`,

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

  /** Queue a narrative summary of a file's QC data and metadata. */
  launchSummary: (objectId: string) =>
    request<JobSummary>("/pipelines/summary", {
      method: "POST",
      body: JSON.stringify({ object_id: objectId, force: true }),
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

  /** Which pipelines to offer for one file, each with the reason it can or
   * cannot run. Advisory: a failure here costs the Actions tab its cards, not
   * its manual controls. */
  suggestions: (objectId: string) =>
    request<{ suggestions: PipelineSuggestion[] }>(
      `/pipelines/suggestions/${objectId}`,
    ),

  /** Post a suggestion's launch payload verbatim. The endpoint and body both
   * come from the card, so this adds nothing -- see `PipelineSuggestion`. */
  launchSuggestion: (endpoint: string, body: Record<string, unknown>) =>
    request<JobSummary>(endpoint, {
      method: "POST",
      body: JSON.stringify(body),
    }),

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

  /** Queue the Results computation for a BAM. Read-only: produces facts and
   * one TSV report, no derived objects. */
  launchBamStats: (objectId: string) =>
    request<JobSummary>("/pipelines/bamstats", {
      method: "POST",
      body: JSON.stringify({ object_id: objectId }),
    }),

  /** A page of the per-contig table, sorted the same way the job wrote it
   * (mapped reads descending). */
  bamStatsContigs: (objectId: string, reportPath: string, offset: number, limit: number) =>
    request<ContigsPage>(
      `/pipelines/bamstats/report/${objectId}/${reportPath}?offset=${offset}&limit=${limit}`,
    ),

  /** URL for downloading the complete per-contig TSV. */
  bamStatsDownloadUrl: (objectId: string, reportPath: string) =>
    `${BASE}/pipelines/bamstats/report/${objectId}/${reportPath}?download=1`,

  /** Queue the Results computation for a VCF/BCF. Read-only: produces facts
   * and a variants TSV, no derived objects. */
  launchVcfStats: (objectId: string) =>
    request<JobSummary>("/pipelines/vcfstats", {
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
    if (q.skipCount) p.set("skip_count", "true");
    return request<VariantsPage>(
      `/pipelines/vcfstats/variants/${objectId}?${p.toString()}`,
    );
  },

  /** URL for downloading the complete variants TSV. */
  vcfStatsDownloadUrl: (objectId: string, reportPath: string) =>
    `${BASE}/pipelines/vcfstats/report/${objectId}/${reportPath}`,

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
