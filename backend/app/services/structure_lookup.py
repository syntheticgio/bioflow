"""Resolving a gene symbol to a protein structure, via UniProt.

Everything this module does is *optional*, in the same sense as
`llm_client`: the structure viewer is additive, and a UniProt outage means
the button reports no structure rather than the variants table returning an
error. Every failure path therefore returns None and logs, and none raises.

Why UniProt and not NCBI Structure, measured against the real yeast callset
before this was written:

- NCBI's Entrez text search mis-parses hyphenated gene names. `YDR524W-C`
  translates to `C[All Fields]` -- the hyphen is read as a boolean -- and
  returns 4,342 structures that merely contain a chain C.
- Even parsed correctly it matches structures that *mention* a gene rather
  than structures *of* it. `BMS1` and `FCF1` both resolve to the same
  40-subunit complex; `MYO2`'s top hit does not contain Myo2 at all.
- On 120 sampled genes it reports a 42.5% hit rate against UniProt's 35%,
  and 10 of its 51 hits are false positives.

UniProt's PDB cross-references are curated, so they say "this structure is
this protein" rather than "these strings co-occur".

The one thing a gene symbol cannot do is identify a protein. UniProt may
attach the same symbol to more than one entry, and its ranking is not always
the one wanted: `gene_exact:SRP1` returns TIR1 (254aa) ahead of importin
alpha (542aa), because TIR1 lists SRP1 as an alias. Since the caller knows
which residue the variant changes, the protein must be long enough to *have*
that residue -- which is what tells the two apart, and why this reads the
sequence length at all.

Uses stdlib urllib rather than httpx, which is a dev-only dependency here;
the call is a simple JSON GET, and it runs in a worker thread.
"""

import asyncio
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass

from app.logging import get_logger
from app.models import StructureLookup

log = get_logger(__name__)

_SEARCH_URL = "https://rest.uniprot.org/uniprotkb/search"

# Long enough for a cold UniProt response, short enough that a click does not
# appear to hang. The UI shows a resolving state for the duration.
_TIMEOUT_SECONDS = 20.0

# UniProt ranks by relevance, and the wanted entry is not always first (SRP1
# is second). A handful is enough to get past an alias collision without
# turning one lookup into a scan.
_MAX_CANDIDATES = 10

# SwissProt only. For yeast this costs nothing -- 118 of 120 sampled genes
# resolve -- and it buys curation. For an organism with no SwissProt coverage
# it will resolve almost nothing, which is the correct outcome rather than a
# regrettable one: unreviewed entries are where symbol collisions are worst,
# and a confident wrong protein is the failure this module exists to avoid.
_QUERY = "gene_exact:{gene} AND organism_id:{taxid} AND reviewed:true"


@dataclass(frozen=True)
class StructureHit:
    """A gene resolved to one protein, with whatever structures it has.

    An empty `pdb_ids` is a successful resolution, not a failure: 65% of
    resolved genes have no experimental structure, and the UI needs to say
    "no structure available" differently from "could not identify this
    protein".
    """

    accession: str
    pdb_ids: list[str]
    length: int


def _get(url: str, *, timeout: float) -> bytes:
    """The transport, isolated so tests can replace it without a network."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _pdb_ids(entry: dict) -> list[str]:
    """PDB cross-references only.

    The list holds dozens of databases -- STRING, AlphaFoldDB, GO. Only PDB
    is an experimental structure, and AlphaFoldDB in particular is
    deliberately not treated as one here: a predicted model and a solved one
    warrant different confidence, and conflating them behind one button is a
    larger design question than this module settles.
    """
    refs = entry.get("uniProtKBCrossReferences")
    if not isinstance(refs, list):
        return []
    return [
        ref["id"]
        for ref in refs
        if isinstance(ref, dict)
        and ref.get("database") == "PDB"
        and isinstance(ref.get("id"), str)
    ]


def _length(entry: dict) -> int | None:
    sequence = entry.get("sequence")
    if not isinstance(sequence, dict):
        return None
    length = sequence.get("length")
    return length if isinstance(length, int) else None


def _choose(entries: list, *, max_aa_pos: int) -> StructureHit | None:
    """The first candidate long enough to contain the variant's residue.

    Returns None rather than the first entry when nothing fits. A gene whose
    residue exceeds every candidate is a resolution failure, and guessing
    there is exactly the bug the length guard exists to prevent -- it would
    show a structure of the wrong protein with a meaningless position
    highlighted, which is worse than showing nothing.
    """
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        accession = entry.get("primaryAccession")
        if not isinstance(accession, str):
            continue
        # An entry with no length cannot be checked, and an unchecked
        # candidate is what this refuses to return.
        length = _length(entry)
        if length is None or length < max_aa_pos:
            continue
        return StructureHit(
            accession=accession, pdb_ids=_pdb_ids(entry), length=length
        )
    return None


def _cache_covers(cached: StructureLookup, *, max_aa_pos: int) -> bool:
    """Whether a stored answer was validated against a long enough protein.

    The cache key is the gene and organism, but the *answer* depends on
    `max_aa_pos` too, and ignoring that reintroduces the bug the length guard
    removes. Measured: resolving SRP1 first for a residue-100 variant stores
    TIR1 (254aa), and a later residue-428 variant would then be served TIR1
    from cache -- the wrong protein, with a position it does not have.

    A stored hit is reusable only when the resolved protein is itself long
    enough for the residue now being asked about.

    A stored *miss* is reusable only for a residue at least as large as the one
    that already failed. The direction is easy to get backwards: a miss at
    residue 900 means no candidate reached 900, which says nothing about
    residue 10 -- a shorter protein the earlier query rejected may fit it
    perfectly. So the miss stands for equal-or-larger residues and must be
    re-queried for smaller ones.
    """
    if cached.length is None:
        return False
    if cached.accession is None:
        # `length` on a miss records the residue that failed, not a protein.
        return max_aa_pos >= cached.length
    return cached.length >= max_aa_pos


async def resolve_structure(
    *, gene: str, taxid: int | None, max_aa_pos: int
) -> StructureHit | None:
    """The protein a gene names in one organism, and its structures.

    `max_aa_pos` is the highest residue position the caller needs to display.
    It is a correctness input, not a hint: a candidate shorter than this is a
    different protein that happens to share the symbol.

    None means "do not offer a structure" and covers every failure --
    unknown organism, unresolvable symbol, UniProt unreachable. They are
    distinguished in the log, not in the return type, because the UI says the
    same thing for all of them.
    """
    if taxid is None or not gene:
        # An unscoped query is a wrong answer rather than a broad one: symbols
        # collide across organisms far more often than within one.
        return None

    cached = await StructureLookup.find_one(
        StructureLookup.gene == gene, StructureLookup.taxid == taxid
    )
    if cached is not None and _cache_covers(cached, max_aa_pos=max_aa_pos):
        if cached.accession is None:
            return None
        return StructureHit(
            accession=cached.accession,
            pdb_ids=list(cached.pdb_ids),
            length=cached.length or 0,
        )

    query = urllib.parse.urlencode(
        {
            "query": _QUERY.format(gene=gene, taxid=taxid),
            "fields": "accession,xref_pdb,sequence",
            "format": "json",
            "size": str(_MAX_CANDIDATES),
        }
    )

    try:
        # A blocking socket on the event loop would stall every other request.
        raw = await asyncio.to_thread(
            _get, f"{_SEARCH_URL}?{query}", timeout=_TIMEOUT_SECONDS
        )
        results = json.loads(raw).get("results", [])
    except Exception as exc:
        # A timeout, a closed connection and an HTML error page all mean the
        # same thing to the caller, and none is worth failing a request over.
        log.info("structure_lookup_failed", gene=gene, taxid=taxid, error=str(exc))
        return None

    hit = _choose(results if isinstance(results, list) else [], max_aa_pos=max_aa_pos)

    # Misses are cached too. 65% of lookups find no structure, and an uncached
    # miss means every render of the variants table re-queries every gene that
    # will never resolve.
    await _remember(gene=gene, taxid=taxid, hit=hit, max_aa_pos=max_aa_pos)

    if hit is None:
        log.info("structure_unresolved", gene=gene, taxid=taxid, candidates=len(results))
    return hit


async def _remember(
    *, gene: str, taxid: int, hit: StructureHit | None, max_aa_pos: int
) -> None:
    """Store a result, including a negative one.

    On a hit `length` is the resolved protein's length. On a miss it is the
    residue that could not be satisfied -- which is what lets `_cache_covers`
    reuse the miss for smaller residues without reusing it for larger ones,
    where a candidate this query rejected might still fit.

    Upsert rather than insert: two rows of the same gene can reach here
    concurrently, and the unique index would turn the loser into an error over
    an answer that is already correct.
    """
    length = hit.length if hit else max_aa_pos
    await StructureLookup.find_one(
        StructureLookup.gene == gene, StructureLookup.taxid == taxid
    ).upsert(
        {
            "$set": {
                "accession": hit.accession if hit else None,
                "pdb_ids": hit.pdb_ids if hit else [],
                "length": length,
            }
        },
        on_insert=StructureLookup(
            gene=gene,
            taxid=taxid,
            accession=hit.accession if hit else None,
            pdb_ids=hit.pdb_ids if hit else [],
            length=length,
        ),
    )
