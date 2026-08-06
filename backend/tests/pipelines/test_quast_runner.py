"""QUAST command construction and output parsing.

The two fixtures below are verbatim from a real `quast 5.3.0` run on
2026-08-05 against real yeast data (GCA_000146045.2 chopped into four
contigs, three carrying deliberate junction errors, aligned against the
GCF_000146045.2 reference). Not invented text -- see the module docstring on
`quast_runner` for why a hand-built fixture is exactly the failure mode this
repo has hit before.

| Contig          | Constructed error                                | QUAST reports    |
|-----------------|---------------------------------------------------|------------------|
| `ctg_transloc`  | 100 kb chrI + 100 kb chrIV joined in one contig    | 1 translocation  |
| `ctg_inv`       | internal 50 kb segment reverse-complemented        | 2 inversions     |
| `ctg_reloc`     | two chrIV loci 600 kb apart joined                 | 1 relocation     |
| `ctg_clean`     | unmodified                                         | --               |
"""

from pathlib import Path

from app.pipelines import quast_runner as runner

# Real output, one run. Reference genome ~12.16 Mb, so `--min-contig`-scale
# NGA50/NGA90 are "-" here -- the fixture's 900 kb of contigs is far short of
# a whole assembly, and QUAST declines to compute them rather than guess.
REPORT_TSV = (
    "Assembly\tassembly\n"
    "# contigs (>= 0 bp)\t4\n"
    "# contigs (>= 1000 bp)\t4\n"
    "# contigs (>= 5000 bp)\t4\n"
    "# contigs (>= 10000 bp)\t4\n"
    "# contigs (>= 25000 bp)\t4\n"
    "# contigs (>= 50000 bp)\t4\n"
    "Total length (>= 0 bp)\t900000\n"
    "Total length (>= 1000 bp)\t900000\n"
    "Total length (>= 5000 bp)\t900000\n"
    "Total length (>= 10000 bp)\t900000\n"
    "Total length (>= 25000 bp)\t900000\n"
    "Total length (>= 50000 bp)\t900000\n"
    "# contigs\t4\n"
    "Largest contig\t300000\n"
    "Total length\t900000\n"
    "Reference length\t12157105\n"
    "GC (%)\t38.32\n"
    "Reference GC (%)\t38.15\n"
    "N50\t200000\n"
    "NG50\t-\n"
    "N90\t200000\n"
    "NG90\t-\n"
    "auN\t233333.3\n"
    "auNG\t17273.8\n"
    "L50\t2\n"
    "LG50\t-\n"
    "L90\t4\n"
    "LG90\t-\n"
    "# misassemblies\t4\n"
    "# misassembled contigs\t3\n"
    "Misassembled contigs length\t700000\n"
    "# local misassemblies\t0\n"
    "# scaffold gap ext. mis.\t0\n"
    "# scaffold gap loc. mis.\t0\n"
    "# unaligned mis. contigs\t0\n"
    "# unaligned contigs\t0 + 0 part\n"
    "Unaligned length\t0\n"
    "Genome fraction (%)\t7.403\n"
    "Duplication ratio\t1.000\n"
    "# N's per 100 kbp\t0.00\n"
    "# mismatches per 100 kbp\t0.00\n"
    "# indels per 100 kbp\t0.00\n"
    "Largest alignment\t200000\n"
    "Total aligned length\t900000\n"
    "NA50\t100002\n"
    "NGA50\t-\n"
    "NA90\t99998\n"
    "NGA90\t-\n"
    "auNA\t127777.8\n"
    "auNGA\t9459.5\n"
    "LA50\t3\n"
    "LGA50\t-\n"
    "LA90\t7\n"
    "LGA90\t-\n"
)

# Real output, the same run.
MISASSEMBLIES_REPORT_TSV = (
    "Assembly\tassembly\n"
    "# misassemblies\t4\n"
    "  # contig misassemblies\t4\n"
    "    # c. relocations\t1\n"
    "    # c. translocations\t1\n"
    "    # c. inversions\t2\n"
    "  # scaffold misassemblies\t0\n"
    "    # s. relocations\t0\n"
    "    # s. translocations\t0\n"
    "    # s. inversions\t0\n"
    "# misassembled contigs\t3\n"
    "Misassembled contigs length\t700000\n"
    "# local misassemblies\t0\n"
    "# scaffold gap ext. mis.\t0\n"
    "# scaffold gap loc. mis.\t0\n"
    "# unaligned mis. contigs\t0\n"
    "# mismatches\t0\n"
    "# indels\t0\n"
    "    # indels (<= 5 bp)\t0\n"
    "    # indels (> 5 bp)\t0\n"
    "Indels length\t0\n"
)

# Real output from a second run: the same reference genome chopped into
# 200 kb chunks with every third chunk reverse-complemented *whole*, no
# internal junction. Confirms a whole-contig inversion is not a
# misassembly -- contig orientation is arbitrary, and only a junction
# *inside* a contig that the reference contradicts counts.
CLEAN_MISASSEMBLIES_REPORT_TSV = (
    "Assembly\tassembly\n"
    "# misassemblies\t0\n"
    "  # contig misassemblies\t0\n"
    "    # c. relocations\t0\n"
    "    # c. translocations\t0\n"
    "    # c. inversions\t0\n"
    "  # scaffold misassemblies\t0\n"
    "    # s. relocations\t0\n"
    "    # s. translocations\t0\n"
    "    # s. inversions\t0\n"
    "# misassembled contigs\t0\n"
    "Misassembled contigs length\t0\n"
    "# local misassemblies\t0\n"
    "# scaffold gap ext. mis.\t0\n"
    "# scaffold gap loc. mis.\t0\n"
    "# unaligned mis. contigs\t0\n"
    "# mismatches\t0\n"
    "# indels\t0\n"
    "    # indels (<= 5 bp)\t0\n"
    "    # indels (> 5 bp)\t0\n"
    "Indels length\t0\n"
)


class TestBuildQuastCommand:
    def test_reference_based_mode_only(self):
        argv = runner.build_quast_command(
            quast_path="quast.py",
            assembly=Path("draft.fasta"),
            reference=Path("ref.fasta"),
            out_dir=Path("out"),
            threads=4,
        )
        assert argv == [
            "quast.py",
            "-o",
            "out",
            "-t",
            "4",
            "-r",
            "ref.fasta",
            "--min-contig",
            "500",
            "-l",
            "assembly",
            "draft.fasta",
        ]

    def test_assembly_is_positional_and_last(self):
        """No transposition trap here unlike RagTag, but assert the shape
        anyway: `-r` names the reference explicitly, so there is no
        ambiguity about which path is which."""
        argv = runner.build_quast_command(
            quast_path="quast.py",
            assembly=Path("draft.fasta"),
            reference=Path("ref.fasta"),
            out_dir=Path("out"),
            threads=1,
        )
        assert argv[-1] == "draft.fasta"
        assert argv[argv.index("-r") + 1] == "ref.fasta"

    def test_custom_min_contig(self):
        argv = runner.build_quast_command(
            quast_path="quast.py",
            assembly=Path("a.fasta"),
            reference=Path("r.fasta"),
            out_dir=Path("out"),
            threads=4,
            min_contig=1000,
        )
        assert argv[argv.index("--min-contig") + 1] == "1000"

    def test_label_is_a_fixed_value_not_the_assembly_name(self):
        """The security-relevant case: `-l` must carry a fixed label, never
        anything derived from the caller's own filename or object name.
        QUAST does not escape the label when it renders report.html --
        verified by exploiting it -- so a caller passing a user-controlled
        string here would reopen a stored XSS this module exists to close."""
        argv = runner.build_quast_command(
            quast_path="quast.py",
            assembly=Path("draft.fasta"),
            reference=Path("ref.fasta"),
            out_dir=Path("out"),
            threads=4,
            label="assembly",
        )
        assert argv[argv.index("-l") + 1] == "assembly"

    def test_no_gene_finding_flags(self):
        """None of the tooling --gene-finding/--rna-finding/
        --conserved-genes-finding need is installed (see
        install-quast.sh), and none of it is reference-based misassembly
        detection -- the one thing this application uses QUAST for."""
        argv = runner.build_quast_command(
            quast_path="quast.py",
            assembly=Path("a.fasta"),
            reference=Path("r.fasta"),
            out_dir=Path("out"),
            threads=4,
        )
        joined = " ".join(argv)
        assert "--gene-finding" not in joined
        assert "--rna-finding" not in joined
        assert "--conserved-genes-finding" not in joined


class TestParseReportTsv:
    def test_real_fixture(self):
        facts = runner.parse_report_tsv(REPORT_TSV)
        assert facts == {
            "assembly_misassembly_total": 4,
            "assembly_misassembly_contigs": 3,
            "assembly_misassembly_contigs_length": 700000,
            "assembly_misassembly_local": 0,
            "assembly_reference_genome_fraction_pct": 7.403,
            "assembly_reference_duplication_ratio": 1.0,
            "assembly_reference_mismatches_per_100kbp": 0.0,
            "assembly_reference_indels_per_100kbp": 0.0,
            "assembly_reference_unaligned_contigs": 0,
            "assembly_reference_unaligned_length": 0,
        }

    def test_no_contiguity_facts_leak_through(self):
        """The exclusion is a rule, not an oversight: N50 and friends are
        already computed for every FASTA at ingest by
        `storage/parsers._parse_fasta`, over the whole assembly rather than
        QUAST's --min-contig-filtered subset. Two facts that are supposed to
        agree, from different code paths with different cutoffs, is the bug
        `assembly_n50` was deleted for. This test exists to fail the moment
        someone "helpfully" widens the parser to include them."""
        facts = runner.parse_report_tsv(REPORT_TSV)
        excluded_from_n50_check = {"assembly_reference_nga50", "assembly_reference_nga90"}
        assert not any(key.startswith("sequence_") for key in facts)
        assert not any(
            "n50" in key.lower() or "n90" in key.lower()
            for key in facts
            if key not in excluded_from_n50_check
        )
        assert not any("l50" in key.lower() or "l90" in key.lower() for key in facts)
        assert not any("aun" in key.lower() for key in facts)
        assert "assembly_reference_total_length" not in facts

    def test_dash_values_are_omitted_not_stored_as_sentinel(self):
        """NGA50/NGA90 are '-' in this fixture -- the reference has no
        declared genome size for QUAST to compute them against. Omitted
        rather than stored as a magic string."""
        facts = runner.parse_report_tsv(REPORT_TSV)
        assert "assembly_reference_nga50" not in facts
        assert "assembly_reference_nga90" not in facts

    def test_unaligned_contigs_takes_only_the_whole_count(self):
        """'0 + 0 part' -- only the leading whole-contig count is stored;
        QUAST reports no length for the partial count, so it is not
        independently useful."""
        text = REPORT_TSV.replace(
            "# unaligned contigs\t0 + 0 part", "# unaligned contigs\t2 + 1 part"
        )
        facts = runner.parse_report_tsv(text)
        assert facts["assembly_reference_unaligned_contigs"] == 2

    def test_unaligned_contigs_is_an_int_not_a_float(self):
        """`2 == 2.0` in Python, so an equality-only assertion would not
        have caught this being parsed as a float -- the bug a real
        end-to-end run against QUAST output actually found. A contig count
        rendered as '0.0' reads as a measurement with false precision."""
        facts = runner.parse_report_tsv(REPORT_TSV)
        assert isinstance(facts["assembly_reference_unaligned_contigs"], int)

    def test_garbage_returns_empty_rather_than_raising(self):
        """A report that fails to parse must not fail a job that already
        spent minutes-to-hours running minimap2 and produced real output."""
        assert runner.parse_report_tsv("not a tsv at all") == {}

    def test_empty_string(self):
        assert runner.parse_report_tsv("") == {}


class TestParseMisassembliesReport:
    def test_real_fixture(self):
        facts = runner.parse_misassemblies_report(MISASSEMBLIES_REPORT_TSV)
        assert facts == {
            "assembly_misassembly_relocations": 1,
            "assembly_misassembly_translocations": 1,
            "assembly_misassembly_inversions": 2,
        }

    def test_internal_inversion_scores_two_not_one(self):
        """The bug this test exists for: a single 50 kb reverse-complemented
        segment inside one contig produced `# c. inversions: 2` in the real
        run, one per junction. `assembly_misassembly_inversions` is
        therefore a breakpoint count, not a contig count --
        `assembly_misassembly_contigs` (from parse_report_tsv) is the contig
        count, and the two must never be conflated in card copy."""
        facts = runner.parse_misassemblies_report(MISASSEMBLIES_REPORT_TSV)
        assert facts["assembly_misassembly_inversions"] == 2

    def test_whole_contig_inversion_is_not_a_misassembly(self):
        """A whole-contig inversion is not a misassembly -- verified against
        a real run: chopping the genome into chunks and
        reverse-complementing every third chunk *whole* gives
        `# misassemblies: 0`, correctly, because contig orientation is
        arbitrary. A junction *inside* a contig is what QUAST flags; an
        entire contig aligning cleanly in reverse asserts nothing false.
        Anyone testing this feature by inverting a sequence and expecting a
        nonzero count will wrongly conclude the tool is broken."""
        facts = runner.parse_misassemblies_report(CLEAN_MISASSEMBLIES_REPORT_TSV)
        assert facts == {
            "assembly_misassembly_relocations": 0,
            "assembly_misassembly_translocations": 0,
            "assembly_misassembly_inversions": 0,
        }

    def test_indentation_does_not_prevent_matching(self):
        """Rows are indented in the real file ('    # c. relocations'); the
        parser matches on the stripped key."""
        facts = runner.parse_misassemblies_report(MISASSEMBLIES_REPORT_TSV)
        assert facts["assembly_misassembly_relocations"] == 1

    def test_garbage_returns_empty_rather_than_raising(self):
        assert runner.parse_misassemblies_report("not a tsv at all") == {}

    def test_empty_string(self):
        assert runner.parse_misassemblies_report("") == {}
