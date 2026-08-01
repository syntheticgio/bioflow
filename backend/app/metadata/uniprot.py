"""Reading UniProt: what an input names, and what UniProt says about it.

Separate from `structure_lookup.py`, which resolves a gene symbol to one
protein for the variants table. This module answers a different question --
"what did the user type, and what can be downloaded for it?" -- and shares
only the choice of transport.

Uses stdlib urllib rather than httpx, which is a dev-only dependency here;
these are simple JSON GETs, and they run in a worker thread.
"""

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum

from app.logging import get_logger

log = get_logger(__name__)

_PROTEOMES_URL = "https://rest.uniprot.org/proteomes/search"
_PROTEOME_ENTRY_URL = "https://rest.uniprot.org/proteomes"
_UNIPROTKB_URL = "https://rest.uniprot.org/uniprotkb/search"
_STREAM_URL = "https://rest.uniprot.org/uniprotkb/stream"

# Matches structure_lookup's timeout, for the same reason: long enough for a
# cold response, short enough that a click does not appear to hang.
_TIMEOUT_SECONDS = 20.0

# How many rows a picker shows. UniProt ranks by relevance and the wanted
# entry is near the top; a larger page turns one lookup into a scan.
_MAX_RESULTS = 25

_PROTEOME_ID = re.compile(r"^UP\d{9}$")

# UniProt's own documented accession pattern. Deliberately strict: a looser
# one classifies the gene symbol EGFR as an accession, which returns nothing.
_ACCESSION = re.compile(
    r"^[OPQ][0-9][A-Z0-9]{3}[0-9]$|^[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}$"
)

_SPLIT = re.compile(r"[\s,;]+")


class InputKind(StrEnum):
    PROTEOME = "proteome"
    ACCESSIONS = "accessions"
    TAXON = "taxon"
    TEXT = "text"


def classify(raw: str) -> InputKind:
    """What the accession box holds.

    Shape only -- no network. An input that looks like nothing in particular
    becomes TEXT, which is the branch that degrades gracefully: a free-text
    search returning no rows is a comprehensible answer, where a malformed
    accession query is an error the user cannot act on.
    """
    value = (raw or "").strip()
    if not value:
        return InputKind.TEXT

    if _PROTEOME_ID.match(value.upper()):
        return InputKind.PROTEOME

    tokens = [t for t in _SPLIT.split(value.upper()) if t]
    if tokens and all(_ACCESSION.match(t) for t in tokens):
        return InputKind.ACCESSIONS

    if value.isdigit():
        return InputKind.TAXON

    return InputKind.TEXT


def parse_accessions(raw: str) -> list[str]:
    """The accessions in an input, uppercased and deduplicated.

    Order is preserved because the picker lists them in it, and a user who
    pasted a deliberate order should see that order back.
    """
    seen: list[str] = []
    for token in _SPLIT.split((raw or "").strip().upper()):
        if token and token not in seen:
            seen.append(token)
    return seen


def reference_proteome_query(taxon_id: int) -> str:
    """The reference proteome for one taxon.

    `reference:true`, not `proteome_type:1`. The latter is the form that
    looks right and appears in older examples; measured against the live API
    it returns zero rows for every organism tried, including taxon 559292,
    which does have a reference proteome.
    """
    return f"organism_id:{taxon_id} AND reference:true"


def all_proteomes_query(taxon_id: int) -> str:
    """Every proteome for one taxon, reference or not.

    The fallback when the reference query is empty, which is not an edge
    case: taxon 4932 (*S. cerevisiae* at species level, the ID a user is
    most likely to type) has no reference proteome because UniProt attaches
    it to strain taxon 559292. Measured 0 against 360.
    """
    return f"organism_id:{taxon_id}"


def organism_name_query(name: str) -> str:
    """Proteomes for an organism named in words.

    No type filter, for the same measured reason as `all_proteomes_query`:
    adding one returns 0 where the unfiltered query returns 481 with the
    wanted proteome ranked first.

    Every double-quote is removed, not just the surrounding pair. The name is
    typed into a search box, and a stray internal quote leaves the phrase
    unbalanced -- measured, `organism_name:"Homo "sapiens"` is a hard HTTP 400
    rather than a poor match. A phrase search cannot contain a quote anyway,
    so dropping them costs nothing.
    """
    cleaned = (name or "").replace('"', "").strip()
    return f'organism_name:"{cleaned}"'


def download_query(
    *, proteome_id: str | None, accessions: list[str], reviewed_only: bool
) -> str:
    """The query the FASTA stream is fetched with.

    One function for both download shapes, because one endpoint serves both:
    a whole proteome and a hand-picked set differ only here.

    `reviewed_only` is deliberately ignored for picked accessions. The user
    named those entries; filtering an unreviewed one back out would hand
    them fewer proteins than they selected, with nothing to explain why.
    """
    if accessions:
        return " OR ".join(f"accession:{a}" for a in accessions)
    query = f"proteome:{proteome_id}"
    if reviewed_only:
        query += " AND reviewed:true"
    return query


def stream_url(query: str) -> str:
    """The FASTA stream endpoint for a query.

    Compressed: a proteome is mostly sequence text and gzip roughly halves
    it (measured 3.9 MB to 1.9 MB for yeast).
    """
    params = urllib.parse.urlencode(
        {"query": query, "format": "fasta", "compressed": "true"}
    )
    return f"{_STREAM_URL}?{params}"


@dataclass(frozen=True)
class ProteomeInfo:
    id: str
    name: str
    taxon_id: int | None
    strain: str | None
    protein_count: int | None
    is_reference: bool
    busco_score: int | None
    # The NCBI assembly this proteome's genome came from, when UniProt names
    # one. Surfaced as a link rather than a combined download: the proteome
    # and the assembly are the same organism's two halves, but merging the
    # two providers' dialogs was considered and rejected.
    genome_assembly: str | None


@dataclass(frozen=True)
class ProteinHit:
    accession: str
    entry_id: str | None
    name: str | None
    organism: str | None
    length: int | None
    reviewed: bool


@dataclass
class TaxonResolution:
    """What an organism input produced.

    `needs_picker` is the reference-proteome question answered: False means
    one card is enough, True means the user must choose. It is not derivable
    from `candidates` being non-empty, because the reference case also lists
    the alternatives behind a disclosure.
    """

    proteome: ProteomeInfo | None = None
    candidates: list[ProteomeInfo] = field(default_factory=list)
    needs_picker: bool = False


def _get(url: str, *, timeout: float = _TIMEOUT_SECONDS) -> bytes:
    """The transport, isolated so tests can replace it without a network."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _get_json(url: str, *, timeout: float = _TIMEOUT_SECONDS) -> dict:
    payload = json.loads(_get(url, timeout=timeout))
    return payload if isinstance(payload, dict) else {}


def count_results(query: str, *, timeout: float = _TIMEOUT_SECONDS) -> int | None:
    """How many entries a query matches, from `X-Total-Results`.

    Exact, which is what lets the dialog show the reviewed/unreviewed split
    (roughly sevenfold for human) and guard a large download on a real
    number rather than a byte estimate.

    Not an assertion about the download: the header and the streamed record
    count differ slightly -- human reviewed reported 20,416 and delivered
    20,427 -- so a handler that failed on a mismatch would fail on correct
    downloads.
    """
    params = urllib.parse.urlencode(
        {"query": query, "format": "json", "size": "1", "fields": "accession"}
    )
    try:
        with urllib.request.urlopen(
            f"{_UNIPROTKB_URL}?{params}", timeout=timeout
        ) as response:
            total = response.headers.get("X-Total-Results")
    except Exception as exc:
        log.info("uniprot_count_failed", query=query, error=str(exc))
        return None
    try:
        return int(total)
    except (TypeError, ValueError):
        return None


def _proteome_info(entry: dict) -> ProteomeInfo | None:
    pid = entry.get("id")
    if not isinstance(pid, str):
        return None
    taxonomy = entry.get("taxonomy") or {}
    busco = (entry.get("proteomeCompletenessReport") or {}).get("buscoReport") or {}
    assembly = entry.get("genomeAssembly") or {}
    return ProteomeInfo(
        id=pid,
        name=taxonomy.get("scientificName") or pid,
        taxon_id=taxonomy.get("taxonId"),
        strain=entry.get("strain"),
        protein_count=entry.get("proteinCount"),
        is_reference=entry.get("proteomeType") == "Reference proteome",
        busco_score=busco.get("score"),
        genome_assembly=assembly.get("assemblyId"),
    )


def _search_proteomes(query: str) -> list[ProteomeInfo]:
    params = urllib.parse.urlencode(
        {"query": query, "format": "json", "size": str(_MAX_RESULTS)}
    )
    try:
        payload = _get_json(f"{_PROTEOMES_URL}?{params}")
    except Exception as exc:
        # Matches structure_lookup: an outage means "found nothing", never an
        # error the caller has to handle. The dialog says the same thing for
        # a timeout and a genuinely empty result.
        log.info("uniprot_proteome_search_failed", query=query, error=str(exc))
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    out = []
    for entry in results:
        if isinstance(entry, dict):
            info = _proteome_info(entry)
            if info is not None:
                out.append(info)
    return out


def resolve_proteome(proteome_id: str) -> ProteomeInfo | None:
    """One proteome by its own ID."""
    try:
        payload = _get_json(f"{_PROTEOME_ENTRY_URL}/{proteome_id}?format=json")
    except Exception as exc:
        log.info("uniprot_proteome_lookup_failed", proteome=proteome_id, error=str(exc))
        return None
    return _proteome_info(payload)


def resolve_taxon(taxon_id: int) -> TaxonResolution:
    """The proteomes for one taxon, reference first.

    Two queries, and the second is mandatory rather than defensive. Taxon
    4932 -- *S. cerevisiae* at species level, the ID a user is most likely to
    type for yeast -- has no reference proteome, because UniProt attaches it
    to strain taxon 559292. Measured: `reference:true` returns 0 while the
    unfiltered query returns 360. Skipping the fallback tells the user yeast
    has no proteome.
    """
    reference = _search_proteomes(reference_proteome_query(taxon_id))
    if reference:
        # The alternatives still load, behind a disclosure: the reference is
        # right for the common case, and strain matters in real work.
        others = [p for p in _search_proteomes(all_proteomes_query(taxon_id))
                  if p.id != reference[0].id]
        return TaxonResolution(
            proteome=reference[0], candidates=others, needs_picker=False
        )

    candidates = _search_proteomes(all_proteomes_query(taxon_id))
    return TaxonResolution(
        proteome=None, candidates=candidates, needs_picker=bool(candidates)
    )


def resolve_organism_name(name: str) -> TaxonResolution:
    """Proteomes for an organism named in words.

    Ranked by UniProt's own relevance, which puts the reference proteome
    first for a plain species name. A reference hit at the top takes the card
    path; anything else opens the picker.
    """
    candidates = _search_proteomes(organism_name_query(name))
    if not candidates:
        return TaxonResolution()
    if candidates[0].is_reference:
        return TaxonResolution(
            proteome=candidates[0], candidates=candidates[1:], needs_picker=False
        )
    return TaxonResolution(candidates=candidates, needs_picker=True)


def _protein_hit(entry: dict) -> ProteinHit | None:
    accession = entry.get("primaryAccession")
    if not isinstance(accession, str):
        return None
    description = entry.get("proteinDescription") or {}
    recommended = description.get("recommendedName") or {}
    full_name = (recommended.get("fullName") or {}).get("value")
    return ProteinHit(
        accession=accession,
        entry_id=entry.get("uniProtkbId"),
        # Unreviewed entries frequently carry no recommendedName at all.
        name=full_name if isinstance(full_name, str) else None,
        organism=(entry.get("organism") or {}).get("scientificName"),
        length=(entry.get("sequence") or {}).get("length"),
        reviewed="reviewed" in (entry.get("entryType") or "").lower()
        and "unreviewed" not in (entry.get("entryType") or "").lower(),
    )


def search_proteins(query: str) -> list[ProteinHit]:
    """Proteins matching free text, or a set of named accessions."""
    params = urllib.parse.urlencode(
        {
            "query": query,
            "format": "json",
            "size": str(_MAX_RESULTS),
            "fields": "accession,id,protein_name,organism_name,length,reviewed",
        }
    )
    try:
        payload = _get_json(f"{_UNIPROTKB_URL}?{params}")
    except Exception as exc:
        log.info("uniprot_protein_search_failed", query=query, error=str(exc))
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    out = []
    for entry in results:
        if isinstance(entry, dict):
            hit = _protein_hit(entry)
            if hit is not None:
                out.append(hit)
    return out
