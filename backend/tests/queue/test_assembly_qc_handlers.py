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


class TestAssessAssemblyQV:
    """`assess_assembly_qv` -- meryl+Merqury k-mer QV assessment.

    Merqury names every output from its inputs' basenames (like CRAQ's
    shell-metacharacter risk, not QUAST's HTML-injection one -- but the
    fix is the same: never let the object's own name reach the command).
    """

    def _ctx(self, tmp_path, payload_extra=None, job_id="job-qv-1"):
        from app.queue.registry import JobContext

        payload = {"object_id": "obj-assembly-1", "assembly_path": None}
        payload.update(payload_extra or {})
        return JobContext(
            job_id=job_id, payload=payload, epoch=1, attempts=1, owner="local"
        )

    def _fake_tools(self, monkeypatch):
        from app.pipelines.tools import Tool

        meryl_tool = Tool(name="meryl", path="/usr/local/bin/meryl", version="1.4.1")
        merqury_tool = Tool(
            name="merqury", path="/usr/local/bin/merqury.sh", version="1.3"
        )
        monkeypatch.setattr(handlers.tools, "meryl", lambda: meryl_tool)
        monkeypatch.setattr(handlers.tools, "merqury", lambda: merqury_tool)
        return meryl_tool, merqury_tool

    def _write_assembly_and_reads(self, tmp_path, *, malicious_name=False):
        assembly = tmp_path / "sources" / "assembly.fasta"
        assembly.parent.mkdir(parents=True, exist_ok=True)
        assembly.write_text(">contig1\nACGT\n")

        reads = tmp_path / "sources" / "reads_R1.fastq.gz"
        reads.write_bytes(b"not real gzip but never read by the fake subprocess")

        name = 'ev<img src=x onerror=alert(7)>; rm -rf /.fasta' if malicious_name else "assembly.fasta"
        return assembly, reads, name

    def test_assess_assembly_qv_links_inputs_under_fixed_names(
        self, monkeypatch, tmp_path
    ):
        """merqury.sh names every output from its input basenames, so the
        object's own name must never reach the command line. Assert the
        linked path, not just that the command was built.
        """
        from app.config import settings
        from app.queue import assembly_qc_handlers

        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
        self._fake_tools(monkeypatch)

        assembly, reads, hostile_name = self._write_assembly_and_reads(
            tmp_path, malicious_name=True
        )

        captured = {}

        def fake_run(ctx, cmd, log_path=None, cwd=None, **kwargs):
            captured.setdefault("cmds", []).append(cmd)
            if len(captured["cmds"]) == 1:
                # meryl count: create the output db directory so _link_tree's
                # sibling code (merqury command build) has something to point
                # at, and so the handler's own out_dir logic proceeds.
                out = Path(cmd[cmd.index("output") + 1])
                out.mkdir(parents=True, exist_ok=True)
            else:
                # merqury.sh: write the two output files it's expected to
                # produce, into cwd.
                out_dir = Path(cwd)
                (out_dir / "qv.qv").write_text("assembly\t10\t1000\t35.5\t0.0002\n")
                (out_dir / "qv.completeness.stats").write_text(
                    "assembly\tall\t950\t1000\t95.0\n"
                )
            return 0

        monkeypatch.setattr(assembly_qc_handlers, "run_subprocess", fake_run)

        ctx = self._ctx(
            tmp_path,
            {
                "object_id": "obj-assembly-1",
                "assembly_path": str(assembly),
                "assembly_name": hostile_name,
                "reads": [{"read_path": str(reads)}],
                "read_object_id": "obj-reads-1",
                "read_object_name": hostile_name,
            },
        )

        result = assembly_qc_handlers.assess_assembly_qv(ctx)

        all_cmd_parts = [part for cmd in captured["cmds"] for part in cmd]
        assert not any(";" in part for part in all_cmd_parts)
        assert not any("<img" in part for part in all_cmd_parts)
        assert any(part.endswith("in_assembly.fasta") for part in all_cmd_parts)
        assert result["facts"]["assembly_qv"] == 35.5
        assert result["facts"]["assembly_qv_completeness_pct"] == 95.0

    def test_parsers_populate_facts_dict_from_real_fixture_content(
        self, monkeypatch, tmp_path
    ):
        from app.config import settings
        from app.queue import assembly_qc_handlers

        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
        self._fake_tools(monkeypatch)

        assembly, reads, name = self._write_assembly_and_reads(tmp_path)

        def fake_run(ctx, cmd, log_path=None, cwd=None, **kwargs):
            if "count" in cmd:
                out = Path(cmd[cmd.index("output") + 1])
                out.mkdir(parents=True, exist_ok=True)
            else:
                out_dir = Path(cwd)
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "qv.qv").write_text(
                    "assembly\t123\t45678\t41.2\t0.0000758\n"
                )
                (out_dir / "qv.completeness.stats").write_text(
                    "assembly\tall\t44000\t45678\t96.33\n"
                )
            return 0

        monkeypatch.setattr(assembly_qc_handlers, "run_subprocess", fake_run)

        ctx = self._ctx(
            tmp_path,
            {
                "object_id": "obj-assembly-2",
                "assembly_path": str(assembly),
                "assembly_name": name,
                "reads": [{"read_path": str(reads)}],
                "k": 21,
            },
        )

        result = assembly_qc_handlers.assess_assembly_qv(ctx)
        facts = result["facts"]

        assert facts["assembly_qv"] == 41.2
        assert facts["assembly_qv_completeness_pct"] == 96.33
        assert facts["assembly_qv_k"] == 21
        assert facts["assembly_qv_tool"] == "merqury"
        # Not the probed Tool.version -- merqury.sh has no --version flag,
        # so the probe's "version" is really its usage banner (confirmed
        # against a real run). assembly_qv_tool_version is the pinned
        # install version instead; see MERQURY_PINNED_VERSION.
        assert (
            facts["assembly_qv_tool_version"]
            == assembly_qc_handlers.MERQURY_PINNED_VERSION
        )
        assert facts["assembly_qv_meryl_version"] == "1.4.1"

    def test_meryl_count_is_skipped_when_read_db_path_is_cached(
        self, monkeypatch, tmp_path
    ):
        """The whole point of the sidecar cache: a payload carrying
        `read_db_path` must never trigger a `meryl count` subprocess call."""
        from app.config import settings
        from app.queue import assembly_qc_handlers

        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
        self._fake_tools(monkeypatch)

        assembly, _reads, name = self._write_assembly_and_reads(tmp_path)

        cached_db = tmp_path / "cached_reads.meryl"
        cached_db.mkdir(parents=True)
        (cached_db / "0x000000.merylIndex").write_text("fake index contents")

        calls = []

        def fake_run(ctx, cmd, log_path=None, cwd=None, **kwargs):
            calls.append(cmd)
            out_dir = Path(cwd) if cwd else None
            if out_dir is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "qv.qv").write_text("assembly\t1\t2\t30.0\t0.001\n")
                (out_dir / "qv.completeness.stats").write_text(
                    "assembly\tall\t2\t2\t100.0\n"
                )
            return 0

        monkeypatch.setattr(assembly_qc_handlers, "run_subprocess", fake_run)

        ctx = self._ctx(
            tmp_path,
            {
                "object_id": "obj-assembly-3",
                "assembly_path": str(assembly),
                "assembly_name": name,
                "read_db_path": str(cached_db),
            },
        )

        result = assembly_qc_handlers.assess_assembly_qv(ctx)

        assert len(calls) == 1, "meryl count must be skipped when a cache is given"
        assert "count" not in calls[0]
        assert "read_db_dir" not in result, (
            "no new database was built, so nothing new should be offered "
            "for the applier to ingest"
        )
