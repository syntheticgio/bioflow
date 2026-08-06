from pathlib import Path

import pytest

from app.pipelines import craq_runner


class TestBuildCraqCommand:
    def test_both_libraries(self):
        cmd = craq_runner.build_craq_command(
            craq_path="craq",
            assembly=Path("/w/assembly.fasta"),
            ngs_bam=Path("/w/ngs.bam"),
            sms_bam=Path("/w/sms.bam"),
            out_dir=Path("/w/out"),
            threads=4,
        )
        assert cmd[0] == "craq"
        assert "-g" in cmd and "/w/assembly.fasta" in cmd
        assert "-ngs" in cmd and "/w/ngs.bam" in cmd
        assert "-sms" in cmd and "/w/sms.bam" in cmd
        assert "-D" in cmd and "/w/out" in cmd

    def test_ngs_only_omits_sms_flag(self):
        cmd = craq_runner.build_craq_command(
            craq_path="craq",
            assembly=Path("/w/assembly.fasta"),
            ngs_bam=Path("/w/ngs.bam"),
            sms_bam=None,
            out_dir=Path("/w/out"),
            threads=4,
        )
        assert "-sms" not in cmd
        assert "-ngs" in cmd

    def test_sms_only_omits_ngs_flag(self):
        cmd = craq_runner.build_craq_command(
            craq_path="craq",
            assembly=Path("/w/assembly.fasta"),
            ngs_bam=None,
            sms_bam=Path("/w/sms.bam"),
            out_dir=Path("/w/out"),
            threads=4,
        )
        assert "-ngs" not in cmd
        assert "-sms" in cmd

    def test_plotting_is_never_enabled(self):
        cmd = craq_runner.build_craq_command(
            craq_path="craq",
            assembly=Path("/w/assembly.fasta"),
            ngs_bam=Path("/w/ngs.bam"),
            sms_bam=Path("/w/sms.bam"),
            out_dir=Path("/w/out"),
            threads=4,
        )
        assert "-pl" not in cmd

    def test_break_is_off_by_default(self):
        cmd = craq_runner.build_craq_command(
            craq_path="craq",
            assembly=Path("/w/assembly.fasta"),
            ngs_bam=Path("/w/ngs.bam"),
            sms_bam=Path("/w/sms.bam"),
            out_dir=Path("/w/out"),
            threads=4,
        )
        assert "-b" not in cmd

    def test_break_when_requested(self):
        cmd = craq_runner.build_craq_command(
            craq_path="craq",
            assembly=Path("/w/assembly.fasta"),
            ngs_bam=Path("/w/ngs.bam"),
            sms_bam=Path("/w/sms.bam"),
            out_dir=Path("/w/out"),
            threads=4,
            break_chimera=True,
        )
        assert cmd[cmd.index("-b") + 1] == "T"

    def test_no_bam_at_all_is_a_programming_error(self):
        with pytest.raises(ValueError):
            craq_runner.build_craq_command(
                craq_path="craq",
                assembly=Path("/w/assembly.fasta"),
                ngs_bam=None,
                sms_bam=None,
                out_dir=Path("/w/out"),
                threads=4,
            )


# Row key is "Genome", not "all" -- verified against a real 1.10 run on
# 2026-08-06 and confirmed hardcoded in
# src/final_short_report_minlen.pl:42. An earlier version of this fixture
# used "all", which no real report ever contains; every test below passed
# against a fixture that didn't match reality until a real end-to-end run
# caught it.
_REPORT = (
    "Short Report:\n"
    "#Chr\tCovered.Rate\tLow-conf.Rate\tAvg.CRH\tAvg.CSH\t"
    "Avg.CRE(R-AQI)\tAvg.CSE(S-AQI)\tAQI\n"
    "Genome\t0.998\t0.012\t1.250\t0.310\t0.512(94.881)\t0.104(97.220)\t96.031\n"
)


class TestParseFinalReport:
    def test_both_libraries_parses_everything(self):
        facts = craq_runner.parse_final_report(_REPORT, has_ngs=True, has_sms=True)
        assert facts["assembly_error_r_aqi"] == 94.881
        assert facts["assembly_error_s_aqi"] == 97.220
        assert facts["assembly_error_aqi"] == 96.031
        assert facts["assembly_error_covered_rate"] == 0.998
        assert facts["assembly_error_low_confidence_rate"] == 0.012

    def test_aqi_values_are_floats(self):
        facts = craq_runner.parse_final_report(_REPORT, has_ngs=True, has_sms=True)
        assert isinstance(facts["assembly_error_r_aqi"], float)
        assert isinstance(facts["assembly_error_s_aqi"], float)

    def test_ngs_only_omits_structural_facts_entirely(self):
        """The load-bearing test. A short-only run's report still *contains*
        CSE, S-AQI and AQI columns -- upstream's formatter always prints
        them -- and they are meaningless. Absent, never zero."""
        facts = craq_runner.parse_final_report(_REPORT, has_ngs=True, has_sms=False)
        assert "assembly_error_s_aqi" not in facts
        assert "assembly_error_aqi" not in facts
        assert facts["assembly_error_r_aqi"] == 94.881

    def test_sms_only_keeps_regional_facts(self):
        """Upstream says CRE is merely undercounted without short reads, not
        undetectable -- so unlike CSE it is kept, with the caveat carried by
        the has_ngs fact rather than by dropping the number."""
        facts = craq_runner.parse_final_report(_REPORT, has_ngs=False, has_sms=True)
        assert facts["assembly_error_r_aqi"] == 94.881
        assert facts["assembly_error_s_aqi"] == 97.220

    def test_unparseable_returns_empty(self):
        assert craq_runner.parse_final_report("garbage", has_ngs=True, has_sms=True) == {}

    def test_missing_genome_row_returns_empty(self):
        text = (
            "Short Report:\n"
            "#Chr\tCovered.Rate\tLow-conf.Rate\tAvg.CRH\tAvg.CSH\t"
            "Avg.CRE(R-AQI)\tAvg.CSE(S-AQI)\tAQI\n"
        )
        assert craq_runner.parse_final_report(text, has_ngs=True, has_sms=True) == {}

    def test_per_contig_rows_are_not_mistaken_for_the_genome_row(self):
        """A real report's per-contig rows (e.g. `BK006938.2\t...`) must not
        be parsed as the whole-assembly summary -- only the literal
        `Genome` row is. Regression for the same class of bug the row-key
        fix itself was: a plausible-looking row that isn't actually the
        aggregate."""
        text = (
            "Short Report:\n"
            "#Chr\tCovered.Rate\tLow-conf.Rate\tAvg.CRH\tAvg.CSH\t"
            "Avg.CRE(R-AQI)\tAvg.CSE(S-AQI)\tAQI\n"
            "BK006938.2\t0.781\t0.000\t91.484\t0.000\t"
            "49.293(0.723)\t0.000(100.000)\t1.436\n"
        )
        assert craq_runner.parse_final_report(text, has_ngs=True, has_sms=True) == {}


class TestCountBedRecords:
    def test_counts_lines(self, tmp_path):
        bed = tmp_path / "out_final.CRE.bed"
        bed.write_text("chr1\t100\t101\tchr1:100\tCRE\nchr1\t200\t201\tchr1:200\tCRE\n")
        assert craq_runner.count_bed_records(bed) == 2

    def test_missing_file_is_none_not_zero(self, tmp_path):
        """A .bed CRAQ never wrote is unmeasured, not a count of zero --
        same reasoning as the CSE omission."""
        assert craq_runner.count_bed_records(tmp_path / "absent.bed") is None

    def test_empty_file_is_zero(self, tmp_path):
        bed = tmp_path / "out_final.CRE.bed"
        bed.write_text("")
        assert craq_runner.count_bed_records(bed) == 0

    def test_counts_are_ints(self, tmp_path):
        bed = tmp_path / "out_final.CRE.bed"
        bed.write_text("chr1\t100\t101\tchr1:100\tCRE\n")
        assert isinstance(craq_runner.count_bed_records(bed), int)
