from pathlib import Path

from app.pipelines.variants_in_regions_runner import build_command, parse_output


def test_build_command(tmp_path: Path):
    vcf = tmp_path / "vars.vcf"
    gff = tmp_path / "anno.gff"
    genome = tmp_path / "ref.genome"
    cmd = build_command(vcf, gff, genome)
    assert cmd == [
        "bedtools", "intersect",
        "-sorted", "-g", str(genome),
        "-a", str(vcf), "-b", str(gff), "-wao"
    ]


def test_parse_output_gff(tmp_path: Path):
    stdout_path = tmp_path / "intersect.out"
    # Mock bedtools intersect -wao stdout
    # Columns: VCF (8 cols) + GFF (9 cols) + overlap (1 col) = 18 cols
    line1 = (
        "chr1\t100\trs1\tA\tT\t100\tPASS\t.\t"  # VCF
        "chr1\tsrc\tgene\t50\t150\t.\t+\t.\tID=gene1;Name=GeneA\t"  # GFF
        "1\n"  # overlap
    )
    line2 = (
        "chr1\t200\trs2\tC\tG\t100\tPASS\t.\t"  # VCF
        ".\t-1\t-1\t.\t.\t.\t.\t.\t.\t"  # GFF null
        "0\n"  # overlap 0
    )
    stdout_path.write_text(line1 + line2)

    res = parse_output(stdout_path, annotation_format="gff")
    assert res["total_variants"] == 2
    assert res["variants_in_features"] == 1
    assert res["feature_type_counts"] == {"gene": 1}
    assert len(res["feature_variant_counts"]) == 1
    assert res["feature_variant_counts"][0]["name"] == "GeneA"
    assert res["feature_variant_counts"][0]["variant_count"] == 1
