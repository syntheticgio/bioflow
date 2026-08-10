import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { formatBytes, formatKindLabel } from "../lib/format";
import { readQuality } from "../lib/readQuality";
import { recordProjectVisit } from "../lib/recentProjects";
import { notify } from "../stores/messageStore";
import { useUploads } from "../hooks/useUploads";
import { QualityBadge } from "./QualityBadge";
import { BioIcon, FileIcon } from "../icons/BioIcon";
import { NewProjectModal } from "./NewProjectModal";
import { NcbiDownloadDialog } from "./NcbiDownloadDialog";
import { UniProtDownloadDialog } from "./UniProtDownloadDialog";
import {
  buildStageRail,
  groupPairs,
  orderWithPairs,
  type OrderedFile,
  type StageRailEntry,
} from "../lib/pairing";
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
            <span className="row-icon">
              <BioIcon name="project" size={24} />
            </span>
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
  /** Per-sample counts and DE results. Both are anonymous TSV, so without a
   * category of their own they land in "other" beside genuinely unrecognized
   * files -- which is where a counts matrix is least findable and most
   * looks like junk. */
  | "expression"
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
  if (obj.role === "counts" || obj.role === "de_results") return "expression";

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
    expression: [],
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
  { key: "expression", label: "Expression" },
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
  const [uniprotOpen, setUniprotOpen] = useState(false);
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

  // Recorded on view, not on any mutation -- a rename or tag edit elsewhere
  // must not count as the user having "opened" this project just now. Guard
  // on projectId (not just `project`) so a same-project refetch triggered by
  // an adjacent panel's mutation doesn't re-bump the timestamp -- only an
  // actual navigation to a different project should.
  const recordedFor = useRef<string | null>(null);
  useEffect(() => {
    if (project && recordedFor.current !== projectId) {
      recordProjectVisit(project.id, project.name);
      recordedFor.current = projectId;
    }
  }, [project, projectId]);

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
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => {
                        setAddMenuOpen(false);
                        setUniprotOpen(true);
                      }}
                    >
                      Download from UniProt…
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

      {uniprotOpen && (
        <UniProtDownloadDialog
          projectId={projectId}
          onClose={() => setUniprotOpen(false)}
        />
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

                {isExpanded && category.key === "reads" &&
                  buildStageRail(categoryFiles).map((entry) => (
                    <StageRailCard
                      key={entry.key}
                      entry={entry}
                      sel={sel}
                      onSelect={select}
                      onDelete={(id, name) => {
                        if (confirm(`Delete "${name}"?`)) delObject.mutate(id);
                      }}
                    />
                  ))}

                {isExpanded && category.key !== "reads" &&
                  groupPairs(displayFiles).map((group) => {
                    const rows = group.files.map((o) => (
                      <FileRow
                        key={o.id}
                        object={o}
                        selected={sel === `object:${o.id}`}
                        inPair={group.pairLabel !== null}
                        onSelect={() => select(`object:${o.id}`)}
                        onDelete={() => {
                          if (confirm(`Delete "${o.name}"?`)) delObject.mutate(o.id);
                        }}
                      />
                    ));

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

/** One read file's row: name, size, format, quality, download/delete actions.
 *  Shared by the plain (non-reads) list and the stage rail's per-mate rows. */
function FileRow({
  object,
  selected,
  inPair,
  readBadge,
  readsStage,
  onSelect,
  onDelete,
}: {
  object: DataObject;
  selected: boolean;
  inPair: boolean;
  /** Read number badge to show, if any. Defaults to the object's own
   *  read_number so the plain list keeps its existing behaviour. */
  readBadge?: number | null;
  /** Which version of a stage rail's FASTQ is on screen, so the row icon
   *  matches the Raw/Trimmed toggle rather than always drawing the generic
   *  reads mark. Omitted outside the stage rail. */
  readsStage?: "raw" | "trimmed";
  onSelect: () => void;
  onDelete: () => void;
}) {
  const quality = readQuality(object);
  const badge = readBadge !== undefined ? readBadge : object.read_number;

  return (
    <div
      className={`row ${selected ? "selected" : ""}${inPair ? " row-in-pair" : ""}`}
      onClick={onSelect}
    >
      {/* The grade rides the icon's corner; the word stays in the metadata
          line below, so the tier never depends on reading the mark alone. */}
      <span className="row-icon">
        <FileIcon
          formatKind={object.format.kind}
          role={object.role}
          size={30}
          readsStage={readsStage}
        />
        {quality && <QualityBadge quality={quality} />}
      </span>
      <div className="row-main">
        <div className="row-name">{object.name}</div>
        <div className="row-sub">
          {/* Read number leads the metadata line, whether paired or not, so
              the filename above keeps its full width instead of being
              squeezed to make room for the badge. */}
          {badge != null && <span className="read-badge">R{badge}</span>}
          <span>{formatBytes(object.size)}</span>
          {object.format.kind !== "unknown" && (
            <span>{formatKindLabel(object.format.kind)}</span>
          )}
          {object.status !== "ready" && <span>{object.status}</span>}
        </div>
      </div>
      {/* An <a> rather than a button so the browser streams the file to disk
          itself; these run to gigabytes. stopPropagation keeps the click
          from also selecting the row behind it. Hidden until there are bytes
          to serve, which is the same gate the Actions tab uses. */}
      {object.blob_sha256 && (
        <a
          className="icon-btn row-action"
          href={api.objectDownloadUrl(object.id)}
          download={object.name}
          title={`Download ${object.name}`}
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
          onDelete();
        }}
      >
        ×
      </button>
    </div>
  );
}

/**
 * One accession's card: header with a RAW/TRIMMED toggle, and one row per
 * mate (or the single read) showing whichever version is currently toggled.
 *
 * Defaults to TRIMMED when every mate has a trimmed version, since that is
 * the file someone actually wants to work with once it exists -- matching
 * the mockup, which shows TRIMMED selected. Falls back to RAW-only (no
 * toggle) when nothing has been trimmed yet, so an untouched accession looks
 * exactly like a plain row still does.
 */
function StageRailCard({
  entry,
  sel,
  onSelect,
  onDelete,
}: {
  entry: StageRailEntry;
  sel: string | null;
  onSelect: (value: string) => void;
  onDelete: (id: string, name: string) => void;
}) {
  const hasAnyTrimmed = entry.trimmed.some((t) => t !== null);
  const [stage, setStage] = useState<"raw" | "trimmed">(
    hasAnyTrimmed ? "trimmed" : "raw",
  );

  const displayed = entry.raw.map((raw, i) => {
    const trimmed = entry.trimmed[i];
    return stage === "trimmed" && trimmed ? trimmed : raw;
  });

  // When navigation (e.g. clicking a "Derived from" link) selects an
  // object in the other stage, sync the toggle so the file row is visible
  // in the list. The Raw/Trimmed buttons call switchStage (which also
  // re-selects); this handles external navigation where only the URL
  // changed -- browser back, bookmarks, and cross-stage links.
  useEffect(() => {
    if (sel === null || !sel.startsWith("object:")) return;
    const targetId = sel.slice("object:".length);

    const inRaw = entry.raw.some((o) => o.id === targetId);
    const inTrimmed = entry.trimmed.some((o) => o?.id === targetId);

    if (stage === "trimmed" && inRaw && !inTrimmed) {
      setStage("raw");
    } else if (stage === "raw" && inTrimmed && !inRaw) {
      setStage("trimmed");
    }
  }, [sel, entry.raw, entry.trimmed, stage]);

  // Switching stage swaps which objects are on screen, so a selected row's
  // id goes stale -- it would keep pointing at the old stage's object until
  // the user clicked a row again. Clicking Raw/Trimmed on this card always
  // brings its selection along: if one of this card's reads was already
  // selected, follow it to its counterpart at the same index in the new
  // stage; otherwise default to this card's first read (the only one, for
  // a single read).
  const switchStage = (next: "raw" | "trimmed") => {
    setStage(next);
    const selectedIndex = entry.raw.findIndex(
      (raw, i) => sel === `object:${raw.id}` || sel === `object:${entry.trimmed[i]?.id}`,
    );
    const index = selectedIndex === -1 ? 0 : selectedIndex;
    const trimmed = entry.trimmed[index];
    const target = next === "trimmed" && trimmed ? trimmed : entry.raw[index];
    if (target) onSelect(`object:${target.id}`);
  };

  return (
    <div className="pair-group stage-rail-card">
      <div className="pair-label stage-rail-header">
        <span>{entry.paired ? "Paired" : "Single"}</span>
        {entry.label && <span className="pair-stem">{entry.label}</span>}
      </div>

      {hasAnyTrimmed && (
        <div className="stage-toggle" role="group" aria-label="Read stage">
          <button
            type="button"
            className={stage === "raw" ? "active" : ""}
            onClick={() => switchStage("raw")}
          >
            Raw
          </button>
          <button
            type="button"
            className={stage === "trimmed" ? "active" : ""}
            onClick={() => switchStage("trimmed")}
          >
            Trimmed
          </button>
        </div>
      )}

      {displayed.map((o, i) => (
        <FileRow
          key={o.id}
          object={o}
          selected={sel === `object:${o.id}`}
          inPair={entry.paired}
          readBadge={entry.raw[i].read_number}
          readsStage={stage}
          onSelect={() => onSelect(`object:${o.id}`)}
          onDelete={() => onDelete(o.id, o.name)}
        />
      ))}
    </div>
  );
}
