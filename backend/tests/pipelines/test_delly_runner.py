"""Delly command construction. Pure functions, no queue, no filesystem."""

from pathlib import Path

import pytest

from app.errors import ValidationError
from app.pipelines.delly_runner import (
    DellyParams,
    build_bcf_to_vcf_command,
    build_delly_command,
)


class TestDellyParams:
    def test_defaults(self):
        params = DellyParams()
        assert params.threads == 4
        # Delly's own default for min. paired-end mapping quality, from
        # src/delly.h at v2.6.0.
        assert params.min_map_quality == 1

    def test_from_dict_rejects_zero_threads(self):
        with pytest.raises(ValidationError):
            DellyParams.from_dict({"threads": 0})

    def test_from_dict_rejects_negative_map_quality(self):
        with pytest.raises(ValidationError):
            DellyParams.from_dict({"min_map_quality": -1})

    def test_from_dict_accepts_zero_map_quality(self):
        """0 is meaningful to Delly: accept every mapping quality."""
        assert DellyParams.from_dict({"min_map_quality": 0}).min_map_quality == 0

    def test_round_trips_through_as_dict(self):
        params = DellyParams(threads=8, min_map_quality=20)
        assert DellyParams.from_dict(params.as_dict()) == params

    def test_has_no_minimum_sv_length(self):
        """Delly has no call-size floor flag -- its -m is minrefsep, which
        governs breakpoint clustering, not reported call size. Offering a
        min_sv_length here would be a wrong mapping that looks right."""
        assert not hasattr(DellyParams(), "min_sv_length")


class TestBuildDellyCommand:
    def _cmd(self, params=None):
        return build_delly_command(
            delly_path="delly",
            bam=Path("/w/in.bam"),
            reference=Path("/w/ref.fa"),
            output=Path("/w/out.bcf"),
            params=params or DellyParams(),
        )

    def test_uses_the_sr_subcommand(self):
        """Delly 2.x replaced `delly call` with `sr` (short-read) and `lr`.
        A `call` invocation targets a CLI that no longer exists."""
        cmd = self._cmd()
        assert cmd[0] == "delly"
        assert cmd[1] == "sr"

    def test_passes_reference_output_and_input(self):
        cmd = self._cmd()
        assert cmd[cmd.index("-g") + 1] == "/w/ref.fa"
        assert cmd[cmd.index("-o") + 1] == "/w/out.bcf"
        # The BAM is positional and last.
        assert cmd[-1] == "/w/in.bam"

    def test_passes_threads_and_map_quality(self):
        cmd = self._cmd(DellyParams(threads=8, min_map_quality=20))
        assert cmd[cmd.index("-h") + 1] == "8"
        assert cmd[cmd.index("-q") + 1] == "20"

    def test_never_uses_the_long_read_subcommand(self):
        """Sniffles2 is this pipeline's long-read caller. Requirement
        SV-620-4."""
        assert "lr" not in self._cmd()


class TestBcfConversion:
    def test_converts_to_bgzipped_vcf(self):
        cmd = build_bcf_to_vcf_command(
            bcftools_path="bcftools",
            bcf=Path("/w/out.bcf"),
            output=Path("/w/out.vcf.gz"),
        )
        assert cmd[:2] == ["bcftools", "view"]
        assert "/w/out.bcf" in cmd
        assert cmd[cmd.index("-o") + 1] == "/w/out.vcf.gz"
        # -O z is bgzipped VCF, which is what tabix can index.
        assert cmd[cmd.index("-O") + 1] == "z"
