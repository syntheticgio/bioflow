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
#
# Taken from the fuller observed phrase rather than the shortest distinctive
# fragment: "duplicate id" alone is generic enough to appear in a fatal message
# about the VCF or its index, and swallowing one of those would leave a job
# reading as successful while producing no annotations. The three others are
# specific enough that a real failure is unlikely to contain them.
_BENIGN_GFF_MARKERS = (
    "unknown phase",
    "features with duplicate id",
    "unknown biotype",
    "incomplete CDS",
)

# bcftools prefixes genuine errors this way. Checked first so the classifier's
# safe direction is explicit rather than a consequence of which substrings
# happen to be listed above.
_ERROR_PREFIX = "[E::"


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
    those is a warning about a record it skipped, not about the run. Callers
    should log these at debug rather than surface them, or a successful
    annotation looks alarming.

    Substring matching against another tool's stderr is version-fragile, and
    deliberately fragile in the safe direction: if bcftools rephrases a
    message the match simply stops, the line is treated as noteworthy, and the
    user sees extra noise. Noise is recoverable; a swallowed error is not.
    """
    if line.lstrip().startswith(_ERROR_PREFIX):
        return False
    return any(marker in line for marker in _BENIGN_GFF_MARKERS)


# Suffixes a VCF may arrive with, longest first so `.vcf.gz` is stripped whole
# rather than leaving a stray `.vcf`.
_VCF_SUFFIXES = (".vcf.gz", ".vcf", ".bcf")


def annotated_name(vcf_name: str) -> str:
    """The output name for an annotated copy of `vcf_name`.

    Not `variant_runner.output_name`, which takes `Path(name).stem` because
    its input is a BAM. A `.vcf.gz` has a double extension, so the stem keeps
    the inner `.vcf` and the result is `foo.bcftools.vcf.csq.vcf.gz`.
    """
    for suffix in _VCF_SUFFIXES:
        if vcf_name.endswith(suffix):
            return f"{vcf_name[: -len(suffix)]}.csq.vcf.gz"
    return f"{vcf_name}.csq.vcf.gz"
