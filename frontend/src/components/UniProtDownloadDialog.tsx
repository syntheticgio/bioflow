import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { ModalBackdrop } from "./ModalBackdrop";
import { notify } from "../stores/messageStore";
import type {
  UniProtProteome,
  UniProtProtein,
  UniProtResolveResponse,
} from "../api/types";

/**
 * Find protein sequences at UniProt and download them into a project.
 *
 * Its own dialog rather than a branch inside `NcbiDownloadDialog`. The
 * accession namespaces do not collide, so merging was possible -- but that
 * component already carries two result shapes in 762 lines, and its
 * resolver's question ("is this SRA or an assembly?") is coherent because it
 * is about one provider. This copies the style, not the component.
 *
 * One field, four input classes, decided server-side: a proteome id, one or
 * more accessions, a taxon or organism, or free text. The body switches
 * between a proteome card and a protein picker on what comes back.
 */
export function UniProtDownloadDialog({
  projectId,
  onClose,
}: {
  projectId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const [query, setQuery] = useState("");
  const [resolved, setResolved] = useState<UniProtResolveResponse | null>(null);
  const [reviewedOnly, setReviewedOnly] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [chosenProteome, setChosenProteome] = useState<UniProtProteome | null>(
    null,
  );
  const resolve = useMutation({
    mutationFn: () => api.uniprotResolve({ query: query.trim(), project_id: projectId }),
    onSuccess: (data) => {
      setResolved(data);
      setChosenProteome(data.proteome);
      setSelected(new Set());
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const download = useMutation({
    mutationFn: () =>
      api.uniprotDownload({
        project_id: projectId,
        proteome_id: chosenProteome?.id ?? null,
        accessions: chosenProteome ? [] : [...selected],
        reviewed_only: reviewedOnly,
        organism: chosenProteome?.name ?? null,
        protein_count: chosenProteome
          ? reviewedOnly
            ? chosenProteome.reviewed_count
            : chosenProteome.total_count
          : null,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["runs"] });
      notify.success("Downloading from UniProt");
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const toggle = (accession: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(accession)) next.delete(accession);
      else next.add(accession);
      return next;
    });

  const allSelected =
    resolved != null &&
    resolved.proteins.length > 0 &&
    resolved.proteins.every((p) => selected.has(p.accession));

  const toggleAll = () =>
    setSelected(
      allSelected
        ? new Set()
        : new Set((resolved?.proteins ?? []).map((p) => p.accession)),
    );

  const count = chosenProteome
    ? reviewedOnly
      ? chosenProteome.reviewed_count
      : chosenProteome.total_count
    : selected.size;

  const canDownload = chosenProteome != null || selected.size > 0;

  return (
    <ModalBackdrop onClick={onClose}>
      <div
        className="modal sra-modal"
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: 820, width: "90vw" }}
      >
        <h2>Download from UniProt</h2>

        <form
          className="sra-search"
          onSubmit={(e) => {
            e.preventDefault();
            if (query.trim()) resolve.mutate();
          }}
        >
          <label className="sra-search-accession">
            <span>Search</span>
            <input
              autoFocus
              value={query}
              placeholder="UP000002311, P0DTC2, 559292, spike glycoprotein…"
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
          <button
            type="submit"
            className="btn primary"
            disabled={!query.trim() || resolve.isPending}
          >
            {resolve.isPending ? "Looking up…" : "Look up"}
          </button>
        </form>

        <small className="sra-search-hint">
          A proteome (UP…), one or more accessions, a taxon id, an organism
          name, or a protein name.
        </small>

        {resolve.isPending && (
          <div className="empty">
            <span className="spinner" /> Asking UniProt about {query.trim()}…
          </div>
        )}

        {resolved?.message && (
          <div className="warn-box" style={{ fontSize: 12 }}>
            {resolved.message}
          </div>
        )}

        {chosenProteome && (
          <ProteomeCard
            proteome={chosenProteome}
            reviewedOnly={reviewedOnly}
            onReviewedChange={setReviewedOnly}
          />
        )}

        {resolved && resolved.proteins.length > 0 && (
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
                  <th>Accession</th>
                  <th>Protein</th>
                  <th>Organism</th>
                  <th>Length</th>
                </tr>
              </thead>
              <tbody>
                {resolved.proteins.map((p) => (
                  <ProteinRow
                    key={p.accession}
                    protein={p}
                    checked={selected.has(p.accession)}
                    onToggle={() => toggle(p.accession)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="modal-actions">
          <div
            style={{ marginRight: "auto", fontSize: 12, color: "var(--text-faint)" }}
          >
            {count != null && count > 0 && (
              <>{count.toLocaleString()} proteins</>
            )}
          </div>
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={!canDownload || download.isPending}
            onClick={() => download.mutate()}
          >
            {download.isPending ? "Queueing…" : "Download"}
          </button>
        </div>
      </div>
    </ModalBackdrop>
  );
}

/** A resolved proteome: what it is, and how much of it to fetch. */
function ProteomeCard({
  proteome,
  reviewedOnly,
  onReviewedChange,
}: {
  proteome: UniProtProteome;
  reviewedOnly: boolean;
  onReviewedChange: (v: boolean) => void;
}) {
  // Only worth offering the choice when it changes the answer. For a fully
  // curated organism both counts are identical and the checkbox is noise.
  const differs =
    proteome.reviewed_count != null &&
    proteome.total_count != null &&
    proteome.reviewed_count !== proteome.total_count;

  return (
    <>
      <div className="sra-summary">
        <div>
          <strong className="mono">{proteome.id}</strong>
          {" · "}
          <span style={{ fontStyle: "italic" }}>{proteome.name}</span>
          {proteome.is_reference && (
            <span className="sra-have-tag" title="UniProt's reference proteome">
              reference
            </span>
          )}
        </div>
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          {[
            proteome.strain,
            proteome.protein_count != null &&
              `${proteome.protein_count.toLocaleString()} proteins`,
            proteome.busco_score != null && `BUSCO ${proteome.busco_score}%`,
          ]
            .filter(Boolean)
            .join(" · ")}
        </div>
        {proteome.genome_assembly && (
          <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
            Genome:{" "}
            <a
              href={`https://www.ncbi.nlm.nih.gov/datasets/genome/${proteome.genome_assembly}/`}
              target="_blank"
              rel="noreferrer"
              className="mono"
            >
              {proteome.genome_assembly}
            </a>{" "}
            — downloadable from the NCBI dialog.
          </div>
        )}
      </div>

      {differs && (
        <label className="trim-check" style={{ marginTop: 10 }}>
          <input
            type="checkbox"
            checked={reviewedOnly}
            onChange={(e) => onReviewedChange(e.target.checked)}
          />
          <span>
            Reviewed entries only (Swiss-Prot)
            <small style={{ display: "block", color: "var(--text-faint)" }}>
              {proteome.reviewed_count?.toLocaleString()} reviewed ·{" "}
              {proteome.total_count?.toLocaleString()} including unreviewed
              (TrEMBL)
            </small>
          </span>
        </label>
      )}
    </>
  );
}

function ProteinRow({
  protein,
  checked,
  onToggle,
}: {
  protein: UniProtProtein;
  checked: boolean;
  onToggle: () => void;
}) {
  return (
    <tr>
      <td>
        <input type="checkbox" checked={checked} onChange={onToggle} />
      </td>
      <td className="mono">
        {protein.accession}
        {protein.reviewed && (
          <span className="sra-have-tag" title="Reviewed (Swiss-Prot)">
            reviewed
          </span>
        )}
      </td>
      <td className="sra-dim">{protein.name ?? "—"}</td>
      <td className="sra-dim" style={{ fontStyle: "italic" }}>
        {protein.organism ?? "—"}
      </td>
      <td className="sra-num">
        {protein.length != null ? `${protein.length.toLocaleString()} aa` : "—"}
      </td>
    </tr>
  );
}
