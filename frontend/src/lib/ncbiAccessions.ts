/**
 * Mirrors the backend's accession-prefix lists (`sra.py`, `ncbi_assembly.py`,
 * `sra_resolver.py`). Duplicated deliberately -- those three modules each
 * already keep their own prefix tuple, and this fixed, rarely-changing list
 * isn't worth a cross-language sync mechanism.
 */
const KNOWN_ACCESSION_PREFIXES = [
  "SRR",
  "ERR",
  "DRR",
  "SRX",
  "ERX",
  "DRX",
  "SRS",
  "ERS",
  "DRS",
  "SRP",
  "ERP",
  "DRP",
  "PRJNA",
  "PRJEB",
  "PRJDB",
  "SAMN",
  "SAME",
  "SAMD",
  "GCA",
  "GCF",
];

/**
 * Whether the first 3 characters of `text` could be the start of a known
 * accession prefix. Used only to decide whether to skip firing the organism
 * autocomplete request -- not for full accession validation, which stays
 * server-side.
 */
export function looksLikeAccessionPrefix(text: string): boolean {
  const head = text.trim().slice(0, 3).toUpperCase();
  if (head.length < 3) return false;
  return KNOWN_ACCESSION_PREFIXES.some((prefix) => prefix.startsWith(head));
}
