"""External tool discovery and version parsing."""

import os
import sys

import pytest

from app.errors import PermanentError
from app.pipelines import tools


@pytest.fixture(autouse=True)
def clear_cache():
    """Versions are cached for the process lifetime, so tests must not leak
    into each other."""
    tools.reset_cache()
    yield
    tools.reset_cache()


class TestVersionParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("fastp 0.24.0", "0.24.0"),
            ("FastQC v0.12.1", "0.12.1"),
            ("fastp 0.23", "0.23"),
            ("  fastp 0.24.0  \n", "0.24.0"),
            # Multi-line output: only the first line is the version.
            ("FastQC v0.12.1\nCopyright 2023", "0.12.1"),
        ],
    )
    def test_extracts_the_bare_version(self, raw, expected):
        assert tools._clean_version(raw) == expected

    def test_unparseable_output_is_kept_verbatim(self):
        """Better to show the user something odd than to silently report no
        version for a tool that is plainly installed."""
        assert tools._clean_version("some unexpected banner") == "some unexpected banner"

    def test_empty_output(self):
        assert tools._clean_version("") == ""


class TestProbe:
    def test_missing_binary_is_reported_not_raised(self):
        """A missing tool must surface in the launch dialog, not as a job that
        dies after the user has walked away."""
        tool = tools._probe("nope", "definitely-not-a-real-binary-xyz", ["--version"])
        assert not tool.available
        assert tool.path is None
        assert "not found on PATH" in tool.error

    def test_resolves_a_real_binary_and_reads_its_version(self):
        tool = tools._probe("python", sys.executable, ["--version"])
        assert tool.available
        assert tool.path is not None
        assert tool.version is not None
        assert tool.version.startswith("3.")

    def test_reads_a_version_written_to_stderr(self, tmp_path, monkeypatch):
        """fastp reports its version on stderr and FastQC on stdout; the probe
        must not care which."""
        script = tmp_path / "stderrtool"
        script.write_text("#!/bin/sh\necho 'faketool 1.2.3' >&2\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        tool = tools._probe("faketool", "stderrtool", ["--version"])
        assert tool.available
        assert tool.version == "1.2.3"

    def test_resolves_through_path_not_just_absolute(self, tmp_path, monkeypatch):
        """Settings default to bare names, so PATH resolution is the norm."""
        script = tmp_path / "ontool"
        script.write_text("#!/bin/sh\necho 'ontool 4.5.6'\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        tool = tools._probe("ontool", "ontool", ["--version"])
        assert tool.path == str(script)
        assert tool.version == "4.5.6"

    def test_a_nonzero_exit_still_yields_a_version(self, tmp_path, monkeypatch):
        """Some tools exit non-zero from --version. The output is what matters."""
        script = tmp_path / "grumpy"
        script.write_text("#!/bin/sh\necho 'grumpy 9.9.9'\nexit 1\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        tool = tools._probe("grumpy", "grumpy", ["--version"])
        assert tool.available
        assert tool.version == "9.9.9"

    def test_a_hung_binary_does_not_hang_the_probe(self, tmp_path, monkeypatch):
        """A wedged tool must fail the probe, not the request that asked."""
        script = tmp_path / "hangs"
        # Sleeps via the interpreter rather than sleep(1), so the script does
        # not depend on anything else being on PATH.
        script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        script.chmod(0o755)
        monkeypatch.setattr(tools, "VERSION_TIMEOUT_SECONDS", 1)

        tool = tools._probe("hangs", str(script), ["--version"])
        assert not tool.available
        assert tool.error


class TestRequire:
    def test_raises_permanently_for_a_missing_tool(self):
        """PermanentError, not Retryable: a missing binary will not appear on
        its own, and retrying only delays the error the user needs."""
        missing = tools.Tool(name="fastp", path=None, version=None, error="not found")
        with pytest.raises(PermanentError) as exc:
            tools.require(missing)
        assert "fastp" in str(exc.value)

    def test_passes_an_available_tool_through(self):
        ok = tools.Tool(name="fastp", path="/usr/bin/fastp", version="0.24.0")
        assert tools.require(ok) is ok


class TestSerialization:
    def test_as_dict_carries_what_the_ui_needs(self):
        tool = tools.Tool(name="fastp", path="/usr/bin/fastp", version="0.24.0")
        assert tool.as_dict() == {
            "name": "fastp",
            "path": "/usr/bin/fastp",
            "version": "0.24.0",
            "available": True,
            "error": None,
        }

    def test_all_tools_covers_both(self):
        assert {t.name for t in tools.all_tools()} == {"fastp", "fastqc"}
