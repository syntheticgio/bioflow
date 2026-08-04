import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import { useDownloadStore } from "./downloadStore";

/**
 * The last screen before something is queued.
 *
 * Two branches, because ncbiResolve returns an assembly or a run list and
 * never both. Runs are a checklist; an assembly is its component set. Both
 * preselect exactly what the desktop dialog preselects -- a phone that
 * quietly downloaded less than the desktop would is how a missing GTF turns
 * up weeks later as "why can't I quantify this".
 */
export function MobileConfirm() {
  const { accession = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();

  const projectId = useDownloadStore((s) => s.projectId);
  const sra = useDownloadStore((s) => s.sra);
  const assembly = useDownloadStore((s) => s.assembly);

  const [runs, setRuns] = useState<Set<string>>(new Set());
  const [components, setComponents] = useState<Set<string>>(new Set());
  const [runQC, setRunQC] = useState(true);

  // Preselect on arrival, matching the desktop defaults: every run not
  // already held, and every available component.
  useEffect(() => {
    if (sra) {
      setRuns(
        new Set(
          sra.runs.filter((r) => !r.already_downloaded).map((r) => r.accession),
        ),
      );
    }
    if (assembly) {
      setComponents(
        new Set(assembly.components.filter((c) => c.available).map((c) => c.key)),
      );
    }
  }, [sra, assembly]);

  const done = (message: string) => {
    qc.invalidateQueries({ queryKey: ["jobs"] });
    qc.invalidateQueries({ queryKey: ["runs"] });
    notify.success(message);
    navigate("/m/activity");
  };

  const downloadRuns = useMutation({
    mutationFn: () =>
      api.sraDownload({
        project_id: projectId!,
        run_accessions: [...runs],
        run_qc: runQC,
      }),
    onSuccess: (accepted) => {
      const n = accepted.download_job_ids.length;
      done(`Downloading ${n} ${n === 1 ? "run" : "runs"}`);
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const downloadAssembly = useMutation({
    mutationFn: () =>
      api.ncbiDownloadAssembly({
        project_id: projectId!,
        accession: assembly!.accession,
        components: [...components],
      }),
    onSuccess: () => done(`Downloading ${assembly!.accession}`),
    onError: (e: Error) => notify.error(e.message),
  });

  // A reload lands here with an empty store. Resolving again would be a
  // second NCBI call for a screen the user can simply re-enter, so this
  // sends them back rather than silently re-fetching.
  if (!sra && !assembly) {
    return (
      <>
        <div className="m-empty">
          Nothing loaded for {accession}. Start from the search screen.
        </div>
        <button className="m-button" onClick={() => navigate("/m/download")}>
          Back to search
        </button>
      </>
    );
  }

  const toggle = (set: Set<string>, key: string) => {
    const next = new Set(set);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    return next;
  };

  if (assembly) {
    return (
      <>
        <div className="m-section-head">
          <span>{assembly.accession}</span>
        </div>
        <div className="m-note">
          {[assembly.organism, assembly.strain, assembly.assembly_level]
            .filter(Boolean)
            .join(" · ")}
        </div>

        <div className="m-section-head">
          <span>Files to download</span>
        </div>
        {assembly.components.map((c) => (
          <button
            key={c.key}
            className="m-check-row"
            disabled={!c.available || c.key === "genome"}
            onClick={() => setComponents((s) => toggle(s, c.key))}
          >
            <span
              className={`m-check${components.has(c.key) ? " on" : ""}`}
            />
            <div>
              <div className="m-row-title">{c.label}</div>
              <div className="m-row-sub">
                {c.key === "genome"
                  ? "always included"
                  : c.available
                    ? c.size_bytes
                      ? `${(c.size_bytes / 1e6).toFixed(1)} MB`
                      : "available"
                    : (c.reason ?? "not available")}
              </div>
            </div>
          </button>
        ))}

        <button
          className="m-button"
          disabled={components.size === 0 || downloadAssembly.isPending}
          onClick={() => downloadAssembly.mutate()}
        >
          {downloadAssembly.isPending
            ? "Queueing…"
            : `Download ${components.size} ${components.size === 1 ? "file" : "files"}`}
        </button>
      </>
    );
  }

  return (
    <>
      <div className="m-section-head">
        <span>{sra!.accession}</span>
        <span>{sra!.total_run_count} runs</span>
      </div>
      {sra!.title && <div className="m-note">{sra!.title}</div>}

      {sra!.truncated && (
        <div className="m-note">
          Showing the first {sra!.runs.length} runs of this study. Use the
          desktop view to reach the rest.
        </div>
      )}

      {sra!.runs.map((r) => (
        <button
          key={r.accession}
          className="m-check-row"
          disabled={downloadRuns.isPending}
          onClick={() => setRuns((s) => toggle(s, r.accession))}
        >
          <span className={`m-check${runs.has(r.accession) ? " on" : ""}`} />
          <div>
            <div className="m-row-title">{r.accession}</div>
            <div className="m-row-sub">
              {[
                r.platform,
                r.bytes ? `${(r.bytes / 1e9).toFixed(1)} GB` : null,
                r.already_downloaded ? "already in library" : null,
              ]
                .filter(Boolean)
                .join(" · ")}
            </div>
          </div>
        </button>
      ))}

      <button
        className="m-check-row"
        onClick={() => setRunQC((v) => !v)}
        style={{ marginTop: 8 }}
      >
        <span className={`m-check${runQC ? " on" : ""}`} />
        <div>
          <div className="m-row-title">Run QC after downloading</div>
          <div className="m-row-sub">
            There is no way to turn this on later.
          </div>
        </div>
      </button>

      <button
        className="m-button"
        disabled={runs.size === 0 || downloadRuns.isPending}
        onClick={() => downloadRuns.mutate()}
      >
        {downloadRuns.isPending
          ? "Queueing…"
          : `Download ${runs.size} ${runs.size === 1 ? "run" : "runs"}`}
      </button>
    </>
  );
}
