import { useState, useMemo, useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { DATABASES } from "../data/databases";
import { api } from "../api/client";
import { LOCAL_DATABASE_CATEGORY_LABELS } from "../api/types";
import { AddLocalDatabaseModal } from "./AddLocalDatabaseModal";

/**
 * Reference index of external genetics/bioinformatics databases — not
 * things BioFlow integrates with (that's Data Sources), just a catalog for
 * the user to browse and follow links out from.
 *
 * The data array lives in data/databases.json (extracted from the standalone
 * page at ~/Programming/hermes/bioinformatics-databases.html) so there's a
 * single source of truth. Styles are scoped under .db-index-doc in styles.css
 * (ported 1:1 from the standalone page's <style> block).
 *
 * Interaction model:
 *  - Category filter: collapsible multi-select chips, OR logic. "All" is
 *    exclusive — clicking it clears the selection. Clicking a category twice
 *    toggles it off. "All" auto-activates when the selection is empty.
 *  - Access dropdown: AND-composes with the above + search.
 *  - Search: matches name, category, tags, description, access string.
 *  - 🎲 Surprise me: shows a random database via a toast overlay.
 *  - Esc: clears all filters and the surprise. / : focuses the search box.
 */

type Access = "web" | "api" | "dl" | "ct" | "";

const ACCESS_LABELS: Record<Exclude<Access, "">, string> = {
  web: "Web UI",
  api: "REST API / E-utilities / BioMart",
  dl: "FTP / bulk download / S3",
  ct: "Controlled / license / subscription",
};

const ACCESS_MATCHERS: Record<Exclude<Access, "">, RegExp> = {
  web: /web ui/i,
  api: /api|e-utilities|biomart|sparql/i,
  dl: /ftp|bulk|download|s3/i,
  ct: /controlled|license|subscription|restricted/i,
};

function accessBucket(a: string): Access {
  if (ACCESS_MATCHERS.api.test(a)) return "api";
  if (ACCESS_MATCHERS.dl.test(a)) return "dl";
  if (ACCESS_MATCHERS.ct.test(a)) return "ct";
  return "web";
}

function accCls(a: string): string {
  const b = accessBucket(a);
  return `db-acc db-acc-${b}`;
}

export function HelpDatabases() {
  const [query, setQuery] = useState("");
  const [access, setAccess] = useState<Access>("");
  const [categories, setCategories] = useState<Set<string>>(new Set());
  const [showAddLocalDatabase, setShowAddLocalDatabase] = useState(false);
  const queryClient = useQueryClient();
  const localDatabasesQuery = useQuery({
    queryKey: ["local-databases"],
    queryFn: api.listLocalDatabases,
  });
  const localDatabases = localDatabasesQuery.data ?? [];

  const allCategories = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const d of DATABASES) counts[d.c] = (counts[d.c] ?? 0) + 1;
    return Object.entries(counts)
      .sort(([, a], [, b]) => b - a)
      .map(([c]) => c);
  }, []);

  const catCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const d of DATABASES) counts[d.c] = (counts[d.c] ?? 0) + 1;
    return counts;
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return DATABASES.filter((d) => {
      if (categories.size > 0 && !categories.has(d.c)) return false;
      if (access && !ACCESS_MATCHERS[access].test(d.a)) return false;
      if (!q) return true;
      return (
        d.n.toLowerCase().includes(q) ||
        d.c.toLowerCase().includes(q) ||
        d.t.toLowerCase().includes(q) ||
        d.d.toLowerCase().includes(q) ||
        d.a.toLowerCase().includes(q)
      );
    });
  }, [query, access, categories]);

  const toggleCategory = (c: string) => {
    setCategories((prev) => {
      const next = new Set(prev);
      if (c === "") {
        next.clear();
      } else if (next.has(c)) {
        next.delete(c);
      } else {
        next.add(c);
      }
      return next;
    });
  };

  const clearFilters = () => {
    setQuery("");
    setAccess("");
    setCategories(new Set());
  };

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "/") {
        const active = document.activeElement;
        if (active?.tagName !== "INPUT" && active?.tagName !== "TEXTAREA") {
          e.preventDefault();
          (document.getElementById("db-search") as HTMLInputElement | null)?.focus();
        }
      }
      if (e.key === "Escape") {
        clearFilters();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const catClause =
    categories.size === 1
      ? ` in "${[...categories][0]}"`
      : categories.size > 1
      ? ` in ${categories.size} categories`
      : "";

  return (
    <div className="help-page db-index-doc">
      <h1>Database Index</h1>
      <p className="db-intro">
        A searchable index of genetics &amp; bioinformatics data resources — the
        famous, the specialized, and the obscure. Filter by category or access
        method, or search.
      </p>

      <section className="db-local-section">
        <div className="db-local-header">
          <h2>Local Databases</h2>
          <button type="button" className="db-btn" onClick={() => setShowAddLocalDatabase(true)}>
            Submit a database
          </button>
        </div>

        {localDatabasesQuery.isLoading && <p className="db-empty">Loading…</p>}
        {localDatabasesQuery.isError && (
          <p className="db-empty">Could not reach the server to list local databases.</p>
        )}
        {!localDatabasesQuery.isLoading && !localDatabasesQuery.isError && localDatabases.length === 0 && (
          <p className="db-empty">No local databases yet — submit one above.</p>
        )}

        {localDatabases.length > 0 && (
          <div className="db-cards">
            {localDatabases.map((d) => (
              <article key={d.id} className="db-card">
                <div className="db-card-top">
                  <h3 className="db-card-name">
                    <a href={d.url} target="_blank" rel="noopener noreferrer">
                      {d.name} ↗
                    </a>
                  </h3>
                  <span className="db-card-cat">{LOCAL_DATABASE_CATEGORY_LABELS[d.category]}</span>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      {showAddLocalDatabase && (
        <AddLocalDatabaseModal
          onCreated={() => {
            setShowAddLocalDatabase(false);
            queryClient.invalidateQueries({ queryKey: ["local-databases"] });
          }}
          onClose={() => setShowAddLocalDatabase(false)}
        />
      )}

      <p className="db-stats">
        <b>{DATABASES.length}</b> databases · <b>{allCategories.length}</b> categories ·
        offline-capable · access methods tagged (API / FTP / bulk / web / controlled / license)
      </p>

      <div className="db-tools">
        <input
          id="db-search"
          type="search"
          placeholder="Search by name, keyword, or tag…  (press / to focus, Esc to clear)"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          autoComplete="off"
        />
        <select value={access} onChange={(e) => setAccess(e.target.value as Access)} title="Filter by access method">
          <option value="">All access methods</option>
          <option value="web">Web UI</option>
          <option value="api">REST API / E-utilities / BioMart</option>
          <option value="dl">FTP / bulk download / S3</option>
          <option value="ct">Controlled / license / subscription</option>
        </select>
        <button
          type="button"
          className="db-btn"
          title="Show a random database"
          onClick={() => {
            const d = DATABASES[Math.floor(Math.random() * DATABASES.length)];
            // scroll the user to a random card as the "surprise"
            const el = document.getElementById("db-" + encodeURIComponent(d.n));
            el?.scrollIntoView({ behavior: "smooth", block: "center" });
            el?.dispatchEvent(new MouseEvent("mouseenter", { bubbles: true }));
          }}
        >
          Surprise me
        </button>
      </div>

      <details className="db-chips-details" open={true}>
        <summary className="db-category-summary">
          <span className="db-arrow">▸</span> Categories{" "}
          <span className="db-active">
            {categories.size === 0
              ? `All (${DATABASES.length})`
              : `${categories.size} categories`}
          </span>
        </summary>
        <span className="db-chip-divider" aria-hidden="true">
          |
        </span>
        <div className="db-chips">
          <span
            className={`db-chip${categories.size === 0 ? " db-chip-on" : ""}`}
            data-c=""
            onClick={() => toggleCategory("")}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                toggleCategory("");
              }
            }}
          >
            All <span className="db-chip-count">({DATABASES.length})</span>
          </span>
          {allCategories.map((c) => {
            const on = categories.has(c);
            return (
              <span
                key={c}
                className={`db-chip${on ? " db-chip-on" : ""}`}
                data-c={c}
                onClick={() => toggleCategory(c)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    toggleCategory(c);
                  }
                }}
              >
                {c} <span className="db-chip-count">({catCounts[c]})</span>
              </span>
            );
          })}
        </div>
      </details>

      <p className="db-meta">
        {filtered.length} of {DATABASES.length} databases
        {catClause}
        {access ? ` · ${ACCESS_LABELS[access]}` : ""}
        {query ? ` matching "${query}"` : ""}
      </p>

      {filtered.length === 0 ? (
        <p className="db-empty">
          No databases match your search. Try a different term or clear the
          category filter.
        </p>
      ) : (
        <div className="db-cards">
          {filtered.map((d) => (
            <article key={d.n} className="db-card" id={"db-" + encodeURIComponent(d.n)}>
              <div className="db-card-top">
                <h3 className="db-card-name">
                  {d.u ? (
                    <a href={d.u} target="_blank" rel="noopener noreferrer">
                      {d.n} ↗
                    </a>
                  ) : (
                    d.n
                  )}
                </h3>
                <span className="db-card-cat">{d.c}</span>
              </div>
              <p className="db-card-desc">{d.d}</p>
              <span className={accCls(d.a)}>{d.a}</span>
              <p className="db-card-tags">{d.t}</p>
            </article>
          ))}
        </div>
      )}

      <footer className="db-footer">
        <h2>How to go deeper</h2>
        <ul>
          <li>
            <b>NAR Molecular Biology Database Collection</b> — the canonical
            catalog: ~1,800 databases, updated yearly with the Nucleic Acids
            Research Database Issue.{" "}
            <a
              href="https://www.oxfordjournals.org/nar/database/c/"
              target="_blank"
              rel="noopener noreferrer"
            >
              oxfordjournals.org/nar/database/c/
            </a>
          </li>
          <li>
            <b>FAIRsharing</b> — registry of data resources, metadata standards
            &amp; policies.{" "}
            <a href="https://fairsharing.org/" target="_blank" rel="noopener noreferrer">
              fairsharing.org
            </a>
          </li>
          <li>
            <b>re3data</b> — registry of research data repositories.{" "}
            <a href="https://www.re3data.org/" target="_blank" rel="noopener noreferrer">
              re3data.org
            </a>
          </li>
          <li>
            <b>bio.tools</b> — registry of bioinformatics software (ELIXIR).{" "}
            <a href="https://bio.tools/" target="_blank" rel="noopener noreferrer">
              bio.tools
            </a>
          </li>
          <li>
            <b>OMICtools</b> — searchable catalog of omics tools &amp; databases.{" "}
            <a href="https://omictools.com/" target="_blank" rel="noopener noreferrer">
              omictools.com
            </a>
          </li>
        </ul>
        <h3>Data access notes</h3>
        <ul>
          <li>
            <b>NCBI family:</b> use E-utilities (<code>esearch</code>/
            <code>esummary</code>/<code>efetch</code>) with a free API key for
            ~10x higher rate limits; bulk files live under{" "}
            <code>ftp.ncbi.nlm.nih.gov</code>. SRA reads:{" "}
            <code>prefetch</code>/<code>fasterq-dump</code> (sra-tools) or direct
            cloud buckets (AWS S3 / GCS) for the largest projects.
          </li>
          <li>
            <b>EMBL-EBI family:</b> a uniform REST API pattern (documented at
            ebi.ac.uk/Tools) — ENA, GWAS Catalog, the eQTL Catalogue, Expression
            Atlas and most EBI resources expose JSON endpoints; bulk files share
            the EBI FTP area. Ensembl adds BioMart for tabular exports.
          </li>
          <li>
            <b>Version-pin everything:</b> record the release (Ensembl release
            51, IWGSC RefSeq v2.1, GRCh38.p14, gnomAD v4.1) and the exact
            accession set — annotations drift between releases. Use the archived
            release sites Ensembl/UCSC keep for old builds.
          </li>
          <li>
            <b>License gates:</b> OMIM, HGMD, COSMIC, DrugBank, KEGG,
            BioCyc/MetaCyc, TRANSFAC, RepBase and GeneCards require licensing
            for bulk or API use. Open equivalents often exist (ClinVar ↔ HGMD,
            gnomAD ↔ OMIM, AlphaFold DB ↔ PDB) — check the terms page before
            scripting anything.
          </li>
          <li>
            <b>Controlled access:</b> human-level data (dbGaP, EGA, UK
            Biobank, FinnGen, All of Us, TOPMed raw reads, DECIPHER patient
            variants) requires DAC approval and a data-use agreement; derived
            summary statistics (gnomAD frequencies, UKB-PPP pQTLs, FinnGen
            sumstats) are usually open — prefer them for quick work.
          </li>
          <li>
            <b>NGDC (China) family:</b> GSA, GWH, MethBank, EWAS and LncBook at
            ngdc.cncb.ac.cn are authoritative for many non-Western projects
            (incl. many plant/animal assemblies) but can be slow or time out
            from outside China — retry, use their FTP/Open API, or look for
            NCBI/ENA mirrors of the same BioProject.
          </li>
          <li>
            <b>Dead small databases:</b> academic one-offs (CANTATAdb, PLncDB,
            GreeNC…) go dark without notice. Before building a pipeline on one,
            snapshot the dump locally; verify currency via the NAR Database
            Issue entry or FAIRsharing status.
          </li>
          <li>
            <b>Bulk pattern:</b> most big resources publish whole-database
            dumps (UniProt FASTA, gnomAD VCFs, GTEx TPM matrices, ENCODE
            peak/BED files, Ensembl GFF3). Look for "Downloads / Data / FTP"
            links; prefer HTTPS/cloud over legacy FTP where offered, and use{" "}
            <code>rsync</code> or <code>curl -C -</code> for resumable pulls.
          </li>
          <li>
            <b>Cite the database, not just the data:</b> every resource here has
            a primary paper (mostly in the NAR Database Issue) with a DOI —
            cite it plus the version and date of your download.
          </li>
        </ul>
        <p>
          Availability note: several specialized databases (esp. small academic
          ones) go offline or change domains without notice. If a link is dead,
          search the name — mirrors frequently exist. Always cite the original
          database publication when using data in your work.
        </p>
      </footer>
    </div>
  );
}
