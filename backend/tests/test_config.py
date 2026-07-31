"""Settings properties derived from bioinfo_home."""

from pathlib import Path

from app.config import Settings


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
