"""Genus-to-domain classification, shared by aligner choice and lineage
inference.

Was `suggestion_service._PROKARYOTE_GENERA` alone, for `is_eukaryotic`'s use
in choosing a splice-aware aligner. Moved here rather than duplicated when
compleasm's lineage inference needed the same genus recognition: `suggestion_
service` is in `app/services`, and a `app/pipelines` module importing from it
would invert the direction every other import in this codebase already
runs -- pipelines is the lower layer, services the higher one.

Hand-maintained and deliberately small: `organism` is free text, and this
only has to separate "eukaryote" from "prokaryote" well enough for two
narrow decisions (splice-aware alignment; a broad compleasm lineage when
nothing more specific is known). An unrecognised genus defaults to
eukaryotic in both call sites -- see `is_eukaryotic` and
`lineage_inference.infer_lineage`.
"""

PROKARYOTE_GENERA: frozenset[str] = frozenset({
    "escherichia", "bacillus", "staphylococcus", "streptococcus",
    "salmonella", "pseudomonas", "mycobacterium", "listeria",
    "campylobacter", "clostridium", "vibrio", "helicobacter",
    "neisseria", "klebsiella", "acinetobacter", "enterococcus",
    "lactobacillus", "borrelia", "rickettsia", "chlamydia",
})


def is_eukaryotic(organism: str | None) -> bool:
    """Whether this organism is a eukaryote, by genus.

    Unrecognised and missing names default to True. `suggestion_service`
    relies on this asymmetry for aligner choice (a non-splice-aware aligner
    on a genome that has introns drops real alignments without saying so);
    `lineage_inference` relies on it for the same reason a wrong-domain
    lineage would silently score everything as missing rather than fail.
    """
    if not organism or not organism.strip():
        return True
    genus = organism.strip().split()[0].lower()
    return genus not in PROKARYOTE_GENERA
