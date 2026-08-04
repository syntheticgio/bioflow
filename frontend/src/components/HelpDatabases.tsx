import { useMemo, useState } from "react";
import { DATABASES, type DbEntry } from "../data/databases";

/**
 * Reference index of external genetics/bioinformatics databases -- not
 * things BioFlow integrates with (that's Data Sources), just a catalog for
 * the user to browse and follow links out from.
 */

type Access = "web" | "api" | "dl" | "ct" | "";

function accessBucket(a: string): Access {
  const s = a.toLowerCase();
  if (s.includes("api") || s.includes("e-utilities")) return "api";
  if (s.includes("bulk") || s.includes("ftp") || s.includes("download") || s.includes("s3")) return "dl";
  if (s.includes("controlled") || s.includes("license") || s.includes("subscription")) return "ct";
  return "web";
}

const ACCESS_LABELS: Record<Exclude<Access, "">, string> = {
  web: "Web UI",
  api: "REST API / E-utilities / BioMart",
  dl: "FTP / bulk download / S3",
  ct: "Controlled / license / subscription",
};

function DbCard({ entry }: { entry: DbEntry }) {
  return (
    <article className="db-card">
      <h3 className="db-card-name">
        {entry.u ? (
          <a href={entry.u} target="_blank" rel="noopener noreferrer">
            {entry.n} ↗
          </a>
        ) : (
          entry.n
        )}
      </h3>
      <p className="db-card-desc">{entry.d}</p>
      <div className="db-card-meta">
        <span className="db-card-cat">{entry.c}</span>
        <span className="db-card-access">{entry.a}</span>
      </div>
    </article>
  );
}

export function HelpDatabases() {
  const [query, setQuery] = useState("");
  const [access, setAccess] = useState<Access>("");
  const [category, setCategory] = useState<string>("");

  const categories = useMemo(() => {
    const counts = new Map<string, number>();
    for (const d of DATABASES) counts.set(d.c, (counts.get(d.c) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return DATABASES.filter((d) => {
      if (category && d.c !== category) return false;
      if (access && accessBucket(d.a) !== access) return false;
      if (!q) return true;
      return (
        d.n.toLowerCase().includes(q) ||
        d.t.toLowerCase().includes(q) ||
        d.d.toLowerCase().includes(q)
      );
    });
  }, [query, access, category]);

  return (
    <div className="help-page software-page">
      <h1>Database Index</h1>
      <p className="help-intro">
        A searchable index of genetics &amp; bioinformatics data resources -- the
        famous, the specialized, and the obscure. This is a reference list, not
        anything BioFlow talks to; see{" "}
        <a href="#/help/sources">Data Sources</a> for what's actually
        integrated.
      </p>
      <p className="software-note">
        {filtered.length} of {DATABASES.length} databases
        {category ? ` in "${category}"` : ""}
        {access ? ` · ${ACCESS_LABELS[access]}` : ""}
      </p>

      <div className="db-tools">
        <input
          type="search"
          placeholder="Search by name, keyword, or description…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          autoComplete="off"
        />
        <select value={access} onChange={(e) => setAccess(e.target.value as Access)}>
          <option value="">All access methods</option>
          <option value="web">Web UI</option>
          <option value="api">REST API / E-utilities / BioMart</option>
          <option value="dl">FTP / bulk download / S3</option>
          <option value="ct">Controlled / license / subscription</option>
        </select>
        <select value={category} onChange={(e) => setCategory(e.target.value)}>
          <option value="">All categories</option>
          {categories.map(([c, n]) => (
            <option key={c} value={c}>
              {c} ({n})
            </option>
          ))}
        </select>
      </div>

      {filtered.length === 0 ? (
        <p className="help-intro">
          No databases match your search. Try a different term or clear the
          category filter.
        </p>
      ) : (
        <div className="db-cards">
          {filtered.map((d) => (
            <DbCard key={d.n} entry={d} />
          ))}
        </div>
      )}

      <div className="help-section" style={{ marginTop: 32 }}>
        <h2>How to go deeper</h2>
        <ul>
          <li>
            <b>NAR Molecular Biology Database Collection</b> -- the canonical
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
            <b>FAIRsharing</b> -- registry of data resources, standards &amp;
            policies.{" "}
            <a href="https://fairsharing.org/" target="_blank" rel="noopener noreferrer">
              fairsharing.org
            </a>
          </li>
          <li>
            <b>re3data</b> -- registry of research data repositories.{" "}
            <a href="https://www.re3data.org/" target="_blank" rel="noopener noreferrer">
              re3data.org
            </a>
          </li>
          <li>
            <b>bio.tools</b> -- registry of bioinformatics software (ELIXIR).{" "}
            <a href="https://bio.tools/" target="_blank" rel="noopener noreferrer">
              bio.tools
            </a>
          </li>
          <li>
            <b>OMICtools</b> -- searchable catalog of omics tools &amp;
            databases.{" "}
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
            <code>prefetch</code>/<code>fasterq-dump</code> (sra-tools) or
            direct cloud buckets (AWS S3 / GCS) for the largest projects.
          </li>
          <li>
            <b>EMBL-EBI family:</b> a uniform REST API pattern (documented at
            ebi.ac.uk/Tools) -- ENA, GWAS Catalog, the eQTL Catalogue,
            Expression Atlas and most EBI resources expose JSON endpoints;
            bulk files share the EBI FTP area. Ensembl adds BioMart for
            tabular exports.
          </li>
          <li>
            <b>Version-pin everything:</b> record the release (Ensembl release
            51, IWGSC RefSeq v2.1, GRCh38.p14, gnomAD v4.1) and the exact
            accession set -- annotations drift between releases. Use the
            archived release sites Ensembl/UCSC keep for old builds.
          </li>
          <li>
            <b>License gates:</b> OMIM, HGMD, COSMIC, DrugBank, KEGG,
            BioCyc/MetaCyc, TRANSFAC, RepBase and GeneCards require licensing
            for bulk or API use. Open equivalents often exist (ClinVar ↔
            HGMD, gnomAD ↔ OMIM, AlphaFold DB ↔ PDB) -- check the terms page
            before scripting anything.
          </li>
          <li>
            <b>Controlled access:</b> human-level data (dbGaP, EGA, UK
            Biobank, FinnGen, All of Us, TOPMed raw reads, DECIPHER patient
            variants) requires DAC approval and a data-use agreement; derived
            summary statistics (gnomAD frequencies, UKB-PPP pQTLs, FinnGen
            sumstats) are usually open -- prefer them for quick work.
          </li>
          <li>
            <b>NGDC (China) family:</b> GSA, GWH, MethBank, EWAS and LncBook
            at ngdc.cncb.ac.cn are authoritative for many non-Western
            projects (incl. many plant/animal assemblies) but can be slow or
            time out from outside China -- retry, use their FTP/Open API, or
            look for NCBI/ENA mirrors of the same BioProject.
          </li>
          <li>
            <b>Dead small databases:</b> academic one-offs (CANTATAdb,
            PLncDB, GreeNC…) go dark without notice. Before building a
            pipeline on one, snapshot the dump locally; verify currency via
            the NAR Database Issue entry or FAIRsharing status.
          </li>
          <li>
            <b>Bulk pattern:</b> most big resources publish whole-database
            dumps (UniProt FASTA, gnomAD VCFs, GTEx TPM matrices, ENCODE
            peak/BED files, Ensembl GFF3). Look for "Downloads / Data / FTP"
            links; prefer HTTPS/cloud over legacy FTP where offered, and use{" "}
            <code>rsync</code> or <code>curl -C -</code> for resumable pulls.
          </li>
          <li>
            <b>Cite the database, not just the data:</b> every resource here
            has a primary paper (mostly in the NAR Database Issue) with a
            DOI -- cite it plus the version and date of your download.
          </li>
        </ul>
        <p>
          Availability note: several specialized databases (esp. small
          academic ones) go offline or change domains without notice. If a
          link is dead, search the name -- mirrors frequently exist. Entries
          with ⚠ flags were unstable as of this list's last review. Always
          cite the original database publication when using data in your
          work.
        </p>
      </div>
    </div>
  );
}
