"""Settings properties derived from bioinfo_home, and the arch-dependent ones."""

from pathlib import Path
from unittest.mock import patch

from app.config import Settings, default_deepvariant_image, is_arm64


class TestBamStatsDir:
    def test_derived_from_bioinfo_home(self):
        s = Settings(bioinfo_home=Path("/data"))
        assert s.bam_stats_dir == Path("/data/bam_stats")


class TestVcfStatsDir:
    def test_vcf_stats_dir_sits_beside_bam_stats(self):
        """Derived, regenerable report data -- outside objects/, like bam_stats."""
        s = Settings(bioinfo_home=Path("/data"))
        assert s.vcf_stats_dir == Path("/data/vcf_stats")
        assert "objects" not in s.vcf_stats_dir.parts


class TestDeepVariantImage:
    """Neither DeepVariant image is multi-architecture, so the default has to
    follow the machine.

    This was wrong until 2026-08-01: the default was pinned to the arm64
    community port on every platform, which on x86-64 pulls with a
    platform-mismatch warning and then dies at `docker run` with "exec format
    error" -- inside a job, long after the launch-time checks that were meant
    to catch an unavailable tool.
    """

    def test_arm64_gets_the_community_port(self):
        with patch("app.config.platform.machine", return_value="aarch64"):
            assert "arm64" in default_deepvariant_image()

    def test_x86_64_gets_the_upstream_image(self):
        """The direction that was broken. Asserting only the arm64 branch would
        have passed against the hardcoded default it replaced."""
        with patch("app.config.platform.machine", return_value="x86_64"):
            assert default_deepvariant_image() == "google/deepvariant:1.9.0"

    def test_the_two_architectures_do_not_get_the_same_image(self):
        with patch("app.config.platform.machine", return_value="aarch64"):
            arm = default_deepvariant_image()
        with patch("app.config.platform.machine", return_value="x86_64"):
            amd = default_deepvariant_image()
        assert arm != amd

    def test_is_arm64_accepts_both_spellings(self):
        """`uname -m` says aarch64 on Linux and arm64 on macOS, and the port is
        needed for both."""
        for name in ("aarch64", "arm64", "ARM64"):
            with patch("app.config.platform.machine", return_value=name):
                assert is_arm64()
        for name in ("x86_64", "amd64"):
            with patch("app.config.platform.machine", return_value=name):
                assert not is_arm64()

    def test_the_environment_still_wins(self):
        """Arch-dependent default, not an arch-dependent override -- pinning a
        specific build must stay possible on either machine."""
        s = Settings(deepvariant_image="pinned/dv:9.9.9")
        assert s.deepvariant_image == "pinned/dv:9.9.9"
