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

from app.models import FormatKind, ObjectRole, SequenceType, SequencingPlatform


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
    # True when the values come from outside this repo -- NCBI, an instrument
    # vendor, a lab's own kit names -- so `options` is a set of suggestions
    # that will never be complete. The UI renders these as a free-text combo
    # rather than a <select>, and an off-list value is not a warning.
    #
    # Inclusion rule: open if the vocabulary is owned elsewhere; closed if this
    # repo or a published spec defines the complete set. Deliberately a
    # hand-maintained per-field flag and deliberately without an exhaustiveness
    # test -- see the spec's note on CLAUDE.md's three-way registry split. This
    # is the middle case, where forcing coverage would make a detector guess.
    open_vocabulary: bool = False

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
            "open_vocabulary": self.open_vocabulary,
        }


# --- Fields that apply to any file -----------------------------------------

COMMON_FIELDS: tuple[FieldDef, ...] = (
    # Common rather than reference-only on purpose: any sequence file can be
    # genomic, CDS, protein or RNA, and the tag is worth being able to set by
    # hand on a BAM or a FASTQ that came from one. Only *autodetection* is
    # scoped to references -- see `detect_sequence_type` in app.metadata.enrich.
    FieldDef(
        "sequence_type",
        "Sequence type",
        type=FieldType.ENUM,
        options=tuple(t.value for t in SequenceType),
        help="What kind of sequence this file holds. Detected from the name for "
             "references; set it here if that was absent or wrong.",
        group="Sequence",
    ),
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
        ),
        help="The species the sample came from, in binomial form. It decides "
             "which reference an alignment should use, and the expected GC "
             "content QC compares against. The list is suggestions -- any "
             "organism can be typed in.",
        group="Sample",
        suggested=True,
        open_vocabulary=True,
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
             options=("female", "male", "unknown"),
             help="The sample donor's sex. Worth recording because it changes "
                  "what to expect on the sex chromosomes: coverage there is a "
                  "standard sanity check on a whole-genome run.",
             group="Sample"),
    FieldDef("collection_date", "Collection date", type=FieldType.DATE,
             help="When the biological sample was taken, not when it was "
                  "sequenced -- the gap between the two is what long storage "
                  "or a repeat run looks like in the record.",
             group="Sample"),
    FieldDef(
        "molecule_type",
        "Molecule type",
        type=FieldType.ENUM,
        options=("DNA", "RNA", "Other"),
        group="Experiment",
        suggested=True,
        help="What the library was made from. Derived from the SRA record's "
             "library source, or inferred from the FASTQ's own bases via the "
             "Infer button when no SRA record is available.",
    ),
    FieldDef(
        "library_source",
        "Library source",
        type=FieldType.ENUM,
        # SRA's vocabulary includes OTHER, dropped here on purpose: the field
        # is open, so a value outside this list is typed in as itself rather
        # than flattened to a sentinel that records nothing.
        options=(
            "Genomic",
            "Transcriptomic",
            "Metagenomic",
            "Metatranscriptomic",
            "Synthetic",
            "Viral RNA",
        ),
        group="Experiment",
        open_vocabulary=True,
        help="SRA's own library source classification, where available.",
    ),
    FieldDef(
        "assay",
        "Assay",
        type=FieldType.ENUM,
        options=("WGS", "WES", "RNA-seq", "scRNA-seq", "ATAC-seq", "ChIP-seq",
                 "Bisulfite-seq", "Amplicon", "Targeted panel"),
        help="What the experiment was measuring. It is the single field that "
             "decides which pipeline suits the file -- whole-genome and "
             "RNA-seq reads look alike until this says otherwise. The list is "
             "suggestions; an unlisted assay is stored as typed.",
        group="Experiment",
        suggested=True,
        open_vocabulary=True,
    ),
    FieldDef("batch", "Batch", help="Sequencing or processing batch.",
             group="Experiment"),
    FieldDef("notes", "Notes",
             help="Anything about this file worth telling the next person to "
                  "open it, where no other field fits.",
             group="Experiment"),

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
    FieldDef("sra_sample", "SRA sample",
             help="e.g. SRS6702123. The archive's own identifier for the "
                  "biological sample, which several runs can share. Filled by "
                  "the NCBI lookup rather than used to trigger it.",
             group="Archive"),
    FieldDef("sra_study", "SRA study",
             help="e.g. SRP263061. The archive's identifier for the study all "
                  "these runs belong to. Filled by the NCBI lookup.",
             group="Archive"),
    FieldDef("bioproject", "BioProject",
             help="e.g. PRJNA631678. NCBI's identifier for the research "
                  "project a set of studies belongs to -- the broadest of the "
                  "archive accessions.",
             group="Archive"),
    FieldDef("biosample", "BioSample",
             help="e.g. SAMN14886310. NCBI's identifier for the physical "
                  "sample, which is what ties several runs back to one piece "
                  "of biological material.",
             group="Archive"),
    FieldDef("study_title", "Study title",
             help="The submitted title of the study this file came from, as "
                  "the archive holds it. Usually the fastest way to remember "
                  "what a downloaded accession actually is.",
             group="Archive"),
)


# --- Format-specific additions ---------------------------------------------

FASTQ_FIELDS: tuple[FieldDef, ...] = (
    FieldDef(
        "library_prep",
        "Library prep",
        type=FieldType.ENUM,
        options=("TruSeq", "Nextera", "NEBNext", "KAPA", "SMART-seq", "10x"),
        help="The library preparation kit or protocol. It determines which "
             "adapter sequence is in the reads, so it is what tells trimming "
             "what to look for. Kit names are a lab's own vocabulary -- an "
             "unlisted one is stored as typed.",
        group="Library",
        suggested=True,
        open_vocabulary=True,
    ),
    FieldDef("library_id", "Library ID",
             help="Your identifier for the prepared library. Two files sharing "
                  "one is what makes duplicate reads across them expected "
                  "rather than a finding.",
             group="Library"),
    FieldDef(
        "read_type",
        "Read type",
        type=FieldType.ENUM,
        options=("single-end", "paired-end"),
        help="Whether each fragment was read from one end or both. It decides "
             "how the reads are handed to an aligner, and paired files are "
             "expected to arrive as a matched R1 and R2 pair.",
        group="Library",
        suggested=True,
    ),
    FieldDef("mate", "Mate", type=FieldType.ENUM, options=("R1", "R2", "index"),
             help="Usually inferred from the filename; override if wrong.",
             group="Library"),
    FieldDef("insert_size", "Insert size", type=FieldType.INTEGER, unit="bp",
             help="The expected distance between the two ends of a pair, "
                  "fragment length rather than read length. An insert shorter "
                  "than twice the read length means the mates overlap, which "
                  "some tools exploit and others cannot handle.",
             group="Library"),
    FieldDef(
        "platform",
        "Sequencing platform",
        type=FieldType.ENUM,
        # Derived from the enum rather than restated, so NCBI's vocabulary is
        # spelled once. Closed, unlike almost every other externally-owned
        # vocabulary here, because NCBI publishes the complete set -- a value
        # outside it is a genuine mistake rather than a machine this list has
        # not caught up with. The machine name goes in `instrument_model`.
        options=tuple(p.value for p in SequencingPlatform),
        help="Who made the reads, in NCBI's spelling. The specific machine "
             "goes in Instrument model.",
        group="Sequencing",
        suggested=True,
    ),
    FieldDef(
        "instrument_model",
        "Instrument model",
        type=FieldType.ENUM,
        # Suggestions only, and deliberately incomplete: new machines ship
        # constantly and SRA writes whatever the submitter recorded. These are
        # the models actually present in this installation's data, which makes
        # them useful defaults without pretending to be a vocabulary.
        options=("Illumina NovaSeq X Plus", "Illumina NovaSeq 6000",
                 "Illumina HiSeq 4000", "Illumina HiSeq 2000",
                 "Illumina MiSeq", "NextSeq 550",
                 "MinION", "PromethION", "GridION",
                 "Sequel IIe", "Revio", "PacBio RS"),
        help="The specific sequencer, e.g. 'NextSeq 550'. Free text -- an "
             "unlisted model is stored as typed.",
        group="Sequencing",
        suggested=True,
        open_vocabulary=True,
    ),
    FieldDef("run_id", "Run ID",
             help="The sequencer's identifier for the run this file came off. "
                  "It is how a batch effect gets traced back to one run.",
             group="Sequencing"),
    FieldDef("flowcell", "Flowcell",
             help="The flowcell identifier. Together with the lane it is the "
                  "physical origin of the reads, which is what a per-tile "
                  "quality problem is localised to.",
             group="Sequencing"),
    FieldDef("lane", "Lane", type=FieldType.INTEGER,
             help="Which lane of the flowcell these reads came from. One bad "
                  "lane in an otherwise good run is a real and recurring "
                  "failure, and this is what makes it attributable.",
             group="Sequencing"),
)

ALIGNMENT_FIELDS: tuple[FieldDef, ...] = (
    FieldDef(
        "reference_build",
        "Reference build",
        type=FieldType.ENUM,
        options=("GRCh38", "GRCh37/hg19", "T2T-CHM13", "GRCm39", "GRCm38/mm10"),
        help="Which genome build the reads were aligned to. Mixing builds is a "
             "common and costly mistake, so it is worth recording explicitly.",
        group="Alignment",
        suggested=True,
        open_vocabulary=True,
    ),
    FieldDef(
        "aligner",
        "Aligner",
        type=FieldType.ENUM,
        options=("BWA-MEM", "BWA-MEM2", "Bowtie2", "STAR", "HISAT2", "minimap2",
                 "DRAGEN"),
        help="Which program aligned the reads. Aligners differ in how they "
             "score multi-mapping reads and set MAPQ, so comparing mapping "
             "rates across two of them is comparing two different questions.",
        group="Alignment",
        suggested=True,
        open_vocabulary=True,
    ),
    FieldDef("aligner_version", "Aligner version",
             help="The aligner's version string. Alignment output is not "
                  "identical across versions, so this is part of what makes a "
                  "run reproducible.",
             group="Alignment"),
    FieldDef("duplicates_marked", "Duplicates marked", type=FieldType.BOOLEAN,
             help="Whether PCR and optical duplicates have been flagged in the "
                  "file. Callers that skip flagged reads count the same "
                  "fragment once instead of several times, so an unmarked file "
                  "can turn one molecule into apparent support for a variant.",
             group="Alignment"),
    FieldDef("bqsr_applied", "BQSR applied", type=FieldType.BOOLEAN,
             help="Base quality score recalibration.", group="Alignment"),
    FieldDef("mean_coverage", "Mean coverage", type=FieldType.NUMBER, unit="x",
             help="Average depth across the target region -- how many reads "
                  "cover a typical base. A mean hides unevenness, so a good "
                  "mean over a badly covered genome is a real and common case.",
             group="Quality"),
    FieldDef("percent_mapped", "Mapped reads", type=FieldType.NUMBER, unit="%",
             help="The share of reads that aligned to the reference at all. A "
                  "low value usually means contamination or the wrong "
                  "reference rather than poor sequencing.",
             group="Quality"),
)

VARIANT_FIELDS: tuple[FieldDef, ...] = (
    FieldDef(
        "reference_build",
        "Reference build",
        type=FieldType.ENUM,
        options=("GRCh38", "GRCh37/hg19", "T2T-CHM13", "GRCm39", "GRCm38/mm10"),
        help="Which genome build these coordinates are on. A position means "
             "nothing without it, and coordinates do not carry between builds "
             "without a liftover -- which is why it is asked rather than "
             "assumed.",
        group="Variants",
        suggested=True,
        open_vocabulary=True,
    ),
    FieldDef(
        "variant_caller",
        "Variant caller",
        type=FieldType.ENUM,
        options=("GATK HaplotypeCaller", "GATK Mutect2", "DeepVariant", "FreeBayes",
                 "bcftools", "Strelka2", "DRAGEN"),
        help="Which program made these calls. Callers disagree most on indels "
             "and in low-coverage regions, so this is needed to read a "
             "call set's false-positive profile.",
        group="Variants",
        suggested=True,
        open_vocabulary=True,
    ),
    FieldDef("caller_version", "Caller version",
             help="The variant caller's version string. Calls change between "
                  "versions, so this is part of what makes a call set "
                  "reproducible.",
             group="Variants"),
    FieldDef(
        "variant_type",
        "Variant type",
        type=FieldType.ENUM,
        options=("germline", "somatic", "joint-genotyped", "structural", "CNV"),
        help="What kind of calls the file holds. It decides how the records "
             "should be read: a somatic call set carries allele fractions "
             "rather than genotypes, and filtering a somatic file by "
             "germline expectations discards the findings.",
        group="Variants",
        suggested=True,
    ),
    FieldDef("filtered", "Filtered", type=FieldType.BOOLEAN,
             help="Whether low-quality calls have already been removed.",
             group="Variants"),
    FieldDef("annotated_with", "Annotation",
             help="e.g. VEP, SnpEff, ANNOVAR. Which tool added the functional "
                  "consequence fields. They disagree on transcript choice, so "
                  "the same variant can be called high-impact by one and "
                  "harmless by another.",
             group="Variants"),
)

REFERENCE_FIELDS: tuple[FieldDef, ...] = (
    # Free text rather than an enum: reference builds are open-ended, including
    # custom assemblies and patch releases that no fixed list would cover.
    FieldDef("reference_build", "Build",
             help="The name of this assembly build, e.g. GRCh38 or a custom "
                  "one. Free text rather than a list, because patch releases "
                  "and in-house assemblies are the normal case here. Every "
                  "coordinate produced against this file inherits it.",
             group="Reference", suggested=True),
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
             help="Whether repeats are masked, hard (replaced with N) or soft "
                  "(lower-cased). Aligners treat the two differently, and a "
                  "hard-masked reference cannot align reads from the masked "
                  "regions at all.",
             group="Reference"),

    # Filled by the NCBI assembly lookup rather than by hand, so none are
    # suggested -- they appear once enrichment has run.
    FieldDef("tax_id", "NCBI taxonomy ID", type=FieldType.INTEGER,
             help="e.g. 9606 for human. Set from the assembly record.",
             group="Reference"),
    FieldDef("assembly_level", "Assembly level", type=FieldType.ENUM,
             options=("Complete Genome", "Chromosome", "Scaffold", "Contig"),
             help="How finished the assembly is, from a set of unordered "
                  "contigs up to a complete genome. It sets what the "
                  "contiguity numbers should look like.",
             group="Reference"),
    FieldDef("assembly_date", "Release date", type=FieldType.DATE,
             help="When NCBI published this assembly.", group="Reference"),
    FieldDef("paired_accession", "Paired accession",
             help="The GenBank counterpart of a RefSeq assembly, or vice versa.",
             group="Reference"),
)

# Protein and CDS FASTA downloaded alongside an assembly. Deliberately not
# REFERENCE_FIELDS: a protein FASTA has no assembly level, no primary-assembly
# distinction and no scaffold N50, and asking about them would imply it is a
# genome -- the exact confusion the PROTEIN role exists to prevent.
SEQUENCE_SET_FIELDS: tuple[FieldDef, ...] = (
    FieldDef("organism", "Organism",
             help="The species these sequences belong to. Free text here, "
                  "unlike the sample-level field, because a sequence set is "
                  "often named for a strain or assembly rather than a species.",
             group="Sequences", suggested=True),
    FieldDef("assembly_accession", "Assembly accession",
             help="The assembly these sequences were derived from, "
                  "e.g. GCF_000002445.2.",
             group="Sequences", suggested=True),
    FieldDef("sequence_count", "Sequences", type=FieldType.INTEGER,
             help="Number of records in the file.", group="Sequences"),
    FieldDef("source", "Source",
             help="e.g. NCBI RefSeq annotation release 104.",
             group="Sequences", suggested=True),
)

INTERVAL_FIELDS: tuple[FieldDef, ...] = (
    FieldDef("reference_build", "Reference build",
             help="Which genome build these intervals are on. Intervals are "
                  "coordinates and nothing else, so applying them against a "
                  "different build silently selects the wrong regions.",
             group="Intervals", suggested=True),
    FieldDef("interval_type", "Interval type", type=FieldType.ENUM,
             options=("capture targets", "exons", "genes", "regions of interest",
                      "blacklist"),
             help="What these regions represent, which decides whether they "
                  "are somewhere to look or somewhere to avoid -- capture "
                  "targets restrict an analysis, a blacklist excludes from "
                  "one, and confusing the two inverts the result.",
             group="Intervals", open_vocabulary=True),
    FieldDef("source", "Source",
             help="Where the intervals came from, e.g. a capture kit's "
                  "manufacturer BED or an ENCODE blacklist release. Interval "
                  "sets are revised, so naming the release matters.",
             group="Intervals"),
)


FORMAT_FIELDS: dict[FormatKind, tuple[FieldDef, ...]] = {
    FormatKind.FASTQ: FASTQ_FIELDS,
    FormatKind.BAM: ALIGNMENT_FIELDS,
    FormatKind.SAM: ALIGNMENT_FIELDS,
    FormatKind.CRAM: ALIGNMENT_FIELDS,
    FormatKind.VCF: VARIANT_FIELDS,
    FormatKind.BCF: VARIANT_FIELDS,
    FormatKind.BED: INTERVAL_FIELDS,
    FormatKind.GFF: INTERVAL_FIELDS,
    FormatKind.GTF: INTERVAL_FIELDS,
    FormatKind.GENBANK: INTERVAL_FIELDS,
}

# Formats whose questions are entirely the common ones -- listed explicitly
# for the same reason FORMAT_DERIVED_ROLES is: a format added without thought
# should fail a test rather than quietly fall through to COMMON_FIELDS with
# nothing to say so.
FORMAT_COMMON_ONLY: frozenset[FormatKind] = frozenset(
    {
        # A FASTA is no longer assumed to be a reference -- that now comes
        # from the object's role, so a FASTA of reads is not asked reference
        # questions.
        FormatKind.FASTA,
        # An assembly graph's questions are its role's, not its format's: see
        # ObjectRole.ASSEMBLY_GRAPH in FORMAT_DERIVED_ROLES above. A GFA
        # reached without that role (still possible -- role is optional)
        # falls back to here rather than to a field group that does not
        # exist.
        FormatKind.GFA,
        # samtools FASTA index: name, length, offset, linebases, linewidth.
        # Sidecar data a person does not annotate.
        FormatKind.FAI,
        # Free-form text with no format-specific shape to ask about.
        FormatKind.TEXT,
        # Not a format at all so much as the absence of an answer -- included
        # here rather than carved out of the exhaustiveness check below,
        # since an exception in the assertion is a hole in exactly the place
        # the assertion exists to close.
        FormatKind.UNKNOWN,
    }
)

# Keyed by role rather than format, and consulted first: see fields_for. Kept as
# a dict so a new role-specific field group is a one-line entry that both
# fields_for and all_known_fields pick up automatically.
ROLE_FIELDS: dict[ObjectRole, tuple[FieldDef, ...]] = {
    ObjectRole.REFERENCE: REFERENCE_FIELDS,
    # Both sequence sets share one vocabulary: they differ in what the
    # sequences *are*, which the role already records, not in what is worth
    # asking about them.
    ObjectRole.PROTEIN: SEQUENCE_SET_FIELDS,
    ObjectRole.TRANSCRIPT: SEQUENCE_SET_FIELDS,
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
# VARIANTS follows ALIGNMENT exactly: a called VCF and an uploaded VCF describe
# the same biology, and which caller produced it is already recorded in facts
# by the applier rather than being something to ask the user.
#
# ANNOTATION joins them for the same reason: a published GFF3 and a
# user-supplied BED both describe intervals on a reference, and
# INTERVAL_FIELDS already asks the right questions. The role records that
# these annotations are NCBI's rather than the user's, which is provenance
# rather than a different question.
#
# Listed explicitly rather than left implicit so that a role added without
# thought still fails the "every role is accounted for" test.
FORMAT_DERIVED_ROLES: frozenset[ObjectRole] = frozenset(
    {
        ObjectRole.TRIMMED_READS,
        ObjectRole.FILTERED_READS,
        ObjectRole.ALIGNMENT,
        ObjectRole.VARIANTS,
        ObjectRole.ANNOTATION,
        # COUNTS gets no group of its own, which is worth explaining because
        # the differential expression design *is* metadata and it would be
        # reasonable to expect one here.
        #
        # It does not need one: `condition`, `sample_id` and `batch` are
        # already COMMON_FIELDS, so the design can be recorded on the reads at
        # upload time, and every applier copies metadata forward -- reads to
        # trimmed reads to BAM to counts. Tagging six FASTQs as "treated" with
        # the bulk edit bar therefore arrives at the DE dialog as a filled-in
        # design without anyone touching a counts file. Adding a duplicate
        # `condition` here would have shadowed the common one and split the
        # same concept across two keys.
        ObjectRole.COUNTS,
        # DE_RESULTS has nothing worth asking. Everything that describes it --
        # which samples, which contrast, which engine version -- is provenance
        # the applier already records from the run that produced it, and a
        # results table nobody produced here is not a thing that exists.
        ObjectRole.DE_RESULTS,
        # ASSEMBLED_TRANSCRIPTS belongs here for DE_RESULTS' reason directly
        # above: there is nothing to ask a user about it. Which alignment,
        # which reference annotation, which tool and version -- all of it is
        # provenance the applier records from the run that produced it, and a
        # transcript assembly nobody produced here is not a thing that exists.
        # The sample-level metadata (condition, sample_id, batch) is already
        # COMMON_FIELDS and travels forward from the reads.
        ObjectRole.ASSEMBLED_TRANSCRIPTS,
        # An assembly graph's questions are its format's. There is nothing to
        # ask that the GFA does not already answer -- and pointedly no
        # assembly accession, since a de novo graph is precisely the case
        # where no published assembly exists to name.
        ObjectRole.ASSEMBLY_GRAPH,
    }
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
            # An open field's options are suggestions from a vocabulary this
            # repo does not own, so an off-list value is the normal case rather
            # than a mistake -- SRA writes instrument models the dropdown never
            # listed. Warning on those was wrong about which value was
            # authoritative. A closed field's list really is complete, so an
            # off-list value there still earns the warning.
            if spec.options and not spec.open_vocabulary and s not in spec.options:
                # Kept regardless: lab vocabularies always outgrow a fixed list.
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
