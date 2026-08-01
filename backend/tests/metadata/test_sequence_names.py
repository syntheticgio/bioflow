"""Per-sequence chromosome names from NCBI's sequence_reports endpoint.

Every fixture is a real captured response. The Aspergillus one exists
specifically because two of its records share `chr_name: "11"` -- labelling by
chr_name alone would put two bars reading "11" on the strip, one of them the
largest bar there is.
"""

import json
from pathlib import Path
from unittest.mock import patch

from app.metadata import ncbi_assembly, enrich
from app.models import FormatKind

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


YEAST = "ncbi_seqreports_GCF_000146045.2.json"
ASPERGILLUS = "ncbi_seqreports_GCF_000002445.2.json"
HUMAN = "ncbi_seqreports_GCF_000001405.40_slice.json"


class TestParseSequenceReports:
    def test_maps_both_accession_namespaces_to_one_label(self):
        """One lookup must label the GCA file and the GCF file alike."""
        labels = ncbi_assembly.parse_sequence_reports(_load(YEAST))
        assert labels["NC_001133.9"] == "I"
        assert labels["BK006935.2"] == "I"

    def test_labels_the_mitochondrion(self):
        labels = ncbi_assembly.parse_sequence_reports(_load(YEAST))
        assert labels["NC_001224.1"] == "MT"

    def test_distinguishes_records_sharing_a_chr_name(self):
        """The regression a naive chr_name implementation produces.

        Both of these report chr_name "11"; they are different scaffolds and
        must not both read "11".
        """
        labels = ncbi_assembly.parse_sequence_reports(_load(ASPERGILLUS))
        assert labels["NT_165288.1"] == "chr11-scaffold01"
        assert labels["NT_165287.1"] == "chr11-scaffold02"
        assert labels["NT_165288.1"] != labels["NT_165287.1"]

    def test_assembled_molecules_still_use_chr_name(self):
        labels = ncbi_assembly.parse_sequence_reports(_load(ASPERGILLUS))
        assert labels["NC_008409.1"] == "1"
        assert labels["NC_005063.2"] == "2"

    def test_human_unlocalized_scaffolds_get_their_own_names(self):
        labels = ncbi_assembly.parse_sequence_reports(_load(HUMAN))
        assert labels["NC_000001.11"] == "1"
        assert labels["NT_187361.1"] == "HSCHR1_CTG1_UNLOCALIZED"
        assert labels["NC_012920.1"] == "MT"

    def test_caps_the_map(self):
        """Bounded like sequence_lengths: the strip draws 24 bars and lists the
        rest, so labels past the cap have nothing to label."""
        reports = [
            {
                "chr_name": str(i),
                "refseq_accession": f"NC_{i:06d}.1",
                "role": "assembled-molecule",
                "sort_order": i,
            }
            for i in range(200)
        ]
        labels = ncbi_assembly.parse_sequence_reports({"reports": reports})
        assert len(labels) <= ncbi_assembly.MAX_STORED_LABELS

    def test_survives_malformed_payloads(self):
        """Same never-raises contract as parse_report."""
        assert ncbi_assembly.parse_sequence_reports({}) == {}
        assert ncbi_assembly.parse_sequence_reports({"reports": None}) == {}
        assert ncbi_assembly.parse_sequence_reports({"reports": "nope"}) == {}
        assert ncbi_assembly.parse_sequence_reports({"reports": [None, 3, "x"]}) == {}
        # A record with no usable accession contributes nothing but must not raise.
        assert ncbi_assembly.parse_sequence_reports({"reports": [{"chr_name": "I"}]}) == {}
        # A record with an accession but no name has nothing to say either.
        assert (
            ncbi_assembly.parse_sequence_reports(
                {"reports": [{"refseq_accession": "NC_1.1"}]}
            )
            == {}
        )


class TestLookupSequenceNames:
    def test_returns_labels_from_a_real_payload(self):
        body = (FIXTURES / YEAST).read_bytes()
        with patch("app.metadata.ncbi_assembly._get", return_value=body) as get:
            labels = ncbi_assembly.lookup_sequence_names("GCF_000146045.2")
        assert labels["NC_001133.9"] == "I"
        assert "sequence_reports" in get.call_args[0][0]

    def test_rejects_a_malformed_accession_without_calling_out(self):
        with patch("app.metadata.ncbi_assembly._get") as get:
            assert ncbi_assembly.lookup_sequence_names("not-an-accession") is None
        get.assert_not_called()

    def test_returns_none_when_the_request_fails(self):
        with patch("app.metadata.ncbi_assembly._get", return_value=None):
            assert ncbi_assembly.lookup_sequence_names("GCF_000146045.2") is None

    def test_returns_none_on_unparseable_json(self):
        with patch("app.metadata.ncbi_assembly._get", return_value=b"<html>nope"):
            assert ncbi_assembly.lookup_sequence_names("GCF_000146045.2") is None

    def test_returns_none_rather_than_an_empty_map(self):
        """An empty map and a failed lookup are the same to the caller, and
        None keeps a meaningless `sequence_labels: {}` out of facts."""
        with patch("app.metadata.ncbi_assembly._get", return_value=b'{"reports": []}'):
            assert ncbi_assembly.lookup_sequence_names("GCF_000146045.2") is None

    def test_never_raises_when_the_helper_explodes(self):
        with patch("app.metadata.ncbi_assembly._get", side_effect=RuntimeError("boom")):
            assert ncbi_assembly.lookup_sequence_names("GCF_000146045.2") is None


class TestEnrichmentStoresLabels:
    def _meta(self):
        return ncbi_assembly.AssemblyMetadata(
            accession="GCF_000146045.2", assembly_name="R64"
        )

    def test_labels_land_in_facts(self):
        labels = {"NC_001133.9": "I"}
        with (
            patch("app.metadata.ncbi_assembly.lookup", return_value=self._meta()),
            patch("app.metadata.ncbi_assembly.lookup_sequence_names", return_value=labels),
        ):
            result = enrich.enrich_from_assembly(
                filename="GCF_000146045.2_R64_genomic.fna",
                existing_metadata={},
                format_kind=FormatKind.FASTA,
            )
        assert result.facts["sequence_labels"] == labels

    def test_a_failed_name_lookup_leaves_the_rest_intact(self):
        """The names are a bonus. Losing them must not cost the stats that the
        assembly lookup already succeeded in fetching."""
        with (
            patch("app.metadata.ncbi_assembly.lookup", return_value=self._meta()),
            patch("app.metadata.ncbi_assembly.lookup_sequence_names", return_value=None),
        ):
            result = enrich.enrich_from_assembly(
                filename="GCF_000146045.2_R64_genomic.fna",
                existing_metadata={},
                format_kind=FormatKind.FASTA,
            )
        assert "sequence_labels" not in result.facts
        assert result.facts["ncbi_assembly_name"] == "R64"

    def test_a_raising_name_lookup_does_not_break_ingest(self):
        with (
            patch("app.metadata.ncbi_assembly.lookup", return_value=self._meta()),
            patch(
                "app.metadata.ncbi_assembly.lookup_sequence_names",
                side_effect=RuntimeError("boom"),
            ),
        ):
            result = enrich.enrich_from_assembly(
                filename="GCF_000146045.2_R64_genomic.fna",
                existing_metadata={},
                format_kind=FormatKind.FASTA,
            )
        assert "sequence_labels" not in result.facts
        assert result.facts["ncbi_assembly_name"] == "R64"
