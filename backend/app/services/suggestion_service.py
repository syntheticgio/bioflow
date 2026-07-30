"""Which pipelines to suggest for a file, and why.

A presentation layer over `pipeline_service`, not a second copy of its
judgement: tool selection, chemistry lookup and reference resolution all
delegate there. What lives here is the mapping from "what we know about this
file" to "what the Actions tab should offer", including the honest reason a
card cannot run.

Every card is `available` with a launch payload or `unavailable` with a
reason. There is deliberately no third state where a gated card runs its own
prerequisite -- that is DAG behaviour, and a real pipeline system will
replace it rather than inherit it.
"""

from dataclasses import dataclass
from enum import StrEnum


class CardStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


# Genus -> domain. Hand-maintained and deliberately small: `organism` is free
# text, and this only has to separate "has introns" from "does not" well
# enough to pick a short-read aligner. Matched on the first word of the name.
#
# An unrecognised genus is treated as eukaryotic (see `is_eukaryotic`), so
# this table only needs the prokaryotes it is likely to meet.
_PROKARYOTE_GENERA: frozenset[str] = frozenset({
    "escherichia", "bacillus", "staphylococcus", "streptococcus",
    "salmonella", "pseudomonas", "mycobacterium", "listeria",
    "campylobacter", "clostridium", "vibrio", "helicobacter",
    "neisseria", "klebsiella", "acinetobacter", "enterococcus",
    "lactobacillus", "borrelia", "rickettsia", "chlamydia",
})


def is_eukaryotic(organism: str | None) -> bool:
    """Whether splice-aware alignment is appropriate for this organism.

    Unrecognised and missing names default to True. The asymmetry is
    deliberate: hisat2 on an intron-free genome simply finds no junctions,
    while a non-splice-aware aligner on a genome that has them drops real
    alignments without saying so.
    """
    if not organism or not organism.strip():
        return True
    genus = organism.strip().split()[0].lower()
    return genus not in _PROKARYOTE_GENERA


@dataclass(frozen=True)
class SuggestionCard:
    """One pipeline offer.

    `launch` is `{"endpoint": str, "body": dict}` where `body` is the
    *complete* JSON body for that endpoint, assembled server-side where the
    object id and its defaults are known. The frontend posts it verbatim and
    adds nothing -- the three launch endpoints do not share a request shape
    (`/variants` keys on `bam_id`, the others on `object_id`), so anything
    the client had to merge in would be a shape it had to know about.

    `launch` and `status` must agree: an available card without a payload
    would render as a button that does nothing.
    """

    kind: str
    category: str
    title: str
    description: str
    why: str | None = None
    status: CardStatus = CardStatus.UNAVAILABLE
    reason: str | None = None
    launch: dict | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "category": self.category,
            "title": self.title,
            "description": self.description,
            "why": self.why,
            "status": self.status.value,
            "reason": self.reason,
            "launch": self.launch,
        }
