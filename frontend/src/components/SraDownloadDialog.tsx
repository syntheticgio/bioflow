import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { formatBytes } from "../lib/format";
import { notify } from "../stores/messageStore";
import type { SraResolveResponse, SraRunInfo } from "../api/types";

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
 * Find sequencing runs at NCBI and download them into a project.
 *
 * Two steps rather than the four an earlier sketch had. The hierarchy view and
 * the run checklist collapsed into one screen: the hierarchy is derived from
 * the same runs the table lists, so presenting them separately meant two
 * screens showing one dataset and a "back" that discarded a selection. And
 * there is no progress screen -- downloads become jobs in a `PipelineRun`, and
 * the activity view already renders those better than a modal could.
 */
export function SraDownloadDialog({
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

  const resolve = useMutation({
    mutationFn: () =>
      api.sraResolve({
        accession: accession.trim(),
        platform_filter: platform || null,
        project_id: projectId,
      }),
    onSuccess: (data) => {
      setResolved(data);
      setPage(0);
      // Everything not already present, pre-selected. The common case is
      // "give me this run" or "give me this sample", and re-selecting by hand
      // would be busywork; a large study is the case where the user wants to
      // choose, and there the count in the button makes the scale obvious.
      setSelected(
        new Set(
          data.runs.filter((r) => !r.already_downloaded).map((r) => r.accession),
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

  const pageCount = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const visible = sorted.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

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

  const sortBy = (key: SortKey) =>
    setSort((s) => ({ key, desc: s.key === key ? !s.desc : false }));

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal sra-modal"
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: 900, width: "90vw" }}
      >
        <h2>Download from NCBI SRA</h2>

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
              placeholder="SRR11768093, PRJNA631678, SAMN14886310…"
              onChange={(e) => setAccession(e.target.value)}
            />
          </label>

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

          <button
            type="submit"
            className="btn primary"
            disabled={!accession.trim() || resolve.isPending}
          >
            {resolve.isPending ? "Looking up…" : "Look up"}
          </button>
        </form>

        <small className="sra-search-hint">
          A run, experiment, sample, study, BioProject, or BioSample.
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
                  {visible.map((run) => (
                    <RunRow
                      key={run.accession}
                      run={run}
                      checked={selected.has(run.accession)}
                      onToggle={() => toggle(run.accession)}
                    />
                  ))}
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
            {selected.size > 0 && (
              <>
                {selected.size} selected
                {selectedBytes > 0 && <> · {formatBytes(selectedBytes)}</>}
              </>
            )}
          </div>
          <button type="button" onClick={onClose}>
            Cancel
          </button>
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
        </div>
      </div>
    </div>
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
