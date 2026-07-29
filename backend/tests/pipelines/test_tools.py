"""External tool discovery and version parsing."""

import os
import re
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
            # Multi-line output: the version wins over trailing prose.
            ("FastQC v0.12.1\nCopyright 2023", "0.12.1"),
            ("2.28-r1209", "2.28"),  # minimap2
            ("samtools 1.19.2\nUsing htslib 1.19.1", "1.19.2"),
        ],
    )
    def test_extracts_the_bare_version(self, raw, expected):
        assert tools._clean_version(raw) == expected

    def test_skips_a_banner_line_that_precedes_the_version(self):
        """bwa-mem2 has no --version flag: it prints a dispatch line naming the
        CPU-specific binary it chose *before* the version. Reading only the
        first line captured that message instead -- and a tool version that is
        quietly wrong is worse than a missing one, since it is the half of a
        run's provenance a methods section reports."""
        raw = (
            'Looking to launch executable "bwa-mem2.avx2"\n'
            "Version: 2.2.1\n"
            "Usage: bwa-mem2 <command> <arguments>"
        )
        assert tools._clean_version(raw) == "2.2.1"

    def test_a_digit_in_a_binary_name_is_not_mistaken_for_a_version(self):
        """`bwa-mem2.avx2` contains a digit-dot-digit sequence. Matching it
        would report the aligner's version as '2.2'."""
        assert tools._clean_version('Looking to launch "bwa-mem2.avx2"\nVersion: 2.2.1') == "2.2.1"

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

    def test_undecodable_output_does_not_raise(self, tmp_path, monkeypatch):
        """Found by running the real image: an x86-64 bwa-mem2 under Rosetta
        prints a loader error that is not valid UTF-8, and decoding it with
        `text=True` raised straight out of the probe -- turning one broken tool
        into a failure of the entire tool panel."""
        script = tmp_path / "badbytes"
        script.write_text("#!/bin/sh\nprintf 'rosetta error: \\253\\300\\n' >&2\nexit 133\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        tool = tools._probe("badbytes", "badbytes", ["version"])
        assert not tool.available
        assert "rosetta error" in tool.error

    def test_a_binary_that_cannot_execute_is_not_available(self, tmp_path, monkeypatch):
        """`which` finding a binary says nothing about whether it can run: an
        x86-64 binary on arm64 resolves fine and fails only when executed.
        Reporting it available defers that discovery to a job the user has
        already walked away from."""
        script = tmp_path / "brokentool"
        script.write_text("#!/bin/sh\necho 'cannot execute binary' >&2\nexit 126\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        tool = tools._probe("brokentool", "brokentool", ["--version"])
        assert not tool.available
        assert "could not be run" in tool.error

    def test_a_nonzero_exit_that_still_prints_a_version_is_fine(self, tmp_path, monkeypatch):
        """bwa-mem2 has no --version flag: `bwa-mem2 version` prints the
        version and exits non-zero. Treating every non-zero exit as a failure
        would report a perfectly working aligner as missing."""
        script = tmp_path / "usagetool"
        script.write_text("#!/bin/sh\necho 'Version: 2.2.1'\nexit 1\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        tool = tools._probe("usagetool", "usagetool", ["version"])
        assert tool.available
        assert tool.version == "2.2.1"

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

    def test_all_tools_covers_every_probed_binary(self):
        """`all_tools` drives the UI's tool-availability panel, so a binary
        missing from it is one whose absence the user discovers as a failed job
        rather than a greyed-out button."""
        assert {t.name for t in tools.all_tools()} == {
            "fastp",
            "fastqc",
            "cutadapt",
            "trimmomatic",
            "nanoplot",
            "bwa-mem2",
            "minimap2",
            "bowtie2",
            "hisat2",
            "samtools",
            "bcftools",
            "clair3",
            "fasterq-dump",
            "prefetch",
        }


class TestToolMeta:
    def test_every_probed_tool_has_a_description(self):
        """A tool added to `all_tools` without an entry here would reach the
        selector as a nameless row with an empty summary -- available to pick
        and impossible to choose between. Failing at the table is cheaper."""
        missing = [t.name for t in tools.all_tools() if t.name not in tools.TOOL_META]
        assert missing == []

    def test_every_tool_belongs_to_at_least_one_pipeline(self):
        """`pipelines` is what the selector filters on: an empty tuple is a
        tool that exists but appears on no screen."""
        for name, meta in tools.TOOL_META.items():
            assert meta.pipelines, f"{name} belongs to no pipeline"

    def test_fastp_is_both_a_trimmer_and_a_qc_tool(self):
        """The reason the field is a tuple rather than a single value. A
        singular `pipeline` would drop fastp from one of the two lists."""
        assert tools.PipelineType.TRIM in tools.TOOL_META["fastp"].pipelines
        assert tools.PipelineType.QC in tools.TOOL_META["fastp"].pipelines


class TestVariantToolProbes:
    def test_clair3_probes(self):
        tool = tools.clair3()
        assert tool.name == "clair3"
        assert isinstance(tool.available, bool)

    def test_bcftools_probes(self):
        tool = tools.bcftools()
        assert tool.name == "bcftools"
        assert isinstance(tool.available, bool)

    def test_clair3_reports_a_plausible_version(self):
        """Regression: the probe originally used --help, which exits 0 and
        dumps a usage block -- _clean_version scraped a line of that into the
        version field, so the tool panel and every run's recorded provenance
        showed a garbled argument list instead of a version."""
        tool = tools.clair3()
        if not tool.available or not tool.version:
            pytest.skip("clair3 not installed in this environment")
        assert "usage" not in tool.version.lower()
        # A version is a short dotted number, not a sentence of flags.
        assert re.fullmatch(r"[\d.]+", tool.version), tool.version

    def test_both_are_variant_tools(self):
        for name in ("clair3", "bcftools"):
            assert tools.PipelineType.VARIANT in tools.TOOL_META[name].pipelines

    def test_bcftools_is_also_a_utility(self):
        """Like samtools, bcftools is a general-purpose toolkit that happens to
        call variants -- it belongs on the utility list too."""
        assert tools.PipelineType.UTILITY in tools.TOOL_META["bcftools"].pipelines

    def test_both_are_runnable(self):
        """`runnable` means a handler actually dispatches on the tool. Both do
        as of call_variants, so neither should be greyed out for the reason
        cutadapt once was."""
        assert tools.TOOL_META["clair3"].runnable
        assert tools.TOOL_META["bcftools"].runnable

    def test_meta_is_merged_onto_the_probe_result(self):
        tool = tools.Tool(name="fastqc", path="/usr/bin/fastqc", version="0.12.1")
        enriched = tools.tool_with_meta(tool)
        assert enriched["name"] == "fastqc"
        assert enriched["available"] is True
        assert enriched["pipelines"] == ["qc"]
        assert enriched["summary"]
        assert enriched["strengths"]

    def test_an_undescribed_tool_serializes_with_empty_metadata(self):
        """The API must not 500 over a missing description. The coverage test
        above is what stops one shipping; this is what stops it being fatal."""
        enriched = tools.tool_with_meta(tools.Tool(name="mystery", path="/x", version="1"))
        assert enriched["pipelines"] == []
        assert enriched["summary"] == ""
        assert enriched["strengths"] == []

    def test_runnable_defaults_true(self):
        """Most tools have exactly one code path and it exists, so the common
        case should need no annotation."""
        assert tools.TOOL_META["fastp"].runnable is True
        assert tools.TOOL_META["minimap2"].runnable is True

    def test_cutadapt_and_trimmomatic_are_runnable(self):
        assert tools.TOOL_META["cutadapt"].runnable is True
        assert tools.TOOL_META["trimmomatic"].runnable is True

    def test_runnable_survives_serialization(self):
        tool = tools.Tool(name="cutadapt", path="/usr/bin/cutadapt", version="4.7")
        assert tools.tool_with_meta(tool)["runnable"] is True

        tool = tools.Tool(name="fastp", path="/usr/bin/fastp", version="0.24.0")
        assert tools.tool_with_meta(tool)["runnable"] is True

    def test_an_undescribed_tool_is_not_runnable(self):
        """A tool this application does not describe is not one it has a code
        path for either -- the absent-metadata default matches the described
        default for a tool with no handler, not the default for one with one."""
        enriched = tools.tool_with_meta(tools.Tool(name="mystery", path="/x", version="1"))
        assert enriched["runnable"] is False


class TestNewAlignerProbes:
    def test_bowtie2_probes(self):
        """Runs against the real binary in the image. An installed-but-broken
        tool is exactly what `available` exists to report, so this asserts the
        probe returns a Tool rather than asserting availability."""
        t = tools.bowtie2()
        assert t.name == "bowtie2"

    def test_hisat2_probes(self):
        t = tools.hisat2()
        assert t.name == "hisat2"

    def test_both_are_in_all_tools(self):
        names = {t.name for t in tools.all_tools()}
        assert "bowtie2" in names
        assert "hisat2" in names

    def test_both_have_metadata(self):
        """A tool with no TOOL_META entry defaults to runnable=False and would
        render as a permanently greyed-out card."""
        assert tools.TOOL_META["bowtie2"].runnable is True
        assert tools.TOOL_META["hisat2"].runnable is True

    def test_both_are_align_pipeline_tools(self):
        from app.pipelines.tools import PipelineType

        assert PipelineType.ALIGN in tools.TOOL_META["bowtie2"].pipelines
        assert PipelineType.ALIGN in tools.TOOL_META["hisat2"].pipelines

    def test_every_tool_meta_has_a_one_liner(self):
        """The selector rail shows this instead of the full summary."""
        for name, meta in tools.TOOL_META.items():
            assert meta.one_liner.strip(), f"{name} has no one_liner"
