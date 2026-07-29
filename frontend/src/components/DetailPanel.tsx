import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type {
  AlignerName,
  ObjectDetail as ObjectDetailData,
  VariantCallerName,
} from "../api/types";
import {
  compressionLabel,
  formatBytes,
  formatDate,
  formatKindLabel,
  shortHash,
} from "../lib/format";
import { readQuality } from "../lib/readQuality";
import { notify } from "../stores/messageStore";
import { AssemblyFacts } from "./AssemblyFacts";
import { FactsTable } from "./FactsTable";
import { IngestProgress } from "./IngestProgress";
import { BaseCompositionChart, QualityChart } from "./SequenceCharts";
import { JobList } from "./JobList";
import { MetadataEditor } from "./MetadataEditor";
import { RoleConverter } from "./RoleConverter";
import { SchemaMetadataEditor } from "./SchemaMetadataEditor";
import { DerivedFiles } from "./DerivedFiles";
import { ActivePipelineJobs } from "./ActivePipelineJobs";
import { AlignDialog } from "./AlignDialog";
import { BamResults } from "./BamResults";
import { IndexStatus } from "./IndexStatus";
import { PipelineToolSelector } from "./PipelineToolSelector";
import { ProjectDangerZone } from "./ProjectDangerZone";
import { TrimDialog } from "./TrimDialog";
import { VariantDialog } from "./VariantDialog";
import { QcReport } from "./QcReport";
import { TrimReport } from "./TrimReport";
import { SraPanel } from "./SraPanel";
import { TabPanel, Tabs, type TabDef } from "./Tabs";
import { TagEditor } from "./TagEditor";

/** The right panel: details of whatever is selected in the left panel. */
export function DetailPanel() {
  const [params] = useSearchParams();
  const sel = params.get("sel");

  if (!sel) return <EmptyDetail />;
  const [kind, id] = sel.split(":");
  if (kind === "project") return <ProjectDetail id={id} />;
  if (kind === "object") return <ObjectDetail id={id} />;
  return <EmptyDetail />;
}

function EmptyDetail() {
  return (
    <div className="panel">
      <div className="panel-header">
        <span className="panel-title">Details</span>
      </div>
      <div className="panel-body">
        <div className="empty">
          <div className="empty-title">Nothing selected</div>
          <div>Select a project or file to see its details.</div>
        </div>
      </div>
    </div>
  );
}

function ProjectDetail({ id }: { id: string }) {
  const qc = useQueryClient();
  const { data: project, isLoading } = useQuery({
    queryKey: ["project", id],
    queryFn: () => api.getProject(id),
  });

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
          <div
            style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 2 }}
          >
            <div className="detail-title" style={{ marginBottom: 0 }}>
              {project.name}
            </div>
            <button
              type="button"
              className="icon-btn"
              title="Rename project"
              onClick={startEditingName}
            >
              ✎
            </button>
          </div>
        )}
        <div className="detail-subtitle">{project.description || "No description"}</div>

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
          <div className="section-title">Metadata</div>
          <MetadataEditor
            value={project.metadata}
            onSave={(m) => save.mutate(m)}
            saving={save.isPending}
          />
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

/** Ordered so the panel opens on the question people ask most: is this file
 * good? Results sits next to QC -- they answer adjacent questions -- and only
 * appears for BAMs, which is the only format it currently describes. */
function tabsFor(formatKind: string): TabDef[] {
  const tabs: TabDef[] = [{ id: "qc", label: "QC" }];
  if (formatKind === "bam") {
    tabs.push({ id: "results", label: "Results" });
  }
  tabs.push({ id: "metadata", label: "Metadata" }, { id: "actions", label: "Actions" });
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
      notify.success("Metadata saved");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const reingest = useMutation({
    mutationFn: () => api.reingestObject(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["object", id] });
      qc.invalidateQueries({ queryKey: ["jobs"] });
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

  const tabs = tabsFor(obj.format.kind);
  const raw = params.get("tab");
  const tab = tabs.some((t) => t.id === raw) ? raw! : "qc";

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

  // Same function the explorer rows use, so the word here and the word there
  // can never disagree.
  const quality = readQuality(obj);

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

  return (
    <div className="panel">
      <div className="panel-header">
        <span className="panel-title">{isReference ? "Reference" : "File"}</span>
        {canTrim && (
          <button
            type="button"
            className="btn"
            style={{ padding: "2px 10px", fontSize: 12, marginLeft: 8 }}
            onClick={() => startFlow("trim")}
            title="Adapter-trim and quality-filter these reads"
          >
            Trim
          </button>
        )}
        {canAlign && (
          <button
            type="button"
            className="btn"
            style={{ padding: "2px 10px", fontSize: 12, marginLeft: 6 }}
            onClick={() => startFlow("align")}
            title="Align these reads against a reference"
          >
            Align
          </button>
        )}
        {canCallVariants && (
          <button
            type="button"
            className="btn"
            style={{ padding: "2px 10px", fontSize: 12, marginLeft: 6 }}
            onClick={() => startFlow("variant")}
            title="Call variants from this alignment"
          >
            Call variants
          </button>
        )}
        {/* Same eligibility as trimming, and in the header rather than the QC
            tab so it stays reachable from whichever tab is open -- including
            from Metadata, where noticing that QC has never been run is most
            likely. */}
        {canQC && (
          <button
            type="button"
            className="btn"
            style={{ padding: "2px 10px", fontSize: 12, marginLeft: 6 }}
            onClick={() => runQC.mutate()}
            disabled={runQC.isPending}
            title="Measure read quality with fastp and FastQC"
          >
            {runQC.isPending ? "QC…" : "QC"}
          </button>
        )}
        {/* A reminder, not a guard: a second run with different settings is
            legitimate, and the dedup key already stops an identical repeat. */}
        <ActivePipelineJobs objectId={obj.id} />
        <span className={`badge ${obj.status}`} style={{ marginLeft: "auto" }}>
          {obj.status}
        </span>
      </div>

      <div className="panel-body detail">
        <div className="detail-title">{obj.name}</div>
        <div className="detail-subtitle">
          {formatBytes(obj.size)}
          {obj.format.kind !== "unknown" && ` · ${formatKindLabel(obj.format.kind)}`}
          {compression && ` · ${compression}`}
          {/* Read-only here: the species is identifying enough to belong at the
              top, but one editable control per field is enough, and it already
              lives in the metadata form. Italic is the convention for a
              scientific name and separates it from the format tokens without
              changing the type. */}
          {species && (
            <>
              {" · "}
              <span
                style={{ fontStyle: "italic" }}
                title="Change under Metadata → Sample → Organism"
              >
                {species}
              </span>
            </>
          )}
          {/* Last in the line: it is a judgement about the file rather than
              an identifying property of it, and it carries the caveats. */}
          {quality && (
            <>
              {" · "}
              <span title={quality.tooltip} style={{ cursor: "help" }}>
                {quality.word}
              </span>
            </>
          )}
        </div>

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
            <QcTab obj={obj} isReference={isReference} reingest={reingest} />
          </TabPanel>
        )}

        {tab === "results" && (
          <TabPanel id="results" idPrefix="obj">
            <BamResults obj={obj} />
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

        {tab === "actions" && (
          <TabPanel id="actions" idPrefix="obj">
            <ActionsTab
              obj={obj}
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
  reingest,
}: {
  obj: ObjectDetailData;
  isReference: boolean;
  reingest: { mutate: () => void; isPending: boolean };
}) {
  return (
    <>
      <div className="section">
        <div
          className="section-title"
          style={{ display: "flex", alignItems: "center", gap: 8 }}
        >
          <span>{isReference ? "Assembly" : "Parsed facts"}</span>
          <button
            type="button"
            onClick={() => reingest.mutate()}
            disabled={reingest.isPending || !obj.blob_sha256}
            style={{
              marginLeft: "auto",
              color: "var(--accent)",
              fontSize: 11,
              textTransform: "none",
              letterSpacing: 0,
            }}
            title="Re-run format detection and header parsing"
          >
            {reingest.isPending ? "queued…" : "re-ingest"}
          </button>
        </div>

        {Object.keys(obj.facts).length > 0 ? (
          <>
            {isReference ? (
              <AssemblyFacts facts={obj.facts} />
            ) : (
              <FactsTable facts={obj.facts} />
            )}
            <div style={{ display: "flex", gap: 24, marginTop: 14, flexWrap: "wrap" }}>
              {Array.isArray(obj.facts.base_composition) && (
                <div style={{ flex: "0 1 auto" }}>
                  <div
                    style={{
                      fontSize: 11,
                      color: "var(--text-faint)",
                      marginBottom: 6,
                    }}
                  >
                    Base composition
                  </div>
                  <BaseCompositionChart
                    composition={obj.facts.base_composition as never}
                    sampledReads={obj.facts.stats_sampled_reads as number | undefined}
                    sampledBases={obj.facts.stats_sampled_bases as number | undefined}
                    gcPercent={obj.facts.gc_content_percent as number | undefined}
                  />
                </div>
              )}
              {/* A FASTA carries no per-base qualities, so the quality curve
                  is meaningless for a reference. */}
              {!isReference && Array.isArray(obj.facts.quality_per_position) && (
                <div style={{ flex: "1 1 auto", minWidth: 300 }}>
                  <div
                    style={{
                      fontSize: 11,
                      color: "var(--text-faint)",
                      marginBottom: 6,
                    }}
                  >
                    Quality per position
                  </div>
                  <QualityChart curve={obj.facts.quality_per_position as never} />
                </div>
              )}
            </div>
          </>
        ) : (
          <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
            {obj.status === "ingesting"
              ? "Parsing headers…"
              : "No header facts extracted for this format."}
          </div>
        )}
      </div>

      {/* The file as it is, before anything was done to it. Above the trim
          comparison because it describes the starting point that comparison
          is against. */}
      <QcReport facts={obj.facts} objectId={obj.id} />

      {/* Before/after comparison, on the source file rather than the output:
          "what did trimming do to my reads" is a question about the input. */}
      <TrimReport facts={obj.facts} />
    </>
  );
}

/** What the file is and where it came from. */
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
      <div className="section">
        <div className="section-title">Format</div>
        <dl className="kv">
          <dt>Detected</dt>
          <dd>{formatKindLabel(obj.format.kind)}</dd>
          <dt>Compression</dt>
          <dd>{compression ?? "None"}</dd>
          <dt>Confidence</dt>
          <dd>{obj.format.confidence}</dd>
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
          <dd className="mono" title={obj.blob_sha256 ?? ""}>
            {shortHash(obj.blob_sha256, 16)}
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
              <dd className="mono">
                {obj.blob.external_path ?? obj.blob.rel_path ?? "—"}
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
        </dl>
      </div>

      {/* Indexes are hidden from the explorer listing -- five files per
          reference would bury real work -- so they surface here instead. */}
      {canIndex && <IndexStatus object={obj} />}

      <DerivedFiles object={obj} />

      {/* SRA run/experiment accessions are the wrong archive for an
          assembly; assembly_accession links to NCBI Datasets instead. */}
      {!isReference && <SraPanel facts={obj.facts} formatKind={obj.format.kind} />}

      <div className="section">
        <div className="section-title">Metadata</div>
        {/* Keyed on the role so a conversion remounts the editor: its schema
            changes underneath, and in-progress edits belong to the previous
            role's fields. Without this its dirty guard would keep them. */}
        <SchemaMetadataEditor
          key={obj.role ?? "none"}
          value={obj.metadata}
          formatKind={obj.format.kind}
          role={obj.role}
          onSave={onSave}
          saving={saving}
          onDirtyChange={onDirtyChange}
        />
      </div>

      <div className="section">
        <div className="section-title">Record</div>
        <dl className="kv">
          <dt>Created</dt>
          <dd>{formatDate(obj.created_at)}</dd>
          <dt>Updated</dt>
          <dd>{formatDate(obj.updated_at)}</dd>
          <dt>ID</dt>
          <dd className="mono">{obj.id}</dd>
        </dl>
      </div>
    </>
  );
}

/** The operations that change the record rather than describe it. */
function ActionsTab({
  obj,
  confirmingDelete,
  setConfirmingDelete,
  remove,
  onTagsChanged,
  metadataDirty,
}: {
  obj: ObjectDetailData;
  confirmingDelete: boolean;
  setConfirmingDelete: (v: boolean) => void;
  remove: { mutate: () => void; isPending: boolean };
  onTagsChanged: () => void;
  metadataDirty: boolean;
}) {
  return (
    <>
      <div className="section">
        <div className="section-title">Tags</div>
        <TagEditor
          objectId={obj.id}
          tags={obj.tags}
          onChanged={onTagsChanged}
        />
      </div>

      <RoleConverter obj={obj} metadataDirty={metadataDirty} />

      <div className="section">
        <div className="section-title">Delete</div>

        {!confirmingDelete ? (
          <div>
            <button
              type="button"
              className="btn danger"
              onClick={() => setConfirmingDelete(true)}
            >
              Delete file
            </button>
            <div
              style={{ color: "var(--text-faint)", fontSize: 11, marginTop: 6 }}
            >
              {obj.blob?.storage === "external"
                ? "Removes this entry. The original file on disk is left untouched."
                : (obj.blob?.ref_count ?? 0) > 1
                  ? `Removes this entry. ${obj.blob!.ref_count - 1} other file(s) share the same content, so the stored data is kept.`
                  : "Removes this entry. The stored data is reclaimed later by garbage collection."}
            </div>
          </div>
        ) : (
          <div className="error-box" style={{ marginBottom: 0 }}>
            <div style={{ marginBottom: 8 }}>
              Delete <strong>{obj.name}</strong>? This cannot be undone.
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button
                type="button"
                className="btn danger"
                onClick={() => remove.mutate()}
                disabled={remove.isPending}
              >
                {remove.isPending ? "Deleting…" : "Yes, delete"}
              </button>
              <button
                type="button"
                className="btn"
                onClick={() => setConfirmingDelete(false)}
                disabled={remove.isPending}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
    </>
  );
}
