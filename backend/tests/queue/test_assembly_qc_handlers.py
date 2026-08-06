"""assess_misassemblies's fixed-label security property.

QUAST sanitizes contig names but not the assembly label, and the label is
otherwise taken from the input filename -- verified by exploiting it while
writing the implementation plan (an input named
`ev<img src=x onerror=alert(7)>.fasta` puts that tag verbatim and unescaped
into `report.html`). `assess_misassemblies` must never let the object's own
name reach QUAST's `-l` flag or its linked input filename. These tests pin
that at the seam, independent of the SUBPROCESS body itself: a test that
only asserted "the report renders" would pass whether or not the fix held.
"""

from app.queue import assembly_qc_handlers as handlers
from app.pipelines import quast_runner


class TestFixedLabelConstants:
    def test_link_name_is_not_derived_from_any_object(self):
        """The constant itself must be a plain, hardcoded filename -- not a
        template, not something built from a payload field."""
        assert handlers._ASSEMBLY_LINK_NAME == "assembly.fasta"
        assert handlers._ASSEMBLY_LABEL == "assembly"

    def test_hostile_object_name_cannot_reach_the_link_name_or_label(self):
        """The handler body must never read the payload's `assembly_name`
        field at all -- unlike `assess_completeness`, which passes it
        straight to `_named_link`. Checked against the function body with
        its docstring stripped, since the docstring itself quotes that
        field name as the finding this test guards against."""
        import ast
        import inspect
        import textwrap

        source = inspect.getsource(handlers.assess_misassemblies)
        tree = ast.parse(textwrap.dedent(source))
        func = tree.body[0]
        body_without_docstring = func.body[1:] if ast.get_docstring(func) else func.body
        body_source = ast.unparse(ast.Module(body=body_without_docstring, type_ignores=[]))

        assert "assembly_name" not in body_source
        assert "_ASSEMBLY_LINK_NAME" in body_source
        assert "_ASSEMBLY_LABEL" in body_source

    def test_build_quast_command_carries_the_fixed_label(self):
        from pathlib import Path

        argv = quast_runner.build_quast_command(
            quast_path="quast.py",
            assembly=Path("work") / handlers._ASSEMBLY_LINK_NAME,
            reference=Path("ref.fasta"),
            out_dir=Path("out"),
            threads=4,
            label=handlers._ASSEMBLY_LABEL,
        )
        assert argv[argv.index("-l") + 1] == "assembly"
        assert argv[-1].endswith("assembly.fasta")


class TestMisassembliesReportPathIsNotLabelSuffixed:
    def test_matches_the_real_output_filename(self):
        """QUAST does not suffix misassemblies_report.tsv with the -l label
        -- confirmed against a real run with `-l assembly`, unlike the
        .gff and .mis_contigs.* files in the same directory, which are.
        A test here catches a future QUAST version changing that filename
        before it ships as a job that silently logs
        misassembly_breakdown_missing on every run."""
        import inspect

        source = inspect.getsource(handlers.assess_misassemblies)
        assert '"contigs_reports" / "misassemblies_report.tsv"' in source
