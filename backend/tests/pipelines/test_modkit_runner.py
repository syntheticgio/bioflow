"""Unit tests for the pure modkit runner.

`TestHasModificationTags` is the load-bearing class: it asserts the
documented false negative decision K1 accepts (a BAM whose MM/ML tags only
appear after the sampled prefix reads as "not found"), not just the two easy
directions. See docs/superpowers/specs/2026-08-20-modkit-methylation-design.md.

The bedMethyl fixture at tests/fixtures/modkit/pileup.bed is hand-written to
the 18-column layout documented in modkit's README ("Description of
bedMethyl output", current master/0.6.x as of 2026-08-20): columns 1-9
tab-delimited BED9, columns 10-18 space-delimited. No real modkit binary was
available to capture a run from in this environment.
"""

import array
from pathlib import Path

import pytest

from app.pipelines import modkit_runner as mr

pysam = pytest.importorskip("pysam")

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "modkit" / "pileup.bed"


def _write_bam(path, *, n_reads, mm_tag_positions):
    """Build a small synthetic BAM. `mm_tag_positions` is the set of 0-based
    read indices that carry an MM/ML tag pair; every other read carries
    neither.
    """
    header = {
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [{"SN": "chr1", "LN": 100000}],
    }
    with pysam.AlignmentFile(str(path), "wb", header=header) as out:
        for i in range(n_reads):
            a = pysam.AlignedSegment()
            a.query_name = f"read{i}"
            a.query_sequence = "ACGT" * 25  # 100 bp
            a.flag = 0
            a.reference_id = 0
            a.reference_start = i * 10
            a.mapping_quality = 60
            a.cigar = [(0, 100)]
            a.query_qualities = pysam.qualitystring_to_array("I" * 100)
            if i in mm_tag_positions:
                a.set_tag("MM", "C+m,0;", value_type="Z")
                # A `B` array tag: pysam infers the SAM array typecode from
                # the Python array's own typecode ("B" = unsigned char) when
                # no value_type is given -- passing value_type explicitly
                # for an array tag raises in this pysam version.
                a.set_tag("ML", array.array("B", [255]))
            out.write(a)


class TestHasModificationTags:
    def test_tags_present_in_early_reads_are_found(self, tmp_path):
        bam = tmp_path / "with_tags.bam"
        _write_bam(bam, n_reads=10, mm_tag_positions={2})
        probe = mr.has_modification_tags(bam, limit=1000)
        assert probe.found is True
        # Scanning stops as soon as a tag is found -- read index 2 means 3
        # records were read (0, 1, 2).
        assert probe.records_scanned == 3

    def test_no_tags_anywhere_is_not_found(self, tmp_path):
        bam = tmp_path / "no_tags.bam"
        _write_bam(bam, n_reads=50, mm_tag_positions=set())
        probe = mr.has_modification_tags(bam, limit=1000)
        assert probe.found is False
        # No early exit possible, so every record in the (small) file is
        # scanned -- the "how far it looked" the caller can report.
        assert probe.records_scanned == 50

    def test_tags_only_after_the_sampled_prefix_are_a_documented_false_negative(
        self, tmp_path
    ):
        """The honest boundary of the whole design (K1): a bounded prefix
        scan cannot see a tag that first appears past the limit, and this
        test asserts that miss explicitly rather than pretending it cannot
        happen. Deleting this test to "fix" the bound into a full scan of a
        multi-gigabyte BAM is exactly the mistake the design spec warns
        against.
        """
        bam = tmp_path / "tags_after_prefix.bam"
        limit = 20
        # The only tagged read sits well past the limit.
        _write_bam(bam, n_reads=limit + 10, mm_tag_positions={limit + 5})
        probe = mr.has_modification_tags(bam, limit=limit)
        assert probe.found is False
        assert probe.records_scanned == limit


class TestBuildPileupCommand:
    def test_shape(self, tmp_path):
        bam = tmp_path / "a.bam"
        out = tmp_path / "out.bed"
        cmd = mr.build_pileup_command(bam, out, threads=4)
        assert cmd == ["modkit", "pileup", str(bam), str(out), "--threads", "4"]

    def test_no_ref_or_cpg_flags(self, tmp_path):
        """Per the design's spike answer (S-4/S-5 in the plan): `--cpg` is
        the only subcommand mode that needs `--ref`, and this feature never
        passes `--cpg`, so neither flag belongs in the built command."""
        cmd = mr.build_pileup_command(tmp_path / "a.bam", tmp_path / "out.bed")
        assert "--ref" not in cmd
        assert "--cpg" not in cmd

    def test_default_threads(self, tmp_path):
        cmd = mr.build_pileup_command(tmp_path / "a.bam", tmp_path / "out.bed")
        assert cmd[-2:] == ["--threads", "2"]


class TestParseBedmethyl:
    def test_parses_the_captured_fixture(self):
        records = mr.parse_bedmethyl(FIXTURE_PATH)
        assert len(records) == 4

    def test_columns_1_to_9_are_tab_delimited_and_10_to_18_space_delimited(self):
        records = mr.parse_bedmethyl(FIXTURE_PATH)
        first = records[0]
        assert first.chrom == "chr1"
        assert first.start == 1000
        assert first.end == 1001
        assert first.modified_base_code == "m"
        assert first.score == 20
        assert first.strand == "+"
        assert first.n_valid_cov == 20
        assert first.fraction_modified == pytest.approx(75.0)
        assert first.n_mod == 15
        assert first.n_canonical == 5

    def test_modification_codes_are_preserved_per_row(self):
        records = mr.parse_bedmethyl(FIXTURE_PATH)
        codes = {r.modified_base_code for r in records}
        assert codes == {"m", "h", "a"}

    def test_missing_file_returns_empty_list(self, tmp_path):
        assert mr.parse_bedmethyl(tmp_path / "does_not_exist.bed") == []

    def test_empty_file_returns_empty_list(self, tmp_path):
        empty = tmp_path / "empty.bed"
        empty.write_text("")
        assert mr.parse_bedmethyl(empty) == []


class TestSummarize:
    def test_no_records_returns_empty_dict(self):
        """K3's other half: this is what lets the handler tell 'zero rows'
        apart from 'a run that produced facts', by checking truthiness of
        the return rather than duplicating a row-count check."""
        assert mr.summarize([]) == {}

    def test_site_count_and_weighted_mean(self):
        records = mr.parse_bedmethyl(FIXTURE_PATH)
        facts = mr.summarize(records)
        assert facts["methylation_site_count"] == 4
        # sum(N_mod) = 15+9+6+10 = 40; sum(N_valid_cov) = 20+18+30+10 = 78
        assert facts["methylation_mean_pct"] == pytest.approx(
            round(100.0 * 40 / 78, 2)
        )

    def test_per_code_breakdown(self):
        records = mr.parse_bedmethyl(FIXTURE_PATH)
        facts = mr.summarize(records)
        by_code = facts["methylation_by_code"]
        assert set(by_code) == {"m", "h", "a"}
        assert by_code["m"]["sites"] == 2
        assert by_code["h"]["sites"] == 1
        assert by_code["a"]["sites"] == 1
        # "a" code: single row, N_mod=10, N_valid_cov=10 -> 100%.
        assert by_code["a"]["mean_pct"] == pytest.approx(100.0)
