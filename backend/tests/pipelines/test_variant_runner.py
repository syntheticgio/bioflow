"""Variant calling command construction, caller selection, and progress."""

from pathlib import Path

import pytest

from app.errors import ValidationError
from app.pipelines import variant_runner
from app.pipelines.align_runner import ReadChemistry
from app.pipelines.variant_runner import (
    BcftoolsParams,
    Clair3Params,
    VariantCaller,
    VariantParams,
    build_bcftools_command,
    build_clair3_command,
    build_index_command,
    caller_for_chemistry,
    clair3_platform_for_chemistry,
    output_name,
)


class TestCallerForChemistry:
    @pytest.mark.parametrize(
        "chemistry",
        [ReadChemistry.ONT_SIMPLEX, ReadChemistry.ONT_DUPLEX, ReadChemistry.HIFI],
    )
    def test_high_accuracy_long_reads_select_clair3(self, chemistry):
        assert caller_for_chemistry(chemistry) is VariantCaller.CLAIR3

    def test_short_selects_bcftools(self):
        assert caller_for_chemistry(ReadChemistry.SHORT) is VariantCaller.BCFTOOLS

    def test_unknown_defaults_to_bcftools(self):
        """QC may never have run. Short-read is both the common case and the
        conservative one: bcftools on long reads calls badly, but Clair3 on
        short reads needs a model that is not installed."""
        assert caller_for_chemistry(ReadChemistry.UNKNOWN) is VariantCaller.BCFTOOLS

    def test_clr_is_refused(self):
        with pytest.raises(ValidationError, match="CLR"):
            caller_for_chemistry(ReadChemistry.CLR)

    def test_clr_refusal_names_the_alternative(self):
        """The error is the whole UX for this case, so it must say what to do
        instead rather than only what went wrong."""
        with pytest.raises(ValidationError) as exc:
            caller_for_chemistry(ReadChemistry.CLR)
        assert "HiFi" in str(exc.value)

    def test_every_chemistry_is_handled(self):
        """No chemistry falls through unclassified: each either picks a caller
        or is explicitly refused."""
        for chemistry in ReadChemistry:
            if chemistry is ReadChemistry.CLR:
                with pytest.raises(ValidationError):
                    caller_for_chemistry(chemistry)
            else:
                assert isinstance(caller_for_chemistry(chemistry), VariantCaller)


class TestClair3Platform:
    def test_hifi_maps_to_hifi(self):
        assert clair3_platform_for_chemistry(ReadChemistry.HIFI) == "hifi"

    @pytest.mark.parametrize(
        "chemistry", [ReadChemistry.ONT_SIMPLEX, ReadChemistry.ONT_DUPLEX]
    )
    def test_ont_maps_to_ont(self, chemistry):
        """Clair3 ships one ONT model; duplex uses the same SUP model as
        simplex, so the chemistry drives platform, not a separate model."""
        assert clair3_platform_for_chemistry(chemistry) == "ont"

    def test_unknown_falls_back_to_ont(self):
        assert clair3_platform_for_chemistry(None) == "ont"


class TestClair3Command:
    def _cmd(self, **overrides):
        kwargs = {
            "clair3_path": "run_clair3.sh",
            "bam": Path("/w/aligned.bam"),
            "reference": Path("/w/ref/genome.fna"),
            "output_dir": Path("/w/out"),
            "model_path": Path("/opt/clair3/models/ont"),
            "params": Clair3Params(threads=4, platform="ont"),
        }
        kwargs.update(overrides)
        return build_clair3_command(**kwargs)

    def test_builds_run_clair3_invocation(self):
        cmd = self._cmd()
        assert cmd[0] == "run_clair3.sh"
        assert "--bam_fn=/w/aligned.bam" in cmd
        assert "--ref_fn=/w/ref/genome.fna" in cmd
        assert "--threads=4" in cmd
        assert "--platform=ont" in cmd
        assert "--model_path=/opt/clair3/models/ont" in cmd
        assert "--output=/w/out" in cmd

    def test_includes_all_contigs(self):
        """Clair3 otherwise restricts calling to chr1..22,X,Y, which silently
        yields an empty VCF for any non-human assembly."""
        assert "--include_all_ctgs" in self._cmd()

    def test_hifi_platform_is_passed_through(self):
        cmd = self._cmd(params=Clair3Params(threads=8, platform="hifi"))
        assert "--platform=hifi" in cmd
        assert "--threads=8" in cmd


class TestBcftoolsCommand:
    def _cmd(self, **overrides):
        kwargs = {
            "bcftools_path": "bcftools",
            "reference": Path("/w/ref/genome.fna"),
            "bam": Path("/w/aligned.bam"),
            "output": Path("/w/out/calls.vcf.gz"),
            "params": BcftoolsParams(threads=4),
        }
        kwargs.update(overrides)
        return build_bcftools_command(**kwargs)

    def test_builds_mpileup_call_view_pipeline(self):
        script = self._cmd()[-1]
        assert "bcftools mpileup" in script
        assert "bcftools call" in script
        assert "bcftools view" in script

    def test_output_is_compressed_vcf(self):
        """-O z, not -O b: a .tbi index requires bgzipped VCF, not BCF."""
        script = self._cmd()[-1]
        assert "-O z" in script
        assert "/w/out/calls.vcf.gz" in script

    def test_runs_under_sh_with_pipefail(self):
        """Without pipefail the pipe reports the *last* command's status, so a
        failed mpileup would look like a successful run with an empty VCF."""
        assert self._cmd()[:3] == ["/bin/sh", "-o", "pipefail"]

    def test_max_depth_is_passed(self):
        script = self._cmd(params=BcftoolsParams(threads=2, max_depth=500))[-1]
        assert "500" in script

    def test_paths_are_shell_quoted(self):
        """These paths come from user-supplied filenames and land in a shell
        string, so quoting is a correctness requirement, not tidiness."""
        script = self._cmd(bam=Path("/w/my reads.bam"))[-1]
        assert "'/w/my reads.bam'" in script


class TestIndexCommand:
    def test_builds_bcftools_index_tbi(self):
        assert build_index_command(
            bcftools_path="bcftools", vcf=Path("/w/out/calls.vcf.gz")
        ) == ["bcftools", "index", "-t", "/w/out/calls.vcf.gz"]


class TestOutputName:
    def test_derives_from_bam_and_caller(self):
        """Named after the BAM rather than the reads, so two alignments of the
        same reads produce distinguishable VCFs."""
        assert output_name("sample.bam", "clair3") == "sample.clair3.vcf.gz"

    def test_strips_only_the_bam_suffix(self):
        assert output_name("sample.sorted.bam", "bcftools") == (
            "sample.sorted.bcftools.vcf.gz"
        )


class TestVariantParams:
    def test_defaults(self):
        p = VariantParams.from_dict(None)
        assert p.caller is VariantCaller.CLAIR3
        assert p.threads == 4

    def test_overrides(self):
        p = VariantParams.from_dict({"caller": "bcftools", "threads": 8})
        assert p.caller is VariantCaller.BCFTOOLS
        assert p.threads == 8

    def test_invalid_caller_rejected(self):
        with pytest.raises(ValidationError, match="Unknown variant caller"):
            VariantParams.from_dict({"caller": "gatk"})

    def test_invalid_caller_lists_the_valid_ones(self):
        with pytest.raises(ValidationError) as exc:
            VariantParams.from_dict({"caller": "gatk"})
        assert "clair3" in str(exc.value.details)

    def test_threads_must_be_positive(self):
        with pytest.raises(ValidationError, match="threads"):
            VariantParams.from_dict({"threads": 0})

    def test_round_trips_through_as_dict(self):
        p = VariantParams(caller=VariantCaller.BCFTOOLS, threads=6)
        assert VariantParams.from_dict(p.as_dict()) == p


class TestVariantProgress:
    def test_reports_phase_changes(self):
        p = variant_runner.VariantProgress()
        assert p.feed("[INFO] Calling variants in pileup mode") is True
        assert p.phase == "pileup"

    def test_repeated_phase_does_not_republish(self):
        """The callback writes to the database, so a banner repeated on every
        line must not mean an update on every line."""
        p = variant_runner.VariantProgress()
        p.feed("running pileup model")
        assert p.feed("pileup still going") is False

    def test_full_alignment_beats_pileup(self):
        """Clair3's full-alignment banner also contains the word 'pileup'; the
        more specific phase has to win or the bar sticks."""
        p = variant_runner.VariantProgress()
        p.feed("[INFO] Calling variants in full-alignment mode from pileup input")
        assert p.phase == "full_alignment"

    def test_unrecognized_lines_are_ignored(self):
        p = variant_runner.VariantProgress()
        assert p.feed("some unrelated tool chatter") is False

    def test_pct_is_always_none(self):
        """Neither caller reports measurable progress. None is honest; a
        fabricated fraction is not."""
        p = variant_runner.VariantProgress()
        p.feed("full-alignment")
        assert p.pct is None

    def test_message_tracks_phase(self):
        p = variant_runner.VariantProgress()
        assert p.message() == "calling variants"
        p.feed("merging outputs now")
        assert p.message() == "merging outputs"
