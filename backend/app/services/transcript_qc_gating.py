"""Whether the RNA-seq QC charts apply to a given BAM.

There is no hard stored answer. `pipeline_service` records that RNA-ness is
not knowable from a BAM's bytes, and while `molecule_type` now exists as a
metadata field, it is only populated when an SRA record supplied it -- zero
of the nine BAMs in a real working database carry it, while `assay` is
populated on all nine and discriminates correctly. Gating on `molecule_type`
alone would therefore ship a feature nobody could see, with a green test
suite.

So this is a fallback chain, strongest signal first, and the result carries
the reason so the UI can say what it inferred from rather than presenting a
guess as a fact.
"""

from dataclasses import dataclass

SPLICE_AWARE_ALIGNERS = {"star", "hisat2"}

# ChIP-seq is DNA, so a gene body curve says nothing -- but where its reads
# sit relative to gene structure is precisely the question being asked.
FEATURE_ONLY_ASSAYS = {"ChIP-seq", "ATAC-seq"}

# Deliberately excludes "scRNA-seq", a first-class value of the same `assay`
# enum (schemas.py). Both charts here are computed by pooling reads across
# the whole BAM with no regard for which cell they came from. For bulk
# RNA-seq that pooling is the point -- it's what produces the average
# coverage shape across a population of molecules that these metrics are
# meant to measure. For single-cell data, pooling across cells mixes each
# cell's separate capture/degradation profile into one curve that doesn't
# represent any individual library, unlike the bulk case. So scRNA-seq falls
# through to the `if assay: return _NONE` branch below and is treated as not
# applicable -- an explicit decision, not a gap in this set. See
# test_single_cell_rnaseq_assay_does_not_apply.
RNA_ASSAYS = {"RNA-seq"}


@dataclass
class Applicability:
    gene_body: bool
    feature_distribution: bool
    #: Which signal decided it: "molecule_type", "assay", "aligner", or None.
    reason: str | None


_NONE = Applicability(gene_body=False, feature_distribution=False, reason=None)


def applicability(obj: dict) -> Applicability:
    """Decide from metadata and facts, strongest signal first."""
    metadata = obj.get("metadata") or {}
    facts = obj.get("facts") or {}

    molecule_type = metadata.get("molecule_type")
    if molecule_type == "RNA":
        return Applicability(True, True, "molecule_type")
    if molecule_type in {"DNA", "Other"}:
        # An explicit answer outranks every inference below. STAR is used for
        # DNA in some workflows, so the aligner must not override this.
        return _NONE

    assay = metadata.get("assay")
    if assay in RNA_ASSAYS:
        return Applicability(True, True, "assay")
    if assay in FEATURE_ONLY_ASSAYS:
        return Applicability(False, True, "assay")
    if assay:
        # A stated non-RNA assay (WGS, WES, ...) is an answer too.
        return _NONE

    if str(facts.get("aligned_by") or "").lower() in SPLICE_AWARE_ALIGNERS:
        # Weakest signal: a splice-aware aligner is evidence, not proof.
        return Applicability(True, True, "aligner")

    return _NONE
