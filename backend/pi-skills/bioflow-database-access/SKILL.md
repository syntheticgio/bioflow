---
name: bioflow-database-access
---

# Reach Public Bioinformatics Databases

## When to Use

The user needs data from outside BioFlow — a genome, a sequence, an
assembly, taxonomy, literature — or asks how to get data into a project.

## Procedure

1. NCBI first, in-app: `bioflow_search_ncbi(term)` then
   `bioflow_download_reference(accession, project_id)`. This is the primary
   path for genomes — it registers the genome, annotation, protein and CDS
   as project objects, which the rest of the pipeline can use directly.
2. NCBI Datasets browsing: the `datasets` MCP server
   (`ncbi-datasets-mcp`) for assembly/taxonomy discovery and downloads that
   go beyond a single known accession — `genome_summary_by_taxon`,
   `genome_summary_by_accession`, `taxonomy_summary`, and the download
   tools. It installs the NCBI CLI on first use; the download lands outside
   BioFlow, so register or import the result into the project afterwards.
3. Literature and web content: the `fetch` MCP server for any URL — PubMed,
   journal pages, Europe PMC, the bioRxiv search API. This is the
   literature-verification path; there is no paid-key skill installed.
4. Beyond NCBI: EBI/Ensembl/UniProt are reachable through their public REST
   APIs via the `fetch` MCP server — Ensembl REST for genes/transcripts,
   UniProt REST for proteins, EBI search for literature. Fetch the API's
   documented endpoint and parse the JSON response.

## Pitfalls

- Prefer the in-app tools: they register results as project objects. Use
  the external servers only when the in-app path cannot answer.
- Never guess accession formats; get them from a search result.
- The `fetch` server reads web content; for structured databases use their
  documented API endpoints, not a scrape of the HTML site.
- An external download (datasets CLI) does not appear in BioFlow until it
  is imported — tell the user when a manual import is needed.
