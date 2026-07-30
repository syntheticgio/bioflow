import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { formatBytes, formatKindLabel } from "../lib/format";
import { readQuality } from "../lib/readQuality";
import { QualityBadge } from "./QualityBadge";
import { notify } from "../stores/messageStore";
import { useUploads } from "../hooks/useUploads";
import { NewProjectModal } from "./NewProjectModal";
import { SraDownloadDialog } from "./SraDownloadDialog";
import { orderWithPairs, type OrderedFile } from "../lib/pairing";
import type { DataObject } from "../api/types";

/**
 * The left panel. At the root it lists projects; clicking one navigates *into*
 * it within this same panel, with breadcrumbs back out. Navigation state lives
 * in the URL so reload and browser-back behave.
 */
export function ProjectExplorer() {
  const { projectId } = useParams();
  return projectId ? <ProjectView projectId={projectId} /> : <RootView />;
}

function useSelection() {
  const [params, setParams] = useSearchParams();
  const sel = params.get("sel");
  const select = (value: string | null) => {
    const next = new URLSearchParams(params);
    if (value) next.set("sel", value);
    else next.delete("sel");
    setParams(next, { replace: true });
  };
  return { sel, select };
}

function RootView() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { sel, select } = useSelection();
  const [showModal, setShowModal] = useState(false);

  const { data: projects, isLoading } = useQuery({
    queryKey: ["projects", null],
    queryFn: () => api.listProjects(),
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteProject(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      notify.success("Project deleted");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  return (
    <div className="panel panel-left">
      <div className="panel-header">
        <span className="panel-title">Projects</span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
          <button
            type="button"
            className="icon-btn"
            title="Search all files"
            onClick={() => navigate("/search")}
          >
            ⌕
          </button>
          <button
            type="button"
            className="icon-btn primary"
            title="New project"
            onClick={() => setShowModal(true)}
          >
            +
          </button>
        </div>
      </div>

      <div className="panel-body">
        {isLoading && (
          <div className="empty">
            <span className="spinner" /> Loading…
          </div>
        )}

        {projects?.length === 0 && (
          <div className="empty">
            <div className="empty-title">No projects yet</div>
            <div>Click + to create one.</div>
          </div>
        )}

        {projects?.map((p) => (
          <div
            key={p.id}
            className={`row ${sel === `project:${p.id}` ? "selected" : ""}`}
            onClick={() => select(`project:${p.id}`)}
            onDoubleClick={() => navigate(`/p/${p.id}`)}
          >
            <span className="row-icon">📁</span>
            <div className="row-main">
              <div className="row-name">{p.name}</div>
              <div className="row-sub">
                <span>{p.object_count} files</span>
                <span>{formatBytes(p.total_bytes)}</span>
              </div>
            </div>
            <button
              type="button"
              className="icon-btn row-action"
              title="Open"
              onClick={(e) => {
                e.stopPropagation();
                navigate(`/p/${p.id}`);
              }}
            >
              ›
            </button>
            <button
              type="button"
              className="icon-btn row-action"
              title="Delete project"
              onClick={(e) => {
                e.stopPropagation();
                if (confirm(`Delete project "${p.name}"?`)) del.mutate(p.id);
              }}
            >
              ×
            </button>
          </div>
        ))}
      </div>

      {showModal && <NewProjectModal onClose={() => setShowModal(false)} />}
    </div>
  );
}

type FileCategory =
  | "reads"
  | "references"
  | "alignments"
  | "variants"
  | "annotations"
  /** Protein and CDS FASTA: derived from an assembly, not reads and not a
   * reference. */
  | "sequences"
  | "hic"
  | "other";

type CategorizedFiles = Record<FileCategory, DataObject[]>;

function categorizeFile(obj: DataObject): FileCategory {
  // Role is an override: when set it decides outright, because the format
  // cannot tell a reference genome from a pile of reads -- nor from a protein
  // or CDS FASTA, which are the same format as both.
  if (obj.role === "reference") return "references";
  if (obj.role === "annotation") return "annotations";
  if (obj.role === "protein" || obj.role === "transcript") return "sequences";

  const kind = obj.format.kind.toLowerCase();
  if (kind === "fastq" || kind === "fasta") return "reads";
  if (["bam", "sam", "cram"].includes(kind)) return "alignments";
  if (["vcf", "bcf"].includes(kind)) return "variants";
  if (["bed", "gff", "gtf"].includes(kind)) return "annotations";
  if (kind === "hic") return "hic";
  return "other";
}

function categorizeObjects(objects: DataObject[] | undefined): CategorizedFiles {
  const categorized: CategorizedFiles = {
    reads: [],
    references: [],
    alignments: [],
    variants: [],
    annotations: [],
    sequences: [],
    hic: [],
    other: [],
  };

  objects?.forEach((obj) => {
    const category = categorizeFile(obj);
    categorized[category].push(obj);
  });

  return categorized;
}

const CATEGORIES: { key: FileCategory; label: string }[] = [
  { key: "reads", label: "Reads" },
  { key: "references", label: "References" },
  { key: "alignments", label: "Alignments" },
  { key: "variants", label: "Variants" },
  { key: "annotations", label: "Annotations" },
  { key: "sequences", label: "Protein & CDS" },
  { key: "hic", label: "Hi-C" },
  { key: "other", label: "Other" },
];

function ProjectView({ projectId }: { projectId: string }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { sel, select } = useSelection();
  const [dragging, setDragging] = useState(false);
  const [addMenuOpen, setAddMenuOpen] = useState(false);
  const [sraOpen, setSraOpen] = useState(false);
  const [expandedCategories, setExpandedCategories] = useState<Set<FileCategory>>(
    new Set(["reads", "references", "alignments"])
  );
  const { uploadFiles } = useUploads(projectId);

  const delObject = useMutation({
    mutationFn: (objectId: string) => api.deleteObject(objectId),
    onSuccess: (_r, objectId) => {
      qc.invalidateQueries({ queryKey: ["objects", projectId] });
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["system", "stats"] });
      if (sel === `object:${objectId}`) select(null);
      notify.success("File deleted");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const { data: project } = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId),
  });

  const { data: objects, isLoading } = useQuery({
    queryKey: ["objects", projectId],
    queryFn: () => api.listObjects(projectId),
    refetchInterval: (q) => {
      const list = q.state.data as DataObject[] | undefined;
      return list?.some((o) => o.status !== "ready" && o.status !== "error")
        ? 1500
        : false;
    },
  });

  const toggleCategory = (category: FileCategory) => {
    const next = new Set(expandedCategories);
    if (next.has(category)) {
      next.delete(category);
    } else {
      next.add(category);
    }
    setExpandedCategories(next);
  };

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const files = Array.from(e.dataTransfer.files);
    if (files.length) void uploadFiles(files);
  };

  return (
    <div className="panel panel-left">
      <div className="panel-header">
        <nav className="breadcrumbs">
          <button type="button" onClick={() => navigate("/")}>
            Projects
          </button>
          {project?.breadcrumbs.map((c, i, all) => (
            <span key={c.id} style={{ display: "contents" }}>
              <span className="sep">/</span>
              {i === all.length - 1 ? (
                <span className="crumb-current">{c.name}</span>
              ) : (
                <button type="button" onClick={() => navigate(`/p/${c.id}`)}>
                  {c.name}
                </button>
              )}
            </span>
          ))}
        </nav>

        <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
          <button
            type="button"
            className="icon-btn"
            title="Search within this project"
            onClick={() => navigate(`/search?project_id=${projectId}`)}
          >
            ⌕
          </button>
          {/* A split button: uploading is much the commoner action and keeps
              the one-click path, while the chevron reaches the alternative
              without turning every upload into a menu choice. */}
          <div className="split-btn">
            <label className="icon-btn primary" title="Upload files" style={{ cursor: "pointer" }}>
              +
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
            <button
              type="button"
              className="icon-btn primary split-btn-toggle"
              title="More ways to add files"
              aria-haspopup="menu"
              aria-expanded={addMenuOpen}
              onClick={() => setAddMenuOpen((v) => !v)}
            >
              ▾
            </button>
            {addMenuOpen && (
              <>
                {/* Click-away rather than a focus trap: the menu holds one
                    item, and dismissing it must not steal focus from the
                    dialog it opens. */}
                <div className="menu-scrim" onClick={() => setAddMenuOpen(false)} />
                <div className="split-btn-menu" role="menu">
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setAddMenuOpen(false);
                      setSraOpen(true);
                    }}
                  >
                    Download from NCBI SRA…
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {sraOpen && (
        <SraDownloadDialog projectId={projectId} onClose={() => setSraOpen(false)} />
      )}

      <div
        className={`dropzone ${dragging ? "dragging" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragging(false);
        }}
        onDrop={onDrop}
      >
        {isLoading && (
          <div className="empty">
            <span className="spinner" /> Loading…
          </div>
        )}

        {objects?.length === 0 && !isLoading && (
          <div className="dropzone-hint">
            <div style={{ fontSize: 26, marginBottom: 8 }}>⬚</div>
            <div className="empty-title">No files in this project</div>
            <div>Drag files here to upload.</div>
          </div>
        )}

        {objects && objects.length > 0 &&
          CATEGORIES.map((category) => {
            const categoryFiles = categorizeObjects(objects)[category.key];
            if (categoryFiles.length === 0) return null;

            const isExpanded = expandedCategories.has(category.key);

            // Only Reads carries mate pairs, and the reorder that draws the
            // spine adjacently is a real change from the API's newest-first
            // order -- confined to the one category it's meaningful for, so
            // References/Alignments/Variants/etc. keep their existing order.
            const displayFiles: OrderedFile[] =
              category.key === "reads"
                ? orderWithPairs(categoryFiles)
                : categoryFiles.map((o) => ({ object: o, pair: null }));

            return (
              <div key={category.key}>
                <button
                  type="button"
                  className="group-title"
                  aria-expanded={isExpanded}
                  onClick={() => toggleCategory(category.key)}
                >
                  <span className="group-chevron">▶</span>
                  <span>{category.label}</span>
                  <span className="group-count">{categoryFiles.length}</span>
                </button>

                {isExpanded &&
                  displayFiles.map(({ object: o, pair }) => {
                    const quality = readQuality(o);
                    return (
                      <div
                        key={o.id}
                        className={`row ${sel === `object:${o.id}` ? "selected" : ""}${
                          pair ? ` paired paired-${pair}` : ""
                        }`}
                        onClick={() => select(`object:${o.id}`)}
                      >
                        <span className="row-icon">
                          {o.status !== "ready"
                            ? "⏳"
                            : o.role === "reference"
                              ? "📗"
                              : "📄"}
                          {quality && <QualityBadge quality={quality} />}
                        </span>
                        <div className="row-main">
                          <div className="row-name">{o.name}</div>
                          <div className="row-sub">
                            <span>{formatBytes(o.size)}</span>
                            {o.format.kind !== "unknown" && (
                              <span>{formatKindLabel(o.format.kind)}</span>
                            )}
                            {/* After size and format, matching the detail
                                panel's ordering. */}
                            {quality && (
                              <span title={quality.tooltip}>{quality.word}</span>
                            )}
                            {o.status !== "ready" && <span>{o.status}</span>}
                            {o.read_number != null && (
                              <span className="read-badge">R{o.read_number}</span>
                            )}
                          </div>
                        </div>
                        <button
                          type="button"
                          className="icon-btn row-action"
                          title="Delete file"
                          onClick={(e) => {
                            e.stopPropagation();
                            if (confirm(`Delete "${o.name}"?`))
                              delObject.mutate(o.id);
                          }}
                        >
                          ×
                        </button>
                      </div>
                    );
                  })}
              </div>
            );
          })}
      </div>
    </div>
  );
}
