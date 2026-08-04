import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import { useDownloadStore } from "./downloadStore";
import type {
  OrganismAssemblySummary,
  OrganismSearchResponse,
  OrganismSuggestion,
  SraRunInfo,
} from "../api/types";

/** Matches the desktop dialog's page size. */
const PAGE_SIZE = 20;

/**
 * Queue an NCBI download from a phone.
 *
 * The project picker is first because both download endpoints require a
 * project_id, and unlike the desktop dialog -- which opens from inside a
 * project -- there is no explorer here to have supplied one.
 *
 * Organism search is the reason this screen is worth having. Away from your
 * desk is exactly where you do not have an accession in front of you.
 */
export function MobileDownload() {
  const navigate = useNavigate();
  const projectId = useDownloadStore((s) => s.projectId);
  const setProject = useDownloadStore((s) => s.setProject);
  const setResolved = useDownloadStore((s) => s.setResolved);

  const [query, setQuery] = useState("");
  const [organism, setOrganism] = useState<OrganismSuggestion | null>(null);
  const [results, setResults] = useState<OrganismSearchResponse | null>(null);
  const [tab, setTab] = useState<"assemblies" | "sra">("assemblies");

  const { data: projects = [] } = useQuery({
    queryKey: ["projects", "mobile"],
    queryFn: () => api.listProjects(),
  });

  const looksLikeAccession = /^(SRR|SRX|SRS|SRP|PRJ|GCF_|GCA_|ERR|DRR)/i.test(
    query.trim(),
  );

  const { data: suggestions } = useQuery({
    queryKey: ["organismSuggest", query],
    queryFn: () => api.ncbiOrganismSuggest(query),
    // Two characters is not a search, it is every organism on earth. And an
    // accession-shaped query should never reach NCBI's suggest endpoint at
    // all -- not just have its (wasted) result hidden from render.
    enabled: query.trim().length >= 3 && !organism && !looksLikeAccession,
  });

  const search = useMutation({
    mutationFn: (vars: {
      org: OrganismSuggestion;
      section: "both" | "assemblies" | "sra";
      assemblyPageToken?: string | null;
      sraOffset?: number;
    }) =>
      api.ncbiOrganismSearch({
        tax_id: vars.org.tax_id,
        sci_name: vars.org.sci_name,
        project_id: projectId,
        assembly_page_token: vars.assemblyPageToken ?? null,
        sra_offset: vars.sraOffset ?? 0,
        page_size: PAGE_SIZE,
        section: vars.section,
      }),
    onSuccess: (data, vars) => {
      // Paging one list must not discard the other: the API returns only the
      // requested section, so merge rather than replace.
      setResults((prev) =>
        !prev || vars.section === "both"
          ? data
          : vars.section === "assemblies"
            ? {
                ...prev,
                assemblies: [...prev.assemblies, ...data.assemblies],
                assemblies_next_page_token: data.assemblies_next_page_token,
              }
            : {
                ...prev,
                sra_runs: [...prev.sra_runs, ...data.sra_runs],
                sra_next_offset: data.sra_next_offset,
              },
      );
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const resolve = useMutation({
    mutationFn: (accession: string) =>
      api.ncbiResolve({ accession, project_id: projectId }),
    onSuccess: (data, accession) => {
      if (!data.sra && !data.assembly) {
        notify.error(`Nothing found for ${accession}`);
        return;
      }
      setResolved({ sra: data.sra, assembly: data.assembly });
      navigate(`/m/download/${encodeURIComponent(accession)}`);
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const pickOrganism = (o: OrganismSuggestion) => {
    setOrganism(o);
    setQuery(o.sci_name);
    setResults(null);
    setTab("assemblies");
    search.mutate({ org: o, section: "both" });
  };

  const restart = () => {
    setOrganism(null);
    setResults(null);
    setQuery("");
  };

  return (
    <>
      <label className="m-label" style={{ padding: "16px 16px 6px" }}>
        Into project
      </label>
      <div className="m-field">
        <select
          className="m-select"
          value={projectId ?? ""}
          onChange={(e) => setProject(e.target.value)}
        >
          <option value="" disabled>
            Choose a project…
          </option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </div>

      <label className="m-label" style={{ padding: "8px 16px 6px" }}>
        Organism or accession
      </label>
      <div className="m-field">
        <input
          className="m-input"
          value={query}
          placeholder="e.g. Escherichia coli, or SRR2584863"
          onChange={(e) => {
            setQuery(e.target.value);
            if (organism) restart();
          }}
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
        />
      </div>

      {looksLikeAccession && (
        <button
          className="m-button"
          disabled={!projectId || resolve.isPending}
          onClick={() => resolve.mutate(query.trim())}
        >
          {resolve.isPending ? "Looking up…" : `Look up ${query.trim()}`}
        </button>
      )}

      {!organism &&
        !looksLikeAccession &&
        suggestions?.suggestions?.map((o) => (
          <button
            key={o.tax_id}
            className="m-suggestion"
            onClick={() => pickOrganism(o)}
          >
            <div className="m-row-title">{o.sci_name}</div>
            <div className="m-row-sub">
              taxon {o.tax_id}
              {o.common_name ? ` · ${o.common_name}` : ""}
            </div>
          </button>
        ))}

      {search.isPending && !results && (
        <div className="m-empty">Searching…</div>
      )}

      {results && (
        <>
          <div className="m-segs">
            <button
              className={`m-seg${tab === "assemblies" ? " active" : ""}`}
              onClick={() => setTab("assemblies")}
            >
              Assemblies {results.assemblies.length}
            </button>
            <button
              className={`m-seg${tab === "sra" ? " active" : ""}`}
              onClick={() => setTab("sra")}
            >
              Runs {results.sra_total_count}
            </button>
          </div>

          {tab === "assemblies" ? (
            <>
              {results.assemblies.length === 0 && (
                <div className="m-empty">No assemblies for this organism.</div>
              )}
              {results.assemblies.map((a) => (
                <AssemblyRow
                  key={a.accession ?? Math.random()}
                  a={a}
                  disabled={!projectId || resolve.isPending}
                  onPick={() => a.accession && resolve.mutate(a.accession)}
                />
              ))}
              {results.assemblies_next_page_token && organism && (
                <button
                  className="m-more"
                  disabled={search.isPending}
                  onClick={() =>
                    search.mutate({
                      org: organism,
                      section: "assemblies",
                      assemblyPageToken: results.assemblies_next_page_token,
                    })
                  }
                >
                  {search.isPending ? "Loading…" : "Load more"}
                </button>
              )}
            </>
          ) : (
            <>
              {results.sra_runs.length === 0 && (
                <div className="m-empty">No sequencing runs found.</div>
              )}
              {results.sra_runs.map((r) => (
                <RunRow
                  key={r.accession}
                  r={r}
                  disabled={!projectId || resolve.isPending}
                  onPick={() => resolve.mutate(r.accession)}
                />
              ))}
              {results.sra_next_offset != null && organism && (
                <button
                  className="m-more"
                  disabled={search.isPending}
                  onClick={() =>
                    search.mutate({
                      org: organism,
                      section: "sra",
                      sraOffset: results.sra_next_offset ?? 0,
                    })
                  }
                >
                  {search.isPending ? "Loading…" : "Load more"}
                </button>
              )}
            </>
          )}
        </>
      )}

      {!projectId && (
        <div className="m-note">Choose a project before downloading.</div>
      )}
    </>
  );
}

function AssemblyRow({
  a,
  disabled,
  onPick,
}: {
  a: OrganismAssemblySummary;
  disabled: boolean;
  onPick: () => void;
}) {
  return (
    <button className="m-check-row" disabled={disabled} onClick={onPick}>
      <div>
        <div className="m-row-title">{a.accession ?? "unknown"}</div>
        <div className="m-row-sub">
          {[
            a.strain ?? a.assembly_name,
            a.assembly_level,
            a.refseq_category,
            a.already_downloaded ? "already in library" : null,
          ]
            .filter(Boolean)
            .join(" · ")}
        </div>
      </div>
    </button>
  );
}

function RunRow({
  r,
  disabled,
  onPick,
}: {
  r: SraRunInfo;
  disabled: boolean;
  onPick: () => void;
}) {
  return (
    <button className="m-check-row" disabled={disabled} onClick={onPick}>
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
  );
}
