"""Unit tests for GenBank ORIGIN sequence extraction."""

from app.pipelines import genbank_sequence


class TestSequenceLine:
    def test_strips_counter_and_spaces(self):
        line = "        1 agcttttcat tctgactgca acgggcaata"
        assert genbank_sequence.sequence_line_bases(line) == (
            "agcttttcattctgactgcaacgggcaata"
        )

    def test_handles_a_later_counter(self):
        line = "       61 tgatagcagc ttctgaactg"
        assert genbank_sequence.sequence_line_bases(line) == "tgatagcagcttctgaactg"

    def test_blank_line_yields_nothing(self):
        assert genbank_sequence.sequence_line_bases("   ") == ""

    def test_line_without_counter_still_reads(self):
        # Not every writer emits the counter; the bases are what matter.
        assert genbank_sequence.sequence_line_bases("agct tttc") == "agcttttc"
