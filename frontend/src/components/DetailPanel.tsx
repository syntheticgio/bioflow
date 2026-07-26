import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import {
  compressionLabel,
  formatBytes,
  formatDate,
  formatKindLabel,
  shortHash,
} from "../lib/format";
import { notify } from "../stores/messageStore";
import { AssemblyFacts } from "./AssemblyFacts";
import { FactsTable } from "./FactsTable";
import { IngestProgress } from "./IngestProgress";
import { BaseCompositionChart, QualityChart } from "./SequenceCharts";
import { JobList } from "./JobList";
import { MetadataEditor } from "./MetadataEditor";
import { RoleConverter } from "./RoleConverter";
import { SchemaMetadataEditor } from "./SchemaMetadataEditor";
import { SraPanel } from "./SraPanel";
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
      </div>
    </div>
  );
}

function ObjectDetail({ id }: { id: string }) {
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const clearSelection = () => {
    const next = new URLSearchParams(params);
    next.delete("sel");
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

  const compression = compressionLabel(obj.format.compression);
  // A .bam that isn't a BAM is worth telling the user about rather than
  // silently trusting one signal over the other.
  const formatDisagreement =
    obj.format.magic_says &&
    obj.format.extension_says &&
    obj.format.magic_says !== obj.format.extension_says;

  const isReference = obj.role === "reference";

  return (
    <div className="panel">
      <div className="panel-header">
        <span className="panel-title">{isReference ? "Reference" : "File"}</span>
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
            onSave={(m) => save.mutate(m)}
            saving={save.isPending}
          />
        </div>

        <div className="section">
          <div className="section-title">Tags</div>
          <TagEditor
            objectId={obj.id}
            tags={obj.tags}
            onChanged={() => qc.invalidateQueries({ queryKey: ["object", id] })}
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

        <RoleConverter obj={obj} />

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
      </div>
    </div>
  );
}
