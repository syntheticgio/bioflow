"""Which compleasm lineage to score an assembly against, from organism
metadata BioFlow already has.

Not autolineage. compleasm's own `--autolineage` answers this by downloading
several candidate lineage datasets and placing the assembly phylogenetically
among them -- accurate, but the expensive way to answer a question this
application mostly already knows: `metadata["organism"]` is populated by SRA,
NCBI assembly and UniProt enrichment before assembly QC ever runs. Choosing
from that avoids the extra downloads and the extra dependency (`--autolineage`
needs `sepp`, which this image does not install -- see
install-compleasm.sh).

The mapping is deliberately shallow. compleasm ships lineages from domain
(`bacteria_odb12`) down to genus (`escherichia` has none, but `lactobacillus`
does) and beyond -- 1192 remote names in the 0.2.2 dataset, checked on
2026-08-02. Matching all of them from a free-text organism string is not
worth attempting: a broad, always-available domain lineage is honest and
correct, if less specific than a perfect genus match would be. The overrides
below cover organisms actually seen in this project's test fixtures and
library, not an attempt at completeness.
"""

from app.pipelines.organism_taxonomy import is_eukaryotic

# Genus/family -> a more specific lineage than the domain fallback would give,
# for organisms common enough in sequencing work to be worth naming. Bare
# names, never suffixed with an OrthoDB version -- see completeness_runner's
# CompletenessParams for why a suffix here would be silently discarded by
# compleasm's own download_lineage.
#
# Verified present in the real `compleasm list --remote --odb odb12` output
# on 2026-08-02, not assumed from taxonomic knowledge alone: several
# plausible genus names (escherichia among them) have no dedicated lineage at
# all, and guessing one would fail at download time instead of at inference
# time.
_GENUS_LINEAGE: dict[str, str] = {
    "saccharomyces": "saccharomycetaceae",
    "mycobacterium": "mycobacteriaceae",
    "streptomyces": "streptomyces",
    "bacillus": "bacillus",
    "escherichia": "enterobacterales",
    "salmonella": "enterobacterales",
    "pseudomonas": "pseudomonadales",
    "staphylococcus": "staphylococcaceae",
    "lactobacillus": "lactobacillaceae",
    "vibrio": "vibrionales",
}

# Broad fallback when no genus-level override applies. `eukaryota`/`bacteria`
# always exist as lineages (checked against the same remote listing), so this
# never fails to name something -- it only ever fails to name something
# specific.
_EUKARYOTE_DEFAULT = "eukaryota"
_PROKARYOTE_DEFAULT = "bacteria"


def infer_lineage(organism: str | None) -> str | None:
    """The best compleasm lineage name for this organism, or None.

    None means "cannot even guess a domain" -- an organism string BioFlow
    could not parse at all, as opposed to one it parsed but has no specific
    override for (which still returns the domain fallback). The caller
    distinguishes these: no organism and no override is a normal case for an
    uploaded assembly, and the dialog should ask rather than silently score
    against a guessed domain that might be wrong in the more consequential
    direction (a eukaryotic assembly scored as bacteria reports every real
    gene as "missing").
    """
    if not organism or not organism.strip():
        return None
    genus = organism.strip().split()[0].lower()
    if genus in _GENUS_LINEAGE:
        return _GENUS_LINEAGE[genus]
    return _EUKARYOTE_DEFAULT if is_eukaryotic(organism) else _PROKARYOTE_DEFAULT


def is_specific(lineage: str) -> bool:
    """Whether this is a genus/family-level lineage rather than a domain
    fallback -- for a dialog that wants to say "inferred from organism" with
    appropriate confidence rather than presenting a guessed domain as if it
    were a precise match."""
    return lineage not in (_EUKARYOTE_DEFAULT, _PROKARYOTE_DEFAULT)
