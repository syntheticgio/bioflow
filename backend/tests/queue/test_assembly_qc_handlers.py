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

from pathlib import Path

import pytest

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


class TestCopyReport:
    """`_copy_report` -- selective, not a directory copy, since QUAST's
    `out_dir` also holds report.tex/report.pdf/transposed_report* and
    per-tool stdout/stderr logs this application has no reader for."""

    def _ctx(self, tmp_path, object_id="obj1"):
        from app.queue.registry import JobContext

        return JobContext(
            job_id="job1",
            payload={"object_id": object_id},
            epoch=1,
            attempts=1,
            owner="local",
        )

    def _real_quast_output(self, tmp_path):
        """The subset of a real quast.py -l assembly run's out_dir this
        function reads -- named and shaped exactly as verified against a
        real 5.3.0 run on 2026-08-05."""
        out_dir = tmp_path / "out"
        (out_dir / "contigs_reports").mkdir(parents=True)
        (out_dir / "icarus_viewers").mkdir(parents=True)
        (out_dir / "report.html").write_text("<html>report</html>")
        (out_dir / "icarus.html").write_text("<html>icarus</html>")
        (out_dir / "icarus_viewers" / "alignment_viewer.html").write_text(
            "<html>alignment viewer</html>"
        )
        (out_dir / "icarus_viewers" / "contig_size_viewer.html").write_text(
            "<html>contig size viewer</html>"
        )
        (out_dir / "contigs_reports" / "assembly.misassemblies.gff").write_text(
            "##gff-version 3\n"
        )
        # Present in a real run's out_dir, never read by this function --
        # confirms the copy is selective rather than a directory copy.
        (out_dir / "report.pdf").write_bytes(b"not a real pdf")
        (out_dir / "report.tex").write_text("not read either")
        return out_dir

    def test_copies_report_and_icarus_into_qc_reports(self, tmp_path, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
        out_dir = self._real_quast_output(tmp_path)
        ctx = self._ctx(tmp_path)

        result = handlers._copy_report(ctx, out_dir)

        report_dir = settings.qc_reports_dir / "obj1" / "quast"
        assert result == "quast/report.html"
        assert (report_dir / "report.html").read_text() == "<html>report</html>"
        assert (report_dir / "icarus.html").read_text() == "<html>icarus</html>"
        assert (
            report_dir / "icarus_viewers" / "alignment_viewer.html"
        ).read_text() == "<html>alignment viewer</html>"
        assert (report_dir / "misassemblies.gff").read_text() == "##gff-version 3\n"

    def test_does_not_copy_files_it_never_reads(self, tmp_path, monkeypatch):
        """The selective-copy property itself: report.pdf and report.tex
        exist in a real out_dir and must not end up in qc_reports/."""
        from app.config import settings

        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
        out_dir = self._real_quast_output(tmp_path)
        ctx = self._ctx(tmp_path)

        handlers._copy_report(ctx, out_dir)

        report_dir = settings.qc_reports_dir / "obj1" / "quast"
        assert not (report_dir / "report.pdf").exists()
        assert not (report_dir / "report.tex").exists()

    def test_missing_report_html_returns_none(self, tmp_path, monkeypatch):
        """Nothing to copy -- the caller (assess_misassemblies) already
        raised before reaching this point in the real handler, but this
        function's own contract must not assume that."""
        from app.config import settings

        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
        empty_out_dir = tmp_path / "empty_out"
        empty_out_dir.mkdir()
        ctx = self._ctx(tmp_path)

        assert handlers._copy_report(ctx, empty_out_dir) is None

    def test_missing_icarus_and_gff_are_not_fatal(self, tmp_path, monkeypatch):
        """report.html alone is enough to succeed -- icarus.html and the
        .gff are extras, not required for the report link itself to work."""
        from app.config import settings

        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "report.html").write_text("<html>report only</html>")
        ctx = self._ctx(tmp_path)

        result = handlers._copy_report(ctx, out_dir)

        assert result == "quast/report.html"

    def test_no_object_id_returns_none(self, tmp_path, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
        out_dir = self._real_quast_output(tmp_path)
        ctx = self._ctx(tmp_path, object_id=None)
        ctx.payload["object_id"] = None

        assert handlers._copy_report(ctx, out_dir) is None


class TestAssessAssemblyContinuity:
    """`assess_assembly_continuity` -- GCI's handler. Modeled closely on
    `assess_assembly_errors` (CRAQ): a BAM's `.bai` must land beside its
    fixed-name link in the workdir, the same `_link_bam_index` contract CRAQ's
    handler relies on, or GCI fails deep inside a samtools call rather than
    with a clear error.

    Mocks at the same seams `assess_assembly_errors` itself is built from:
    `tools.gci()` (fake but `available`), `run_subprocess` (writes a fake
    `gci.gci` and returns 0), and `_resolve_input` (resolves straight from
    tmp_path fixture files instead of content-addressed storage) -- there is
    no existing end-to-end SUBPROCESS-mode test for CRAQ or QUAST in this
    file to copy verbatim, so this follows the narrower-grain approach the
    plan calls out as acceptable.
    """

    def _ctx(self, payload):
        from app.queue.registry import JobContext

        return JobContext(
            job_id="job1",
            payload=payload,
            epoch=1,
            attempts=1,
            owner="local",
        )

    def _fake_gci_output(self):
        return (
            "Chr\tExp_N50\tObs_N50\tExp_contig\tObs_contig\tGCI\n"
            "chr1\t100\t90\t2\t3\t0.90\n"
            "Genome\t1000\t950\t10\t12\t0.95\n"
        )

    def test_hifi_bai_lands_beside_the_linked_bam(self, tmp_path, monkeypatch):
        from app.config import settings
        from app.pipelines import tools as tools_module
        from app.pipelines.tools import InstallState, Tool
        from app.queue import assembly_qc_handlers as handlers

        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")

        # Real fixture files standing in for content-addressed storage.
        assembly_src = tmp_path / "src" / "assembly.fasta"
        hifi_bam_src = tmp_path / "src" / "hifi.bam"
        hifi_bai_src = tmp_path / "src" / "hifi.bam.bai"
        for f, content in (
            (assembly_src, ">chr1\nACGT\n"),
            (hifi_bam_src, "not a real bam"),
            (hifi_bai_src, "not a real bai"),
        ):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)

        payload = {
            "object_id": "obj1",
            "assembly_path": str(assembly_src),
            "hifi_bam_path": str(hifi_bam_src),
            "hifi_bai_path": str(hifi_bai_src),
        }
        ctx = self._ctx(payload)

        fake_tool = Tool(
            name="gci",
            path="/usr/bin/gci",
            version="1.0",
            install_state=InstallState.INSTALLED,
        )
        monkeypatch.setattr(tools_module, "gci", lambda: fake_tool)

        captured_out_dir = {}

        def fake_run_subprocess(ctx, cmd, log_path=None, parser=None):
            # -d <out_dir> is the directory GCI is told to write into.
            out_dir = Path(cmd[cmd.index("-d") + 1])
            captured_out_dir["path"] = out_dir
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "gci.gci").write_text(self._fake_gci_output())
            return 0

        monkeypatch.setattr(handlers, "run_subprocess", fake_run_subprocess)

        result = handlers.assess_assembly_continuity(ctx)

        out_dir = captured_out_dir["path"]
        work = out_dir.parent
        # `_named_link` places the link at work / f"in_{name}", not the bare
        # name -- see pipeline_handlers._named_link.
        linked_bam = work / f"in_{handlers._GCI_HIFI_LINK}"
        linked_bai = Path(f"{linked_bam}.bai")

        assert linked_bam.exists() or linked_bam.is_symlink()
        assert linked_bai.exists() or linked_bai.is_symlink()
        assert linked_bai.resolve() == hifi_bai_src.resolve()

        assert result["facts"]["assembly_continuity_gci"] == 0.95
        assert result["facts"]["assembly_continuity_observed_n50"] == 950

    def test_no_long_read_bam_raises_permanent_error(self, tmp_path, monkeypatch):
        from app.config import settings
        from app.errors import PermanentError
        from app.pipelines import tools as tools_module
        from app.pipelines.tools import InstallState, Tool
        from app.queue import assembly_qc_handlers as handlers

        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")

        assembly_src = tmp_path / "src" / "assembly.fasta"
        assembly_src.parent.mkdir(parents=True, exist_ok=True)
        assembly_src.write_text(">chr1\nACGT\n")

        payload = {"object_id": "obj1", "assembly_path": str(assembly_src)}
        ctx = self._ctx(payload)

        fake_tool = Tool(
            name="gci",
            path="/usr/bin/gci",
            version="1.0",
            install_state=InstallState.INSTALLED,
        )
        monkeypatch.setattr(tools_module, "gci", lambda: fake_tool)

        with pytest.raises(PermanentError):
            handlers.assess_assembly_continuity(ctx)
