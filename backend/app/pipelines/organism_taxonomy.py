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

The large/repetitive (plant) genera set was added for bwa-mem2 organism-type
presets (#132). Plants with large, repeat-heavy genomes (wheat, maize, barley,
rye, etc.) need different bwa-mem2 tuning than standard eukaryotes like human.
"""

from enum import StrEnum


class OrganismClass(StrEnum):
    """Three-way classification for organism-type preset selection.

    More granular than the binary is_eukaryotic() needed for splice-aware
    alignment: bwa-mem2 presets distinguish large/repetitive genomes (plants
    with extensive repeats, polyploids) from standard eukaryotes like human.
    """

    BACTERIA = "bacteria"
    LARGE_REPETITIVE = "large_repetitive"  # Plants with large/repetitive genomes
    EUKARYOTE = "eukaryote"  # Standard eukaryotes (human, mouse, fly, etc.)


PROKARYOTE_GENERA: frozenset[str] = frozenset({
    "escherichia", "bacillus", "staphylococcus", "streptococcus",
    "salmonella", "pseudomonas", "mycobacterium", "listeria",
    "campylobacter", "clostridium", "vibrio", "helicobacter",
    "neisseria", "klebsiella", "acinetobacter", "enterococcus",
    "lactobacillus", "borrelia", "rickettsia", "chlamydia",
})

# Genera of plants with large, repeat-heavy genomes that benefit from
# adjusted bwa-mem2 seeding heuristics. Hand-maintained, matching the
# existing PROKARYOTE_GENERA pattern. Unrecognised eukaryotes default to
# standard Eukaryote tuning, which is conservative: it won't perform as well
# on a large polyploid genome, but it will still produce correct alignments.
LARGE_REPETITIVE_GENERA: frozenset[str] = frozenset({
    # Triticeae tribe -- wheat, barley, rye
    "triticum", "hordeum", "secale", "triticale",
    # Maize and its relatives
    "zea", "tripsacum",
    # Major cereal crops
    "oryza",  # rice (moderate size ~400 Mb, but complex repeats)
    "sorghum",
    "avena",  # oat
    "pennisetum",  # pearl millet
    # Polyploid crops
    "brassica",  # rapeseed, cabbage -- multiple ploidy levels
    "gossypium",  # cotton -- polyploid
    "glycine",  # soybean -- paleopolyploid
    "arachis",  # peanut -- tetraploid
    "nicotiana",  # tobacco -- tetraploid
    "solanum",  # potato -- tetraploid; tomato is also here but smaller
    "saccharum",  # sugarcane -- complex polyploid
    # Large-conifer genomes
    "pinus", "picea", "abies",  # pines, spruce, fir -- 20+ Gb
    # Large genomes in general
    "fritillaria",  # ~1 Gb per chromosome
    "lilium",  # lily -- very large genome
    "allium",  # onion -- ~16 Gb
})


def classify_organism(organism: str | None) -> OrganismClass:
    """Classify an organism into one of three bwa-mem2 preset classes.

    Returns OrganismClass.LARGE_REPETITIVE for known large/repetitive-plant
    genera, OrganismClass.BACTERIA for known prokaryote genera, and
    OrganismClass.EUKARYOTE for everything else (including unknown genera,
    which defaults to the conservative Eukaryote preset).

    This is a superset of is_eukaryotic(): any organism that returns True
    from is_eukaryotic() may be either EUKARYOTE or LARGE_REPETITIVE here.
    """
    if not organism or not organism.strip():
        return OrganismClass.EUKARYOTE
    genus = organism.strip().split()[0].lower()
    if genus in LARGE_REPETITIVE_GENERA:
        return OrganismClass.LARGE_REPETITIVE
    if genus in PROKARYOTE_GENERA:
        return OrganismClass.BACTERIA
    return OrganismClass.EUKARYOTE


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
