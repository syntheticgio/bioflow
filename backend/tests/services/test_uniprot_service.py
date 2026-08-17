"""Launch validation for a UniProt download.

The rules that must hold before any job is queued, tested without HTTP --
the same shape as `test_assembly_service.py`.
"""

import pytest
from app.errors import ValidationError
from app.services import uniprot_service


class TestValidation:
    def test_a_request_with_neither_proteome_nor_accessions_is_rejected(self):
        with pytest.raises(ValidationError, match="proteome or at least one"):
            uniprot_service.validate_request(proteome_id=None, accessions=[])

    def test_a_malformed_proteome_id_is_rejected(self):
        """Queueing a download for it would fail an hour later with a
        UniProt error the user cannot act on."""
        with pytest.raises(ValidationError, match="proteome"):
            uniprot_service.validate_request(proteome_id="UP123", accessions=[])

    def test_a_malformed_accession_is_rejected(self):
        with pytest.raises(ValidationError, match="accession"):
            uniprot_service.validate_request(proteome_id=None, accessions=["EGFR"])

    def test_a_valid_proteome_passes(self):
        uniprot_service.validate_request(proteome_id="UP000002311", accessions=[])

    def test_valid_accessions_pass(self):
        uniprot_service.validate_request(
            proteome_id=None, accessions=["P0DTC2", "P00533"]
        )

    def test_a_request_naming_both_a_proteome_and_accessions_is_rejected(self):
        """The two disagree downstream: the query would fetch the accessions
        while the label and filename describe the proteome, producing a file
        named for 6,067 proteins that holds one."""
        with pytest.raises(ValidationError, match="not both"):
            uniprot_service.validate_request(
                proteome_id="UP000002311", accessions=["P0DTC2"]
            )

    def test_too_many_accessions_are_rejected(self):
        """The URL is a GET query string; a thousand OR clauses exceeds what
        UniProt accepts and fails opaquely."""
        with pytest.raises(ValidationError, match="at once"):
            uniprot_service.validate_request(
                proteome_id=None, accessions=[f"P{i:05d}" for i in range(600)]
            )


class TestLabel:
    def test_a_proteome_label_names_it_and_counts_proteins(self):
        label = uniprot_service.download_label(
            proteome_id="UP000002311",
            accessions=[],
            organism="Saccharomyces cerevisiae",
            protein_count=6067,
        )
        assert "UP000002311" in label
        assert "6,067" in label

    def test_a_proteome_label_without_a_count_still_reads(self):
        label = uniprot_service.download_label(
            proteome_id="UP000002311", accessions=[], organism=None, protein_count=None
        )
        assert "UP000002311" in label

    def test_a_picked_set_counts_the_proteins(self):
        label = uniprot_service.download_label(
            proteome_id=None,
            accessions=["P0DTC2", "P00533"],
            organism=None,
            protein_count=None,
        )
        assert "2" in label
        assert "UniProt" in label

    def test_one_picked_protein_reads_singular(self):
        label = uniprot_service.download_label(
            proteome_id=None, accessions=["P0DTC2"], organism=None, protein_count=None
        )
        assert "P0DTC2" in label


class TestFilename:
    def test_a_proteome_is_named_for_its_id(self):
        assert (
            uniprot_service.output_filename(
                proteome_id="UP000002311", accessions=[], reviewed_only=True
            )
            == "UP000002311_reviewed.fasta"
        )

    def test_an_unreviewed_proteome_says_so(self):
        """The 7x difference between these two files for human is invisible
        once they are both sitting in a project called the same thing."""
        assert (
            uniprot_service.output_filename(
                proteome_id="UP000002311", accessions=[], reviewed_only=False
            )
            == "UP000002311_all.fasta"
        )

    def test_a_single_accession_is_named_for_it(self):
        assert (
            uniprot_service.output_filename(
                proteome_id=None, accessions=["P0DTC2"], reviewed_only=True
            )
            == "P0DTC2.fasta"
        )

    def test_several_accessions_get_a_counted_name(self):
        assert (
            uniprot_service.output_filename(
                proteome_id=None,
                accessions=["P0DTC2", "P00533", "P0DTC1"],
                reviewed_only=True,
            )
            == "uniprot_3_proteins.fasta"
        )
