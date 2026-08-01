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
    """
    cleaned = (name or "").strip().strip('"')
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
