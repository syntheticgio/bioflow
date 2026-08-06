"""Variant calling command construction, caller selection, and progress."""

from pathlib import Path

import pytest

from app.errors import PermanentError, ValidationError
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
        ) == ["bcftools", "index", "-t", "-f", "/w/out/calls.vcf.gz"]

    def test_overwrites_an_existing_index(self):
        """Found by running a real DeepVariant job: it writes its own .tbi, and
        without -f bcftools calls that an error and exits 1 -- failing a job
        whose calling stage had already succeeded. Clair3 and bcftools leave no
        index, so nothing caught this until a third caller existed."""
        assert "-f" in build_index_command(
            bcftools_path="bcftools", vcf=Path("/w/out/calls.vcf.gz")
        )


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
        assert p.feed("[INFO] 1/7 Call variants using pileup model") is True
        assert p.phase == "pileup"

    def test_repeated_phase_does_not_republish(self):
        """The callback writes to the database, so a banner repeated on every
        line must not mean an update on every line."""
        p = variant_runner.VariantProgress()
        p.feed("[INFO] 1/7 Call variants using pileup model")
        assert p.feed("[INFO] 1/7 Call variants using pileup model") is False

    def test_full_alignment_beats_pileup(self):
        """Clair3's full-alignment banner also contains the word 'pileup'; the
        more specific phase has to win or the bar sticks."""
        p = variant_runner.VariantProgress()
        p.feed(
            "[INFO] 6/7 Call low-quality variants using full-alignment model"
        )
        assert p.phase == "full_alignment"

    def test_unrecognized_lines_are_ignored(self):
        p = variant_runner.VariantProgress()
        assert p.feed("some unrelated tool chatter") is False

    def test_config_echo_does_not_trigger_full_alignment(self):
        """Found via a real captured log: Clair3 echoes this config line long
        before full-alignment work starts, and a loose 'full.?alignment'
        substring match used to fire on it prematurely."""
        p = variant_runner.VariantProgress()
        assert (
            p.feed("[INFO] ENABLE NO PHASING FOR FULL ALIGNMENT: False") is False
        )

    def test_per_contig_summary_lines_do_not_flip_phase(self):
        """Found via a real captured log: these per-contig summary lines near
        the end of a run used to match the loose 'pileup'/'full.?alignment'
        substrings and flip the phase back and forth after the run was
        effectively done."""
        p = variant_runner.VariantProgress()
        p.feed("[INFO] 6/7 Call low-quality variants using full-alignment model")
        assert p.feed("[INFO] Pileup variants processed in NC_001147.6: 0") is False
        assert (
            p.feed("[INFO] Full-alignment variants processed in NC_001147.6: 0")
            is False
        )
        assert p.phase == "full_alignment"

    def test_pct_is_always_none(self):
        """Neither caller reports measurable progress. None is honest; a
        fabricated fraction is not."""
        p = variant_runner.VariantProgress()
        p.feed("[INFO] 6/7 Call low-quality variants using full-alignment model")
        assert p.pct is None

    def test_message_tracks_phase(self):
        p = variant_runner.VariantProgress()
        assert p.message() == "calling variants"
        p.feed("[INFO] 7/7 Merge pileup VCF and full-alignment VCF")
        assert p.message() == "merging outputs"


class TestVariantProgressChunkUnits:
    """Clair3 declares its whole chunk plan before doing any work, one line
    naming the contigs and the next naming each one's chunk count, in the
    same order. That total is fixed for the whole pileup phase -- a genuine
    units_total, unlike a fraction derived from it (see the class docstring
    for why pct still stays None)."""

    def test_chunk_plan_sets_units_total(self):
        p = variant_runner.VariantProgress()
        assert (
            p.feed("[INFO] Call variant in contigs: NC_001135.5 NC_001147.6")
            is False
        )
        assert p.feed("[INFO] Chunk number for each contig: 3 2") is True
        assert p.units_total == 5

    def test_chunk_completion_increments_units_done(self):
        p = variant_runner.VariantProgress()
        p.feed("[INFO] Chunk number for each contig: 1 1")
        assert (
            p.feed("Total processed positions in NC_001135.5 (chunk 1/1) : 26")
            is True
        )
        assert p.units_done == 1
        assert (
            p.feed("Total processed positions in NC_001147.6 (chunk 1/1) : 3")
            is True
        )
        assert p.units_done == 2

    def test_a_repeated_chunk_line_does_not_double_count(self):
        """Clair3 reports the same contig's chunk again during
        full-alignment calling; that is not a new unit of pileup work."""
        p = variant_runner.VariantProgress()
        p.feed("[INFO] Chunk number for each contig: 1")
        p.feed("Total processed positions in NC_001135.5 (chunk 1/1) : 26")
        assert (
            p.feed("Total processed positions in NC_001135.5 (chunk 1/1) : 7")
            is False
        )
        assert p.units_done == 1

    def test_units_done_is_none_before_any_chunk_completes(self):
        p = variant_runner.VariantProgress()
        assert p.units_done is None

    def test_units_absent_from_snapshot_before_the_plan_is_known(self):
        """No chunk-plan line seen yet -- units must not appear as 0/None,
        which would read as a stalled run rather than an unmeasured one."""
        p = variant_runner.VariantProgress()
        p.feed("[INFO] 1/7 Call variants using pileup model")
        snap = p.snapshot()
        assert "units_total" not in snap
        assert "units_done" not in snap
        assert "unit_label" not in snap

    def test_message_includes_chunk_count_once_known(self):
        p = variant_runner.VariantProgress()
        p.feed("[INFO] 1/7 Call variants using pileup model")
        p.feed("[INFO] Chunk number for each contig: 2")
        p.feed("Total processed positions in NC_001135.5 (chunk 1/1) : 26")
        assert p.message() == "pileup calling: 1/2 chunks"


class TestDeepVariantCommand:
    def _cmd(self, **over):
        kwargs = dict(
            image="dv:test",
            bam=Path("/data/objects/aa/reads.bam"),
            reference=Path("/data/objects/bb/ref.fa"),
            output_vcf=Path("/data/tmp/out.vcf.gz"),
            container_root="/data",
            host_root="/HOST/bio",
            params=variant_runner.DeepVariantParams(threads=4, model_type="WGS"),
            # Always explicit. Left to its default this reads the *host*
            # architecture, so every assertion below would quietly mean
            # something different on an arm64 machine than on an x86-64 one --
            # and the fastmath tests would pass or fail by accident of where
            # they ran rather than by what the code does.
            arm64=False,
        )
        kwargs.update(over)
        return variant_runner.build_deepvariant_command(**kwargs)

    def test_mounts_the_host_root_not_the_container_root(self):
        """The whole point. Mounting /data would mount an empty directory on
        the host and fail on a file that exists."""
        cmd = self._cmd()
        assert "-v" in cmd
        assert "/HOST/bio:/data" in cmd
        assert "/data:/data" not in cmd

    def test_paths_are_passed_as_container_paths(self):
        """Inside the sibling container the mount is still at /data, so the
        tool's own arguments use container paths -- only the *mount* is
        translated."""
        cmd = self._cmd()
        joined = " ".join(cmd)
        assert "--reads=/data/objects/aa/reads.bam" in joined
        assert "--ref=/data/objects/bb/ref.fa" in joined
        assert "--output_vcf=/data/tmp/out.vcf.gz" in joined

    def test_passes_the_model_type_and_shards(self):
        cmd = self._cmd()
        joined = " ".join(cmd)
        assert "--model_type=WGS" in joined
        assert "--num_shards=4" in joined

    def test_runs_the_named_image(self):
        assert "dv:test" in self._cmd()

    def test_removes_the_container_when_done(self):
        """Without --rm a 8.8GB-image container is left behind per run."""
        assert "--rm" in self._cmd()

    def test_disables_bf16_fastmath_on_arm64(self):
        """Not cosmetic: without these the arm64 port dies with SIGILL inside
        TensorFlow. That image targets Graviton3 and defaults to BF16 fastmath,
        and Docker on macOS advertises bf16 in /proc/cpuinfo while faulting on
        the instruction. Measured 2026-08-01 -- a refactor that drops these
        reintroduces a crash whose message names nothing about its cause."""
        cmd = self._cmd(arm64=True)
        assert "DNNL_DEFAULT_FPMATH_MODE=STRICT" in cmd
        assert "TF_ENABLE_ONEDNN_OPTS=0" in cmd
        # Passed as `-e VALUE` pairs, so each must follow an -e.
        for var in ("DNNL_DEFAULT_FPMATH_MODE=STRICT", "TF_ENABLE_ONEDNN_OPTS=0"):
            assert cmd[cmd.index(var) - 1] == "-e"

    def test_does_not_disable_fastmath_on_x86_64(self):
        """The mirror of the test above, and the direction that actually
        regresses. There is no SIGILL to avoid on x86-64, and
        TF_ENABLE_ONEDNN_OPTS=0 switches off the oneDNN kernels DeepVariant
        leans on -- so carrying the arm64 workaround across would not fail
        anything, it would just make every run much slower with nothing to
        show why."""
        cmd = self._cmd(arm64=False)
        assert "DNNL_DEFAULT_FPMATH_MODE=STRICT" not in cmd
        assert "TF_ENABLE_ONEDNN_OPTS=0" not in cmd

    def test_a_bam_outside_the_storage_root_raises(self):
        with pytest.raises(PermanentError):
            self._cmd(bam=Path("/tmp/elsewhere.bam"))
