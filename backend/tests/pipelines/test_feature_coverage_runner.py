from pathlib import Path

from app.pipelines import feature_coverage_runner as fcr


def test_build_command_uses_sorted_streaming(tmp_path):
    cmd = fcr.build_command(
        annotation=tmp_path / "ann.sorted.gff",
        bam=tmp_path / "aln.bam",
        genome_file=tmp_path / "ref.genome",
    )
    assert cmd[0] == "bedtools"
    assert cmd[1] == "coverage"
    assert "-sorted" in cmd
    assert "-g" in cmd
    # -a is the annotation (features reported per-row), -b the BAM
    assert str(tmp_path / "ann.sorted.gff") == cmd[cmd.index("-a") + 1]
    assert str(tmp_path / "aln.bam") == cmd[cmd.index("-b") + 1]


def test_build_genome_file_orders_like_fai(tmp_path):
    fai = tmp_path / "ref.fa.fai"
    fai.write_text("chr2\t500\t6\t60\t61\nchr1\t300\t520\t60\t61\n")
    out = fcr.build_genome_file(fai, tmp_path / "ref.genome")
    assert out.read_text() == "chr2\t500\nchr1\t300\n"


def test_parse_coverage_gff(tmp_path):
    # bedtools coverage appends 4 columns to each annotation row:
    # read count, bases covered, feature length, breadth fraction.
    out = tmp_path / "coverage.tsv"
    out.write_text(
        "chr1\tRefSeq\tgene\t100\t400\t.\t+\t.\tID=gene-abcA;Name=abcA\t"
        "12\t250\t301\t0.8305648\n"
        "chr1\tRefSeq\tgene\t900\t1400\t.\t-\t.\tID=gene-abcB\t"
        "0\t0\t501\t0.0000000\n"
    )
    report = fcr.parse_coverage(out, annotation_format="gff")
    assert report["feature_count"] == 2
    assert report["features_zero_coverage"] == 1
    rows = report["features"]
    assert rows[0]["name"] == "abcB"  # sorted ascending by breadth
    assert rows[0]["breadth"] == 0.0
    assert rows[1] == {
        "name": "abcA",
        "type": "gene",
        "seq_id": "chr1",
        "start": 100,
        "end": 400,
        "strand": "+",
        "read_count": 12,
        "bases_covered": 250,
        "length": 301,
        "breadth": 0.8305648,
    }
    assert 0.0 <= report["median_breadth"] <= 1.0


def test_parse_coverage_matches_real_bedtools_output(tmp_path):
    # Captured verbatim from `bedtools coverage -sorted -g ref.genome -a
    # ann.gff -b reads.bam` (bedtools v2.31.1) against a hand-built 3-gene
    # GFF3 (chrT: abcA 1-200, abcB 300-500, abcC 700-900) and a synthetic
    # sorted+indexed BAM: 4 reads fully covering abcA, 2 reads partially
    # covering abcB, none touching abcC. Confirms the parser's cols[-4:]
    # column-count assumption (9 GFF columns + 4 appended) against a real
    # bedtools run rather than an assumed layout.
    out = tmp_path / "real_coverage.tsv"
    out.write_text(
        "chrT\tRefSeq\tgene\t1\t200\t.\t+\t.\tID=gene-abcA;Name=abcA\t"
        "4\t200\t200\t1.0000000\n"
        "chrT\tRefSeq\tgene\t300\t500\t.\t-\t.\tID=gene-abcB;Name=abcB\t"
        "2\t100\t201\t0.4975124\n"
        "chrT\tRefSeq\tgene\t700\t900\t.\t+\t.\tID=gene-abcC;Name=abcC\t"
        "0\t0\t201\t0.0000000\n"
    )
    report = fcr.parse_coverage(out, annotation_format="gff")
    assert report["feature_count"] == 3
    assert report["features_zero_coverage"] == 1
    rows = report["features"]
    assert [r["name"] for r in rows] == ["abcC", "abcB", "abcA"]
    assert rows[0]["breadth"] == 0.0
    assert rows[1] == {
        "name": "abcB",
        "type": "gene",
        "seq_id": "chrT",
        "start": 300,
        "end": 500,
        "strand": "-",
        "read_count": 2,
        "bases_covered": 100,
        "length": 201,
        "breadth": 0.4975124,
    }
    assert rows[2]["name"] == "abcA"
    assert rows[2]["breadth"] == 1.0
    assert report["median_breadth"] == 0.4975124
