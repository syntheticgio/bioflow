"""Running SnpEff for variant consequence annotation.

SnpEff is a self-contained Java-based annotator that builds its own database
from a GFF3/GTF and reference genome, then writes per-variant consequences
into the VCF's ANN field.

Database build step (inside Docker):
    snpEff build -gff3 -v -dataDir <dir> <genome_name> -noCheckCds -noCheckProtein

Annotation step (inside Docker):
    snpEff ann -dataDir <dir> -noLog -noStats <genome_name> input.vcf > output.vcf

The pegi3s/snpeff Docker image wraps the Java invocation in a `snpEff` entrypoint
script (capital E in the binary name). The container mounts bioinfo_home so that
the reference, GFF3, and SnpEff data directory are all accessible from within.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

# SnpEff impact severity levels, highest first.
# Used for ranking when multiple consequences affect the same variant.
_IMPACT_ORDER = ("HIGH", "MODERATE", "LOW", "MODIFIER")
_IMPACT_RANK = {name: i for i, name in enumerate(_IMPACT_ORDER)}

# Mapping from SnpEff consequence types to bcftools csq consequence names.
# SnpEff appends "_variant" to most types where bcftools csq does not.
# Unknown types are passed through as-is with a rank between MODERATE and LOW.
_SNPEFF_TO_CSQ: dict[str, str] = {
    "chromosome_number": "intergenic",
    "exon_loss_variant": "coding_sequence",
    "frameshift_variant": "frameshift",
    "stop_gained": "stop_gained",
    "stop_lost": "stop_lost",
    "start_lost": "start_lost",
    "splice_acceptor_variant": "splice_acceptor",
    "splice_donor_variant": "splice_donor",
    "splice_region_variant": "splice_region",
    "inframe_deletion": "inframe_deletion",
    "inframe_insertion": "inframe_insertion",
    "missense_variant": "missense",
    "synonymous_variant": "synonymous",
    "stop_retained_variant": "stop_retained",
    "start_retained_variant": "start_retained",
    "5_prime_UTR_variant": "5_prime_utr",
    "3_prime_UTR_variant": "3_prime_utr",
    "non_coding_transcript_exon_variant": "non_coding",
    "intron_variant": "intron",
    "intergenic_region": "intergenic",
    "upstream_gene_variant": "intergenic",
    "downstream_gene_variant": "intergenic",
    # Additional SnpEff types not in bcftools csq's vocabulary
    "conservative_inframe_deletion": "inframe_deletion",
    "conservative_inframe_insertion": "inframe_insertion",
    "disruptive_inframe_deletion": "inframe_deletion",
    "disruptive_inframe_insertion": "inframe_insertion",
    "rare_amino_acid_variant": "missense",
    "initiator_codon_variant": "start_lost",
    "stop_gained__conservative_inframe_deletion": "stop_gained",
    "stop_gained__conservative_inframe_insertion": "stop_gained",
    "stop_gained__disruptive_inframe_deletion": "stop_gained",
    "stop_gained__disruptive_inframe_insertion": "stop_gained",
}


def _csq_rank(consequence: str) -> float:
    """Rank a consequence for severity comparison.

    Maps SnpEff names to our csq severity ordering. Unknown types rank between
    MODERATE and LOW (impact-based), above synonymous but below missense.
    """
    from app.pipelines.csq_parse import _RANK as csq_ranks

    mapped = _SNPEFF_TO_CSQ.get(consequence, consequence)
    return csq_ranks.get(mapped, 9.5)


# Position in the AA change string — "160K>160M" and "99P" both start with
# the residue number.
_AA_POS = re.compile(r"^(\d+)")


@dataclass(frozen=True)
class Consequence:
    """One variant's effect, compatible with csq_parse.Consequence."""

    consequence: str
    gene: str | None
    transcript: str | None
    aa_change: str | None
    aa_pos: int | None
    impact: str | None  # HIGH, MODERATE, LOW, MODIFIER
    hgvs_c: str | None  # Coding-level HGVS notation
    hgvs_p: str | None  # Protein-level HGVS notation
    compound: bool = False
    additional: int = 0

    def to_csq_consequence(self):
        """Convert to csq_parse.Consequence for compatibility.

        Drops SnpEff-specific fields (impact, hgvs) to match the common
        interface consumers expect.
        """
        from app.pipelines.csq_parse import Consequence as CsqConsequence

        return CsqConsequence(
            consequence=self.consequence,
            gene=self.gene,
            transcript=self.transcript,
            aa_change=self.aa_change,
            aa_pos=self.aa_pos,
            compound=self.compound,
            additional=self.additional,
        )


def _parse_ann_field(fields: list[str]) -> Consequence | None:
    """Parse one ANN entry into a Consequence.

    ANN format (0-indexed):
        0: Allele
        1: Annotation (consequence type)
        2: Annotation_Impact (HIGH/MODERATE/LOW/MODIFIER)
        3: Gene_Name
        4: Gene_ID
        5: Feature_Type
        6: Feature_ID
        7: Transcript_BioType
        8: Rank (exon/intron number)
        9: HGVS.c
       10: HGVS.p
       11: cDNA.pos
       12: CDS.pos
       13: AA.pos
       14: Distance
       15: ERRORS
       16: WARNINGS
    """
    if len(fields) < 2:
        return None

    kind = fields[1]
    if not kind or kind == "?":
        return None

    impact = fields[2] if len(fields) > 2 else None
    gene_name = fields[3] if len(fields) > 3 and fields[3] else None
    feature_id = fields[6] if len(fields) > 6 and fields[6] else None
    hgvs_c = fields[9] if len(fields) > 9 and fields[9] else None
    hgvs_p = fields[10] if len(fields) > 10 and fields[10] else None
    aa_change = fields[13] if len(fields) > 13 and fields[13] else None

    aa_pos = None
    if aa_change:
        match = _AA_POS.match(aa_change)
        if match:
            aa_pos = int(match.group(1))

    return Consequence(
        consequence=kind,
        gene=gene_name,
        transcript=feature_id,
        aa_change=aa_change,
        aa_pos=aa_pos,
        impact=impact,
        hgvs_c=hgvs_c,
        hgvs_p=hgvs_p,
    )


def parse_ann(value: str | None) -> Consequence | None:
    """The most severe consequence in an ANN field, or None.

    None is an ordinary answer, not a failure: variants with no annotated
    consequence simply have no ANN entry.
    """
    if not value:
        return None
    text = value.strip()
    if not text or text == ".":
        return None

    # ANN values are comma-separated, one per transcript
    parsed: list[Consequence] = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        fields = item.split("|")
        one = _parse_ann_field(fields)
        if one is not None:
            parsed.append(one)

    if not parsed:
        return None

    # Rank by severity: first by impact (HIGH > MODERATE > LOW > MODIFIER),
    # then by our csq severity ordering within the same impact tier.
    def sort_key(c: Consequence) -> tuple:
        impact_rank = _IMPACT_RANK.get(c.impact, 99)
        csq_r = _csq_rank(c.consequence)
        return (impact_rank, csq_r)

    best = min(parsed, key=sort_key)
    if len(parsed) == 1:
        return best
    return Consequence(
        consequence=best.consequence,
        gene=best.gene,
        transcript=best.transcript,
        aa_change=best.aa_change,
        aa_pos=best.aa_pos,
        impact=best.impact,
        hgvs_c=best.hgvs_c,
        hgvs_p=best.hgvs_p,
        compound=best.compound,
        additional=len(parsed) - 1,
    )


def genome_dir(accession: str) -> Path:
    """The directory where SnpEff stores its database for this genome.

    SnpEff expects databases in <data_dir>/data/<genome_name>/.
    """
    return settings.snpeff_data_dir / "data" / accession


def database_exists(accession: str) -> bool:
    """Whether a SnpEff database has been built for this genome accession."""
    return (genome_dir(accession) / "snpEffectPredictor.bin").exists()


def build_db_command(
    image: str,
    genome_name: str,
    *,
    container_root: str | None = None,
    host_root: str | None = None,
) -> list[str]:
    """Build a SnpEff database from a GFF3 and reference genome.

    Before calling this command, the handler must stage the reference FASTA
    and GFF3 into the expected locations:
        <snpeff_data_dir>/data/<genome_name>/genome.fa
        <snpeff_data_dir>/data/<genome_name>/genes.gff

    SnpEff reads these files to build its own binary predictor database,
    stored in <data_dir>/data/<genome_name>/snpEffectPredictor.bin.

    Returns a `docker run` command list. bioinfo_home is mounted into the
    container so the SnpEff data directory is accessible.

    The `-noCheckCds` and `-noCheckProtein` flags skip validation of CDS
    integrity and protein translation, which are unnecessary for annotation
    and can fail on incomplete annotations.
    """
    from app.pipelines.variant_runner import host_path_for

    host_root_path = host_path_for(
        container_root if container_root is not None else str(settings.bioinfo_home),
        container_root=container_root,
        host_root=host_root,
    )
    mount_at = container_root if container_root is not None else str(settings.bioinfo_home)

    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{host_root_path}:{mount_at}",
        image,
        "snpEff",
        "build",
        "-gff3",
        "-v",
        "-dataDir",
        str(settings.snpeff_data_dir),
        "-noCheckCds",
        "-noCheckProtein",
        genome_name,
    ]


def annotate_command(
    image: str,
    genome_name: str,
    input_vcf: str,
    output_vcf: str,
    *,
    container_root: str | None = None,
    host_root: str | None = None,
) -> list[str]:
    """Run SnpEff annotation on a VCF.

    Writes annotated VCF to output_vcf. The ANN field contains the
    consequence annotations.

    Returns a `docker run` command list. bioinfo_home is mounted into the
    container so the input VCF and output path are accessible.

    The `-noLog` flag suppresses SnpEff's log file, and `-noStats` skips
    the HTML stats report (not useful in this context).
    """
    from app.pipelines.variant_runner import host_path_for

    host_root_path = host_path_for(
        container_root if container_root is not None else str(settings.bioinfo_home),
        container_root=container_root,
        host_root=host_root,
    )
    mount_at = container_root if container_root is not None else str(settings.bioinfo_home)

    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{host_root_path}:{mount_at}",
        image,
        "snpEff",
        "ann",
        "-dataDir",
        str(settings.snpeff_data_dir),
        "-noLog",
        "-noStats",
        genome_name,
        input_vcf,
    ]


# Benign stderr messages from SnpEff's database build step.
# These are informational and not failures.
_BENIGN_BUILD_MARKERS = (
    "Warning: File",
    "Warning: Could not find",
    "It is highly recommended",
)

# SnpEff prefixes genuine errors with "Error:".
_ERROR_PREFIX = "Error:"


def is_benign_build_warning(line: str) -> bool:
    """Whether a stderr line from `snpEff build` is routine noise.

    SnpEff warns about missing cDNA sequences, deprecated GFF tags, and
    other non-fatal issues during database building. These should be logged
    at debug rather than surfaced as warnings.
    """
    if line.lstrip().startswith(_ERROR_PREFIX):
        return False
    return any(marker in line for marker in _BENIGN_BUILD_MARKERS)


# Suffixes a VCF may arrive with, longest first.
_VCF_SUFFIXES = (".vcf.gz", ".vcf", ".bcf")


def annotated_name(vcf_name: str) -> str:
    """The output name for an SnpEff-annotated copy of `vcf_name`.

    Mirrors csq_runner.annotated_name but with a distinct suffix to avoid
    naming collisions when both annotators are used on the same VCF.
    """
    for suffix in _VCF_SUFFIXES:
        if vcf_name.endswith(suffix):
            return f"{vcf_name[: -len(suffix)]}.snpeff.vcf.gz"
    return f"{vcf_name}.snpeff.vcf.gz"
