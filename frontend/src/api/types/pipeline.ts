// --- Pipelines ---

export type PipelineType =
  | "trim"
  | "align"
  | "qc"
  | "utility"
  | "download"
  | "variant"
  | "structural_variant"
  | "expression"
  | "assemble"
  | "reference_assembly"
  | "assembly_qc";

export interface PipelineTool {
  name: string;
  path: string | null;
  version: string | null;
  available: boolean;
  error: string | null;
  /**
   * Only set for an "on_demand" tool (see `delivery` below); null for every
   * bundled one. Distinguishes "not installed" (an offer -- show Install)
   * from "unknown" (a fault -- no docker client, or an unreachable daemon).
   * `available` is already false in both cases, but the UI must not render
   * them the same way: one is a button, the other is an error state.
   */
  install_state: "installed" | "not_installed" | "unknown" | null;
  /** Plural: fastp is both a trimmer and a QC tool. Mirrors TOOL_META. */
  pipelines: PipelineType[];
  summary: string;
  one_liner: string;
  strengths: string[];
  /**
   * Whether a job handler actually branches on this tool, independent of
   * `available` (whether the binary works). The selector must use this, not
   * `available`, to decide whether a card is selectable: `available` alone
   * would offer a choice that silently does nothing.
   *
   * This comment used to name cutadapt and Trimmomatic as the unrunnable
   * examples. That has not been true since trim_reads grew its three-way
   * dispatch, and no TOOL_META entry sets it false today -- the case that
   * still reaches here is `tool_with_meta`'s fallback, which defaults it to
   * false for a tool the backend has no metadata entry for at all.
   */
  runnable: boolean;

  /**
   * Reference data for the Software help page, from ToolMeta. Any of these
   * may be empty: a tool with no public repository or no paper is a real
   * case, and the page renders the absence rather than a dead link.
   */
  homepage: string;
  repository: string;
  citation: string;
  citation_url: string;
  license: string;
  /** How BioFlow uses this tool -- the part no upstream page can tell you. */
  usage: string;

  /**
   * Tool selection recommendations keyed on read chemistry bucket.
   * Keys are "short" or "long"; values are "recommended" or "compatible".
   * An empty or absent entry means no opinion.
   */
  recommendations: Record<string, string>;

  /**
   * Buckets for which this tool is *the* default among several recommended
   * ones. Auto-select prefers a tool listing the reads' bucket here over one
   * that is merely recommended, so a family with two recommended tools (QC
   * short reads: fastp and fastqc) opens on a fixed choice rather than on
   * whichever the backend's registry happened to list first (#588).
   */
  default_for: string[];

  /**
   * How this tool reaches the running stack. "bundled" ships in the backend
   * image; "on_demand" is a pinned OCI image pulled on first use and run as
   * a sibling container (the DeepVariant shape). See
   * docs/superpowers/specs/2026-08-05-optional-tool-delivery-design.md.
   */
  delivery: "bundled" | "on_demand";
  /** The pinned image reference, only set when delivery is "on_demand". */
  image: string | null;
  /**
   * Compressed transfer size an Install button should state, only set when
   * delivery is "on_demand". Not the on-disk size after decompression --
   * DeepVariant's 2.99 GB pull becomes 8.83 GB on disk, and the download is
   * the number a user weighing their connection actually wants.
   */
  download_bytes: number | null;
}

export interface PipelineTools {
  tools: PipelineTool[];
  all_available: boolean;
}

/** An external data source. Mirrors sources.DataSource.
 *
 *  No version field, deliberately: a source has nothing to probe, and
 *  NCBI Datasets is whatever the API returned today. */
export interface DataSource {
  name: string;
  kind: "api" | "database" | "reference";
  summary: string;
  usage: string;
  homepage: string;
  docs: string;
  citation: string;
  citation_url: string;
  terms: string;
}

export interface DataSources {
  sources: DataSource[];
}

/** One input in the generated parameter form. Mirrors registry ParamField. */
export interface ParamFieldMeta {
  key: string;
  label: string;
  kind: "int" | "float" | "bool" | "select" | "text";
  default: unknown;
  help: string;
  group: "biology" | "performance" | "filters";
  min: number | null;
  max: number | null;
  choices: { value: string; label: string }[];
}

/** Mirrors resource_estimator.MemoryModel. */
export interface MemoryModel {
  index_bytes_per_ref_base: number;
  fixed_overhead_mb: number;
  bytes_per_thread_mb: number;
  index_build_multiplier: number;
}

/** One knob the re-planner moved. Mirrors replan_service.Change. */
export interface ReplanChange {
  name: string;
  before: number;
  after: number;
}

/**
 * Mirrors replan_service.ReplanResult.
 *
 * A tagged union rather than a nullable proposal: "nothing fits" and "there is
 * nothing here to tune" call for different prose and different next steps, and
 * collapsing both into null loses exactly the distinction the user needs.
 */
export type ReplanResult =
  | {
      kind: "proposal";
      params: Record<string, unknown>;
      estimate_mb: number;
      changes: ReplanChange[];
      note: string;
    }
  | { kind: "infeasible"; reason: string }
  | { kind: "no_knobs" };

/**
 * The `details` payload of a 422 resource refusal.
 *
 * Assembly renders the card straight from this; alignment builds the same
 * shape client-side from its envelope, so both dialogs feed one component.
 */
export interface ResourceRefusalDetails {
  /** Which refusal this is. "declared" carries no estimate and no replan:
   *  nothing about the run changes a fixed reservation, which is why the
   *  card must render without them. */
  refusal: "estimate" | "declared";
  budget_mb: number;
  /** Present only when refusal === "estimate". */
  estimate_mb?: number;
  /** Present only when refusal === "declared". */
  declared_mb?: number;
  estimate_source?: "measured" | "heuristic" | "declared" | "unknown";
  detail?: string;
  replan?: ReplanResult;
}

/** Mirrors fastp_runner.TrimParams. Nulls mean "let fastp decide". */
export interface TrimParams {
  quality_threshold: number;
  unqualified_percent_limit: number;
  min_length: number;
  trim_poly_g: boolean | null;
  trim_poly_x: boolean;
  dedup: boolean;
  detect_adapter_for_pe: boolean;
  adapter_r1: string | null;
  adapter_r2: string | null;
  threads: number;
  compression: number;
}

/** Mirrors cutadapt_runner.CutadaptParams. */
export interface CutadaptParams {
  quality_cutoff: number;
  min_length: number;
  adapter_r1: string | null;
  adapter_r2: string | null;
  threads: number;
}

/** Mirrors trimmomatic_runner.TrimmomaticParams. */
export interface TrimmomaticParams {
  quality_leading: number;
  quality_trailing: number;
  sliding_window_size: number;
  sliding_window_quality: number;
  min_length: number;
  adapter_file: string | null;
  threads: number;
}

/** Mirrors filtlong_runner.FiltlongParams. Conservative defaults tuned for long reads. */
export interface FiltlongParams {
  min_length: number;
  min_mean_q: number;
  keep_percent: number;
  target_bases: number | null;
  threads: number;
}

export type TrimToolParams = TrimParams | CutadaptParams | TrimmomaticParams;

export interface TrimDefaults {
  params: TrimToolParams;
  max_threads: number;
}

export interface MateSuggestion {
  object_id: string;
  name: string;
  mate: "R1" | "R2" | null;
}

export interface TrimRequest {
  object_id: string;
  mate_object_id?: string | null;
  paired?: boolean;
  params?: Partial<TrimToolParams>;
  tool?: string;
}

/** Request body for launching Filtlong long-read filtering. */
export interface FilterLongReadsRequest {
  object_id: string;
  mate_object_id?: string | null;
  params?: Partial<FiltlongParams>;
}

/** One output file of a prior run. `exists` is false once the file has been
 *  deleted -- the run still happened, so the row keeps its recorded name and
 *  renders as plain text rather than a dead link. */
export interface PriorRunOutput {
  object_id: string;
  name: string;
  exists: boolean;
}

/** A run that already did what a card offers. Failed runs are included and
 *  carry no outputs: a card that hid its failures would invite the same
 *  failed launch again. */
export interface PriorRun {
  run_id: string;
  finished_at: string;
  status: "succeeded" | "partial" | "failed";
  outputs: PriorRunOutput[];
}

/**
 * One pipeline offer for a data file, as rendered in the Actions tab.
 *
 * Every card is either `available` with a `launch` payload or `unavailable`
 * with a `reason` -- the two always agree, since an available card without a
 * payload would render as a button that does nothing. `why` is populated only
 * on available cards, `reason` only on unavailable ones.
 *
 * `body` is deliberately opaque. It is the *complete* JSON body for
 * `endpoint`, assembled server-side where the object id and its defaults are
 * known, and the client posts it verbatim rather than merging anything in:
 * the three launch endpoints do not share a request shape (`/variants` keys
 * on `bam_id`, the others on `object_id`), so anything the client had to add
 * would be a shape it had to know about.
 */
export interface PipelineSuggestion {
  kind: string;
  category: string;
  title: string;
  description: string;
  why: string | null;
  /**
   * "needs_install" is not blocked -- it is one click from working, and the
   * card keeps a real `launch` payload just like "available" does. It exists
   * so the UI can tell "one click from working" apart from "unavailable"
   * (a dead end with a reason): rendering a not-yet-installed optional tool
   * as unavailable would read as permanently broken and the user would never
   * learn the tool exists at all.
   */
  status: "available" | "unavailable" | "needs_install";
  reason: string | null;
  launch: { endpoint: string; body: Record<string, unknown> } | null;
  /** Set only when status is "needs_install": what pressing Launch costs. */
  requires_install: { tool: string; download_bytes: number | null } | null;
  /**
   * Which settings dialog can adjust this card's run, when one exists.
   *
   * Null for the twelve kinds with no dialog, and for any card that cannot
   * launch -- there is no body to seed a dialog with. `dialog` is a name
   * `DetailPanel` switches on, not a component: keeping the kind-to-dialog
   * mapping server-side is what lets this component stay ignorant of the
   * launch request shapes, the same reason it posts `launch.body` verbatim.
   */
  configure: { dialog: string } | null;
  prior_runs: PriorRun[];
  /**
   * Work this card offers is queued or running right now.
   *
   * Server-derived, which is the point: the client's own record of what it
   * launched dies with a page reload, and a run started from the Computations
   * dialog was never in it at all. Both cases leave a Launch button enabled
   * beside work already in flight.
   */
  running: boolean;
}
