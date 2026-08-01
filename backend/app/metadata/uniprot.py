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
