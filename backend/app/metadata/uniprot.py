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
    if tokens and all(is_valid_accession(t) for t in tokens):
        return InputKind.ACCESSIONS

    if value.isdigit():
        return InputKind.TAXON

    return InputKind.TEXT


def is_valid_accession(value: str) -> bool:
    """Whether one token is a UniProtKB accession.

    Public because `uniprot_service` validates a submitted request against the
    same pattern `classify` uses, and two copies of an accession regex is
    exactly the kind of pair that drifts. Mirrors `assembly.is_valid_accession`,
    which `assembly_service` calls for the same reason.
    """
    return bool(_ACCESSION.match((value or "").strip().upper()))


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


def _obj(value: object) -> dict:
    """A nested field as a dict, whatever it actually is.

    `x.get("k") or {}` guards only against None. UniProt occasionally returns
    a field as a string or a list where a dict is expected, and `.get()` on
    that raises -- escaping the try/except around the request, since parsing
    happens after it. A raise here would surface as a 500 from what this
    module promises to report as "found nothing".
    """
    return value if isinstance(value, dict) else {}


def _proteome_info(entry: dict) -> ProteomeInfo | None:
    pid = entry.get("id")
    if not isinstance(pid, str):
        return None
    taxonomy = _obj(entry.get("taxonomy"))
    busco = _obj(_obj(entry.get("proteomeCompletenessReport")).get("buscoReport"))
    assembly = _obj(entry.get("genomeAssembly"))
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
    """The proteome for one taxon.

    A strain taxon (559292) has a reference proteome and resolves in one
    query. A species taxon (4932, the ID a user is more likely to type for
    yeast) does not, because UniProt attaches the reference proteome to the
    strain -- so the second step re-asks by the organism's *name*, which does
    find it.

    An earlier version offered the species taxon's other proteomes as a
    strain picker instead. That was wrong, and measurably so: those proteomes
    exist in UniParc but not in UniProtKB's searchable index, which is what
    both the count and the download query go through. Sampled across yeast,
    *E. coli*, *M. tuberculosis*, and *S. aureus* on 2026-07-31, **0 of 100**
    non-reference proteomes returned any results -- `proteome:UP000037662`
    gives 0 rows and an empty FASTA although its own record claims 5,389
    proteins. The picker could only ever offer dead ends, and it offered them
    in place of the reference proteome the name query finds immediately.

    So a taxon that names no reference proteome falls back to its name rather
    than to a list. Measured: 4932 -> UP000002311, 562 -> UP000000625,
    1280 -> UP000001939, all downloadable.
    """
    reference = _search_proteomes(reference_proteome_query(taxon_id))
    if reference:
        return TaxonResolution(proteome=reference[0], needs_picker=False)

    # The unfiltered query is asked for its organism *name*, not its list: the
    # entries carry `scientificName`, so this costs no extra request.
    others = _search_proteomes(all_proteomes_query(taxon_id))
    if not others:
        return TaxonResolution()

    # "Saccharomyces cerevisiae (strain ATCC 204508 / S288c)" -> the species,
    # which is what the name search wants.
    name = others[0].name.split(" (")[0]
    return resolve_organism_name(name)


def resolve_organism_name(name: str) -> TaxonResolution:
    """The reference proteome for an organism named in words.

    Ranked by UniProt's own relevance, which puts the reference proteome
    first for a plain species name -- measured for yeast, *E. coli*, and
    *S. aureus*.

    Only a reference proteome is offered. A non-reference one is not a lesser
    answer here but an unusable one: its entries are in UniParc rather than
    UniProtKB's searchable index, so `proteome:<id>` returns no rows and the
    download writes an empty file. Returning nothing is honest; returning a
    strain the user cannot download is not.
    """
    candidates = _search_proteomes(organism_name_query(name))
    reference = next((c for c in candidates if c.is_reference), None)
    if reference is None:
        return TaxonResolution()
    return TaxonResolution(proteome=reference, needs_picker=False)


def _protein_hit(entry: dict) -> ProteinHit | None:
    accession = entry.get("primaryAccession")
    if not isinstance(accession, str):
        return None
    description = _obj(entry.get("proteinDescription"))
    recommended = _obj(description.get("recommendedName"))
    full_name = _obj(recommended.get("fullName")).get("value")
    return ProteinHit(
        accession=accession,
        entry_id=entry.get("uniProtkbId"),
        name=full_name if isinstance(full_name, str) else None,
        organism=_obj(entry.get("organism")).get("scientificName"),
        length=_obj(entry.get("sequence")).get("length"),
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
