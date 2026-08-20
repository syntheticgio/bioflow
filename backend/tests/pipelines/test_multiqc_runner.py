"""MultiQC command construction.

Every flag asserted here was verified against a real `multiqc 1.35` run
inside the api container on 2026-08-20, against genuine FastQC and fastp
output. The two that are easy to drop and expensive to lose are
`--no-version-check` and the report-exists check `report_path` supports --
see the individual tests for what each one costs.
"""

from pathlib import Path

from app.pipelines import multiqc_runner as runner


class TestBuildCommand:
    def _cmd(self, **over) -> list[str]:
        kwargs = {
            "multiqc_path": "/usr/local/bin/multiqc",
            "input_dir": Path("/tmp/stage"),
            "out_dir": Path("/tmp/out"),
        }
        kwargs.update(over)
        return runner.build_multiqc_command(**kwargs)

    def test_invokes_the_given_binary_over_the_input_dir(self):
        cmd = self._cmd()
        assert cmd[0] == "/usr/local/bin/multiqc"
        assert str(Path("/tmp/stage")) in cmd

    def test_writes_to_the_requested_output_dir(self):
        cmd = self._cmd()
        assert "-o" in cmd
        assert cmd[cmd.index("-o") + 1] == str(Path("/tmp/out"))

    def test_disables_the_network_version_check(self):
        """MultiQC checks PyPI for a newer release on every invocation
        unless told not to. In a worker that is a hang waiting to happen,
        and it is not behaviour a report generator should have at all."""
        assert "--no-version-check" in self._cmd()

    def test_disables_ansi_colour(self):
        """Output is captured to a log file, not a terminal; without this
        the log fills with escape sequences."""
        assert "--no-ansi" in self._cmd()

    def test_forces_overwrite_of_a_previous_report(self):
        """Regenerating a project's report overwrites in place -- there is
        no version history (see the spec's non-goals). Without --force
        MultiQC renames the new report rather than replacing the old one,
        and the route serving a fixed filename would keep serving the
        stale one."""
        assert "--force" in self._cmd()

    def test_passes_a_title_through_when_given(self):
        cmd = self._cmd(title="Project Anopheles")
        assert "--title" in cmd
        assert cmd[cmd.index("--title") + 1] == "Project Anopheles"

    def test_omits_the_title_flag_entirely_when_not_given(self):
        """Not an empty --title: MultiQC would render an empty heading
        rather than falling back to its own default."""
        assert "--title" not in self._cmd()

    def test_every_argument_is_a_string(self):
        """Paths must be stringified here rather than by the caller --
        subprocess accepts PathLike, but the log line this repo writes
        joins argv with ' ' and would raise on a Path."""
        assert all(isinstance(a, str) for a in self._cmd())


class TestReportPath:
    def test_names_the_html_multiqc_writes(self):
        assert runner.report_path(Path("/tmp/out")) == Path(
            "/tmp/out/multiqc_report.html"
        )

    def test_is_the_check_a_caller_needs_because_exit_zero_is_not_enough(self):
        """MultiQC exits 0 having written nothing when it finds no
        parseable input -- verified 2026-08-20. A handler trusting the exit
        code alone records success and leaves no file, so the report's
        existence is the real success signal and this is where its name
        lives."""
        out = Path("/tmp/out")
        assert runner.report_path(out).name == "multiqc_report.html"
