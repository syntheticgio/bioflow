"""winnowmap's meryl-built repetitive-k-mer index commands.

Verified end to end on a real aarch64 build during the design pass: `meryl
count k=15` -> `meryl print greater-than distinct=0.9998` -> `winnowmap -W
... -ax map-pb` -> a sorted BAM, all synthetic reads aligned. These tests
pin the pure command-construction half of that chain.
"""

from pathlib import Path

from app.pipelines import winnowmap_runner


class TestMerylCountCommand:
    def test_counts_the_assembly_not_reads(self):
        """This is what distinguishes winnowmap's meryl step from Merqury's:
        the assembly's k-mers, not the read set's."""
        cmd = winnowmap_runner.build_meryl_count_command(
            meryl_path="/opt/meryl/bin/meryl",
            k=15,
            reference=Path("/w/assembly.fasta"),
            output=Path("/w/winnowmap.meryl"),
            threads=4,
        )
        assert "count" in cmd
        assert "k=15" in cmd
        assert "threads=4" in cmd
        assert cmd[-1] == "/w/assembly.fasta"
        assert "output" in cmd
        assert "/w/winnowmap.meryl" in cmd

    def test_k_reaches_the_command(self):
        cmd = winnowmap_runner.build_meryl_count_command(
            meryl_path="meryl",
            k=21,
            reference=Path("/w/assembly.fasta"),
            output=Path("/w/db.meryl"),
        )
        assert "k=21" in cmd


class TestMerylPrintRepetitiveCommand:
    def test_uses_greater_than_distinct(self):
        """GCI's own README example: `meryl print greater-than
        distinct=0.9998 merylDB`."""
        cmd = winnowmap_runner.build_meryl_print_repetitive_command(
            meryl_path="meryl", distinct=0.9998, database=Path("/w/db.meryl")
        )
        assert cmd == ["meryl", "print", "greater-than", "distinct=0.9998", "/w/db.meryl"]


class TestMerylPrintRepetitiveShellCommand:
    def test_redirects_stdout_to_the_output_file(self):
        cmd = winnowmap_runner.build_meryl_print_repetitive_shell_command(
            meryl_path="meryl",
            distinct=0.9998,
            database=Path("/w/db.meryl"),
            output=Path("/w/genome.fna.repetitive_k15.txt"),
        )
        assert cmd[0] == "/bin/sh"
        assert cmd[1] == "-c"
        script = cmd[2]
        assert "meryl print greater-than distinct=0.9998" in script
        assert "> /w/genome.fna.repetitive_k15.txt" in script

    def test_quotes_paths_with_shell_metacharacters(self):
        """`output` and `database` derive from paths this application
        chose, but neither is guaranteed free of shell metacharacters --
        the same reasoning `align_runner.build_align_command`'s pipe
        quoting is built on. A dangerous path must appear only inside a
        shell-quoted token, never as a bare, executable shell fragment."""
        import shlex

        cmd = winnowmap_runner.build_meryl_print_repetitive_shell_command(
            meryl_path="meryl",
            distinct=0.9998,
            database=Path("/w/a b.meryl"),
            output=Path("/w/out; rm -rf /.txt"),
        )
        script = cmd[2]
        assert "'/w/a b.meryl'" in script
        tokens = shlex.split(script)
        assert "/w/out; rm -rf /.txt" in tokens
        assert "rm" not in [t for t in tokens if t != "/w/out; rm -rf /.txt"]
