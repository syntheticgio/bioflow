"""Per-format metadata field definitions.

These are *suggestions, not restrictions*. The schema drives a sensible form in
the UI and gives values a declared type so they can be searched and compared —
but arbitrary keys remain allowed, because no fixed vocabulary survives contact
with a real lab. A field the schema has never heard of is stored as-is, and
merely does not get a nice input widget.

Validation follows the same principle: a value that does not match its declared
type produces a *warning* and is still stored. Refusing to record what someone
typed loses information; telling them it looks wrong does not.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from app.models import FormatKind, ObjectRole


class FieldType(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    ENUM = "enum"
    DATE = "date"


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    type: FieldType = FieldType.TEXT
    options: tuple[str, ...] = ()
    unit: str | None = None
    help: str | None = None
    group: str = "General"
    # Marks fields worth prompting for. Nothing is ever *required*: a file with
    # incomplete metadata is still a file we must not refuse to store.
    suggested: bool = False

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type.value,
            "options": list(self.options),
            "unit": self.unit,
            "help": self.help,
            "group": self.group,
            "suggested": self.suggested,
        }


# --- Fields that apply to any file -----------------------------------------

COMMON_FIELDS: tuple[FieldDef, ...] = (
    FieldDef(
        "sample_id",
        "Sample ID",
        help="Your identifier for the biological sample this file came from.",
        group="Sample",
        suggested=True,
    ),
    FieldDef(
        "subject_id",
        "Subject / Patient ID",
        help="De-identified subject code. Avoid names or MRNs.",
        group="Sample",
        suggested=True,
    ),
    FieldDef(
        "organism",
        "Organism",
        type=FieldType.ENUM,
        options=(
            "Homo sapiens",
            "Mus musculus",
            "Rattus norvegicus",
            "Danio rerio",
            "Drosophila melanogaster",
            "Caenorhabditis elegans",
            "Saccharomyces cerevisiae",
            "Escherichia coli",
            "Other",
        ),
        group="Sample",
        suggested=True,
    ),
    FieldDef(
        "tissue",
        "Tissue / Source",
        help="e.g. blood, tumour biopsy, cell line name.",
        group="Sample",
        suggested=True,
    ),
    FieldDef(
        "condition",
        "Condition",
        help="Experimental group, phenotype, or treatment arm.",
        group="Sample",
    ),
    FieldDef("sex", "Sex", type=FieldType.ENUM,
             options=("female", "male", "unknown"), group="Sample"),
    FieldDef("collection_date", "Collection date", type=FieldType.DATE, group="Sample"),
    FieldDef(
        "assay",
        "Assay",
        type=FieldType.ENUM,
        options=("WGS", "WES", "RNA-seq", "scRNA-seq", "ATAC-seq", "ChIP-seq",
                 "Bisulfite-seq", "Amplicon", "Targeted panel", "Other"),
        group="Experiment",
        suggested=True,
    ),
    FieldDef("batch", "Batch", help="Sequencing or processing batch.",
             group="Experiment"),
    FieldDef("notes", "Notes", group="Experiment"),

    # --- Public archive accessions ---
    # Entering a run or experiment accession here and re-ingesting triggers a
    # lookup against NCBI, which is the escape hatch for files whose names do
    # not carry the accession.
    FieldDef(
        "sra_run",
        "SRA run",
        help="e.g. SRR11768093. Set this and re-ingest to pull metadata from NCBI.",
        group="Archive",
        suggested=True,
    ),
    FieldDef(
        "sra_experiment",
        "SRA experiment",
        help="e.g. SRX8321150. Used if no run accession is set.",
        group="Archive",
    ),
    FieldDef("sra_sample", "SRA sample", group="Archive"),
    FieldDef("sra_study", "SRA study", group="Archive"),
    FieldDef("bioproject", "BioProject", help="e.g. PRJNA631678.", group="Archive"),
    FieldDef("biosample", "BioSample", help="e.g. SAMN14886310.", group="Archive"),
    FieldDef("study_title", "Study title", group="Archive"),
)


# --- Format-specific additions ---------------------------------------------

FASTQ_FIELDS: tuple[FieldDef, ...] = (
    FieldDef(
        "library_prep",
        "Library prep",
        type=FieldType.ENUM,
        options=("TruSeq", "Nextera", "NEBNext", "KAPA", "SMART-seq", "10x", "Other"),
        group="Library",
        suggested=True,
    ),
    FieldDef("library_id", "Library ID", group="Library"),
    FieldDef(
        "read_type",
        "Read type",
        type=FieldType.ENUM,
        options=("single-end", "paired-end"),
        group="Library",
        suggested=True,
    ),
    FieldDef("mate", "Mate", type=FieldType.ENUM, options=("R1", "R2", "index"),
             help="Usually inferred from the filename; override if wrong.",
             group="Library"),
    FieldDef("insert_size", "Insert size", type=FieldType.INTEGER, unit="bp",
             group="Library"),
    FieldDef(
        "platform",
        "Sequencing platform",
        type=FieldType.ENUM,
        options=("Illumina NovaSeq", "Illumina NextSeq", "Illumina MiSeq",
                 "Illumina HiSeq", "Oxford Nanopore", "PacBio", "Element", "Other"),
        group="Sequencing",
        suggested=True,
    ),
    FieldDef("run_id", "Run ID", group="Sequencing"),
    FieldDef("flowcell", "Flowcell", group="Sequencing"),
    FieldDef("lane", "Lane", type=FieldType.INTEGER, group="Sequencing"),
)

ALIGNMENT_FIELDS: tuple[FieldDef, ...] = (
    FieldDef(
        "reference_build",
        "Reference build",
        type=FieldType.ENUM,
        options=("GRCh38", "GRCh37/hg19", "T2T-CHM13", "GRCm39", "GRCm38/mm10", "Other"),
        help="Which genome build the reads were aligned to. Mixing builds is a "
             "common and costly mistake, so it is worth recording explicitly.",
        group="Alignment",
        suggested=True,
    ),
    FieldDef(
        "aligner",
        "Aligner",
        type=FieldType.ENUM,
        options=("BWA-MEM", "BWA-MEM2", "Bowtie2", "STAR", "HISAT2", "minimap2",
                 "DRAGEN", "Other"),
        group="Alignment",
        suggested=True,
    ),
    FieldDef("aligner_version", "Aligner version", group="Alignment"),
    FieldDef("duplicates_marked", "Duplicates marked", type=FieldType.BOOLEAN,
             group="Alignment"),
    FieldDef("bqsr_applied", "BQSR applied", type=FieldType.BOOLEAN,
             help="Base quality score recalibration.", group="Alignment"),
    FieldDef("mean_coverage", "Mean coverage", type=FieldType.NUMBER, unit="x",
             group="Quality"),
    FieldDef("percent_mapped", "Mapped reads", type=FieldType.NUMBER, unit="%",
             group="Quality"),
)

VARIANT_FIELDS: tuple[FieldDef, ...] = (
    FieldDef(
        "reference_build",
        "Reference build",
        type=FieldType.ENUM,
        options=("GRCh38", "GRCh37/hg19", "T2T-CHM13", "GRCm39", "GRCm38/mm10", "Other"),
        group="Variants",
        suggested=True,
    ),
    FieldDef(
        "variant_caller",
        "Variant caller",
        type=FieldType.ENUM,
        options=("GATK HaplotypeCaller", "GATK Mutect2", "DeepVariant", "FreeBayes",
                 "bcftools", "Strelka2", "DRAGEN", "Other"),
        group="Variants",
        suggested=True,
    ),
    FieldDef("caller_version", "Caller version", group="Variants"),
    FieldDef(
        "variant_type",
        "Variant type",
        type=FieldType.ENUM,
        options=("germline", "somatic", "joint-genotyped", "structural", "CNV"),
        group="Variants",
        suggested=True,
    ),
    FieldDef("filtered", "Filtered", type=FieldType.BOOLEAN,
             help="Whether low-quality calls have already been removed.",
             group="Variants"),
    FieldDef("annotated_with", "Annotation", help="e.g. VEP, SnpEff, ANNOVAR.",
             group="Variants"),
)

REFERENCE_FIELDS: tuple[FieldDef, ...] = (
    # Free text rather than an enum: reference builds are open-ended, including
    # custom assemblies and patch releases that no fixed list would cover.
    FieldDef("reference_build", "Build", group="Reference", suggested=True),
    FieldDef("source", "Source", help="e.g. Ensembl release 110, UCSC, NCBI RefSeq.",
             group="Reference", suggested=True),
    FieldDef("assembly_accession", "Assembly accession",
             help="e.g. GCA_000001405.29. The unambiguous identifier for this assembly.",
             group="Reference", suggested=True),
    FieldDef("is_primary_assembly", "Primary assembly only", type=FieldType.BOOLEAN,
             help="Alt and patch contigs excluded. Mixing this up is a common "
                  "source of surprising alignment results.",
             group="Reference", suggested=True),
    FieldDef("has_decoy", "Includes decoy contigs", type=FieldType.BOOLEAN,
             help="e.g. hs38d1. Affects aligner choice and mapping rates.",
             group="Reference", suggested=True),
    FieldDef("index_types", "Aligner indexes",
             help="Which indexes have been built, e.g. BWA, bowtie2, STAR.",
             group="Reference", suggested=True),
    FieldDef("masked", "Masked", type=FieldType.BOOLEAN,
             help="Repeat-masked sequence.", group="Reference"),

    # Filled by the NCBI assembly lookup rather than by hand, so none are
    # suggested -- they appear once enrichment has run.
    FieldDef("tax_id", "NCBI taxonomy ID", type=FieldType.INTEGER,
             help="e.g. 9606 for human. Set from the assembly record.",
             group="Reference"),
    FieldDef("assembly_level", "Assembly level", type=FieldType.ENUM,
             options=("Complete Genome", "Chromosome", "Scaffold", "Contig"),
             help="How finished the assembly is.", group="Reference"),
    FieldDef("assembly_date", "Release date", type=FieldType.DATE,
             help="When NCBI published this assembly.", group="Reference"),
    FieldDef("paired_accession", "Paired accession",
             help="The GenBank counterpart of a RefSeq assembly, or vice versa.",
             group="Reference"),
)

INTERVAL_FIELDS: tuple[FieldDef, ...] = (
    FieldDef("reference_build", "Reference build", group="Intervals", suggested=True),
    FieldDef("interval_type", "Interval type", type=FieldType.ENUM,
             options=("capture targets", "exons", "genes", "regions of interest",
                      "blacklist", "Other"),
             group="Intervals"),
    FieldDef("source", "Source", group="Intervals"),
)


FORMAT_FIELDS: dict[FormatKind, tuple[FieldDef, ...]] = {
    FormatKind.FASTQ: FASTQ_FIELDS,
    FormatKind.BAM: ALIGNMENT_FIELDS,
    FormatKind.SAM: ALIGNMENT_FIELDS,
    FormatKind.CRAM: ALIGNMENT_FIELDS,
    FormatKind.VCF: VARIANT_FIELDS,
    FormatKind.BCF: VARIANT_FIELDS,
    # A FASTA is no longer assumed to be a reference -- that now comes from the
    # object's role, so a FASTA of reads is not asked reference questions.
    FormatKind.FASTA: (),
    FormatKind.BED: INTERVAL_FIELDS,
    FormatKind.GFF: INTERVAL_FIELDS,
    FormatKind.GTF: INTERVAL_FIELDS,
}

# Keyed by role rather than format, and consulted first: see fields_for. Kept as
# a dict so a new role-specific field group is a one-line entry that both
# fields_for and all_known_fields pick up automatically.
ROLE_FIELDS: dict[ObjectRole, tuple[FieldDef, ...]] = {
    ObjectRole.REFERENCE: REFERENCE_FIELDS,
}

# Roles that deliberately have no field group of their own and defer to the
# format's. Trimmed reads are still reads: they want the same library-prep and
# platform questions a raw FASTQ gets, and answering them twice with different
# vocabularies would be worse than answering them once.
#
# ALIGNMENT is here for the same reason. A BAM this pipeline produced and a BAM
# someone uploaded describe the same biology and deserve the same questions --
# the role records that the provenance is known, which is a fact about where
# the file came from rather than a reason to ask about it differently.
#
# Listed explicitly rather than left implicit so that a role added without
# thought still fails the "every role is accounted for" test.
FORMAT_DERIVED_ROLES: frozenset[ObjectRole] = frozenset(
    {ObjectRole.TRIMMED_READS, ObjectRole.ALIGNMENT}
)


def fields_for(
    kind: FormatKind | str | None, role: ObjectRole | str | None = None
) -> list[FieldDef]:
    """Common fields plus anything specific to this file.

    A role with its own field group wins outright over format: once a file is
    declared a reference, its library and sequencing fields are noise rather
    than context. A role *without* one falls back to the format's fields --
    trimmed reads are still reads, and should still be asked about library
    prep. Format-specific definitions win on key collisions with common ones.
    """
    if isinstance(kind, str):
        try:
            kind = FormatKind(kind)
        except ValueError:
            kind = None

    if isinstance(role, str):
        try:
            role = ObjectRole(role)
        except ValueError:
            role = None

    specific: tuple[FieldDef, ...] = ROLE_FIELDS.get(role, ()) if role else ()
    if not specific:
        specific = FORMAT_FIELDS.get(kind, ()) if kind else ()

    by_key: dict[str, FieldDef] = {f.key: f for f in COMMON_FIELDS}
    by_key.update({f.key: f for f in specific})
    return list(by_key.values())


def field_map(
    kind: FormatKind | str | None = None, role: ObjectRole | str | None = None
) -> dict[str, FieldDef]:
    return {f.key: f for f in fields_for(kind, role)}


def all_known_fields() -> dict[str, FieldDef]:
    """Every field across every format and role, for validating unscoped edits."""
    out: dict[str, FieldDef] = {f.key: f for f in COMMON_FIELDS}
    for group in FORMAT_FIELDS.values():
        for f in group:
            out.setdefault(f.key, f)
    # Role-specific fields are not reachable through FORMAT_FIELDS, so without
    # this they would be treated as unknown keys and skip coercion.
    for group in ROLE_FIELDS.values():
        for f in group:
            out.setdefault(f.key, f)
    return out


# --- Coercion and validation ------------------------------------------------


@dataclass
class ValidationResult:
    values: dict = field(default_factory=dict)
    warnings: list[dict] = field(default_factory=list)


def coerce_and_validate(
    metadata: dict,
    kind: FormatKind | str | None = None,
    role: ObjectRole | str | None = None,
) -> ValidationResult:
    """Coerce values to their declared types, warning rather than rejecting.

    A number typed into a text box should be stored as a number so it sorts and
    compares correctly. But a value that will not coerce is still kept: losing
    what someone typed is worse than storing it in the wrong type.
    """
    # all_known_fields covers keys left over from a previous role or format so
    # they still coerce correctly; the scoped definitions must win on
    # collisions, or a reference's free-text reference_build would be validated
    # against the alignment enum.
    known = {**all_known_fields(), **field_map(kind, role)}

    result = ValidationResult()
    for key, raw in metadata.items():
        spec = known.get(key)
        if spec is None:
            # Unknown key: stored verbatim, by design.
            result.values[key] = raw
            continue

        value, warning = _coerce(spec, raw)
        result.values[key] = value
        if warning:
            result.warnings.append({"key": key, "message": warning})

    return result


def _coerce(spec: FieldDef, raw):
    if raw is None or raw == "":
        return None, None

    try:
        if spec.type is FieldType.INTEGER:
            return int(str(raw).strip()), None
        if spec.type is FieldType.NUMBER:
            return float(str(raw).strip()), None
        if spec.type is FieldType.BOOLEAN:
            if isinstance(raw, bool):
                return raw, None
            s = str(raw).strip().lower()
            if s in ("true", "yes", "y", "1"):
                return True, None
            if s in ("false", "no", "n", "0"):
                return False, None
            return raw, f"{spec.label}: expected yes/no, kept {raw!r} as text"
        if spec.type is FieldType.DATE:
            return _coerce_date(raw), None
        if spec.type is FieldType.ENUM:
            s = str(raw).strip()
            if spec.options and s not in spec.options:
                # Kept: lab vocabularies always outgrow a fixed list.
                return s, (
                    f"{spec.label}: {s!r} is not one of the suggested options; "
                    "stored anyway"
                )
            return s, None
    except (TypeError, ValueError):
        return raw, f"{spec.label}: expected {spec.type.value}, kept {raw!r} as text"

    return str(raw) if not isinstance(raw, str) else raw, None


def _coerce_date(raw) -> str:
    """Normalize to ISO date so values sort and compare correctly."""
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date: {s!r}")


def schema_for_api(
    kind: FormatKind | str | None = None, role: ObjectRole | str | None = None
) -> dict:
    """Grouped field definitions, ordered for rendering."""
    fields = fields_for(kind, role)
    groups: dict[str, list[dict]] = {}
    for f in fields:
        groups.setdefault(f.group, []).append(f.to_dict())

    order = ["Sample", "Experiment", "Archive", "Library", "Sequencing",
             "Alignment", "Variants", "Reference", "Intervals", "Quality",
             "General"]
    ordered = [
        {"group": g, "fields": groups[g]} for g in order if g in groups
    ]
    ordered += [
        {"group": g, "fields": fs} for g, fs in groups.items() if g not in order
    ]
    return {
        "kind": kind.value if isinstance(kind, FormatKind) else kind,
        "role": role.value if isinstance(role, ObjectRole) else role,
        "groups": ordered,
    }
