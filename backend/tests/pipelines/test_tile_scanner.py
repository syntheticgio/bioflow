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
