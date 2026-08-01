"""What kind of thing did the user type into the one accession box?

The cases that matter are the near-misses. `EGFR` is a gene symbol, not an
accession, and `GCF_000002445.2` belongs to the NCBI dialog -- both must reach
the free-text branch rather than being sent to UniProt as accessions.
"""

from app.metadata import uniprot


class TestClassify:
    def test_a_proteome_id(self):
        assert uniprot.classify("UP000002311") == uniprot.InputKind.PROTEOME

    def test_a_proteome_id_is_case_insensitive_and_trimmed(self):
        assert uniprot.classify("  up000002311 ") == uniprot.InputKind.PROTEOME

    def test_a_single_accession(self):
        assert uniprot.classify("P0DTC2") == uniprot.InputKind.ACCESSIONS

    def test_a_long_form_accession(self):
        """The 10-character form. Real: it is the first record in yeast's
        own proteome FASTA."""
        assert uniprot.classify("A0A0B7P3V8") == uniprot.InputKind.ACCESSIONS

    def test_several_accessions_separated_by_spaces_or_commas(self):
        assert uniprot.classify("P00533, P0DTC2") == uniprot.InputKind.ACCESSIONS

    def test_a_bare_taxon_id(self):
        assert uniprot.classify("4932") == uniprot.InputKind.TAXON

    def test_a_gene_symbol_is_not_an_accession(self):
        """EGFR looks accession-shaped to a loose regex. Sending it as one
        returns nothing, where a text search finds the protein."""
        assert uniprot.classify("EGFR") == uniprot.InputKind.TEXT

    def test_an_ncbi_assembly_accession_is_text(self):
        """It belongs to the other dialog. Reaching the text branch produces
        an empty search rather than a confusing UniProt error."""
        assert uniprot.classify("GCF_000002445.2") == uniprot.InputKind.TEXT

    def test_an_sra_accession_is_text(self):
        assert uniprot.classify("SRR11768093") == uniprot.InputKind.TEXT

    def test_a_protein_name_is_text(self):
        assert uniprot.classify("spike glycoprotein") == uniprot.InputKind.TEXT

    def test_empty_input_is_text(self):
        assert uniprot.classify("") == uniprot.InputKind.TEXT


class TestParseAccessions:
    def test_splits_on_commas_and_whitespace(self):
        assert uniprot.parse_accessions("P00533, P0DTC2  P0DTC1") == [
            "P00533",
            "P0DTC2",
            "P0DTC1",
        ]

    def test_uppercases_and_deduplicates_preserving_order(self):
        assert uniprot.parse_accessions("p00533 P00533 p0dtc2") == [
            "P00533",
            "P0DTC2",
        ]
