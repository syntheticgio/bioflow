"""SRA accession parsing, XML parsing, and enrichment semantics.

XML fixtures are real records captured from NCBI (SRR11768093, a ChIP-Seq run
in Trypanosoma brucei, and SRR000001, a human 454 WGS run). Two very different
record shapes, so the parser is exercised against genuine variation rather than
something hand-written to match the code.

No test here touches the network; the live check runs separately.
"""

from pathlib import Path

import pytest

from app.metadata import enrich, sra
from app.models import FormatKind

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def chipseq_xml() -> str:
    return (FIXTURES / "sra_SRR11768093.xml").read_text()


@pytest.fixture
def wgs_xml() -> str:
    return (FIXTURES / "sra_SRR000001.xml").read_text()


class TestAccessionParsing:
    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("SRR11768093.fastq", "SRR11768093"),
            ("SRR11768093_1.fastq.gz", "SRR11768093"),
            ("SRR11768093_2.fastq.gz", "SRR11768093"),
            ("SRX8321150_R1.fq", "SRX8321150"),
            ("ERR1234567.fastq", "ERR1234567"),
            ("DRR0987654.fastq", "DRR0987654"),
            ("cohort_SRR999888_trimmed.fq.gz", "SRR999888"),
            ("srr11768093_1.fastq", "SRR11768093"),  # case-insensitive
        ],
    )
    def test_extracts_accession(self, filename, expected):
        assert sra.parse_accession(filename) == expected

    @pytest.mark.parametrize(
        "filename",
        [
            "sample.fastq",
            "notSRR123456.fastq",  # must be at a word boundary
            "SRR123.fastq",  # too few digits
            "reads_R1.fastq",
            "",
        ],
    )
    def test_rejects_non_accessions(self, filename):
        assert sra.parse_accession(filename) is None

    def test_classifies_accession_kind(self):
        assert sra.accession_kind("SRR11768093") == "run"
        assert sra.accession_kind("SRX8321150") == "experiment"
        assert sra.accession_kind("SRS6640466") == "sample"
        assert sra.accession_kind("SRP261086") == "study"
        assert sra.accession_kind("PRJNA123") is None

    def test_validation(self):
        assert sra.is_valid_accession("SRR11768093")
        assert sra.is_valid_accession("  srr11768093  ")
        assert not sra.is_valid_accession("BOGUS123456")
        assert not sra.is_valid_accession("")


class TestXmlParsing:
    def test_extracts_all_accessions(self, chipseq_xml):
        m = sra.parse_experiment_xml(chipseq_xml, requested="SRR11768093")
        assert m.run == "SRR11768093"
        assert m.experiment == "SRX8321150"
        assert m.sample == "SRS6640466"
        assert m.study == "SRP261086"
        assert m.bioproject == "PRJNA631678"
        assert m.biosample == "SAMN14886310"

    def test_extracts_organism_and_platform(self, chipseq_xml):
        m = sra.parse_experiment_xml(chipseq_xml)
        assert m.organism == "Trypanosoma brucei brucei"
        assert m.taxon_id == "5702"
        assert m.platform == "ILLUMINA"
        assert m.instrument == "NextSeq 550"

    def test_extracts_library_descriptor(self, chipseq_xml):
        m = sra.parse_experiment_xml(chipseq_xml)
        assert m.library_strategy == "ChIP-Seq"
        assert m.library_source == "GENOMIC"
        assert m.library_layout == "PAIRED"

    def test_extracts_run_statistics(self, chipseq_xml):
        m = sra.parse_experiment_xml(chipseq_xml, requested="SRR11768093")
        assert m.total_spots == 6993562
        assert m.total_bases == 1055723708

    def test_extracts_sample_attributes(self, chipseq_xml):
        m = sra.parse_experiment_xml(chipseq_xml)
        assert m.sample_attributes["strain"] == "Lister 427"
        assert m.sample_attributes["source_name"] == "cultured cells"

    def test_parses_a_second_record_shape(self, wgs_xml):
        """A human 454 WGS record differs substantially from Illumina ChIP-Seq."""
        m = sra.parse_experiment_xml(wgs_xml, requested="SRR000001")
        assert m.organism == "Homo sapiens"
        assert m.taxon_id == "9606"
        assert m.platform == "LS454"
        assert m.library_strategy == "WGS"

    def test_malformed_xml_returns_none_rather_than_raising(self):
        assert sra.parse_experiment_xml("<not valid xml") is None

    def test_empty_xml_is_survivable(self):
        assert sra.parse_experiment_xml("<root/>") is not None


class TestMetadataMapping:
    def test_maps_onto_schema_fields(self, chipseq_xml):
        meta = sra.parse_experiment_xml(chipseq_xml, requested="SRR11768093").to_metadata()
        assert meta["sra_run"] == "SRR11768093"
        assert meta["organism"] == "Trypanosoma brucei brucei"
        assert meta["platform"] == "NextSeq 550"
        assert meta["bioproject"] == "PRJNA631678"

    def test_library_strategy_maps_to_our_assay_vocabulary(self, chipseq_xml, wgs_xml):
        chip = sra.parse_experiment_xml(chipseq_xml).to_metadata()
        wgs = sra.parse_experiment_xml(wgs_xml).to_metadata()
        assert chip["assay"] == "ChIP-seq"  # SRA writes "ChIP-Seq"
        assert wgs["assay"] == "WGS"

    def test_unknown_strategy_passes_through_unchanged(self):
        """Losing information to an incomplete lookup table would be worse than
        showing SRA's own wording."""
        m = sra.SraMetadata(library_strategy="Hi-C")
        assert m.to_metadata()["assay"] == "Hi-C"

    def test_layout_becomes_read_type(self, chipseq_xml):
        assert sra.parse_experiment_xml(chipseq_xml).to_metadata()["read_type"] == (
            "paired-end"
        )

    def test_single_layout(self):
        m = sra.SraMetadata(library_layout="SINGLE")
        assert m.to_metadata()["read_type"] == "single-end"

    def test_recognized_sample_attributes_map_to_schema_keys(self, chipseq_xml):
        meta = sra.parse_experiment_xml(chipseq_xml).to_metadata()
        assert meta["tissue"] == "cultured cells"  # from source_name
        assert meta["strain"] == "Lister 427"

    def test_unrecognized_attributes_are_namespaced(self, chipseq_xml):
        """Prefixed rather than dropped, so nothing is lost and nothing
        collides with our own field names."""
        meta = sra.parse_experiment_xml(chipseq_xml).to_metadata()
        assert meta["sra_chromatin_factor"] == "TbDMT"

    def test_library_source_maps_to_our_vocabulary(self, chipseq_xml):
        meta = sra.parse_experiment_xml(chipseq_xml).to_metadata()
        assert meta["library_source"] == "Genomic"  # SRA writes "GENOMIC"

    def test_library_source_derives_molecule_type(self, chipseq_xml):
        meta = sra.parse_experiment_xml(chipseq_xml).to_metadata()
        assert meta["molecule_type"] == "DNA"

    def test_transcriptomic_source_maps_to_rna(self):
        m = sra.SraMetadata(library_source="TRANSCRIPTOMIC")
        out = m.to_metadata()
        assert out["library_source"] == "Transcriptomic"
        assert out["molecule_type"] == "RNA"

    def test_metagenomic_and_synthetic_map_to_dna(self):
        assert sra.SraMetadata(library_source="METAGENOMIC").to_metadata()["molecule_type"] == "DNA"
        assert sra.SraMetadata(library_source="SYNTHETIC").to_metadata()["molecule_type"] == "DNA"

    def test_metatranscriptomic_and_viral_rna_map_to_rna(self):
        metatx = sra.SraMetadata(library_source="METATRANSCRIPTOMIC")
        assert metatx.to_metadata()["molecule_type"] == "RNA"
        viral = sra.SraMetadata(library_source="VIRAL RNA")
        assert viral.to_metadata()["molecule_type"] == "RNA"

    def test_unrecognized_source_passes_through_but_molecule_type_is_other(self):
        """Losing information to an incomplete lookup table would be worse than
        showing SRA's own wording -- same rule test_unknown_strategy_passes_through_unchanged
        applies to assay. molecule_type has no free-text escape hatch (it is a
        closed field), so an unrecognized source becomes "Other" there instead."""
        m = sra.SraMetadata(library_source="OTHER EXOTIC THING")
        out = m.to_metadata()
        assert out["library_source"] == "OTHER EXOTIC THING"
        assert out["molecule_type"] == "Other"

    def test_no_library_source_emits_neither_key(self):
        m = sra.SraMetadata(library_strategy="WGS")
        out = m.to_metadata()
        assert "library_source" not in out
        assert "molecule_type" not in out


class TestAccessionResolution:
    def test_explicit_metadata_beats_the_filename(self):
        """The whole point of the manual field: when a name is missing or
        misparsed, the typed accession must win."""
        acc, source = enrich.resolve_accession(
            {"sra_run": "SRR000001"}, "SRR11768093_1.fastq"
        )
        assert acc == "SRR000001"
        assert source == "metadata"

    def test_experiment_accession_is_accepted(self):
        acc, source = enrich.resolve_accession({"sra_experiment": "SRX8321150"}, "x.fq")
        assert acc == "SRX8321150"
        assert source == "metadata"

    def test_run_takes_priority_over_experiment(self):
        acc, _ = enrich.resolve_accession(
            {"sra_run": "SRR000001", "sra_experiment": "SRX000007"}, "x.fq"
        )
        assert acc == "SRR000001"

    def test_falls_back_to_filename(self):
        acc, source = enrich.resolve_accession({}, "SRR11768093_1.fastq.gz")
        assert acc == "SRR11768093"
        assert source == "filename"

    def test_invalid_manual_accession_falls_back(self):
        acc, source = enrich.resolve_accession(
            {"sra_run": "not-an-accession"}, "SRR11768093.fastq"
        )
        assert acc == "SRR11768093"
        assert source == "filename"

    def test_no_accession_anywhere(self):
        assert enrich.resolve_accession({}, "sample.fastq") == (None, None)


class TestEnrichmentSafety:
    """Enrichment must never overwrite what a person entered."""

    @pytest.fixture(autouse=True)
    def stub_lookup(self, monkeypatch, chipseq_xml):
        parsed = sra.parse_experiment_xml(chipseq_xml, requested="SRR11768093")
        monkeypatch.setattr(sra, "lookup", lambda acc: parsed)

    def test_fills_empty_fields(self):
        r = enrich.enrich_from_sra(
            filename="SRR11768093_1.fastq",
            existing_metadata={},
            format_kind=FormatKind.FASTQ,
        )
        assert r.accession == "SRR11768093"
        assert r.values["organism"] == "Trypanosoma brucei brucei"
        assert r.conflicts == []

    def test_never_overwrites_a_manual_value(self):
        """A user correcting a bad public record must not have that correction
        reverted on every re-ingest."""
        r = enrich.enrich_from_sra(
            filename="SRR11768093_1.fastq",
            existing_metadata={"organism": "Homo sapiens"},
            format_kind=FormatKind.FASTQ,
        )
        assert "organism" not in r.values
        conflict = next(c for c in r.conflicts if c["key"] == "organism")
        assert conflict["yours"] == "Homo sapiens"
        assert conflict["sra"] == "Trypanosoma brucei brucei"

    def test_identical_values_are_not_conflicts(self):
        r = enrich.enrich_from_sra(
            filename="SRR11768093_1.fastq",
            existing_metadata={"organism": "Trypanosoma brucei brucei"},
            format_kind=FormatKind.FASTQ,
        )
        assert r.conflicts == []
        assert "organism" in r.unchanged

    def test_other_fields_still_fill_when_one_conflicts(self):
        r = enrich.enrich_from_sra(
            filename="SRR11768093_1.fastq",
            existing_metadata={"organism": "Homo sapiens"},
            format_kind=FormatKind.FASTQ,
        )
        assert r.values["platform"] == "NextSeq 550"

    def test_manual_accession_is_used_when_filename_has_none(self):
        r = enrich.enrich_from_sra(
            filename="my_reads.fastq",
            existing_metadata={"sra_run": "SRR11768093"},
            format_kind=FormatKind.FASTQ,
        )
        assert r.accession == "SRR11768093"
        assert r.source == "metadata"
        assert r.values["organism"]


class TestEnrichmentScope:
    def test_skipped_for_non_sequence_formats(self, monkeypatch):
        """A BAM whose name happens to contain SRR-like text should not trigger
        a lookup."""
        called = []
        monkeypatch.setattr(sra, "lookup", lambda a: called.append(a))
        r = enrich.enrich_from_sra(
            filename="SRR11768093.bam",
            existing_metadata={},
            format_kind=FormatKind.BAM,
        )
        assert r.accession is None
        assert called == []

    def test_skipped_when_disabled(self, monkeypatch):
        called = []
        monkeypatch.setattr(sra, "lookup", lambda a: called.append(a))
        r = enrich.enrich_from_sra(
            filename="SRR11768093.fastq",
            existing_metadata={},
            format_kind=FormatKind.FASTQ,
            enabled=False,
        )
        assert r.accession is None
        assert called == []

    def test_no_accession_means_no_lookup(self, monkeypatch):
        called = []
        monkeypatch.setattr(sra, "lookup", lambda a: called.append(a))
        enrich.enrich_from_sra(
            filename="ordinary.fastq", existing_metadata={},
            format_kind=FormatKind.FASTQ,
        )
        assert called == []


class TestNetworkFailure:
    def test_lookup_exception_does_not_propagate(self, monkeypatch):
        """A network problem must not turn a good file into a failed ingest."""
        def boom(_acc):
            raise ConnectionError("NCBI unreachable")

        monkeypatch.setattr(sra, "lookup", boom)
        r = enrich.enrich_from_sra(
            filename="SRR11768093.fastq", existing_metadata={},
            format_kind=FormatKind.FASTQ,
        )
        assert r.values == {}
        assert "failed" in r.error.lower()

    def test_missing_record_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(sra, "lookup", lambda a: None)
        r = enrich.enrich_from_sra(
            filename="SRR99999999.fastq", existing_metadata={},
            format_kind=FormatKind.FASTQ,
        )
        assert r.values == {}
        assert "No SRA record" in r.error

    def test_http_failure_returns_none(self, monkeypatch):
        import urllib.error

        def fail(*a, **k):
            raise urllib.error.URLError("down")

        monkeypatch.setattr("urllib.request.urlopen", fail)
        monkeypatch.setattr(sra, "MAX_RETRIES", 0)
        monkeypatch.setattr(sra, "MIN_INTERVAL", 0)
        assert sra.resolve_uid("SRR11768093") is None
