from pathlib import Path

from app.pipelines.variants_in_regions_runner import (
    bed_column_count,
    build_command,
    parse_output,
)


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


def test_parse_output_bed_names_the_feature_not_the_variant(tmp_path: Path):
    """The B record trails the A record, so BED columns must be sliced from
    the end like the GFF branch -- not from the start, which reads the VCF's
    own columns as the feature and names every region after a REF allele.

    Both lines below are real `bedtools intersect -sorted -wao` output
    (v2.31.1), captured rather than hand-written: VCF (8) + BED (6) +
    overlap (1) = 15 columns, with the no-overlap row padded to the same
    width.
    """
    stdout_path = tmp_path / "intersect.out"
    stdout_path.write_text(
        "chr1\t100\trs1\tA\tT\t100\tPASS\t.\t"  # VCF
        "chr1\t50\t150\tRegionA\t0\t+\t"  # BED
        "1\n"  # overlap
        "chr1\t500\trs2\tC\tG\t100\tPASS\t.\t"  # VCF
        ".\t-1\t-1\t.\t-1\t.\t"  # BED null
        "0\n"  # overlap 0
    )

    res = parse_output(stdout_path, annotation_format="bed", bed_columns=6)
    assert res["total_variants"] == 2
    assert res["variants_in_features"] == 1
    assert res["feature_type_counts"] == {"region": 1}
    assert len(res["feature_variant_counts"]) == 1
    hit = res["feature_variant_counts"][0]
    assert hit["name"] == "RegionA"
    assert hit["seq_id"] == "chr1"
    assert hit["variant_count"] == 1


def test_parse_output_bed_with_a_multi_sample_vcf(tmp_path: Path):
    """The A record widens with FORMAT and sample columns. B is located from
    the end by the annotation's own width, so a genotyped VCF parses the same
    as a bare one -- captured from v2.31.1 with a 10-column VCF."""
    stdout_path = tmp_path / "intersect.out"
    stdout_path.write_text(
        "chr1\t100\trs1\tA\tT\t100\tPASS\t.\tGT\t0/1\t"  # VCF + FORMAT/sample
        "chr1\t50\t150\tRegionA\t0\t+\t"  # BED
        "1\n"
    )

    res = parse_output(stdout_path, annotation_format="bed", bed_columns=6)
    assert res["feature_variant_counts"][0]["name"] == "RegionA"


def test_bed_column_count_measures_the_first_real_record(tmp_path: Path):
    bed = tmp_path / "anno.bed"
    bed.write_text(
        "# a comment\n"
        'track name="regions"\n'
        "chr1\t50\t150\tRegionA\t0\t+\n"
    )
    assert bed_column_count(bed) == 6


def test_bed_column_count_falls_back_to_the_minimum_when_empty(tmp_path: Path):
    """An annotation with no records still has to yield a usable width --
    the parser's slice is built from it before any row is read."""
    bed = tmp_path / "empty.bed"
    bed.write_text("# nothing but a comment\n")
    assert bed_column_count(bed) == 3


def test_parse_output_bed_without_a_name_column_falls_back_to_coordinates(
    tmp_path: Path,
):
    """A 3-column BED has no name field; the fallback must describe the
    feature's own span, not coordinates borrowed from the VCF."""
    stdout_path = tmp_path / "intersect.out"
    stdout_path.write_text(
        "chr1\t100\trs1\tA\tT\t100\tPASS\t.\tchr1\t50\t150\t1\n"
    )

    res = parse_output(stdout_path, annotation_format="bed")
    assert res["feature_variant_counts"][0]["name"] == "chr1:50-150"
