from pathlib import Path

from app.pipelines.annotation_comparison_runner import (
    build_jaccard_command,
    build_subtract_command,
    parse_jaccard_output,
    parse_subtract_output,
)


def test_build_commands(tmp_path: Path):
    a = tmp_path / "a.gff"
    b = tmp_path / "b.gff"
    assert build_jaccard_command(a, b) == ["bedtools", "jaccard", "-a", str(a), "-b", str(b)]
    assert build_subtract_command(a, b) == [
        "bedtools", "intersect", "-a", str(a), "-b", str(b), "-v"
    ]


def test_parse_jaccard_output(tmp_path: Path):
    jaccard_file = tmp_path / "jaccard.txt"
    jaccard_file.write_text(
        "intersection\tunion\tjaccard\tn_intersections\n"
        "500\t1000\t0.5\t10\n"
    )
    res = parse_jaccard_output(jaccard_file)
    assert res["intersection_bp"] == 500
    assert res["union_bp"] == 1000
    assert res["jaccard"] == 0.5
    assert res["n_intersections"] == 10


def test_parse_subtract_output(tmp_path: Path):
    subtract_file = tmp_path / "subtract.txt"
    subtract_file.write_text(
        "chr1\tsrc\tgene\t100\t200\t.\t+\t.\tID=gene1;Name=Gene1\n"
    )
    res = parse_subtract_output(subtract_file, annotation_format="gff")
    assert len(res) == 1
    assert res[0]["name"] == "Gene1"
    assert res[0]["type"] == "gene"
    assert res[0]["start"] == 100
    assert res[0]["end"] == 200
