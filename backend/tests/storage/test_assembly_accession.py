"""Assembly accession detection and NCBI Datasets parsing."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.metadata import assembly

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "ncbi_assembly_GCF_000002445.2.json"
)


class TestParseAccession:
    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("GCF_000002445.2_ASM244v1_genomic.fna", "GCF_000002445.2"),
            ("GCA_000001405.29_GRCh38.p14_genomic.fna.gz", "GCA_000001405.29"),
            ("gcf_000002445.2_lowercase.fna", "GCF_000002445.2"),
            ("/data/refs/GCF_000002445.2/genome.fna", "GCF_000002445.2"),
        ],
    )
    def test_finds_accessions(self, filename, expected):
        assert assembly.parse_accession(filename) == expected

    @pytest.mark.parametrize(
        "filename",
        [
            "",
            "sample.fastq.gz",
            "GCF_000002445.fna",       # no version suffix
            "GCF_00000244.2.fna",      # eight digits, not nine
            "MYGCA_000000001.1.fna",   # not at a word boundary
            "SRR11768093.fastq",
        ],
    )
    def test_rejects_non_accessions(self, filename):
        assert assembly.parse_accession(filename) is None

    def test_uppercases_the_result(self):
        """Stored uppercase so a filename's casing does not fragment lookups."""
        assert assembly.parse_accession("gca_000001405.29.fna") == "GCA_000001405.29"


class TestIsValidAccession:
    def test_accepts_bare_accessions(self):
        assert assembly.is_valid_accession("GCF_000002445.2")
        assert assembly.is_valid_accession("gca_000001405.29")

    def test_rejects_malformed(self):
        assert not assembly.is_valid_accession("GCF_000002445")
        assert not assembly.is_valid_accession("SRR11768093")
        assert not assembly.is_valid_accession("")


class TestParseReport:
    """Parsing a real captured NCBI response, offline."""

    @pytest.fixture
    def report(self) -> dict:
        return json.loads(FIXTURE.read_text())

    def test_extracts_identity_fields(self, report):
        meta = assembly.parse_report(report)
        assert meta is not None
        assert meta.accession == "GCF_000002445.2"
        assert meta.organism == "Trypanosoma brucei brucei TREU927"
        assert meta.tax_id == 185431
        assert meta.strain == "927/4 GUTat10.1"
        assert meta.assembly_name == "ASM244v1"
        assert meta.assembly_level == "Chromosome"
        assert meta.submitter == "Trypanosoma brucei consortium"
        assert meta.release_date == "2005-12-14"
        assert meta.bioproject == "PRJNA11756"
        assert meta.paired_accession == "GCA_000002445.1"

    def test_extracts_stats(self, report):
        meta = assembly.parse_report(report)
        assert meta.contig_count == 50
        assert meta.gc_percent == pytest.approx(46.5)
        assert meta.total_length == 26075494

    def test_sequence_count_uses_scaffolds_not_contigs(self):
        """A FASTA's records are scaffolds, so that is what our sequence count
        must be compared against.

        For GCF_000002445.2 these differ sharply: 12 scaffolds versus 50
        contigs. Comparing a correct file's 12 sequences against 50 would
        report a divergence that does not exist.
        """
        meta = assembly.parse_report(json.loads(FIXTURE.read_text()))
        assert meta.scaffold_count == 12
        assert meta.contig_count == 50
        assert meta.to_facts()["ncbi_sequence_count"] == 12

    def test_returns_none_for_an_empty_report(self):
        assert assembly.parse_report({"reports": []}) is None
        assert assembly.parse_report({}) is None

    def test_survives_a_partial_record(self):
        """NCBI omits fields for some assemblies; absence must not raise."""
        meta = assembly.parse_report({"reports": [{"accession": "GCA_000000001.1"}]})
        assert meta is not None
        assert meta.accession == "GCA_000000001.1"
        assert meta.organism is None
        assert meta.contig_count is None

    @pytest.mark.parametrize(
        "payload",
        [
            {"reports": [{"accession": "GCA_1", "assembly_stats": [1, 2, 3]}]},
            {"reports": [{"accession": "GCA_1", "organism": "not a dict"}]},
            {"reports": [{"accession": "GCA_1", "assembly_info": "not a dict"}]},
            {"reports": [{"accession": "GCA_1",
                          "organism": {"infraspecific_names": "not a dict"}}]},
            {"reports": "not a list"},
            {"reports": [None]},
        ],
    )
    def test_never_raises_on_an_unexpected_shape(self, payload):
        """NCBI changing a field's type must degrade, not fail an ingest.

        `x or {}` guards falsy values but not a truthy value of the wrong
        type, which is what a schema change or an error envelope looks like.
        """
        assembly.parse_report(payload)  # must not raise

    def test_lookup_never_raises_on_an_unexpected_shape(self):
        """The never-raises promise must hold end to end, not just in parsing."""
        body = b'{"reports":[{"accession":"GCA_1","organism":"oops"}]}'
        with patch.object(assembly, "_get", return_value=body):
            meta = assembly.lookup("GCF_000002445.2")
        # Degrades to a usable record rather than raising: the wrong-typed
        # organism is dropped, the accession survives.
        assert meta is None or meta.organism is None


class TestToMetadata:
    def test_maps_onto_schema_field_names(self):
        meta = assembly.AssemblyMetadata(
            accession="GCF_000002445.2",
            organism="Trypanosoma brucei brucei TREU927",
            strain="927/4 GUTat10.1",
            assembly_name="ASM244v1",
            submitter="Trypanosoma brucei consortium",
            bioproject="PRJNA11756",
            tax_id=185431,
            assembly_level="Chromosome",
            release_date="2005-12-14",
            paired_accession="GCA_000002445.1",
        )
        out = meta.to_metadata()
        assert out["assembly_accession"] == "GCF_000002445.2"
        assert out["organism"] == "Trypanosoma brucei brucei TREU927"
        assert out["strain"] == "927/4 GUTat10.1"
        assert out["reference_build"] == "ASM244v1"
        assert out["source"] == "Trypanosoma brucei consortium"
        assert out["bioproject"] == "PRJNA11756"
        assert out["tax_id"] == 185431
        assert out["assembly_level"] == "Chromosome"
        assert out["assembly_date"] == "2005-12-14"
        assert out["paired_accession"] == "GCA_000002445.1"

    def test_omits_absent_fields(self):
        """A sparse record must not write empty strings into metadata."""
        out = assembly.AssemblyMetadata(accession="GCA_000000001.1").to_metadata()
        assert out == {"assembly_accession": "GCA_000000001.1"}

    def test_stats_go_to_facts_not_metadata(self):
        """Statistics are measurements, not user-editable metadata."""
        meta = assembly.AssemblyMetadata(
            accession="GCF_000002445.2", contig_count=50, gc_percent=46.5,
            total_length=26075494, assembly_name="ASM244v1",
        )
        assert "contig_count" not in meta.to_metadata()
        facts = meta.to_facts()
        assert facts["ncbi_contig_count"] == 50
        assert facts["ncbi_gc_percent"] == 46.5
        assert facts["ncbi_total_length"] == 26075494
        assert facts["ncbi_assembly_name"] == "ASM244v1"
