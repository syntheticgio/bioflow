"""External tool discovery and version parsing."""

import dataclasses
import os
import re
import subprocess
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
            # STAR's letter suffix is part of the version: 2.7.11a and
            # 2.7.11b are different releases, so truncating to 2.7.11 records
            # a release that never ran.
            ("2.7.11b", "2.7.11b"),
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

    def test_a_slow_starting_tool_gets_the_longer_timeout(self, monkeypatch):
        """NanoPlot must probe with the slow-import timeout, not the default.

        Asserts the timeout actually reaching `subprocess.run`, because the
        bug it guards was silent and load-dependent: NanoPlot needs ~16s cold
        and ~2s warm, so a probe left on the 10s default still passes on a
        warm machine and fails only at startup on a cold one. A test that
        merely checked `nanoplot().available` was green throughout the bug --
        and would also pass here without the patch working at all, since the
        image ships NanoPlot installed.
        """
        seen = {}

        def fake_run(args, **kwargs):
            seen["timeout"] = kwargs["timeout"]
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])

        monkeypatch.setattr(tools.subprocess, "run", fake_run)
        monkeypatch.setattr(tools.shutil, "which", lambda _: "/usr/local/bin/NanoPlot")

        tools.nanoplot()
        assert seen["timeout"] == tools.SLOW_IMPORT_TIMEOUT_SECONDS
        assert seen["timeout"] > tools.VERSION_TIMEOUT_SECONDS

    def test_an_ordinary_tool_keeps_the_default_timeout(self, monkeypatch):
        """The longer timeout is per-tool, not a global relaxation -- a hung
        binary must still fail its probe in VERSION_TIMEOUT_SECONDS."""
        seen = {}

        def fake_run(args, **kwargs):
            seen["timeout"] = kwargs["timeout"]
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])

        monkeypatch.setattr(tools.subprocess, "run", fake_run)
        monkeypatch.setattr(tools.shutil, "which", lambda _: "/usr/local/bin/fastp")

        tools.fastp()
        assert seen["timeout"] == tools.VERSION_TIMEOUT_SECONDS


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
            "install_state": None,
        }

    def test_as_dict_carries_install_state_when_set(self):
        """A BUNDLED tool's install_state is always None, so this only shows
        up for an ON_DEMAND_IMAGE tool -- serialized as its string value, the
        same treatment `Delivery` needs in `tool_with_meta`."""
        tool = tools.Tool(
            name="deepvariant",
            path="/usr/bin/docker",
            version=None,
            install_state=tools.InstallState.NOT_INSTALLED,
        )
        assert tool.as_dict()["install_state"] == "not_installed"

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
            "star",
            "samtools",
            "bcftools",
            "bgzip",
            "clair3",
            "deepvariant",
            "flye",
            "abyss",
            "spades",
            "miniprot",
            "compleasm",
            "fasterq-dump",
            "prefetch",
            "datasets",
            "featurecounts",
            "ivar",
            "quast",
            "craq",
            "meryl",
            "merqury",
            "gci",
            "winnowmap",
            # Not a binary at all -- a Python library, probed by import rather
            # than by shutil.which. It is in `all_tools` deliberately: the
            # version that ran a differential expression test is half that
            # result's provenance, and the panel is where a user reads it.
            "pydeseq2",
        }


class TestIvarProbe:
    def test_ivar_probes(self):
        tool = tools.ivar()
        assert tool.name == "ivar"
        assert isinstance(tool.available, bool)

    def test_probes_with_the_subcommand_not_a_flag(self, tmp_path, monkeypatch):
        """iVar uses `ivar version`, not `ivar --version` -- a probe passing
        --version gets a non-zero exit and reads a working binary as absent.
        Verified against a real installed 1.4.4 binary on 2026-08-05."""
        script = tmp_path / "fakeivar"
        script.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "version" ]; then\n'
            '  echo "iVar version 1.4.4"\n'
            "  exit 0\n"
            "fi\n"
            "exit 1\n"
        )
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        tool = tools._probe("ivar", "fakeivar", ["version"])

        assert tool.available
        assert tool.version == "1.4.4"

    def test_ivar_is_a_reference_assembly_tool(self):
        assert tools.PipelineType.REFERENCE_ASSEMBLY in tools.TOOL_META["ivar"].pipelines

    def test_ivar_is_runnable(self):
        assert tools.TOOL_META["ivar"].runnable

    def test_ivar_is_in_all_tools(self):
        assert "ivar" in {t.name for t in tools.all_tools()}


class TestCraqProbe:
    def test_craq_probes(self):
        tool = tools.craq()
        assert tool.name == "craq"
        assert isinstance(tool.available, bool)

    def test_probes_with_a_flag_that_prints_a_recognizable_version(self, tmp_path, monkeypatch):
        """Bare `craq` prints only an argument error with no version string
        and exits non-zero -- a probe passing no flag reads a working binary
        as absent. `-h` prints "CRAQ Version: X.Y" (still exiting non-zero,
        verified against a real 1.10 install), which `_probe` already
        accepts since the output looks like a version."""
        script = tmp_path / "fakecraq"
        script.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-h" ]; then\n'
            '  echo "craq version 1.0"\n'
            "  exit 0\n"
            "fi\n"
            "exit 1\n"
        )
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        tool = tools._probe("craq", "fakecraq", ["-h"])

        assert tool.available

    def test_craq_is_an_assembly_qc_tool(self):
        assert tools.PipelineType.ASSEMBLY_QC in tools.TOOL_META["craq"].pipelines

    def test_craq_is_runnable(self):
        assert tools.TOOL_META["craq"].runnable

    def test_craq_is_in_all_tools(self):
        assert "craq" in {t.name for t in tools.all_tools()}

    def test_craq_is_documented_and_probeable(self):
        assert "craq" in tools.TOOL_META
        meta = tools.TOOL_META["craq"]
        assert meta.homepage
        assert meta.citation
        assert meta.license
        assert meta.usage


class TestMerylProbe:
    def test_meryl_probe_rejects_debian_celera_build(self, monkeypatch, tmp_path):
        """Debian's `meryl` package is 0~20150903+r2013-9+b1, the Celera
        Assembler k-mer suite, not Marbl meryl -- but that dpkg version
        string is never what the *binary* prints. Verified against the real
        package (2026-08-07): `meryl --version` exits 0 and prints
        "Unknown option '--version'." to stdout, because Celera meryl's
        argument parser doesn't recognise --version at all. A probe that
        only matched the dpkg version shape would never catch this: it
        would leave that message in Tool.version verbatim and report the
        tool as available. This is the acceptance criterion on issue #64.
        """
        fake = tmp_path / "meryl"
        fake.write_text("#!/bin/sh\necho \"Unknown option '--version'.\"\nexit 0\n")
        fake.chmod(0o755)

        monkeypatch.setattr(tools.settings, "meryl_path", str(fake))
        tools.meryl.cache_clear()

        probed = tools.meryl()
        assert probed.version is None
        assert probed.error is not None
        assert "Marbl" in probed.error

    def test_meryl_probe_accepts_marbl_build(self, monkeypatch, tmp_path):
        fake = tmp_path / "meryl"
        fake.write_text("#!/bin/sh\necho 'meryl 1.4.2'\n")
        fake.chmod(0o755)

        monkeypatch.setattr(tools.settings, "meryl_path", str(fake))
        tools.meryl.cache_clear()

        probed = tools.meryl()
        assert probed.error is None
        assert probed.version is not None
        assert "1.4.2" in probed.version

    def test_meryl_is_an_assembly_qc_tool(self):
        assert tools.PipelineType.ASSEMBLY_QC in tools.TOOL_META["meryl"].pipelines

    def test_meryl_is_runnable(self):
        assert tools.TOOL_META["meryl"].runnable

    def test_meryl_is_in_all_tools(self):
        assert "meryl" in {t.name for t in tools.all_tools()}

    def test_meryl_is_documented_and_probeable(self):
        assert "meryl" in tools.TOOL_META
        meta = tools.TOOL_META["meryl"]
        assert meta.homepage
        assert meta.citation
        assert meta.license
        assert meta.usage


class TestMerquryProbe:
    def test_merqury_probes(self):
        tool = tools.merqury()
        assert tool.name == "merqury"
        assert isinstance(tool.available, bool)

    def test_merqury_is_an_assembly_qc_tool(self):
        assert tools.PipelineType.ASSEMBLY_QC in tools.TOOL_META["merqury"].pipelines

    def test_merqury_is_runnable(self):
        assert tools.TOOL_META["merqury"].runnable

    def test_merqury_is_in_all_tools(self):
        assert "merqury" in {t.name for t in tools.all_tools()}

    def test_merqury_is_documented_and_probeable(self):
        assert "merqury" in tools.TOOL_META
        meta = tools.TOOL_META["merqury"]
        assert meta.homepage
        assert meta.citation
        assert meta.license
        assert meta.usage


def test_gci_probe_reports_version(monkeypatch, tmp_path):
    fake = tmp_path / "gci"
    fake.write_text("#!/bin/sh\necho 'GCI v1.0'\n")
    fake.chmod(0o755)

    monkeypatch.setattr(tools.settings, "gci_path", str(fake))
    tools.gci.cache_clear()

    probed = tools.gci()
    assert probed.error is None
    assert probed.version is not None


class TestToolMeta:
    def test_every_probed_tool_has_a_description(self):
        """A tool added to `all_tools` without an entry here would reach the
        selector as a nameless row with an empty summary -- available to pick
        and impossible to choose between. Failing at the table is cheaper."""
        missing = [t.name for t in tools.all_tools() if t.name not in tools.TOOL_META]
        assert missing == []

    def test_every_tool_belongs_to_at_least_one_pipeline(self):
        """`pipelines` is what the selector filters on: an empty tuple is a
        tool that exists but appears on no screen. bgzip is the one
        deliberate exception -- it is dispatched internally by the storage
        layer at ingest, never a user-selectable pipeline step, so it has no
        card to appear on. See docs/superpowers/specs/
        2026-08-05-object-compression-design.md."""
        for name, meta in tools.TOOL_META.items():
            if name == "bgzip":
                continue
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


class TestBgzipProbe:
    def test_bgzip_probes(self):
        """Runs against the real binary in the image."""
        tool = tools.bgzip()
        assert tool.name == "bgzip"
        assert isinstance(tool.available, bool)

    def test_bgzip_is_not_a_pipeline_card(self):
        """No PipelineType: it is dispatched internally by the storage layer
        at ingest, not offered on the tool selector or the Software help
        page. See docs/superpowers/specs/2026-08-05-object-compression-design.md."""
        assert tools.TOOL_META["bgzip"].pipelines == ()

    def test_bgzip_is_not_runnable(self):
        """No job handler branches on it -- cas/object_service call it
        directly -- so it must not read as an actionable pipeline step."""
        assert tools.TOOL_META["bgzip"].runnable is False

    def test_bgzip_falls_back_gracefully_when_probe_fails(self, monkeypatch):
        """Per CLAUDE.md: the image ships bgzip as installed, so asserting it
        is *available* would pass whether or not a missing-binary code path
        works. Assert the failure direction instead."""
        monkeypatch.setattr(tools.shutil, "which", lambda name: None if name == "bgzip" else "/x")
        tool = tools.bgzip()
        assert tool.available is False


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

    def test_one_liner_survives_serialization(self):
        """Regression: `TOOL_META` carrying a one_liner is not enough --
        `tool_with_meta` builds the dict the API actually returns, and it
        silently dropped `one_liner` while forwarding `summary`/`strengths`/
        `runnable` right next to it. The dict-level test above would not have
        caught that; this goes through the same serialization boundary the
        endpoint uses."""
        tool = tools.Tool(name="bowtie2", path="/usr/bin/bowtie2", version="2.5.4")
        enriched = tools.tool_with_meta(tool)
        assert enriched["one_liner"] == tools.TOOL_META["bowtie2"].one_liner
        assert enriched["one_liner"].strip()

    def test_an_undescribed_tool_serializes_with_an_empty_one_liner(self):
        """Same fallback as `summary`: no TOOL_META entry means an empty
        string, not a missing key or a 500."""
        enriched = tools.tool_with_meta(tools.Tool(name="mystery", path="/x", version="1"))
        assert enriched["one_liner"] == ""

    def test_every_tool_meta_field_reaches_the_serialized_output(self):
        """Generic on purpose: this is what should have caught the one_liner
        bug, and would catch the same class of bug for any future field added
        to ToolMeta and forgotten in tool_with_meta."""
        tool = tools.Tool(name="bowtie2", path="/usr/bin/bowtie2", version="2.5.0")
        enriched = tools.tool_with_meta(tool)
        for field in dataclasses.fields(tools.ToolMeta):
            assert field.name in enriched, f"{field.name} missing from serialized output"


class TestBibliographicFields:
    """The help page's reference data lives on ToolMeta, so it reaches the
    API through tool_with_meta's asdict() with no serializer change."""

    def test_tool_meta_carries_bibliographic_fields(self):
        meta = tools.ToolMeta(
            pipelines=(tools.PipelineType.ALIGN,),
            summary="s",
            strengths=(),
            homepage="https://example.org",
            repository="https://github.com/example/x",
            citation="Author et al., Journal 2020",
            citation_url="https://doi.org/10.0000/x",
            license="MIT",
            usage="Runs when you do the thing.",
        )
        assert meta.homepage == "https://example.org"
        assert meta.repository == "https://github.com/example/x"
        assert meta.citation == "Author et al., Journal 2020"
        assert meta.citation_url == "https://doi.org/10.0000/x"
        assert meta.license == "MIT"
        assert meta.usage == "Runs when you do the thing."

    def test_bibliographic_fields_default_to_empty(self):
        """Constructible without them, so an entry can be filled in
        incrementally rather than all at once."""
        meta = tools.ToolMeta(
            pipelines=(tools.PipelineType.ALIGN,), summary="s", strengths=()
        )
        assert meta.homepage == ""
        assert meta.citation_url == ""
        assert meta.license == ""

    def test_fields_reach_the_api_payload(self):
        """The whole point of putting them on ToolMeta: no serializer edit."""
        tool = tools.Tool(name="fastp", path="/usr/bin/fastp", version="0.24.0")
        payload = tools.tool_with_meta(tool)
        assert payload["homepage"].startswith("http")
        assert payload["license"]
        assert payload["usage"]

    def test_undescribed_tool_gets_empty_bibliographic_fields(self):
        """A tool with no TOOL_META entry must still serialize, with blanks
        rather than a KeyError -- the fallback dict enumerates keys by hand."""
        tool = tools.Tool(name="not-a-real-tool", path="/x", version="1.0")
        payload = tools.tool_with_meta(tool)
        assert payload["homepage"] == ""
        assert payload["citation"] == ""
        assert payload["license"] == ""
        assert payload["usage"] == ""

    def test_delivery_defaults_to_bundled(self):
        """Every existing tool -- fastp through pydeseq2 -- ships in the
        image, so BUNDLED must be the default an entry gets without saying
        so, not something every one of them has to state."""
        meta = tools.ToolMeta(
            pipelines=(tools.PipelineType.ALIGN,), summary="s", strengths=()
        )
        assert meta.delivery is tools.Delivery.BUNDLED
        assert meta.image is None
        assert meta.download_bytes is None

    def test_deepvariant_is_on_demand(self):
        """The one tool this delivery mechanism already exists for -- if this
        drifts to BUNDLED, the Install button vanishes and the card falls
        back to a plain unavailable/available toggle that lies about whether
        the image was ever pulled."""
        meta = tools.TOOL_META["deepvariant"]
        assert meta.delivery is tools.Delivery.ON_DEMAND_IMAGE
        assert meta.image
        assert meta.download_bytes and meta.download_bytes > 0

    def test_delivery_reaches_the_api_payload_as_its_string_value(self):
        """StrEnum serializes as its value automatically everywhere else in
        this module (PipelineType needs no special handling either), but
        `delivery` is asserted explicitly since a caller reading `payload["
        delivery"] == "on_demand"` should not have to know it is comparing a
        string to an enum member that merely prints the same."""
        tool = tools.Tool(name="deepvariant", path="/usr/bin/docker", version="1.9.0")
        payload = tools.tool_with_meta(tool)
        assert payload["delivery"] == "on_demand"
        assert isinstance(payload["delivery"], str)

    def test_bundled_tool_serializes_with_no_image_or_size(self):
        tool = tools.Tool(name="fastp", path="/usr/bin/fastp", version="0.24.0")
        payload = tools.tool_with_meta(tool)
        assert payload["delivery"] == "bundled"
        assert payload["image"] is None
        assert payload["download_bytes"] is None

    def test_undescribed_tool_serializes_as_bundled(self):
        """Same fallback shape as every other ToolMeta field: a tool with no
        entry has no delivery story either, so it reads as BUNDLED/absent
        rather than raising or omitting the keys."""
        payload = tools.tool_with_meta(tools.Tool(name="mystery", path="/x", version="1"))
        assert payload["delivery"] == "bundled"
        assert payload["image"] is None
        assert payload["download_bytes"] is None

    def test_every_tool_is_documented(self):
        """Adding a tool without documenting it must fail here rather than
        render a blank help entry.

        repository and citation_url are exempt on purpose: some tools have no
        public repo and some have no paper, and a test that demanded a value
        would only invite a fabricated one.
        """
        required = ("homepage", "citation", "license", "usage")
        missing = {
            name: [f for f in required if not getattr(meta, f)]
            for name, meta in tools.TOOL_META.items()
        }
        missing = {k: v for k, v in missing.items() if v}
        assert not missing, f"undocumented tools: {missing}"

    def test_on_demand_tools_declare_image_and_size(self):
        """A tool delivered as a pinned image must state what it costs to
        install, so an Install button never has to render without a size.

        Checked separately from the four fields above rather than folded into
        `required`: those four apply to every tool, these two only to
        ON_DEMAND_IMAGE ones, and merging the checks would make a BUNDLED
        tool's failure message claim it needs an image it will never have.
        """
        missing = {
            name: [
                f
                for f in ("image", "download_bytes")
                if not getattr(meta, f)
            ]
            for name, meta in tools.TOOL_META.items()
            if meta.delivery is tools.Delivery.ON_DEMAND_IMAGE
        }
        missing = {k: v for k, v in missing.items() if v}
        assert not missing, f"on-demand tools missing image/size: {missing}"

    def test_bundled_tools_have_no_image_or_size(self):
        """The inverse of the check above: a BUNDLED tool with an image or a
        download size would be a stale leftover from a delivery change that
        forgot to flip `delivery` back, and would render an Install button
        for something already in the container."""
        wrong = [
            name
            for name, meta in tools.TOOL_META.items()
            if meta.delivery is tools.Delivery.BUNDLED
            and (meta.image or meta.download_bytes)
        ]
        assert not wrong, f"bundled tools carrying delivery fields: {wrong}"


    def test_documented_urls_are_urls(self):
        """A citation string in the homepage field would render as a dead
        link, which is worse than a blank."""
        for name, meta in tools.TOOL_META.items():
            for field in ("homepage", "repository", "citation_url"):
                value = getattr(meta, field)
                if value:
                    assert value.startswith("https://"), (
                        f"{name}.{field} is not a URL: {value!r}"
                    )


class TestBcftoolsCsq:
    """`csq` ships inside bcftools rather than as its own binary, so the
    question is the version, not the path. Asserting the unavailable direction
    matters most: the image ships bcftools 1.21, so an "available" assertion
    would pass whether or not the patch worked."""

    def setup_method(self):
        tools.reset_cache()

    def teardown_method(self):
        tools.reset_cache()

    def test_unavailable_when_bcftools_is_missing(self, monkeypatch):
        monkeypatch.setattr(
            tools,
            "bcftools",
            lambda: tools.Tool(
                name="bcftools", path=None, version=None, error="not found"
            ),
        )
        t = tools.bcftools_csq()
        assert not t.available
        assert "bcftools" in (t.error or "")

    def test_unavailable_when_bcftools_is_too_old(self, monkeypatch):
        monkeypatch.setattr(
            tools,
            "bcftools",
            lambda: tools.Tool(name="bcftools", path="/usr/bin/bcftools", version="1.6"),
        )
        t = tools.bcftools_csq()
        assert not t.available
        assert "1.7" in (t.error or "")

    def test_available_on_a_new_enough_bcftools(self, monkeypatch):
        monkeypatch.setattr(
            tools,
            "bcftools",
            lambda: tools.Tool(name="bcftools", path="/usr/bin/bcftools", version="1.21"),
        )
        t = tools.bcftools_csq()
        assert t.available
        assert t.version == "1.21"

    # An unparseable version must not be read as "too old" -- that would
    # disable a working tool over a cosmetic parse failure.
    def test_unknown_version_is_allowed(self, monkeypatch):
        monkeypatch.setattr(
            tools,
            "bcftools",
            lambda: tools.Tool(name="bcftools", path="/usr/bin/bcftools", version=None),
        )
        assert tools.bcftools_csq().available


class TestFingerprint:
    def test_fingerprint_changes_when_the_binary_changes(self, tmp_path):
        """The fingerprint is what keeps a stale version out of a methods
        section: an upgraded tool must not be served from cache."""
        binary = tmp_path / "sometool"
        binary.write_text("#!/bin/sh\necho 'sometool 1.0.0'\n")
        binary.chmod(0o755)

        before = tools._fingerprint(str(binary))

        binary.write_text("#!/bin/sh\necho 'sometool 2.0.0'\n")
        binary.chmod(0o755)

        assert tools._fingerprint(str(binary)) != before

    def test_fingerprint_is_stable_for_an_unchanged_binary(self, tmp_path):
        binary = tmp_path / "sometool"
        binary.write_text("#!/bin/sh\necho 'sometool 1.0.0'\n")
        binary.chmod(0o755)

        assert tools._fingerprint(str(binary)) == tools._fingerprint(str(binary))

    def test_fingerprint_of_a_missing_path_is_none(self):
        """A tool `which` cannot resolve has nothing to fingerprint, and must
        always be probed rather than served from a cache entry."""
        assert tools._fingerprint("/definitely/not/here/xyz") is None

    def test_fingerprint_of_none_is_none(self):
        assert tools._fingerprint(None) is None

    def test_fingerprint_differs_for_identical_content_at_different_paths(self, tmp_path):
        """A content hash alone would treat two tools resolving to identical
        bytes -- or one tool moving to a new PATH entry -- as the same
        fingerprint. Path must be part of the identity."""
        content = "#!/bin/sh\necho 'sometool 1.0.0'\n"

        first = tmp_path / "sometool"
        first.write_text(content)
        first.chmod(0o755)

        second = tmp_path / "othertool"
        second.write_text(content)
        second.chmod(0o755)

        assert tools._fingerprint(str(first)) != tools._fingerprint(str(second))


class TestSeeding:
    def test_a_seeded_probe_does_not_shell_out(self, tmp_path, monkeypatch):
        """The whole point: a seeded entry must skip the subprocess. Asserted
        by seeding a version the script does not print -- if the probe ran, it
        would return 1.0.0 instead."""
        script = tmp_path / "seededtool"
        script.write_text("#!/bin/sh\necho 'seededtool 1.0.0'\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        resolved = str(script)
        tools.seed(
            "seededtool",
            tools._fingerprint(resolved),
            tools.Tool(name="seededtool", path=resolved, version="9.9.9"),
        )

        tool = tools._probe("seededtool", "seededtool", ["--version"])
        assert tool.version == "9.9.9"

    def test_a_stale_fingerprint_forces_a_reprobe(self, tmp_path, monkeypatch):
        """The correctness case. An upgraded binary must be re-probed, not
        served from a seed describing the old one."""
        script = tmp_path / "staletool"
        script.write_text("#!/bin/sh\necho 'staletool 1.0.0'\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        tools.seed(
            "staletool",
            "a-fingerprint-that-does-not-match",
            tools.Tool(name="staletool", path=str(script), version="9.9.9"),
        )

        tool = tools._probe("staletool", "staletool", ["--version"])
        assert tool.version == "1.0.0"

    def test_a_seed_for_a_missing_binary_is_ignored(self):
        """`which` failing short-circuits before the seed is consulted: a tool
        that is no longer installed must report unavailable, not report the
        version it had when it was."""
        tools.seed(
            "gonetool",
            "some-fingerprint",
            tools.Tool(name="gonetool", path="/was/here", version="9.9.9"),
        )

        tool = tools._probe("gonetool", "definitely-not-a-real-binary-xyz", ["--version"])
        assert not tool.available
        assert "not found on PATH" in tool.error

    def test_reset_cache_clears_seeds(self, tmp_path, monkeypatch):
        """Otherwise a test or a runtime config change clears the lru_caches
        and immediately repopulates them from the values it meant to discard."""
        script = tmp_path / "clearedtool"
        script.write_text("#!/bin/sh\necho 'clearedtool 1.0.0'\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        resolved = str(script)
        tools.seed(
            "clearedtool",
            tools._fingerprint(resolved),
            tools.Tool(name="clearedtool", path=resolved, version="9.9.9"),
        )
        tools.reset_cache()

        tool = tools._probe("clearedtool", "clearedtool", ["--version"])
        assert tool.version == "1.0.0"

    def test_a_seed_with_no_fingerprint_is_not_stored(self, tmp_path, monkeypatch):
        """A caller that could not fingerprint the binary has nothing to
        validate against, so its offer must be dropped rather than trusted
        unconditionally."""
        script = tmp_path / "nofptool"
        script.write_text("#!/bin/sh\necho 'nofptool 1.0.0'\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        tools.seed(
            "nofptool",
            None,
            tools.Tool(name="nofptool", path=str(script), version="9.9.9"),
        )

        tool = tools._probe("nofptool", "nofptool", ["--version"])
        assert tool.version == "1.0.0"


def _fake_run(*, daemon_returncode=0, daemon_stderr=b"", inspect_returncode=0):
    """A `subprocess.run` stand-in for `_probe_on_demand_image`'s two calls:
    `docker version` (daemon reachability) first, `docker image inspect`
    (image presence) second. Dispatches on argv rather than call order, so a
    test only needs to say which of the two should fail."""

    def _run(cmd, **kwargs):
        if "image" in cmd and "inspect" in cmd:
            return type(
                "R", (), {"returncode": inspect_returncode, "stdout": b"", "stderr": b""}
            )()
        return type(
            "R",
            (),
            {"returncode": daemon_returncode, "stdout": b"27.3.1", "stderr": daemon_stderr},
        )()

    return _run


class TestDeepVariantProbe:
    def test_unavailable_when_there_is_no_docker_client(self, monkeypatch):
        """The direction that fails when the seam breaks. The image ships most
        tools as installed, so asserting availability passes whether or not a
        patch took effect -- assert the refusal instead."""
        monkeypatch.setattr(tools.shutil, "which", lambda _: None)
        tools.reset_cache()

        tool = tools.deepvariant()
        assert not tool.available
        assert "docker" in (tool.error or "").lower()
        assert tool.install_state is tools.InstallState.UNKNOWN

    def test_available_and_versioned_when_the_image_is_present(self, monkeypatch):
        """There is no binary to ask for a version, and the image tag is the
        provenance that matters -- it is what a methods section would cite.

        Asserted against the *configured* image rather than a literal, because
        the default is architecture-dependent: this previously hardcoded the
        arm64 port's name and so described the host it ran on rather than the
        code. Pinning an image here also proves the version tracks the
        setting instead of a constant that happens to match it.
        """
        monkeypatch.setattr(tools.shutil, "which", lambda _: "/usr/local/bin/docker")
        monkeypatch.setattr(tools.subprocess, "run", _fake_run())
        monkeypatch.setattr(
            tools.settings, "deepvariant_image", "example.invalid/dv:v9.9.9-test"
        )
        tools.reset_cache()

        tool = tools.deepvariant()
        assert tool.available
        assert tool.version == "dv:v9.9.9-test"
        assert tool.install_state is tools.InstallState.INSTALLED

    def test_unavailable_when_the_daemon_is_unreachable(self, monkeypatch):
        """A mounted socket that answers nothing is the compose-misconfigured
        case, and must not read as installed -- it is a fault (UNKNOWN), not
        an offer to install."""
        monkeypatch.setattr(tools.shutil, "which", lambda _: "/usr/local/bin/docker")
        monkeypatch.setattr(
            tools.subprocess,
            "run",
            _fake_run(
                daemon_returncode=1,
                daemon_stderr=b"Cannot connect to the Docker daemon",
            ),
        )
        tools.reset_cache()

        tool = tools.deepvariant()
        assert not tool.available
        assert "daemon" in (tool.error or "").lower()
        assert tool.install_state is tools.InstallState.UNKNOWN

    def test_unavailable_when_the_image_was_never_pulled(self, monkeypatch):
        """The regression this task exists to fix: a reachable daemon used to
        be enough to report `available=True`, whether or not the image had
        ever been pulled. `docker image inspect` failing on an unreachable
        image must read as NOT_INSTALLED -- an offer, not a fault -- and
        `available` must actually be False for it, not merely for a daemon
        that cannot be reached."""
        monkeypatch.setattr(tools.shutil, "which", lambda _: "/usr/local/bin/docker")
        monkeypatch.setattr(
            tools.subprocess, "run", _fake_run(inspect_returncode=1)
        )
        tools.reset_cache()

        tool = tools.deepvariant()
        assert not tool.available
        assert tool.install_state is tools.InstallState.NOT_INSTALLED
        assert "not installed" in (tool.error or "").lower()
        # Reads as an offer, not a fault: the old wording ("not found") is
        # exactly what a not-yet-installed optional tool must not say.
        assert "not found" not in (tool.error or "").lower()

    def test_not_installed_still_reports_no_version(self, monkeypatch):
        """A not-installed tool has no image to read a tag from, so version
        must stay None rather than guessing at the configured image name --
        that would claim a version for software that was never pulled."""
        monkeypatch.setattr(tools.shutil, "which", lambda _: "/usr/local/bin/docker")
        monkeypatch.setattr(
            tools.subprocess, "run", _fake_run(inspect_returncode=1)
        )
        tools.reset_cache()

        tool = tools.deepvariant()
        assert tool.version is None


def test_abyss_is_declared_and_documented():
    """abyss must be probeable and carry complete help-page metadata."""
    from app.pipelines import tools

    tool = tools.abyss()
    assert tool.name == "abyss"

    meta = tools.TOOL_META["abyss"]
    assert meta.homepage
    assert meta.citation
    assert meta.license
    assert meta.usage
