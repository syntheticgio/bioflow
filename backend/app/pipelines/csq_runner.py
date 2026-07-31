"""Running `bcftools csq` to add consequence annotations to a VCF.

The command is small; the two decisions in it are not, and both were settled by
running the tool against the real yeast callset rather than from the manual.
"""

from pathlib import Path

# How to treat unphased heterozygous genotypes.
#
# `csq` defaults to "r", which *requires* phasing and exits 255 on the first
# unphased het -- and `bcftools call`, which produced every VCF this app
# annotates, emits unphased genotypes. So the default is not merely suboptimal
# here, it fails every run.
#
# Measured on DRR1066343.bcftools.vcf.gz (6,641 variants):
#     -p r  exit 255, aborts
#     -p a  4,152 annotated
#     -p m  4,149 annotated
#     -p s  3,355 annotated
#
# "a" takes genotypes as they are and creates haplotypes regardless of phase.
# That is an arbitrary phase, which is the honest choice for data carrying
# none, and it only affects haplotype-aware calls across adjacent variants.
# "s" was rejected for silently dropping ~800 heterozygous sites: a quietly
# incomplete table is worse than an approximate one.
PHASE_MODE = "a"

# Substrings of the lines real NCBI GFF3 files produce on a normal run. The
# T. brucei annotation emits all of these and still annotates correctly.
_BENIGN_GFF_MARKERS = (
    "unknown phase",
    "duplicate id",
    "unknown biotype",
    "incomplete CDS",
)


def build_csq_command(
    *,
    bcftools_path: str,
    vcf: Path,
    reference: Path,
    annotation: Path,
    out: Path,
) -> list[str]:
    """`bcftools csq` over one VCF, writing a bgzipped VCF.

    The reference needs an accompanying `.fai`; callers stage that the same way
    the alignment and variant runners already do.
    """
    return [
        bcftools_path,
        "csq",
        "-f",
        str(reference),
        "-g",
        str(annotation),
        "-p",
        PHASE_MODE,
        "-O",
        "z",
        "-o",
        str(out),
        str(vcf),
    ]


def is_benign_gff_warning(line: str) -> bool:
    """Whether a stderr line is ordinary GFF3 noise rather than a failure.

    Real NCBI annotations are not clean by bcftools' standards -- partial
    features, duplicate ids, tRNA biotypes it does not model. Every one of
    those is a warning about a record it skipped, not about the run. Logged at
    debug rather than surfaced, or a successful annotation would look alarming.
    """
    return any(marker in line for marker in _BENIGN_GFF_MARKERS)
