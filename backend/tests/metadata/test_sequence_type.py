"""Guessing Genomic/CDS/Protein/RNA from a filename.

The names here are real NCBI Datasets output rather than shapes invented to
match the code, because the one thing this detector must not do is what the
suggestion rules once did: read `cds_from_genomic.fna` as a genome because the
word "genomic" is in it.

Silence is a valid answer throughout. A name that does not say gets None, not a
guess -- an absent tag is a question the user can answer, a wrong one is a
claim they have to notice first.
"""

import pytest

from app.metadata import enrich, schemas
from app.models import FormatKind, ObjectRole


class TestNcbiNames:
    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("GCF_000002445.2_ASM244v1_genomic.fna", "Genomic"),
            ("GCF_000002445.2_ASM244v1_cds_from_genomic.fna", "CDS"),
            ("GCF_000002445.2_ASM244v1_rna_from_genomic.fna", "RNA"),
            ("GCF_000002445.2_ASM244v1_protein.faa", "Protein"),
            ("GCA_000001405.29_GRCh38.p14_genomic.fna.gz", "Genomic"),
        ],
    )
    def test_component_suffix_wins(self, filename, expected):
        assert self._detect(filename) == expected

    def test_cds_from_genomic_is_not_genomic(self):
        """The failure this detector exists to avoid: `genomic` appears in the
        name of a file that is emphatically not a genome."""
        assert self._detect("x_cds_from_genomic.fna") == "CDS"

    @staticmethod
    def _detect(filename: str) -> str | None:
        return enrich.detect_sequence_type(
            filename=filename, format_kind=FormatKind.FASTA
        )


class TestTokenMatchingNotSubstring:
    def test_alternative_is_not_rna(self):
        """`alte-rna-tive` contains "rna". Substring matching would call this
        an RNA file; token matching does not."""
        assert self._detect("alternative_contigs.fna") is None

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("protein.faa", "Protein"),
            ("transcripts.fa", "RNA"),
            ("mrna.fasta", "RNA"),
            ("ecoli-cds.fna", "CDS"),
            ("hg38.genome.fa", "Genomic"),
        ],
    )
    def test_bare_tokens(self, filename, expected):
        assert self._detect(filename) == expected

    @pytest.mark.parametrize(
        "filename",
        ["reads.fa", "sample1.fasta", "scaffolds.fna", "contigs.fa.gz", ""],
    )
    def test_uninformative_names_get_no_guess(self, filename):
        assert self._detect(filename) is None

    @staticmethod
    def _detect(filename: str) -> str | None:
        return enrich.detect_sequence_type(
            filename=filename, format_kind=FormatKind.FASTA
        )


class TestExtensionFallback:
    """`.faa`/`.ffn`/`.frn` carry meaning by convention, and are the last
    resort once no token has said anything."""

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("something.faa", "Protein"),
            ("something.ffn", "CDS"),
            ("something.frn", "RNA"),
        ],
    )
    def test_extension_is_consulted_last(self, filename, expected):
        assert (
            enrich.detect_sequence_type(
                filename=filename, format_kind=FormatKind.FASTA
            )
            == expected
        )

    def test_a_token_outranks_the_extension(self):
        """`.faa` says protein, but the name says CDS. The explicit token is
        the stronger claim."""
        assert (
            enrich.detect_sequence_type(
                filename="weird_cds.faa", format_kind=FormatKind.FASTA
            )
            == "CDS"
        )


class TestScopedToReferences:
    """Autodetection is deliberately FASTA-only. Non-sequence files can still
    be tagged by hand -- the field is common to every format."""

    @pytest.mark.parametrize(
        "kind",
        [FormatKind.FASTQ, FormatKind.BAM, FormatKind.VCF, FormatKind.BED],
    )
    def test_other_formats_are_never_guessed_at(self, kind):
        assert (
            enrich.detect_sequence_type(
                filename="sample_protein_genomic.fastq", format_kind=kind
            )
            is None
        )

    def test_missing_format_is_not_guessed_at(self):
        assert (
            enrich.detect_sequence_type(filename="x_genomic.fna", format_kind=None)
            is None
        )

    def test_string_format_kind_is_accepted(self):
        """The handler passes `detection.kind`, but results come back across a
        process boundary as plain strings elsewhere."""
        assert (
            enrich.detect_sequence_type(
                filename="x_genomic.fna", format_kind="fasta"
            )
            == "Genomic"
        )


class TestNeverOverwrites:
    """Same promise the SRA and assembly enrichers make: a value the user
    entered survives re-ingest."""

    def test_existing_value_ends_it(self):
        assert (
            enrich.detect_sequence_type(
                filename="x_genomic.fna",
                existing_metadata={"sequence_type": "Protein"},
                format_kind=FormatKind.FASTA,
            )
            is None
        )

    @pytest.mark.parametrize("blank", [None, ""])
    def test_blank_value_is_not_an_answer(self, blank):
        assert (
            enrich.detect_sequence_type(
                filename="x_genomic.fna",
                existing_metadata={"sequence_type": blank},
                format_kind=FormatKind.FASTA,
            )
            == "Genomic"
        )


class TestFieldIsAvailableEverywhere:
    """The tag is editable on every file, not just references -- that is why it
    lives in COMMON_FIELDS rather than REFERENCE_FIELDS."""

    @pytest.mark.parametrize(
        "kind,role",
        [
            (FormatKind.FASTA, ObjectRole.REFERENCE),
            (FormatKind.FASTA, ObjectRole.PROTEIN),
            (FormatKind.FASTQ, None),
            (FormatKind.BAM, ObjectRole.ALIGNMENT),
            (FormatKind.VCF, ObjectRole.VARIANTS),
        ],
    )
    def test_present_for_every_format_and_role(self, kind, role):
        keys = {f.key for f in schemas.fields_for(kind, role)}
        assert "sequence_type" in keys

    def test_options_are_the_four_asked_for(self):
        field = schemas.all_known_fields()["sequence_type"]
        assert field.type == schemas.FieldType.ENUM
        assert field.options == ("Genomic", "CDS", "Protein", "RNA")

    def test_every_detected_value_is_a_valid_option(self):
        """A detector returning something the dropdown cannot show would leave
        the user unable to see what was chosen for them."""
        field = schemas.all_known_fields()["sequence_type"]
        detected = {
            v for _, v in enrich._COMPOUND_SEQUENCE_TYPES
        } | set(enrich._TOKEN_SEQUENCE_TYPES.values()) | set(
            enrich._EXTENSION_SEQUENCE_TYPES.values()
        )
        assert detected <= set(field.options)
