"""Parsing the spatial fields out of a read header.

The formats that are *not* Illumina matter as much as the one that is: an
SRA-stripped file and a Nanopore file must both parse to None rather than to
a wrong tile number, because a wrong tile silently produces a wrong heatmap.
"""

from app.pipelines import tile_scanner


def test_parses_illumina_header():
    header = "@M01939:146:000000000-D3WVL:1:1101:15351:1594 1:N:0:1"
    assert tile_scanner.parse_header(header) == tile_scanner.ReadPosition(
        tile=1101, x=15351, y=1594
    )


def test_parses_header_without_the_trailing_read_field():
    # The space-separated remainder is optional in some writers' output.
    header = "@M01939:146:000000000-D3WVL:1:1101:15351:1594"
    assert tile_scanner.parse_header(header) == tile_scanner.ReadPosition(
        tile=1101, x=15351, y=1594
    )


def test_sra_stripped_header_yields_none():
    assert tile_scanner.parse_header("@SRR123456.1 1 length=100") is None


def test_nanopore_uuid_header_yields_none():
    header = "@a1b2c3d4-e5f6-7890-abcd-ef1234567890 runid=xyz ch=42"
    assert tile_scanner.parse_header(header) is None


def test_pacbio_zmw_header_yields_none():
    assert tile_scanner.parse_header("@m54238_180901_011437/4194374/0_1000") is None


def test_truncated_header_yields_none():
    assert tile_scanner.parse_header("@M01939:146:000000000-D3WVL:1") is None


def test_non_numeric_tile_yields_none():
    header = "@M01939:146:000000000-D3WVL:1:notatile:15351:1594"
    assert tile_scanner.parse_header(header) is None


def test_empty_and_bare_at_yield_none():
    assert tile_scanner.parse_header("") is None
    assert tile_scanner.parse_header("@") is None


import gzip

import pytest


def _write_fastq(path, records):
    """Write (header, seq, qual) triples as a 4-line-per-record FASTQ."""
    lines = []
    for header, seq, qual in records:
        lines += [header, seq, "+", qual]
    text = "\n".join(lines) + "\n"
    if str(path).endswith(".gz"):
        with gzip.open(path, "wt") as fh:
            fh.write(text)
    else:
        path.write_text(text)
    return path


def _illumina(tile, x=1000, y=2000, qual="IIII"):
    return (f"@M01939:146:FC:1:{tile}:{x}:{y} 1:N:0:1", "ACGT", qual)


def test_scan_groups_quality_by_tile_and_position(tmp_path):
    # 'I' is Phred 40, '5' is Phred 20 in Sanger encoding (ord - 33).
    path = _write_fastq(
        tmp_path / "r1.fastq",
        [_illumina(1101, qual="IIII"), _illumina(1102, qual="5555")],
    )
    result = tile_scanner.scan(path)

    assert result.source == "present"
    assert result.tile_count == 2
    assert result.matrix[1101] == [40.0, 40.0, 40.0, 40.0]
    assert result.matrix[1102] == [20.0, 20.0, 20.0, 20.0]


def test_scan_averages_reads_within_a_tile(tmp_path):
    path = _write_fastq(
        tmp_path / "r1.fastq",
        [_illumina(1101, qual="IIII"), _illumina(1101, qual="5555")],
    )
    result = tile_scanner.scan(path)
    # Mean of Q40 and Q20 at every position.
    assert result.matrix[1101] == [30.0, 30.0, 30.0, 30.0]


def test_scan_reads_gzipped_input(tmp_path):
    path = _write_fastq(tmp_path / "r1.fastq.gz", [_illumina(1101)])
    result = tile_scanner.scan(path)
    assert result.source == "present"
    assert result.tile_count == 1


def test_scan_bails_out_early_on_headers_without_tiles(tmp_path):
    # More records than the probe window, so a scan that did not bail would
    # read past it. All are SRA-stripped.
    records = [(f"@SRR123456.{i}", "ACGT", "IIII") for i in range(3000)]
    path = _write_fastq(tmp_path / "r1.fastq", records)

    result = tile_scanner.scan(path)

    assert result.source == "absent"
    assert result.matrix == {}
    # The probe window bounds what was inspected, not the file's length.
    assert result.records_inspected <= tile_scanner.PROBE_RECORDS


def test_scan_keeps_going_when_tiles_appear_within_the_probe_window(tmp_path):
    # A handful of junk headers ahead of real ones must not trigger the bail.
    records = [("@SRR1.1", "ACGT", "IIII")] * 10 + [_illumina(1101)] * 20
    path = _write_fastq(tmp_path / "r1.fastq", records)

    result = tile_scanner.scan(path)

    assert result.source == "present"
    assert result.matrix[1101] == [40.0, 40.0, 40.0, 40.0]


def test_scan_samples_a_small_file_completely(tmp_path):
    path = _write_fastq(tmp_path / "r1.fastq", [_illumina(1101)] * 50)
    result = tile_scanner.scan(path, target_sampled_reads=1000)
    assert result.sample_rate == 1
    assert result.sampled_reads == 50


def test_scan_thins_a_file_larger_than_the_target(tmp_path):
    path = _write_fastq(tmp_path / "r1.fastq", [_illumina(1101)] * 100)
    # 100 identical records estimate to ~100 records; against a target of 10
    # that is a 1-in-10 rate, landing at 10 sampled reads. A loose ">1"/"<100"
    # assertion here would pass even if the rate math drifted by an order of
    # magnitude, which is exactly the kind of regression this function exists
    # to avoid on real multi-gigabyte files.
    result = tile_scanner.scan(path, target_sampled_reads=10)
    assert result.sample_rate == 10
    assert result.sampled_reads == 10


def test_scan_records_per_tile_coordinate_extents(tmp_path):
    path = _write_fastq(
        tmp_path / "r1.fastq",
        [_illumina(1101, x=10, y=20), _illumina(1101, x=90, y=80)],
    )
    result = tile_scanner.scan(path)
    extent = result.extents[1101]
    assert (extent.x_min, extent.x_max) == (10, 90)
    assert (extent.y_min, extent.y_max) == (20, 80)
    assert extent.reads == 2


def test_scan_truncates_beyond_the_tile_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(tile_scanner, "MAX_TILES", 3)
    records = [_illumina(1100 + i) for i in range(10)]
    path = _write_fastq(tmp_path / "r1.fastq", records)

    result = tile_scanner.scan(path)

    assert result.truncated is True
    assert len(result.matrix) == 3


def test_scan_bounds_extents_by_the_same_tile_cap(tmp_path, monkeypatch):
    # extents must stop growing at MAX_TILES too, not just the quality
    # matrix -- otherwise a malformed file with many fabricated tile values
    # defeats half the guardrail's purpose while `matrix` looks capped.
    monkeypatch.setattr(tile_scanner, "MAX_TILES", 3)
    records = [_illumina(1100 + i) for i in range(10)]
    path = _write_fastq(tmp_path / "r1.fastq", records)

    result = tile_scanner.scan(path)

    assert len(result.extents) == 3


def test_scan_truncates_beyond_the_position_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(tile_scanner, "MAX_POSITIONS", 2)
    path = _write_fastq(tmp_path / "r1.fastq", [_illumina(1101, qual="IIII")])

    result = tile_scanner.scan(path)

    assert result.truncated is True
    assert len(result.matrix[1101]) == 2


def test_scan_identifies_the_worst_tile(tmp_path):
    path = _write_fastq(
        tmp_path / "r1.fastq",
        [_illumina(1101, qual="IIII"), _illumina(1102, qual="5555")],
    )
    result = tile_scanner.scan(path)
    assert result.worst_tile == 1102


def test_scan_handles_a_truncated_final_record(tmp_path):
    # A file cut off mid-record must not raise -- QC runs on files that are
    # still being written often enough for this to matter.
    path = tmp_path / "r1.fastq"
    path.write_text("@M01939:146:FC:1:1101:1:2 1:N:0:1\nACGT\n+\n")
    result = tile_scanner.scan(path)
    assert result.source in ("present", "absent")


def test_scan_ignores_quality_longer_than_the_matrix_row(tmp_path):
    # Variable read lengths within one file: the row grows to the longest.
    path = _write_fastq(
        tmp_path / "r1.fastq",
        [
            ("@M01939:146:FC:1:1101:1:2 1:N:0:1", "ACGT", "IIII"),
            ("@M01939:146:FC:1:1101:1:3 1:N:0:1", "ACGTAA", "IIIIII"),
        ],
    )
    result = tile_scanner.scan(path)
    assert len(result.matrix[1101]) == 6
