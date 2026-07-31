import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { formatBytes, formatKindLabel } from "../lib/format";
import { readQuality } from "../lib/readQuality";
import { notify } from "../stores/messageStore";
import { useUploads } from "../hooks/useUploads";
import { NewProjectModal } from "./NewProjectModal";
import { NcbiDownloadDialog } from "./NcbiDownloadDialog";
import { groupPairs, orderWithPairs, type OrderedFile } from "../lib/pairing";
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
  const [filter, setFilter] = useState("");

  const { data: projects, isLoading } = useQuery({
    queryKey: ["projects", null],
    queryFn: () => api.listProjects(),
  });

  // Client-side: the whole project list is already loaded here, and it is a
  // handful of rows -- a round trip per keystroke would be slower and worse.
  const needle = filter.trim().toLowerCase();
  const visible = needle
    ? projects?.filter(
        (p) =>
          p.name.toLowerCase().includes(needle) ||
          (p.description ?? "").toLowerCase().includes(needle),
      )
    : projects;

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
        {/* No search affordance here: /search indexes files, not projects, so
            from the root it would lead somewhere that cannot answer the
            question being asked. The filter below is what searches projects. */}
        <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
          <button
            type="button"
            className="btn-text"
            title="New project"
            onClick={() => setShowModal(true)}
          >
            New project
          </button>
        </div>
      </div>

      <div className="panel-filter">
        <input
          type="search"
          placeholder="Filter projects…"
          aria-label="Filter projects"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
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
            <div>Click New project to create one.</div>
          </div>
        )}

        {/* Distinct from the empty state above: projects exist, this filter
            just does not match any of them. */}
        {projects?.length !== 0 && visible?.length === 0 && (
          <div className="empty">
            <div className="empty-title">No matching projects</div>
            <div>No project matches “{filter.trim()}”.</div>
          </div>
        )}

        {visible?.map((p) => (
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
              className="icon-btn row-action row-action-danger"
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
  const [ncbiOpen, setNcbiOpen] = useState(false);
  const [expandedCategories, setExpandedCategories] = useState<Set<FileCategory>>(
    new Set(["reads", "references", "alignments"])
  );
  const [filter, setFilter] = useState("");
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

  const filteredObjects = objects?.filter((o) => {
    const q = filter.trim().toLowerCase();
    if (!q) return true;
    return (
      o.name.toLowerCase().includes(q) || o.tags.some((t) => t.toLowerCase().includes(q))
    );
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
      <div className="panel-header" style={{ flexDirection: "column", alignItems: "stretch" }}>
        <nav className="breadcrumbs">
          <button type="button" onClick={() => navigate("/")}>
            Projects
          </button>
          <span className="sep">/</span>
          <span className="crumb-current">Files</span>
        </nav>

        <div style={{ display: "flex", alignItems: "center" }}>
          <div className="detail-title" style={{ margin: 0 }}>
            {project?.breadcrumbs.at(-1)?.name}
          </div>

          <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
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
                        setNcbiOpen(true);
                      }}
                    >
                      Download from NCBI…
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      </div>

      {ncbiOpen && (
        <NcbiDownloadDialog projectId={projectId} onClose={() => setNcbiOpen(false)} />
      )}

      <div className="panel-filter">
        <input
          type="search"
          placeholder="Filter files, tags, runs…"
          aria-label="Filter files"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      </div>

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

        {objects && objects.length > 0 && filteredObjects?.length === 0 && (
          <div className="empty">No files match “{filter}”.</div>
        )}

        {filteredObjects && filteredObjects.length > 0 &&
          CATEGORIES.map((category) => {
            const categoryFiles = categorizeObjects(filteredObjects)[category.key];
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
                  groupPairs(displayFiles).map((group) => {
                    const rows = group.files.map((o) => {
                      const quality = readQuality(o);
                      return (
                      <div
                        key={o.id}
                        className={`row ${sel === `object:${o.id}` ? "selected" : ""}${
                          group.pairLabel !== null ? " row-in-pair" : ""
                        }`}
                        onClick={() => select(`object:${o.id}`)}
                      >
                        {/* The read number leads the name inside a pair: it is
                            what distinguishes the two rows, and the eye needs
                            it before the filename, not after the metadata. */}
                        {group.pairLabel !== null && o.read_number != null && (
                          <span className="read-badge">R{o.read_number}</span>
                        )}
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
                            {/* Unpaired files can still carry a read number --
                                a mate that was deleted, or lives elsewhere. It
                                stays in the metadata line there, since there is
                                no sibling row to tell apart. */}
                            {group.pairLabel === null && o.read_number != null && (
                              <span className="read-badge">R{o.read_number}</span>
                            )}
                          </div>
                        </div>
                        {/* An <a> rather than a button so the browser streams
                            the file to disk itself; these run to gigabytes.
                            stopPropagation keeps the click from also selecting
                            the row behind it. Hidden until there are bytes to
                            serve, which is the same gate the Actions tab uses. */}
                        {o.blob_sha256 && (
                          <a
                            className="icon-btn row-action"
                            href={api.objectDownloadUrl(o.id)}
                            download={o.name}
                            title={`Download ${o.name}`}
                            onClick={(e) => e.stopPropagation()}
                          >
                            ↓
                          </a>
                        )}
                        <button
                          type="button"
                          className="icon-btn row-action row-action-danger"
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
                    });

                    // An unpaired file is just its row. A pair is wrapped so
                    // the label and the spine can span both halves, rather
                    // than being stitched together from two adjacent rows.
                    if (group.pairLabel === null) return rows;

                    return (
                      <div key={group.key} className="pair-group">
                        <div className="pair-label">
                          <span>Paired</span>
                          <span className="pair-stem">{group.pairLabel}</span>
                        </div>
                        {rows}
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
