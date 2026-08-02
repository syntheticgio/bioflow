"""compleasm command construction and summary.txt parsing.

The fixture in TestParseSummary.REAL_SUMMARY is verbatim output from a real
compleasm 0.2.9 `run` against a small bacterial FASTA (Flye's own
ecoli_500kb.fasta test data), captured on 2026-08-02 -- not hand-written.
The `I:` line matters: reading compleasm's source in isolation suggested it
was dead code (the `analyze` subcommand's copy of this block has it
commented out), but the `run` subcommand actually used here writes it
unconditionally, which only running the real package showed.
"""

from pathlib import Path

from app.pipelines.completeness_runner import (
    CompletenessParams,
    build_completeness_command,
    build_download_command,
    parse_summary,
)


class TestBuildCompletenessCommand:
    def test_command_shape(self):
        cmd = build_completeness_command(
            compleasm_path="/usr/local/bin/compleasm",
            assembly=Path("/w/assembly.fasta"),
            out_dir=Path("/w/out"),
            library_path=Path("/data/lineages"),
            params=CompletenessParams(threads=4, lineage="bacteria", odb="odb12"),
        )
        assert cmd == [
            "/usr/local/bin/compleasm",
            "run",
            "-a",
            "/w/assembly.fasta",
            "-o",
            "/w/out",
            "-l",
            "bacteria",
            "--odb",
            "odb12",
            "--library_path",
            "/data/lineages",
            "-t",
            "4",
        ]

    def test_lineage_is_never_suffixed_with_an_odb_version(self):
        """compleasm's own download_lineage rewrites whatever suffix is
        present to match --odb (verified against a real run: requesting
        "bacteria_odb10" with the default --odb actually downloaded and
        scored bacteria_odb12). A caller passing a bare name here is the
        only way the suffix in the command matches what compleasm will
        actually use."""
        cmd = build_completeness_command(
            compleasm_path="compleasm",
            assembly=Path("/w/a.fasta"),
            out_dir=Path("/w/out"),
            library_path=Path("/data/lineages"),
            params=CompletenessParams(lineage="eukaryota", odb="odb10"),
        )
        lineage_index = cmd.index("-l") + 1
        assert cmd[lineage_index] == "eukaryota"
        assert "_odb" not in cmd[lineage_index]


class TestBuildDownloadCommand:
    def test_command_shape(self):
        cmd = build_download_command(
            compleasm_path="compleasm",
            lineage="bacteria",
            odb="odb12",
            library_path=Path("/data/lineages"),
        )
        assert cmd == [
            "compleasm",
            "download",
            "bacteria",
            "--odb",
            "odb12",
            "--library_path",
            "/data/lineages",
        ]


class TestParseSummary:
    REAL_SUMMARY = (
        "## lineage: bacteria_odb12\n"
        "S:12.93%, 15\n"
        "D:0.00%, 0\n"
        "F:0.00%, 0\n"
        "I:0.00%, 0\n"
        "M:87.07%, 101\n"
        "N:116\n"
    )

    def test_parses_a_real_run(self):
        facts = parse_summary(self.REAL_SUMMARY)
        assert facts["assembly_completeness_tool"] == "compleasm"
        assert facts["assembly_completeness_lineage"] == "bacteria_odb12"
        assert facts["assembly_completeness_total"] == 116
        assert facts["assembly_completeness_single_pct"] == 12.93
        assert facts["assembly_completeness_duplicated_pct"] == 0.0
        assert facts["assembly_completeness_fragmented_pct"] == 0.0
        assert facts["assembly_completeness_missing_pct"] == 87.07

    def test_complete_pct_is_single_plus_duplicated(self):
        text = (
            "## lineage: bacteria_odb12\n"
            "S:80.00%, 80\n"
            "D:10.00%, 10\n"
            "F:5.00%, 5\n"
            "M:5.00%, 5\n"
            "N:100\n"
        )
        facts = parse_summary(text)
        assert facts["assembly_completeness_complete_pct"] == 90.0

    def test_interspaced_line_does_not_break_parsing(self):
        """The I: line is real (verified against the installed package) but
        not a category BioFlow stores -- it must not appear as a fact and
        must not prevent the rest of the summary from parsing."""
        facts = parse_summary(self.REAL_SUMMARY)
        assert "assembly_completeness_interspaced_pct" not in facts
        assert not any("I:" in str(v) for v in facts.values())

    def test_retrocopy_line_does_not_break_parsing(self):
        """--retrocopy is opt-in and BioFlow never passes it, but the parser
        must not choke if a summary carries an R: line anyway."""
        text = (
            "## lineage: bacteria_odb12\n"
            "S:80.00%, 80\n"
            "D:5.00%, 5\n"
            "R:5.00%, 5\n"
            "F:5.00%, 5\n"
            "I:0.00%, 0\n"
            "M:5.00%, 5\n"
            "N:100\n"
        )
        facts = parse_summary(text)
        assert facts["assembly_completeness_total"] == 100
        assert facts["assembly_completeness_single_pct"] == 80.0

    def test_empty_text_returns_empty_dict(self):
        assert parse_summary("") == {}

    def test_garbage_text_returns_empty_dict_rather_than_raising(self):
        """A summary that failed to parse must not fail a job that spent
        possibly hours running miniprot and hmmsearch over the assembly."""
        assert parse_summary("not a compleasm summary at all\n") == {}

    def test_missing_total_line_returns_empty_dict(self):
        """Category lines with no N: total is not compleasm's format,
        whatever produced this text -- half a parse is not a fact."""
        text = "## lineage: bacteria_odb12\nS:80.00%, 80\nD:0.00%, 0\n"
        assert parse_summary(text) == {}

    def test_lineage_line_is_optional(self):
        """compleasm itself writes "## lineage: xx_xx" when self.lineage is
        None -- tolerate a summary missing the header line entirely rather
        than discarding otherwise-good category data."""
        text = "S:50.00%, 50\nD:0.00%, 0\nF:0.00%, 0\nM:50.00%, 50\nN:100\n"
        facts = parse_summary(text)
        assert facts["assembly_completeness_total"] == 100
        assert "assembly_completeness_lineage" not in facts

    def test_zero_total_genes_does_not_crash(self):
        text = "## lineage: bacteria_odb12\nN:0\n"
        assert parse_summary(text) == {}
