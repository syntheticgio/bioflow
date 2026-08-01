"""The query strings, asserted exactly.

These are not arbitrary. Each was measured against the live API on
2026-07-31, and the obvious-looking alternatives return zero rows:

- `proteome_type:1` returns 0 for every organism tried, including ones that
  do have a reference proteome. The working filter is `reference:true`.
- `organism_id:4932 AND reference:true` returns 0, because UniProt attaches
  yeast's reference proteome to the strain taxon 559292. The unfiltered
  `organism_id:4932` returns 360.

Asserting the literal string is the only way a future edit that "tidies" one
of these into the broken form gets caught.
"""

from app.metadata import uniprot


class TestProteomeQueries:
    def test_reference_proteome_for_a_taxon_uses_reference_true(self):
        """NOT proteome_type:1, which returns nothing for every organism."""
        q = uniprot.reference_proteome_query(559292)
        assert q == "organism_id:559292 AND reference:true"
        assert "proteome_type" not in q

    def test_all_proteomes_for_a_taxon_has_no_type_filter(self):
        """The fallback for a species-level taxon such as 4932, where the
        reference query returns nothing but 360 proteomes exist."""
        assert uniprot.all_proteomes_query(4932) == "organism_id:4932"

    def test_organism_name_search_has_no_type_filter(self):
        """Measured: adding one returns 0 for a name that otherwise gives
        481 hits with the right proteome ranked first."""
        q = uniprot.organism_name_query("Saccharomyces cerevisiae")
        assert q == 'organism_name:"Saccharomyces cerevisiae"'
        assert "reference" not in q

    def test_organism_name_quotes_are_stripped_from_input(self):
        """A pasted name may arrive already quoted; doubling them produces a
        query that matches nothing."""
        assert (
            uniprot.organism_name_query('"Homo sapiens"')
            == 'organism_name:"Homo sapiens"'
        )


class TestDownloadQueries:
    def test_a_whole_proteome_reviewed_only(self):
        assert uniprot.download_query(
            proteome_id="UP000002311", accessions=[], reviewed_only=True
        ) == "proteome:UP000002311 AND reviewed:true"

    def test_a_whole_proteome_including_unreviewed(self):
        """No reviewed clause at all -- that is what 'everything' means, and
        it is the 147,506-protein case for human."""
        assert uniprot.download_query(
            proteome_id="UP000002311", accessions=[], reviewed_only=False
        ) == "proteome:UP000002311"

    def test_picked_accessions_ignore_the_reviewed_flag(self):
        """The user named these entries. Filtering out an unreviewed one they
        explicitly asked for would silently return fewer proteins than were
        selected."""
        assert uniprot.download_query(
            proteome_id=None, accessions=["P0DTC2", "P00533"], reviewed_only=True
        ) == "accession:P0DTC2 OR accession:P00533"

    def test_a_single_accession(self):
        assert uniprot.download_query(
            proteome_id=None, accessions=["P0DTC2"], reviewed_only=False
        ) == "accession:P0DTC2"


class TestStreamUrl:
    def test_requests_compressed_fasta(self):
        url = uniprot.stream_url("proteome:UP000002311")
        assert url.startswith("https://rest.uniprot.org/uniprotkb/stream?")
        assert "format=fasta" in url
        assert "compressed=true" in url
        assert "query=proteome%3AUP000002311" in url
