from pathlib import Path

import pytest

from app.errors import ValidationError
from app.pipelines.sequence_extraction_runner import (
    build_command,
    parse_query_lines,
    write_bed_file,
)


def test_build_command(tmp_path: Path):
    fa = tmp_path / "ref.fa"
    bed = tmp_path / "regions.bed"
    cmd = build_command(fa, bed)
    assert cmd == ["seqkit", "subseq", "--bed", str(bed), str(fa)]


def test_parse_query_lines_valid():
    fai = {"chr1": 1000, "chr2": 2000}
    text = "chr1\nchr2:100-500\n"
    res = parse_query_lines(text, fai)
    assert res == [("chr1", 0, 1000), ("chr2", 99, 500)]


def test_parse_query_lines_invalid_name():
    fai = {"chr1": 1000}
    with pytest.raises(ValidationError, match="No sequence named 'chrX'"):
        parse_query_lines("chrX", fai)


def test_parse_query_lines_invalid_bounds():
    fai = {"chr1": 1000}
    with pytest.raises(ValidationError, match="exceeds sequence length"):
        parse_query_lines("chr1:100-1500", fai)


def test_write_bed_file(tmp_path: Path):
    bed_path = tmp_path / "test.bed"
    regions = [("chr1", 0, 1000), ("chr2", 99, 500)]
    write_bed_file(regions, bed_path)
    assert bed_path.read_text() == "chr1\t0\t1000\nchr2\t99\t500\n"
