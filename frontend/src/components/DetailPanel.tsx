import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { classifyChromosomes } from "../lib/chromosomes";
import { useUploads } from "../hooks/useUploads";
import type {
  AlignerName,
  DataObject,
  ObjectDetail as ObjectDetailData,
  TileMatrix,
  VariantCallerName,
} from "../api/types";
import {
  compressionLabel,
  formatBytes,
  formatDate,
  formatKindLabel,
} from "../lib/format";
import { readQuality } from "../lib/readQuality";
import { notify } from "../stores/messageStore";
import { AiSummary } from "./AiSummary";
import { AssemblyFacts } from "./AssemblyFacts";
import { AssemblyGraph } from "./AssemblyGraph";
import { ChromosomeStrip } from "./ChromosomeStrip";
import { FactsColumns } from "./FactsColumns";
import { countVisibleFacts, FactsTable } from "./FactsTable";
import { assemblyLabel, FileHeadlineStats, fileStats } from "./FileHeadline";
import { IngestProgress } from "./IngestProgress";
import {
  BaseCompositionChart,
  GcDistributionChart,
  LengthDistributionChart,
  NContentChart,
  QualityChart,
} from "./SequenceCharts";
import { AdapterContentChart, DuplicationLevelsChart } from "./ContaminationCharts";
import { TileQualityChart } from "./TileQualityChart";
import { JobList } from "./JobList";
import { MetadataEditor } from "./MetadataEditor";
import { OrganismBlurb } from "./OrganismBlurb";
import { ComputationHistory } from "./ComputationHistory";
import { Computations } from "./Computations";
import { ProvenanceNarrative } from "./ProvenanceNarrative";
import { ManageFile } from "./ManageFile";
import { PipelineSuggestions } from "./PipelineSuggestions";
import { SchemaMetadataEditor } from "./SchemaMetadataEditor";
import { DerivedFiles } from "./DerivedFiles";
import { ActivePipelineJobs } from "./ActivePipelineJobs";
import { AlignDialog } from "./AlignDialog";
import { BamResults } from "./BamResults";
import { ExpressionResults } from "./ExpressionResults";
import { VariantResults } from "./VariantResults";
import { IndexStatus } from "./IndexStatus";
import { PipelineToolSelector } from "./PipelineToolSelector";
import { ProjectDangerZone } from "./ProjectDangerZone";
import { TrimDialog } from "./TrimDialog";
import { AssembleDialog } from "./AssembleDialog";
import { CompletenessDialog } from "./CompletenessDialog";
import { ScaffoldDialog } from "./ScaffoldDialog";
import { QuantifyDialog } from "./QuantifyDialog";
import { DifferentialExpressionDialog } from "./DifferentialExpressionDialog";
import { VariantDialog } from "./VariantDialog";
import { QcReport } from "./QcReport";
import { TrimReport } from "./TrimReport";
import { SraPanel } from "./SraPanel";
import { TabPanel, Tabs, type TabDef } from "./Tabs";
import { TruncatedValue } from "./TruncatedValue";

/** The right panel: details of whatever is selected in the left panel. */
export function DetailPanel() {
  const [params] = useSearchParams();
  const sel = params.get("sel");
  const { pathname } = useLocation();

  if (!sel) {
    // Inside an opened project (/p/:projectId) with nothing picked yet, the
    // right panel should still be about that project -- not the app-wide
    // splash, which belongs at the project list root (/).
    const projectMatch = pathname.match(/^\/p\/([^/]+)/);
    if (projectMatch) return <ProjectDetail id={projectMatch[1]} />;
    return <EmptyDetail />;
  }
  const [kind, id] = sel.split(":");
  if (kind === "project") return <ProjectDetail id={id} />;
  if (kind === "object") return <ObjectDetail id={id} />;
  return <EmptyDetail />;
}

/**
 * Nothing is selected, which on this page is most of the time -- so rather
 * than an empty "Details" panel, this is BioFlow's de facto splash screen: a
 * one-line orientation plus whatever the library currently holds, pulled from
 * the same `["system","stats"]` query the header and activity desk already
 * poll, so this costs no request of its own.
 */
function EmptyDetail() {
  const { data: stats } = useQuery({
    queryKey: ["system", "stats"],
    queryFn: api.systemStats,
    refetchInterval: 15000,
  });

  return (
    <div className="panel">
      <div className="panel-body">
        <div className="splash">
          <div className="splash-title">BioFlow</div>
          <p className="splash-blurb">
            A local bioinformatics pipeline tool: QC, trim, align, call
            variants, quantify and assemble sequencing data, all run on this
            machine.
          </p>

          {stats && (
            <dl className="splash-stats">
              <div className="splash-stat">
                <dt>Projects</dt>
                <dd>{stats.counts.projects}</dd>
              </div>
              <div className="splash-stat">
                <dt>Files</dt>
                <dd>{stats.counts.objects}</dd>
              </div>
              <div className="splash-stat">
                <dt>Stored</dt>
                <dd>{formatBytes(stats.storage.library_bytes)}</dd>
              </div>
            </dl>
          )}

          <p className="splash-prompt">
            Select a project on the left to see its files, or create one to
            get started.
          </p>
        </div>
      </div>
    </div>
  );
}

function ProjectDetail({ id }: { id: string }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { data: project, isLoading } = useQuery({
    queryKey: ["project", id],
    queryFn: () => api.getProject(id),
  });

  // Same upload path the project view's + button uses, so "Add data" is a
  // real action rather than a placeholder.
  const { uploadFiles } = useUploads(id);

  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");

  const save = useMutation({
    mutationFn: (metadata: Record<string, unknown>) =>
      api.updateProject(id, { metadata }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project", id] });
      notify.success("Metadata saved");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const renameProject = useMutation({
    mutationFn: (name: string) => api.updateProject(id, { name }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project", id] });
      // Breadcrumbs and the left-panel listing both show the name.
      qc.invalidateQueries({ queryKey: ["projects"] });
      notify.success("Project renamed");
      setEditingName(false);
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const startEditingName = () => {
    setNameDraft(project?.name ?? "");
    setEditingName(true);
  };

  const submitRename = () => {
    const trimmed = nameDraft.trim();
    if (!trimmed) {
      notify.error("Project name cannot be empty");
      return;
    }
    if (trimmed === project?.name) {
      setEditingName(false);
      return;
    }
    renameProject.mutate(trimmed);
  };

  if (isLoading || !project) {
    return (
      <div className="panel">
        <div className="panel-header">
          <span className="panel-title">Details</span>
        </div>
        <div className="panel-body">
          <div className="empty">
            <span className="spinner" /> Loading…
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="panel">
      <div className="panel-header">
        <span className="panel-title">Project</span>
      </div>
      <div className="panel-body detail">
        {editingName ? (
          <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 2 }}>
            <input
              autoFocus
              value={nameDraft}
              onChange={(e) => setNameDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submitRename();
                if (e.key === "Escape") setEditingName(false);
              }}
              disabled={renameProject.isPending}
              style={{ fontSize: 16, fontWeight: 600, flex: 1, padding: "3px 6px" }}
            />
            <button
              type="button"
              className="btn primary"
              style={{ padding: "3px 10px", fontSize: 12 }}
              onClick={submitRename}
              disabled={renameProject.isPending}
            >
              Save
            </button>
            <button
              type="button"
              className="btn"
              style={{ padding: "3px 10px", fontSize: 12 }}
              onClick={() => setEditingName(false)}
              disabled={renameProject.isPending}
            >
              Cancel
            </button>
          </div>
        ) : (
          <div className="detail-title">{project.name}</div>
        )}
        <div className="detail-subtitle">{project.description || "No description"}</div>

        {/* Named actions rather than an icon beside the title: rename is not
            the primary thing you come here to do, and burying it in a glyph
            made the two commoner actions -- open, add data -- unreachable
            from this panel at all. */}
        {!editingName && (
          <div className="detail-actions">
            <button
              type="button"
              className="btn primary"
              onClick={() => navigate(`/p/${project.id}`)}
            >
              Open project
            </button>
            <label className="btn" style={{ cursor: "pointer" }}>
              Add data
              <input
                type="file"
                multiple
                hidden
                onChange={(e) => {
                  const files = Array.from(e.target.files ?? []);
                  if (files.length) void uploadFiles(files);
                  e.target.value = "";
                }}
              />
            </label>
            <button type="button" className="btn-text" onClick={startEditingName}>
              Rename
            </button>
          </div>
        )}

        {/* Paired because they are both "what this project is" -- the theme
            sets them side by side where there is width for it, and they stack
            on their own when there is not. */}
        <div className="detail-columns">
          <div className="section">
            <div className="section-title">Overview</div>
            <dl className="kv">
              <dt>Files</dt>
              <dd>{project.object_count}</dd>
              <dt>Total size</dt>
              <dd>{formatBytes(project.total_bytes)}</dd>
              <dt>Created</dt>
              <dd>{formatDate(project.created_at)}</dd>
              <dt>Updated</dt>
              <dd>{formatDate(project.updated_at)}</dd>
              <dt>ID</dt>
              <dd className="mono">{project.id}</dd>
            </dl>
          </div>

          <div className="section">
            <div className="section-title">Project metadata</div>
            <div className="section-note">
              Inherited by every file ingested into this project.
            </div>
            <MetadataEditor
              value={project.metadata}
              onSave={(m) => save.mutate(m)}
              saving={save.isPending}
            />
          </div>
        </div>

        <div className="section">
          <div className="section-title">Recent jobs</div>
          <JobList projectId={project.id} />
        </div>

        <ProjectDangerZone projectId={project.id} projectName={project.name} />
      </div>
    </div>
  );
}

/** Ordered so the panel opens on what the file *is* before how good it is.
 *
 * Results leads wherever it exists -- a BAM or a called VCF/BCF is something
 * the user asked the app to produce, and the first question about a produced
 * file is what came out, not whether the input passed QC. Quality follows
 * immediately: the two answer adjacent questions and reordering them is not a
 * reason to separate them.
 *
 * Objects with no Results tab (reads, references, everything else) still open
 * on Quality, which for them is the first real question. */
function tabsFor(obj: DataObject): TabDef[] {
  const factCount = countVisibleFacts(obj.facts);
  const tabs: TabDef[] = [];

  // One tab id across all three formats rather than a push per format: `tab`
  // is persisted in the URL alongside ?sel=, so a link stays on Results when
  // the selection moves between a BAM and the VCF called from it.
  // Keyed on role for DE results rather than on format, unlike the other
  // three. A results table is anonymous TSV -- format cannot tell it from any
  // other tab-separated file, which is exactly why the role exists.
  const hasResults =
    obj.format.kind === "bam" ||
    obj.format.kind === "vcf" ||
    obj.format.kind === "bcf" ||
    obj.role === "de_results";
  if (hasResults) {
    tabs.push({ id: "results", label: "Results" });
  }

  // Hints only where there is something true to say. The mockup shows one on
  // every tab, but Actions holds tags and delete rather than a list of
  // pipelines, and inventing a count for it would be worse than leaving it
  // bare.
  tabs.push(
    {
      id: "qc",
      label: "Quality",
      hint: factCount > 0 ? `${factCount} facts` : undefined,
    },
    {
      id: "metadata",
      label: "Metadata",
    },
    {
      id: "history",
      label: "History",
      hint: typeof obj.facts.sra_accession === "string" ? "Provenance" : undefined,
    },
    { id: "actions", label: "Actions" },
  );
  return tabs;
}

/**
 * Where a Trim or Align click currently is: choosing a tool, or running the
 * parameter dialog with one applied. `tool: null` and the pipeline decided is
 * the whole vocabulary this two-step flow needs -- a boolean per dialog plus
 * a boolean per selector plus a separately-tracked tool name would admit
 * states the flow does not have, like both dialogs open at once.
 */
type PipelineFlow = {
  pipeline: "trim" | "align" | "variant";
  tool: string | null;
} | null;

function ObjectDetail({ id }: { id: string }) {
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [metadataDirty, setMetadataDirty] = useState(false);

  // Trim and Align both go through a tool selection step before their
  // parameter dialog. One piece of state for the whole two-step flow, rather
  // than a boolean per dialog plus a boolean per selector plus a string for
  // the chosen tool: that combination admits states the flow does not have
  // (both dialogs open at once), where this type cannot express them.
  // `tool: null` means the selector is showing; non-null means the parameter
  // dialog is, with that tool applied.
  const [flow, setFlow] = useState<PipelineFlow>(null);
  // The card highlighted in the selector, before Continue commits it into
  // `flow.tool`. Separate from `flow` because the selector needs a
  // provisional choice a user can change their mind about without that
  // partial state leaking into what actually launches a dialog.
  const [pendingTool, setPendingTool] = useState<string | null>(null);
  // Its own boolean rather than a fourth member of `flow`. `flow` exists for
  // the two-step tool-selection dance, and counting has exactly one tool --
  // routing it through a selector would mean a screen offering one card.
  const [quantifyOpen, setQuantifyOpen] = useState(false);
  const [assembleOpen, setAssembleOpen] = useState(false);
  const [completenessOpen, setCompletenessOpen] = useState(false);
  const [scaffoldOpen, setScaffoldOpen] = useState(false);
  const [deOpen, setDeOpen] = useState(false);

  const startFlow = (pipeline: "trim" | "align" | "variant") => {
    setPendingTool(null);
    setFlow({ pipeline, tool: null });
  };

  const clearSelection = () => {
    const next = new URLSearchParams(params);
    next.delete("sel");
    setParams(next, { replace: true });
  };

  // In the URL alongside ?sel=, so the tab survives selecting another file and
  // a link can point at a particular view. An unrecognised value falls back
  // rather than rendering an empty panel. The valid set depends on the
  // object's format (see tabsFor), so which tab is "valid" is resolved below,
  // once obj is available -- setTab itself needs no such check.
  const setTab = (id: string) => {
    const next = new URLSearchParams(params);
    next.set("tab", id);
    // Matches clearSelection: switching tabs is not a navigation step people
    // expect the back button to undo.
    setParams(next, { replace: true });
  };

  const { data: obj, isLoading } = useQuery({
    queryKey: ["object", id],
    queryFn: () => api.getObject(id),
    refetchInterval: (q) => {
      const o = q.state.data;
      return o && o.status !== "ready" && o.status !== "error" ? 1500 : false;
    },
  });

  const save = useMutation({
    mutationFn: (metadata: Record<string, unknown>) => api.updateObject(id, { metadata }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["object", id] });
      // Organism and assay are inputs to the align rule, so editing them can
      // change which cards are offered and what a gated one gives as its
      // reason.
      qc.invalidateQueries({ queryKey: ["suggestions", id] });
      notify.success("Metadata saved");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  // Names the reference the Align button would default to. Same query key the
  // align dialog uses, so opening it afterwards costs no extra request.
  //
  // Must sit above the loading early-return with the other hooks: gating it on
  // `obj` being present would change the hook order between renders.
  const { data: refs } = useQuery({
    queryKey: ["pipelines", "references", obj?.project_id],
    queryFn: () => api.references(obj!.project_id),
    enabled: obj?.status === "ready" && obj?.format.kind === "fastq",
  });
  const references = refs?.references ?? [];

  // Same query ActivePipelineJobs makes for this object, so the two share one
  // poll rather than each running their own. Used to disable Run QC and
  // Preprocess while a matching job is already in flight: unlike Align, there
  // is no legitimate reason to run either of these twice at once on the same
  // file, so disabling doubles as the "it's working" feedback a fire-and-forget
  // button otherwise lacks.
  const { data: activeJobs } = useQuery({
    queryKey: ["jobs", "for-object", id],
    queryFn: () => api.listJobs({ objectId: id, states: "active", limit: 20 }),
    refetchInterval: 5_000,
    enabled: !!id,
  });
  const qcActive = (activeJobs ?? []).some((j) => j.type === "run_qc");
  const trimActive = (activeJobs ?? []).some((j) => j.type === "trim_reads");

  const reingest = useMutation({
    mutationFn: () => api.reingestObject(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["object", id] });
      qc.invalidateQueries({ queryKey: ["jobs"] });
      // Re-ingest re-derives format and facts, which is most of what the cards
      // are computed from.
      qc.invalidateQueries({ queryKey: ["suggestions", id] });
      notify.info("Re-ingest queued");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  // No dialog: QC takes no parameters, so a button that opened a form with
  // nothing in it would be a step for its own sake.
  const runQC = useMutation({
    mutationFn: () => api.launchQC(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      // QC is what un-gates the align card, so leaving this stale would keep
      // "Run QC to determine read chemistry" beside a file that just ran it.
      qc.invalidateQueries({ queryKey: ["suggestions", id] });
      notify.info("QC queued");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const remove = useMutation({
    mutationFn: () => api.deleteObject(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["objects"] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["search"] });
      qc.invalidateQueries({ queryKey: ["system", "stats"] });
      notify.success(`Deleted ${obj?.name ?? "file"}`);
      // The selected object no longer exists; clear it so the panel does not
      // sit on a 404.
      clearSelection();
    },
    onError: (e: Error) => notify.error(e.message),
  });

  if (isLoading || !obj) {
    return (
      <div className="panel">
        <div className="panel-header">
          <span className="panel-title">Details</span>
        </div>
        <div className="panel-body">
          <div className="empty">
            <span className="spinner" /> Loading…
          </div>
        </div>
      </div>
    );
  }

  const tabs = tabsFor(obj);
  const raw = params.get("tab");
  // Falls back to whichever tab `tabsFor` put first, not a hardcoded id: the
  // order encodes which question to open on, and naming one here would mean
  // reordering the tabs silently failed to change what the panel shows.
  const tab = tabs.some((t) => t.id === raw) ? raw! : tabs[0].id;

  const compression = compressionLabel(obj.format.compression);
  // A .bam that isn't a BAM is worth telling the user about rather than
  // silently trusting one signal over the other.
  const formatDisagreement =
    obj.format.magic_says &&
    obj.format.extension_says &&
    obj.format.magic_says !== obj.format.extension_says;

  const isReference = obj.role === "reference";

  // Free-form key inside the metadata blob rather than a column, so it is often
  // absent and is not necessarily one of the schema's enum options -- the enum
  // deliberately stores off-list values. Render whatever string is there.
  const organism = obj.metadata.organism;
  const species =
    typeof organism === "string" && organism.trim() ? organism.trim() : null;

  // Same free-form-metadata read as `organism` above, and for the same reason:
  // the enum stores off-list values, so render whatever string is there rather
  // than matching it against the schema's four options.
  const sequenceTypeRaw = obj.metadata.sequence_type;
  const sequenceType =
    typeof sequenceTypeRaw === "string" && sequenceTypeRaw.trim()
      ? sequenceTypeRaw.trim()
      : null;

  // Same function the explorer rows use, so the word here and the word there
  // can never disagree.
  const quality = readQuality(obj);

  // "Read file" rather than the bare "File": at a glance the kicker should say
  // what kind of thing this is, and reads are the case with the most to say.
  const kindLabel = isReference
    ? "Reference"
    : obj.format.kind === "fastq"
      ? "Read file"
      : obj.format.kind === "bam"
        ? "Alignment"
        : "File";

  const stats = fileStats(obj);

  // Whether QC has already been run, for the outstanding-work badge on its
  // button: qc_tool is written by whichever QC path actually ran, so its
  // presence is the honest test.
  const hasQc = typeof obj.facts.qc_tool === "string";

  // Same idea for Preprocess: trimmed_by is written after a trim job
  // completes (backend/app/queue/results.py), so its presence is the honest
  // test of whether preprocessing has run.
  const hasTrim = typeof obj.facts.trimmed_by === "string";

  // Names the reference the Align button would default to, so it can say
  // "Align to ASM244v1" rather than just "Align". Only when the project holds
  // exactly the one candidate the dialog would preselect: with several, naming
  // one would misstate what the button does, and the dialog asks instead.
  const alignTarget = assemblyLabel(references);

  // Offered for reads that are ready to run. Already-trimmed output is
  // deliberately still eligible -- trimming twice is unusual but legitimate,
  // and the dedup key stops an accidental repeat of the same settings.
  const canTrim = obj.status === "ready" && obj.format.kind === "fastq";

  // Same eligibility as trimming: reads are reads, trimmed or not. Aligning
  // untrimmed reads is a real choice rather than a mistake, so it is offered.
  const canAlign = canTrim;

  // Likewise: QC reads a FASTQ and reports on it, which is exactly the same
  // input requirement. Running it on trimmed output is the normal way to check
  // that trimming did what was wanted.
  const canQC = canTrim;

  // A reference can be indexed ahead of time, rather than discovering the cost
  // as part of the first alignment against it.
  const canIndex = obj.status === "ready" && obj.format.kind === "fasta";

  // Variant calling reads an alignment. The .bai and the reference are checked
  // server-side at launch, which reports what is missing and how to fix it --
  // better than hiding the button and leaving the user to guess why.
  const canCallVariants = obj.status === "ready" && obj.format.kind === "bam";

  // Same reasoning as canCallVariants: format only. Whether an alignment is
  // RNA-seq is not knowable from the file, and the annotation it needs is
  // checked server-side at launch, where the answer can say what is missing.
  const canQuantify = obj.status === "ready" && obj.format.kind === "bam";
  // Long reads only, and only once QC has said which kind. The Actions card
  // explains the two refusals; this button simply does not appear, because a
  // permanently disabled button in a row of live ones reads as broken.
  const canAssemble =
    obj.status === "ready" &&
    obj.format.kind === "fastq" &&
    ["hifi", "clr", "ont_simplex", "ont_duplex"].includes(
      String(obj.facts?.qc_read_chemistry ?? ""),
    );
  // Counts is the natural home: DE always needs a design across the
  // project's counts files, so this is a shortcut into the same
  // project-scoped dialog rather than a per-file operation in disguise.
  const canDifferentialExpression = obj.role === "counts";

  // FASTA, excluding protein/transcript roles -- not gated on provenance, so
  // an uploaded assembly is as eligible as one this application produced.
  // Same rule the card and the launch path both apply server-side; mirrored
  // here only so the button does not appear where the launch would refuse it
  // anyway, the same reasoning canAssemble's own comment gives.
  const canScoreCompleteness =
    obj.status === "ready" &&
    obj.format.kind === "fasta" &&
    obj.role !== "protein" &&
    obj.role !== "transcript";
  // Same assembly-shape gate as canScoreCompleteness, for the same reason:
  // the launch and the card both refuse elsewhere, so the button matching
  // that here is cosmetic consistency, not the actual guard.
  const canScaffold = canScoreCompleteness;

  return (
    <div className="panel">
      <div className="panel-body detail">
        {/* Identity, status and verdict on one line above the name: what kind
            of file this is and whether it is usable, before what it is called. */}
        <div className="detail-kicker">
          <span>{kindLabel}</span>
          <span>{formatBytes(obj.size)}</span>
          {obj.format.kind !== "unknown" && (
            <span>{formatKindLabel(obj.format.kind)}</span>
          )}
          {/* Sits with the format tokens rather than the badges: it says what
              the file holds, which is the same kind of fact as "FASTA", not a
              judgement about it. Absent when unset -- for most files nothing
              detects it, and an "unknown" chip on every FASTQ would be noise. */}
          {sequenceType && <span>{sequenceType}</span>}
          {compression && <span>{compression}</span>}
          <span className={`badge ${obj.status}`}>{obj.status}</span>
          {/* A judgement about the file rather than a property of it, so it
              trails the identifying tokens and carries its caveats. */}
          {quality && (
            <span
              className="badge quality"
              title={quality.tooltip}
              style={{ cursor: "help" }}
            >
              {quality.word}
            </span>
          )}
        </div>

        <div className="detail-headline">
          <div className="detail-headline-main">
            <div className="detail-title">{obj.name}</div>
            {/* Only the species here: size, format and quality already sit in
                the kicker, and repeating them under the name says nothing new.
                Read-only -- the editable control lives in the metadata form. */}
            {species && (
              <div className="detail-subtitle">
                <span
                  style={{ fontStyle: "italic" }}
                  title="Change under Metadata → Sample → Organism"
                >
                  {species}
                </span>
              </div>
            )}

            {/* Background on the species, under its name. Self-suppressing:
                nothing renders without a known organism and a model server, so
                a file with neither keeps the bare headline it had before. */}
            <OrganismBlurb organism={species} />
          </div>

          <FileHeadlineStats stats={stats} />
        </div>

        {/* A reminder, not a guard: a second run with different settings is
            legitimate, and the dedup key already stops an identical repeat. */}
        <ActivePipelineJobs objectId={obj.id} />

        {obj.status !== "ready" && obj.status !== "error" && (
          <IngestProgress objectId={obj.id} />
        )}

        {obj.error && (
          <div className="error-box">
            <strong>{obj.error.code}</strong>: {obj.error.message}
          </div>
        )}

        {formatDisagreement && (
          <div className="warn-box">
            Format mismatch: the filename suggests{" "}
            <strong>{formatKindLabel(obj.format.extension_says!)}</strong> but the
            contents look like{" "}
            <strong>{formatKindLabel(obj.format.magic_says!)}</strong>.
          </div>
        )}

        <Tabs tabs={tabs} active={tab} onChange={setTab} idPrefix="obj" />

        {tab === "qc" && (
          <TabPanel id="qc" idPrefix="obj">
            <QcTab
              obj={obj}
              isReference={isReference}
              // Built here because it needs the same runQC mutation the
              // Computations section drives; QcTab only decides where it sits.
              runQcPrompt={
                canQC && !hasQc ? (
                  <div
                    className="warn-box"
                    style={{
                      marginBottom: 12,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      gap: 12,
                    }}
                  >
                    <span>
                      No QC has been run on this file yet. Read chemistry,
                      adapter content and the quality distribution all come
                      from it — and several pipeline suggestions stay disabled
                      without it.
                    </span>
                    <button
                      type="button"
                      className="btn primary"
                      style={{ flexShrink: 0 }}
                      onClick={() => runQC.mutate()}
                      disabled={runQC.isPending || qcActive}
                    >
                      {runQC.isPending || qcActive ? "Running QC…" : "Run QC"}
                    </button>
                  </div>
                ) : null
              }
            />
          </TabPanel>
        )}

        {tab === "results" && (
          <TabPanel id="results" idPrefix="obj">
            {obj.role === "de_results" ? (
              <ExpressionResults obj={obj} />
            ) : obj.format.kind === "bam" ? (
              <BamResults obj={obj} />
            ) : (
              <VariantResults obj={obj} />
            )}
          </TabPanel>
        )}

        {tab === "metadata" && (
          <TabPanel id="metadata" idPrefix="obj">
            <MetadataTab
              obj={obj}
              isReference={isReference}
              canIndex={canIndex}
              onSave={(m) => save.mutate(m)}
              saving={save.isPending}
              onDirtyChange={setMetadataDirty}
            />
          </TabPanel>
        )}

        {tab === "history" && (
          <TabPanel id="history" idPrefix="obj">
            {/* The runs table lives inside the narrative's left column: it
                owns its own query, but the layout places it under the
                lineage rather than beside it. */}
            <ProvenanceNarrative
              key={obj.id}
              objectId={obj.id}
              runs={<ComputationHistory objectId={obj.id} />}
            />
          </TabPanel>
        )}

        {tab === "actions" && (
          <TabPanel id="actions" idPrefix="obj">
            <ActionsTab
              obj={obj}
              computations={
                <Computations
                  canPreprocess={canTrim}
                  canAlign={canAlign}
                  canCallVariants={canCallVariants}
                  canQuantify={canQuantify}
                  onQuantify={() => setQuantifyOpen(true)}
                  canAssemble={canAssemble}
                  onAssemble={() => setAssembleOpen(true)}
                  canScoreCompleteness={canScoreCompleteness}
                  onScoreCompleteness={() => setCompletenessOpen(true)}
                  canScaffold={canScaffold}
                  onScaffold={() => setScaffoldOpen(true)}
                  canDifferentialExpression={canDifferentialExpression}
                  onDifferentialExpression={() => setDeOpen(true)}
                  canQC={canQC}
                  hasQc={hasQc}
                  hasTrim={hasTrim}
                  trimActive={trimActive}
                  alignTarget={alignTarget}
                  onStart={startFlow}
                  onRunQC={() => runQC.mutate()}
                  qcPending={runQC.isPending || qcActive}
                  onReingest={() => reingest.mutate()}
                  reingestPending={reingest.isPending}
                  reingestDisabled={!obj.blob_sha256}
                />
              }
              confirmingDelete={confirmingDelete}
              setConfirmingDelete={setConfirmingDelete}
              remove={remove}
              onTagsChanged={() => qc.invalidateQueries({ queryKey: ["object", id] })}
              metadataDirty={metadataDirty}
            />
          </TabPanel>
        )}
      </div>

      {flow != null && flow.tool == null && (
        <PipelineToolSelector
          pipeline={flow.pipeline}
          selected={pendingTool}
          onSelect={setPendingTool}
          onContinue={() => {
            if (pendingTool) setFlow({ pipeline: flow.pipeline, tool: pendingTool });
          }}
          onClose={() => setFlow(null)}
          object={obj}
        />
      )}
      {flow?.pipeline === "trim" && flow.tool != null && (
        <TrimDialog
          object={obj}
          selectedTool={flow.tool}
          onBack={() => {
            // Reopens on the previously chosen card rather than nothing
            // highlighted -- "change your mind" should not look like
            // "start over".
            setPendingTool(flow.tool);
            setFlow({ pipeline: "trim", tool: null });
          }}
          onClose={() => setFlow(null)}
        />
      )}
      {flow?.pipeline === "align" && flow.tool != null && (
        <AlignDialog
          object={obj}
          selectedTool={flow.tool as AlignerName}
          onBack={() => {
            setPendingTool(flow.tool);
            setFlow({ pipeline: "align", tool: null });
          }}
          onClose={() => setFlow(null)}
        />
      )}
      {flow?.pipeline === "variant" && flow.tool != null && (
        <VariantDialog
          object={obj}
          selectedTool={flow.tool as VariantCallerName}
          onBack={() => {
            setPendingTool(flow.tool);
            setFlow({ pipeline: "variant", tool: null });
          }}
          onClose={() => setFlow(null)}
        />
      )}
      {quantifyOpen && (
        <QuantifyDialog object={obj} onClose={() => setQuantifyOpen(false)} />
      )}
      {assembleOpen && (
        <AssembleDialog object={obj} onClose={() => setAssembleOpen(false)} />
      )}
      {completenessOpen && (
        <CompletenessDialog
          object={obj}
          onClose={() => setCompletenessOpen(false)}
        />
      )}
      {scaffoldOpen && (
        <ScaffoldDialog object={obj} onClose={() => setScaffoldOpen(false)} />
      )}
      {deOpen && (
        <DifferentialExpressionDialog
          projectId={obj.project_id}
          onClose={() => setDeOpen(false)}
        />
      )}
    </div>
  );
}

/**
 * Whether the file is any good: what the parser found in it, and what the
 * pipeline steps did to it. Trim and alignment reports render nothing when
 * their facts are absent, so an untrimmed FASTQ shows only facts and charts.
 */
function QcTab({
  obj,
  isReference,
  runQcPrompt,
}: {
  obj: ObjectDetailData;
  isReference: boolean;
  /** Offered when QC has never run; see where it is built in ObjectDetail. */
  runQcPrompt: React.ReactNode;
}) {
  const hasFacts = Object.keys(obj.facts).length > 0;
  const composition = Array.isArray(obj.facts.base_composition)
    ? obj.facts.base_composition
    : null;
  // A FASTA carries no per-base qualities, so the quality curve is meaningless
  // for a reference.
  const curve =
    !isReference && Array.isArray(obj.facts.quality_per_position)
      ? obj.facts.quality_per_position
      : null;
  // Both are reads-only: a reference has no per-read GC distribution worth
  // drawing, and no per-cycle N.
  const gcHistogram =
    !isReference && Array.isArray(obj.facts.gc_per_read_histogram)
      ? obj.facts.gc_per_read_histogram
      : null;
  const nCurve =
    !isReference && Array.isArray(obj.facts.qc_n_per_position)
      ? obj.facts.qc_n_per_position
      : null;
  // ChromosomeStrip renders nothing for `kind: "nothing"` (no sequence facts
  // at all, e.g. a GFF sidecar). Without this check, a reference with no
  // composition/curve either would still open an empty .qc-charts grid --
  // a visible gap with nothing in it.
  const showChromStrip =
    isReference && classifyChromosomes(obj.facts).kind !== "nothing";
  const lengthHistogram = Array.isArray(obj.facts.read_length_histogram)
    ? obj.facts.read_length_histogram
    : null;
  // Log axis for the platforms whose read lengths span orders of magnitude;
  // everything else (including "QC never run yet", the common raw-upload
  // case) defaults to linear, matching the reference FastQC single-peak
  // shape. Mirrors LONG_READ_PLATFORMS in backend/app/pipelines/qc_stats.py.
  const isLongReadPlatform =
    obj.facts.qc_platform === "OXFORD_NANOPORE" || obj.facts.qc_platform === "PACBIO_SMRT";

  // Fetched only when this tab is mounted (QcTab only exists in the tree
  // while the QC tab is active) and the file actually has tiles. The matrix
  // is far larger than the object document it's described by, so it must
  // not ride along with the rest of the detail panel's own load.
  const tileSource =
    typeof obj.facts.qc_tile_source === "string" ? obj.facts.qc_tile_source : undefined;
  const [tileMatrix, setTileMatrix] = useState<TileMatrix | null>(null);

  useEffect(() => {
    if (tileSource !== "present") {
      setTileMatrix(null);
      return;
    }
    let cancelled = false;
    api
      .qcTileMatrix(obj.id)
      .then((m) => !cancelled && setTileMatrix(m))
      // A missing or unreadable matrix renders nothing, the same as a file
      // that never had tiles. It is an extra, not a promise.
      .catch(() => !cancelled && setTileMatrix(null));
    return () => {
      cancelled = true;
    };
  }, [obj.id, tileSource]);

  // Which tool produced these numbers. Sits under the tab as one line rather
  // than being repeated as a note on every group below it.
  const provenance = [
    typeof obj.facts.qc_tool === "string"
      ? `Parsed by ${obj.facts.qc_tool}` +
        (typeof obj.facts.qc_tool_version === "string"
          ? ` ${obj.facts.qc_tool_version}`
          : "")
      : null,
    typeof obj.facts.trimmed_by === "string"
      ? `trimmed with ${obj.facts.trimmed_by}` +
        (typeof obj.facts.trim_tool_version === "string"
          ? ` ${obj.facts.trim_tool_version}`
          : "")
      : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <>
      {provenance && <div className="qc-provenance">{provenance}</div>}

      {/* The prompt lives here rather than in the headline, where a Run QC
          button used to sit permanently: this is the screen where noticing
          that QC never ran actually happens, and on this data most files are
          in exactly that state. It also un-gates the align suggestion, which
          says so from the Actions tab without being able to point at a
          button on it. */}
      {runQcPrompt}

      {/* Charts lead: the shape of the data answers "is this any good?" faster
          than any table of it can, and the numbers below are what you check
          once the shape has raised a question. */}
      {(composition ||
        curve ||
        lengthHistogram ||
        gcHistogram ||
        nCurve ||
        showChromStrip ||
        obj.facts.qc_adapter_content != null ||
        obj.facts.qc_duplication_levels != null ||
        tileMatrix) && (
        <div className="qc-charts">
          {composition && (
            <div className="qc-chart">
              <div className="section-title">Base composition</div>
              <BaseCompositionChart
                composition={composition as never}
                sampledReads={obj.facts.stats_sampled_reads as number | undefined}
                sampledBases={obj.facts.stats_sampled_bases as number | undefined}
                gcPercent={obj.facts.gc_content_percent as number | undefined}
              />
            </div>
          )}
          {curve && (
            <div className="qc-chart">
              <div className="section-title">Quality per position</div>
              <QualityChart curve={curve as never} />
            </div>
          )}
          {lengthHistogram && (
            <div className="qc-chart">
              <div className="section-title">Read length distribution</div>
              <LengthDistributionChart
                buckets={lengthHistogram as never}
                logScale={isLongReadPlatform}
                sampledReads={obj.facts.stats_sampled_reads as number | undefined}
              />
            </div>
          )}
          {gcHistogram && (
            <div className="qc-chart">
              <div className="section-title">GC distribution</div>
              <GcDistributionChart
                histogram={gcHistogram as never}
                meanGc={obj.facts.gc_per_read_mean as number | undefined}
                expected={obj.expected_gc}
                sampledReads={obj.facts.stats_sampled_reads as number | undefined}
              />
            </div>
          )}
          {nCurve && (
            <div className="qc-chart">
              <div className="section-title">N content per position</div>
              <NContentChart curve={nCurve as never} />
            </div>
          )}
          {/* Contamination and library complexity, from the whole-file QC
              scan. Both self-suppress on files QC'd before that scan existed,
              so the grid keeps its old shape for them. */}
          {obj.facts.qc_adapter_content != null && (
            <div className="qc-chart">
              <div className="section-title">Adapter content</div>
              <AdapterContentChart
                positions={(obj.facts.qc_adapter_content as never as { positions: number[] }).positions}
                series={(obj.facts.qc_adapter_content as never as { series: { name: string; values: number[] }[] }).series}
              />
            </div>
          )}
          {obj.facts.qc_duplication_levels != null && (
            <div className="qc-chart">
              <div className="section-title">Sequence duplication levels</div>
              <DuplicationLevelsChart
                labels={(obj.facts.qc_duplication_levels as never as { labels: string[] }).labels}
                percentages={(obj.facts.qc_duplication_levels as never as { percentages: number[] }).percentages}
                percentUnique={obj.facts.qc_percent_unique as number | undefined}
                scannedReads={obj.facts.qc_duplication_scanned_reads as number | undefined}
              />
            </div>
          )}
          {/* Second column on a reference, where the quality curve would be
              for reads. Renders nothing when the file has no sequence facts,
              so a GFF sidecar keeps the single-column layout. */}
          {showChromStrip && <ChromosomeStrip facts={obj.facts} />}
          {tileMatrix && (
            <div className="qc-chart">
              <div className="section-title">Quality per tile</div>
              <TileQualityChart data={tileMatrix} />
            </div>
          )}
        </div>
      )}

      {!hasFacts && (
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          {obj.status === "ingesting"
            ? "Parsing headers…"
            : "No header facts extracted for this format."}
        </div>
      )}

      {/* Ahead of the tables it describes, and outside the reference/reads
          split because both kinds of file have something worth narrating.
          Renders nothing at all when there is no summary and no model server,
          so a user without one sees this tab exactly as it was. */}
      <AiSummary
        facts={obj.facts}
        objectId={obj.id}
        fingerprint={obj.summary_fingerprint ?? undefined}
      />

      {isReference ? (
        hasFacts && (
          <AssemblyFacts
            facts={obj.facts}
            objectId={obj.id}
            projectId={obj.project_id}
          />
        )
      ) : (
        /* One column flow for everything below the charts: the parsed-fact
           groups, the QC report and the trim comparison are all cards of the
           same kind, and they pack by height together rather than the last
           two spanning the full width under the rest. */
        <FactsColumns>
          {/* An assembly graph object. Rendered above the fact table because
              the shape is the finding and the counts merely quantify it.
              Absent for a graph past the topology cap, where the parser keeps
              gfa_topology_partial and the counts alone. Wrapped in .section so
              FactsColumns' height measurement (:scope > .facts-group,
              .section) treats it as one indivisible block like its
              siblings. */}
          {Array.isArray(obj.facts.gfa_segments) &&
            Array.isArray(obj.facts.gfa_links) && (
              <div className="section">
                <AssemblyGraph
                  key={obj.id}
                  segments={obj.facts.gfa_segments as [string, number][]}
                  links={obj.facts.gfa_links as [string, string, string, string][]}
                />
              </div>
            )}

          {/* Already grouped by subject -- File contents, Measured quality,
              Header and so on. */}
          {hasFacts && <FactsTable facts={obj.facts} columns />}

          {/* The file as it is, before anything was done to it. Ahead of the
              trim comparison because it describes the starting point that
              comparison is against. */}
          <QcReport facts={obj.facts} objectId={obj.id} />

          {/* Before/after comparison, on the source file rather than the
              output: "what did trimming do to my reads" is a question about
              the input. */}
          <TrimReport facts={obj.facts} projectId={obj.project_id} />
        </FactsColumns>
      )}
    </>
  );
}

/** What the file is and where it came from. */

// Confidence of format detection. The raw enum values are jargon and read
// as bugs; map them to short human-readable labels.
const CONFIDENCE_LABELS: Record<string, string> = {
  magic: "Identified from contents",
  extension: "Guessed from filename",
  user: "Set by user",
  none: "Unknown",
};

function confidenceLabel(raw: string): string {
  return CONFIDENCE_LABELS[raw] ?? raw;
}

function MetadataTab({
  obj,
  isReference,
  canIndex,
  onSave,
  saving,
  onDirtyChange,
}: {
  obj: ObjectDetailData;
  isReference: boolean;
  canIndex: boolean;
  onSave: (metadata: Record<string, unknown>) => void;
  saving: boolean;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const compression = compressionLabel(obj.format.compression);

  return (
    <>
      <div className="section-note tab-intro">
        Where this file came from and what has been made from it
      </div>

      {/* What is true of the file, in two columns. Read-only: these are facts
          about the bytes and the archive, not fields anyone edits here. */}
      <FactsColumns>
        <div className="section">
          <div className="section-title">Format</div>
          <dl className="kv">
            <dt>Detected</dt>
            <dd>{formatKindLabel(obj.format.kind)}</dd>
            <dt>Compression</dt>
            <dd>{compression ?? "None"}</dd>
            <dt>Confidence</dt>
            <dd>{confidenceLabel(obj.format.confidence)}</dd>
            {obj.format.detected_at && (
              <>
                <dt>Detected at</dt>
                <dd>{formatDate(obj.format.detected_at)}</dd>
              </>
            )}
          </dl>
        </div>

        <div className="section">
          <div className="section-title">Storage</div>
          <dl className="kv">
            <dt>SHA-256</dt>
            <dd>
              <TruncatedValue value={obj.blob_sha256} head={16} />
            </dd>
            {obj.blob && (
              <>
                <dt>State</dt>
                <dd>
                  <span className={`badge ${obj.blob.state}`}>{obj.blob.state}</span>
                </dd>
                <dt>Mode</dt>
                <dd>{obj.blob.storage}</dd>
                <dt>Path</dt>
                <dd>
                  <TruncatedValue
                    value={obj.blob.external_path ?? obj.blob.rel_path}
                    head={28}
                  />
                </dd>
                <dt>References</dt>
                <dd>
                  {obj.blob.ref_count}
                  {obj.blob.ref_count > 1 && (
                    <span style={{ color: "var(--text-faint)" }}> (deduplicated)</span>
                  )}
                </dd>
                <dt>Verified</dt>
                <dd>{formatDate(obj.blob.last_verified_at)}</dd>
              </>
            )}

            {/* Record-keeping rather than storage proper, but it belongs with
                the other facts about this row in the database rather than in
                a card of its own. */}
            <dt>Added</dt>
            <dd>{formatDate(obj.created_at)}</dd>
            <dt>Updated</dt>
            <dd>{formatDate(obj.updated_at)}</dd>
            <dt>ID</dt>
            <dd>
              <TruncatedValue value={obj.id} head={24} />
            </dd>
          </dl>
        </div>

        {/* Indexes are hidden from the explorer listing -- five files per
            reference would bury real work -- so they surface here instead. */}
        {canIndex && <IndexStatus object={obj} />}

        <DerivedFiles object={obj} />

        {/* SRA run/experiment accessions are the wrong archive for an
            assembly; assembly_accession links to NCBI Datasets instead. */}
        {!isReference && (
          <SraPanel
            facts={obj.facts}
            formatKind={obj.format.kind}
            metadata={obj.metadata}
          />
        )}
      </FactsColumns>

      {/* The editable form, below everything that merely describes the file.
          Full width: its groups lay out in columns of their own, which a
          half-width column would collapse into one cramped stack. */}
      <div className="section">
        <div className="section-title">Record</div>
        <div className="section-note">
          Editable — these fields travel with the file into every pipeline it
          feeds.
        </div>
        {/* Keyed on the role so a conversion remounts the editor: its schema
            changes underneath, and in-progress edits belong to the previous
            role's fields. Without this its dirty guard would keep them. */}
        <SchemaMetadataEditor
          key={obj.role ?? "none"}
          value={obj.metadata}
          formatKind={obj.format.kind}
          objectId={obj.id}
          role={obj.role}
          onSave={onSave}
          saving={saving}
          onDirtyChange={onDirtyChange}
          // Only when the Public archive section is actually rendered above:
          // on a reference it is not, and hiding the accessions would leave
          // them nowhere.
          dedupeGroups={isReference ? [] : ["Archive"]}
        />
      </div>
    </>
  );
}

/**
 * Everything you can do to this file, in three sections.
 *
 * Computations first, then the suggestion cards, then record-keeping. The
 * order is not cosmetic: a gated card explains itself with a reason like "Run
 * QC to determine read chemistry", and the QC button that resolves it has to
 * be visible above the card saying so -- otherwise the card names a fix that
 * appears to be nowhere on screen.
 *
 * `computations` arrives as a node rather than being built here because those
 * buttons drive the pipeline dialogs, whose state and mutations live in
 * ObjectDetail. Passing the finished element keeps that ownership where it is
 * instead of threading a dozen callbacks through this component.
 */
function ActionsTab({
  obj,
  computations,
  confirmingDelete,
  setConfirmingDelete,
  remove,
  onTagsChanged,
  metadataDirty,
}: {
  obj: ObjectDetailData;
  computations: React.ReactNode;
  confirmingDelete: boolean;
  setConfirmingDelete: (v: boolean) => void;
  remove: { mutate: () => void; isPending: boolean };
  onTagsChanged: () => void;
  metadataDirty: boolean;
}) {
  return (
    <>
      {computations}

      <div className="section">
        <div className="section-title">Launch a pipeline on this file</div>
        {/* Gated on readiness here rather than inside the grid: the backend
            returns an empty list for a file that is not READY, which the
            component can only render as "no suggestions" -- a verdict, when
            the truth is "not yet". Answering it from `obj.status`, which this
            component already has, is simpler than a prop that would only
            restate what the caller knows. */}
        {obj.status === "ready" ? (
          <PipelineSuggestions objectId={obj.id} projectId={obj.project_id} />
        ) : (
          <p className="suggestion-none">
            Suggestions appear once this file has finished ingesting.
          </p>
        )}
      </div>

      <ManageFile
        obj={obj}
        confirmingDelete={confirmingDelete}
        setConfirmingDelete={setConfirmingDelete}
        remove={remove}
        onTagsChanged={onTagsChanged}
        metadataDirty={metadataDirty}
      />
    </>
  );
}
