import { Fragment, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { formatBytes } from "../lib/format";
import { ModalBackdrop } from "./ModalBackdrop";
import { notify } from "../stores/messageStore";
import type {
  AssemblyResolveResponse,
  SraResolveResponse,
  SraRunInfo,
} from "../api/types";

const PAGE_SIZE = 20;

/** Mirrors the server's MAX_RUNS_PER_REQUEST, so the limit is visible here. */
const MAX_SELECTION = 100;

const PLATFORM_FILTERS = [
  { value: "", label: "Any platform" },
  { value: "ILLUMINA", label: "Illumina" },
  { value: "PACBIO_SMRT", label: "PacBio" },
  { value: "OXFORD_NANOPORE", label: "Nanopore" },
];

type SortKey = "accession" | "platform" | "library_strategy" | "spots" | "bytes";

/**
 * Find sequencing runs -- or a GenBank/RefSeq assembly -- at NCBI and
 * download into a project. One accession field, one lookup: the server
 * decides whether the accession names an SRA run/study or an assembly, and
 * this dialog switches its body between the run-picker table and an
 * assembly summary card accordingly.
 *
 * Two steps rather than the four an earlier sketch had. The hierarchy view and
 * the run checklist collapsed into one screen: the hierarchy is derived from
 * the same runs the table lists, so presenting them separately meant two
 * screens showing one dataset and a "back" that discarded a selection. And
 * there is no progress screen -- downloads become jobs in a `PipelineRun`, and
 * the activity view already renders those better than a modal could.
 */
export function NcbiDownloadDialog({
  projectId,
  onClose,
}: {
  projectId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const [accession, setAccession] = useState("");
  const [platform, setPlatform] = useState("");
  const [resolved, setResolved] = useState<SraResolveResponse | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [runQC, setRunQC] = useState(true);
  const [page, setPage] = useState(0);
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({
    key: "accession",
    desc: false,
  });
  const [assembly, setAssembly] = useState<AssemblyResolveResponse | null>(null);
  const [components, setComponents] = useState<Set<string>>(new Set(["genome"]));

  const resolve = useMutation({
    mutationFn: () =>
      api.ncbiResolve({
        accession: accession.trim(),
        platform_filter: platform || null,
        project_id: projectId,
      }),
    onSuccess: (data) => {
      setPage(0);
      // A fresh resolution starts with every group expanded, not whatever
      // was collapsed from the previous lookup.
      setCollapsed(new Set());
      // Only one branch is ever populated, and `kind` says which. Clearing
      // the other matters: leaving a stale run table beside a new assembly
      // card would show two answers for one lookup.
      if (data.assembly) {
        setAssembly(data.assembly);
        setResolved(null);
        setSelected(new Set());
        // Genome plus everything available: the common case is "give me this
        // genome and its annotation", and unchecking is cheaper than hunting
        // for the boxes to check.
        setComponents(
          new Set(
            data.assembly.components.filter((c) => c.available).map((c) => c.key),
          ),
        );
        return;
      }
      setAssembly(null);
      setResolved(data.sra);
      // Everything not already present, pre-selected. The common case is
      // "give me this run" or "give me this sample", and re-selecting by hand
      // would be busywork; a large study is the case where the user wants to
      // choose, and there the count in the button makes the scale obvious.
      setSelected(
        new Set(
          (data.sra?.runs ?? [])
            .filter((r) => !r.already_downloaded)
            .map((r) => r.accession),
        ),
      );
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const download = useMutation({
    mutationFn: () =>
      api.sraDownload({
        project_id: projectId,
        run_accessions: [...selected],
        run_qc: runQC,
      }),
    onSuccess: (accepted) => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["runs"] });
      const n = accepted.download_job_ids.length;
      notify.success(
        `Downloading ${n} ${n === 1 ? "run" : "runs"} from SRA`,
      );
      if (accepted.skipped.length) {
        notify.info(
          `${accepted.skipped.length} already downloading: ${accepted.skipped
            .slice(0, 3)
            .join(", ")}`,
        );
      }
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const downloadAssembly = useMutation({
    mutationFn: () =>
      api.ncbiDownloadAssembly({
        project_id: projectId,
        accession: assembly!.accession,
        components: [...components],
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["runs"] });
      notify.success(`Downloading ${assembly!.accession} from NCBI`);
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const runs = resolved?.runs ?? [];

  const sorted = useMemo(() => {
    const copy = [...runs];
    copy.sort((a, b) => {
      const av = a[sort.key];
      const bv = b[sort.key];
      // Nulls last regardless of direction: a run with no recorded size is
      // not "the smallest", it is unknown, and sorting it to the top of an
      // ascending size sort would misrepresent it.
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      const cmp = typeof av === "number" && typeof bv === "number"
        ? av - bv
        : String(av).localeCompare(String(bv));
      return sort.desc ? -cmp : cmp;
    });
    return copy;
  }, [runs, sort]);

  // Grouping earns its complexity only for a multi-experiment container. A
  // single run, or a sample with one experiment, would get a collapse control
  // around every row for no benefit.
  const groups = useMemo(() => groupByExperiment(sorted), [sorted]);
  const grouped =
    (resolved?.kind === "bioproject" || resolved?.kind === "study") &&
    groups.length > 1;

  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  // Whole experiments per page when grouped: a group split across a page
  // boundary is the confusing case, and the experiment is the unit the user
  // is now reasoning in.
  const GROUPS_PER_PAGE = 5;
  const pageCount = grouped
    ? Math.max(1, Math.ceil(groups.length / GROUPS_PER_PAGE))
    : Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const visibleGroups = grouped
    ? groups.slice(page * GROUPS_PER_PAGE, (page + 1) * GROUPS_PER_PAGE)
    : [];
  const visible = grouped
    ? []
    : sorted.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  const selectable = runs.filter((r) => !r.already_downloaded);
  const allSelected =
    selectable.length > 0 && selectable.every((r) => selected.has(r.accession));

  const selectedBytes = runs
    .filter((r) => selected.has(r.accession))
    .reduce((sum, r) => sum + (r.bytes ?? 0), 0);

  const overLimit = selected.size > MAX_SELECTION;

  const toggle = (acc: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(acc)) next.delete(acc);
      else next.add(acc);
      return next;
    });

  const toggleAll = () =>
    setSelected(
      allSelected ? new Set() : new Set(selectable.map((r) => r.accession)),
    );

  const toggleGroup = (group: RunGroup) => {
    const groupSelectable = group.runs.filter((r) => !r.already_downloaded);
    const allOn = groupSelectable.every((r) => selected.has(r.accession));
    setSelected((prev) => {
      const next = new Set(prev);
      for (const run of groupSelectable) {
        if (allOn) next.delete(run.accession);
        else next.add(run.accession);
      }
      return next;
    });
  };

  const groupState = (group: RunGroup): "all" | "none" | "some" => {
    const groupSelectable = group.runs.filter((r) => !r.already_downloaded);
    if (groupSelectable.length === 0) return "none";
    const on = groupSelectable.filter((r) => selected.has(r.accession)).length;
    if (on === 0) return "none";
    return on === groupSelectable.length ? "all" : "some";
  };

  const sortBy = (key: SortKey) =>
    setSort((s) => ({ key, desc: s.key === key ? !s.desc : false }));

  return (
    <ModalBackdrop onClick={onClose}>
      <div
        className="modal sra-modal"
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: 900, width: "90vw" }}
      >
        <h2>Download from NCBI</h2>

        <form
          className="sra-search"
          onSubmit={(e) => {
            e.preventDefault();
            if (accession.trim()) resolve.mutate();
          }}
        >
          <label className="sra-search-accession">
            <span>Accession</span>
            <input
              autoFocus
              value={accession}
              placeholder="SRR11768093, PRJNA1495534, GCF_000002445.2…"
              onChange={(e) => setAccession(e.target.value)}
            />
          </label>

          {!assembly && (
            <label className="sra-search-platform">
              <span>Platform</span>
              <select value={platform} onChange={(e) => setPlatform(e.target.value)}>
                {PLATFORM_FILTERS.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
            </label>
          )}

          <button
            type="submit"
            className="btn primary"
            disabled={!accession.trim() || resolve.isPending}
          >
            {resolve.isPending ? "Looking up…" : "Look up"}
          </button>
        </form>

        <small className="sra-search-hint">
          A run, experiment, sample, study, BioProject, BioSample, or a
          GenBank/RefSeq assembly (GCA/GCF).
        </small>

        {resolve.isPending && (
          <div className="empty">
            <span className="spinner" /> Asking NCBI about {accession.trim()}…
          </div>
        )}

        {resolved?.error && (
          <div className="warn-box" style={{ fontSize: 12 }}>
            {resolved.error}
          </div>
        )}

        {assembly?.error && (
          <div className="warn-box" style={{ fontSize: 12 }}>
            {assembly.error}
          </div>
        )}

        {assembly && !assembly.error && (
          <AssemblyCard
            assembly={assembly}
            selected={components}
            onToggle={(key) =>
              setComponents((prev) => {
                const next = new Set(prev);
                // Genome is mandatory: everything else describes coordinates
                // or products of it and is close to uninterpretable alone.
                if (key === "genome") return next;
                if (next.has(key)) next.delete(key);
                else next.add(key);
                return next;
              })
            }
          />
        )}

        {resolved && runs.length > 0 && (
          <>
            <div className="sra-summary">
              <div>
                <strong>{resolved.total_run_count}</strong>{" "}
                {resolved.total_run_count === 1 ? "run" : "runs"}
                {resolved.organism && (
                  <>
                    {" · "}
                    <span style={{ fontStyle: "italic" }}>{resolved.organism}</span>
                  </>
                )}
                {resolved.total_bytes_estimate != null && (
                  <> · {formatBytes(resolved.total_bytes_estimate)} total</>
                )}
              </div>
              {resolved.title && (
                <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
                  {resolved.title}
                </div>
              )}
            </div>

            {resolved.truncated && (
              <div className="warn-box" style={{ fontSize: 12 }}>
                This study holds more runs than can be listed at once. The first{" "}
                {runs.length} are shown — narrow by platform, or resolve a
                sample accession, to reach the rest.
              </div>
            )}

            <div className="sra-table-wrap">
              <table className="sra-table">
                <thead>
                  <tr>
                    <th style={{ width: 28 }}>
                      <input
                        type="checkbox"
                        checked={allSelected}
                        onChange={toggleAll}
                        title={allSelected ? "Deselect all" : "Select all"}
                      />
                    </th>
                    <Th label="Run" onClick={() => sortBy("accession")} sort={sort} k="accession" />
                    <Th label="Platform" onClick={() => sortBy("platform")} sort={sort} k="platform" />
                    <th>Instrument</th>
                    <Th label="Strategy" onClick={() => sortBy("library_strategy")} sort={sort} k="library_strategy" />
                    <th>Layout</th>
                    <Th label="Spots" onClick={() => sortBy("spots")} sort={sort} k="spots" />
                    <Th label="Size" onClick={() => sortBy("bytes")} sort={sort} k="bytes" />
                  </tr>
                </thead>
                <tbody>
                  {!grouped &&
                    visible.map((run) => (
                      <RunRow
                        key={run.accession}
                        run={run}
                        checked={selected.has(run.accession)}
                        onToggle={() => toggle(run.accession)}
                      />
                    ))}

                  {grouped &&
                    visibleGroups.map((group) => {
                      const state = groupState(group);
                      const isCollapsed = collapsed.has(group.experiment);
                      return (
                        <Fragment key={group.experiment}>
                          <tr className="sra-group-row">
                            <td>
                              <input
                                type="checkbox"
                                checked={state === "all"}
                                ref={(el) => {
                                  // Tri-state has no HTML attribute; it is a
                                  // DOM property, so it must be set here.
                                  if (el) el.indeterminate = state === "some";
                                }}
                                onChange={() => toggleGroup(group)}
                                title="Select every run in this experiment"
                              />
                            </td>
                            <td colSpan={7}>
                              <button
                                type="button"
                                className="sra-group-toggle"
                                onClick={() =>
                                  setCollapsed((prev) => {
                                    const next = new Set(prev);
                                    if (next.has(group.experiment))
                                      next.delete(group.experiment);
                                    else next.add(group.experiment);
                                    return next;
                                  })
                                }
                              >
                                {isCollapsed ? "▸" : "▾"}
                                <span className="mono">{group.experiment}</span>
                                <span className="sra-dim">
                                  {group.runs.length}{" "}
                                  {group.runs.length === 1 ? "run" : "runs"}
                                  {group.bytes > 0 && ` · ${formatBytes(group.bytes)}`}
                                </span>
                                {group.title && (
                                  <span className="sra-dim">{group.title}</span>
                                )}
                              </button>
                            </td>
                          </tr>
                          {!isCollapsed &&
                            group.runs.map((run) => (
                              <RunRow
                                key={run.accession}
                                run={run}
                                checked={selected.has(run.accession)}
                                onToggle={() => toggle(run.accession)}
                              />
                            ))}
                        </Fragment>
                      );
                    })}
                </tbody>
              </table>
            </div>

            {pageCount > 1 && (
              <div className="sra-pager">
                <button
                  type="button"
                  disabled={page === 0}
                  onClick={() => setPage((p) => p - 1)}
                >
                  ‹ Previous
                </button>
                <span>
                  Page {page + 1} of {pageCount}
                </span>
                <button
                  type="button"
                  disabled={page >= pageCount - 1}
                  onClick={() => setPage((p) => p + 1)}
                >
                  Next ›
                </button>
              </div>
            )}

            <label className="trim-check" style={{ marginTop: 10 }}>
              <input
                type="checkbox"
                checked={runQC}
                onChange={(e) => setRunQC(e.target.checked)}
              />
              <span>
                Run QC on each file once it lands
                <small style={{ display: "block", color: "var(--text-faint)" }}>
                  fastp and FastQC for short reads, NanoPlot for PacBio and
                  Nanopore.
                </small>
              </span>
            </label>

            {overLimit && (
              <div className="warn-box" style={{ fontSize: 12 }}>
                {selected.size} runs selected — the limit is {MAX_SELECTION} per
                request. Deselect some, or download in batches.
              </div>
            )}
          </>
        )}

        <div className="modal-actions">
          <div style={{ marginRight: "auto", fontSize: 12, color: "var(--text-faint)" }}>
            {!assembly && selected.size > 0 && (
              <>
                {selected.size} selected
                {selectedBytes > 0 && <> · {formatBytes(selectedBytes)}</>}
              </>
            )}
          </div>
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          {assembly && !assembly.error ? (
            <button
              type="button"
              className="btn primary"
              disabled={downloadAssembly.isPending}
              onClick={() => downloadAssembly.mutate()}
            >
              {downloadAssembly.isPending
                ? "Queueing…"
                : `Download ${components.size} ${
                    components.size === 1 ? "file" : "files"
                  }`}
            </button>
          ) : (
            <button
              type="button"
              className="btn primary"
              disabled={selected.size === 0 || overLimit || download.isPending}
              onClick={() => download.mutate()}
            >
              {download.isPending
                ? "Queueing…"
                : `Download ${selected.size || ""}`.trim()}
            </button>
          )}
        </div>
      </div>
    </ModalBackdrop>
  );
}

/** A resolved assembly: what it is, and which parts to fetch. */
function AssemblyCard({
  assembly,
  selected,
  onToggle,
}: {
  assembly: AssemblyResolveResponse;
  selected: Set<string>;
  onToggle: (key: string) => void;
}) {
  const totalBytes = assembly.components
    .filter((c) => selected.has(c.key))
    .reduce((sum, c) => sum + (c.size_bytes ?? 0), 0);

  return (
    <>
      <div className="sra-summary">
        <div>
          <strong className="mono">{assembly.accession}</strong>
          {assembly.organism && (
            <>
              {" · "}
              <span style={{ fontStyle: "italic" }}>{assembly.organism}</span>
            </>
          )}
          {assembly.strain && <> · {assembly.strain}</>}
          {assembly.already_downloaded && (
            <span className="sra-have-tag" title="Already in this project">
              have
            </span>
          )}
        </div>
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          {[
            assembly.assembly_name,
            assembly.assembly_level,
            assembly.submitter,
            assembly.release_date,
          ]
            .filter(Boolean)
            .join(" · ")}
        </div>
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          {[
            assembly.total_length != null &&
              `${formatBytes(assembly.total_length)} of sequence`,
            assembly.scaffold_count != null &&
              `${assembly.scaffold_count.toLocaleString()} scaffolds`,
            assembly.scaffold_n50 != null &&
              `N50 ${formatBytes(assembly.scaffold_n50)}`,
            assembly.gc_percent != null && `${assembly.gc_percent}% GC`,
          ]
            .filter(Boolean)
            .join(" · ")}
        </div>
      </div>

      <div className="assembly-components">
        {assembly.components.map((c) => (
          <label
            key={c.key}
            className={`assembly-component${c.available ? "" : " disabled"}`}
          >
            <input
              type="checkbox"
              checked={selected.has(c.key)}
              disabled={!c.available || c.key === "genome"}
              onChange={() => onToggle(c.key)}
            />
            <span>
              {c.label}
              {c.key === "genome" && (
                <small className="assembly-component-note">always included</small>
              )}
              {c.size_bytes != null && c.available && (
                <small className="assembly-component-note">
                  {formatBytes(c.size_bytes)}
                </small>
              )}
              {!c.available && c.reason && (
                <small className="assembly-component-note">{c.reason}</small>
              )}
            </span>
          </label>
        ))}
      </div>

      {totalBytes > 0 && (
        <small className="sra-search-hint">
          About {formatBytes(totalBytes)} to download.
        </small>
      )}
    </>
  );
}

function RunRow({
  run,
  checked,
  onToggle,
}: {
  run: SraRunInfo;
  checked: boolean;
  onToggle: () => void;
}) {
  const have = run.already_downloaded;
  return (
    <tr className={have ? "sra-row-have" : undefined}>
      <td>
        <input
          type="checkbox"
          checked={checked}
          onChange={onToggle}
          // Not disabled: re-downloading is legitimate (a corrupted file, a
          // deleted object), so this is a default rather than a prohibition.
          title={have ? "Already in this project" : undefined}
        />
      </td>
      <td className="mono">
        {run.accession}
        {have && (
          <span className="sra-have-tag" title="Already in this project">
            have
          </span>
        )}
      </td>
      <td>
        <PlatformBadge platform={run.platform} />
      </td>
      <td className="sra-dim">{run.instrument ?? "—"}</td>
      <td className="sra-dim">{run.library_strategy ?? "—"}</td>
      <td className="sra-dim">
        {run.library_layout === "PAIRED"
          ? "Paired"
          : run.library_layout === "SINGLE"
            ? "Single"
            : "—"}
      </td>
      <td className="sra-num">{run.spots?.toLocaleString() ?? "—"}</td>
      <td className="sra-num">{run.bytes != null ? formatBytes(run.bytes) : "—"}</td>
    </tr>
  );
}

/** Colour carries the platform, because it drives which QC tool will run. */
function PlatformBadge({ platform }: { platform: string | null }) {
  if (!platform) return <span className="sra-dim">—</span>;
  const key = platform.toUpperCase();
  const label =
    key === "OXFORD_NANOPORE"
      ? "Nanopore"
      : key === "PACBIO_SMRT"
        ? "PacBio"
        : key === "ILLUMINA"
          ? "Illumina"
          : platform;
  return <span className={`sra-badge sra-${key.toLowerCase()}`}>{label}</span>;
}

function Th({
  label,
  onClick,
  sort,
  k,
}: {
  label: string;
  onClick: () => void;
  sort: { key: SortKey; desc: boolean };
  k: SortKey;
}) {
  const active = sort.key === k;
  return (
    <th onClick={onClick} className="sra-sortable" title={`Sort by ${label}`}>
      {label}
      <span className="sra-sort-caret">{active ? (sort.desc ? "▾" : "▴") : ""}</span>
    </th>
  );
}

/** Runs grouped by the experiment they belong to. */
type RunGroup = {
  experiment: string;
  title: string | null;
  runs: SraRunInfo[];
  bytes: number;
};

/**
 * Group runs by experiment, preserving the incoming sort within each group.
 *
 * Derived here rather than from the resolver's `hierarchy`, which groups by
 * *sample*: every run already names its experiment, so this needs no extra
 * request and no cache invalidation.
 */
function groupByExperiment(runs: SraRunInfo[]): RunGroup[] {
  const groups = new Map<string, RunGroup>();
  for (const run of runs) {
    // A run with no recorded experiment still has to appear somewhere;
    // its own accession is the least surprising bucket.
    const key = run.experiment ?? run.accession;
    let group = groups.get(key);
    if (!group) {
      group = { experiment: key, title: run.title, runs: [], bytes: 0 };
      groups.set(key, group);
    }
    group.runs.push(run);
    group.bytes += run.bytes ?? 0;
  }
  return [...groups.values()].sort((a, b) =>
    a.experiment.localeCompare(b.experiment),
  );
}
