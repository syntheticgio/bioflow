"""Salmon's command construction, output parsing, and the tx2gene refusal.

The refusal is the reason most of this file exists. A transcript-to-gene map
that silently falls back to "each transcript is its own gene" produces a
counts file that merges cleanly, passes every downstream check, and tests a
gene universe nobody intended -- the same silent-success shape that cost STAR
its index sidecars.
"""

import pytest

from app.errors import ValidationError
from app.pipelines import salmon_runner


# A real quant.sf header plus three rows. Columns are Name, Length,
# EffectiveLength, TPM, NumReads -- NumReads last, and fractional, which is
# the whole reason a summarization step exists.
QUANT_SF = """Name\tLength\tEffectiveLength\tTPM\tNumReads
tx1\t1500\t1350.0\t120.5\t340.7
tx2\t900\t750.0\t80.25\t112.3
tx3\t2000\t1850.0\t0.0\t0.0
"""


class TestParseQuant:
    def test_reads_num_reads_per_transcript(self):
        per_tx, _ = salmon_runner.parse_quant(QUANT_SF)
        assert per_tx == {"tx1": 340.7, "tx2": 112.3, "tx3": 0.0}

    def test_counts_detected_separately_from_total(self):
        _, facts = salmon_runner.parse_quant(QUANT_SF)
        assert facts["transcripts_in_index"] == 3
        # tx3 is in the index and got nothing. "Detected" is the signal that
        # separates a bad sample from a wrong reference; the total alone
        # cannot say which.
        assert facts["transcripts_detected"] == 2
        assert facts["estimated_reads"] == pytest.approx(453.0)

    def test_ignores_a_blank_trailing_line(self):
        per_tx, _ = salmon_runner.parse_quant(QUANT_SF + "\n")
        assert len(per_tx) == 3

    def test_empty_table_is_not_an_error_here(self):
        # A header with no rows is a real Salmon output for an empty input.
        # The handler decides whether that is a failure; the parser does not.
        per_tx, facts = salmon_runner.parse_quant(
            "Name\tLength\tEffectiveLength\tTPM\tNumReads\n"
        )
        assert per_tx == {}
        assert facts["transcripts_in_index"] == 0
