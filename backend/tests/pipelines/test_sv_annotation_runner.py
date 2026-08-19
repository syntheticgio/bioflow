"""Tests for SV gene overlap annotation (sv_annotation_runner.py)."""

from pathlib import Path

from app.pipelines import csq_parse, sv_annotation_runner


def test_build_gene_index(tmp_path: Path):
    gff_text = """##gff-version 3
chr1\tRefSeq\tgene\t100\t500\t.\t+\t.\tID=gene1;Name=GENE1;gene_biotype=protein_coding
chr1\tRefSeq\tgene\t400\t800\t.\t-\t.\tID=gene2;Name=GENE2;gene_biotype=protein_coding
chr2\tRefSeq\tgene\t2000\t3000\t.\t+\t.\tID=gene3;Name=GENE3;gene_biotype=protein_coding
"""
    gff_path = tmp_path / "test.gff3"
    gff_path.write_text(gff_text)

    index = sv_annotation_runner.build_gene_index(gff_path)
    assert "chr1" in index
    assert len(index["chr1"]) == 2
    assert index["chr1"][0] == (100, 500, "GENE1")
    assert index["chr1"][1] == (400, 800, "GENE2")


def test_find_overlapping_genes(tmp_path: Path):
    gff_text = """##gff-version 3
chr1\tRefSeq\tgene\t1000\t2000\t.\t+\t.\tID=gene1;Name=YFG1
chr1\tRefSeq\tgene\t2500\t3500\t.\t+\t.\tID=gene2;Name=YFG2
chr1\tRefSeq\tgene\t4000\t5000\t.\t+\t.\tID=gene3;Name=YFG3
"""
    gff_path = tmp_path / "test.gff3"
    gff_path.write_text(gff_text)
    index = sv_annotation_runner.build_gene_index(gff_path)

    # Single gene overlap
    genes = sv_annotation_runner.find_overlapping_genes(index, "chr1", 1200, 1500)
    assert genes == ["YFG1"]

    # Multi-gene deletion spanning YFG1 and YFG2
    multi_genes = sv_annotation_runner.find_overlapping_genes(index, "chr1", 1500, 2600)
    assert multi_genes == ["YFG1", "YFG2"]

    # Deletion spanning all 3 genes
    all_genes = sv_annotation_runner.find_overlapping_genes(index, "chr1", 500, 6000)
    assert all_genes == ["YFG1", "YFG2", "YFG3"]

    # Intergenic region (between YFG2 and YFG3)
    intergenic = sv_annotation_runner.find_overlapping_genes(index, "chr1", 3600, 3900)
    assert intergenic == []

    # Contig normalization (1 vs chr1)
    norm_genes = sv_annotation_runner.find_overlapping_genes(index, "1", 1200, 1500)
    assert norm_genes == ["YFG1"]


def test_extract_sv_interval():
    # END in INFO
    start, end = sv_annotation_runner.extract_sv_interval(100, "N", "SVTYPE=DEL;END=5000")
    assert (start, end) == (100, 5000)

    # SVLEN in INFO
    start, end = sv_annotation_runner.extract_sv_interval(200, "N", "SVTYPE=DEL;SVLEN=-1500")
    assert (start, end) == (200, 1699)

    # Fallback to REF length
    start, end = sv_annotation_runner.extract_sv_interval(300, "ATCG", ".")
    assert (start, end) == (300, 303)


def test_annotate_sv_vcf(tmp_path: Path):
    gff_text = """##gff-version 3
chr1\tRefSeq\tgene\t1000\t2000\t.\t+\t.\tID=g1;Name=GENE_A
chr1\tRefSeq\tgene\t3000\t4000\t.\t+\t.\tID=g2;Name=GENE_B
"""
    gff_path = tmp_path / "ref.gff3"
    gff_path.write_text(gff_text)

    vcf_text = """##fileformat=VCFv4.2
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO
chr1\t1500\t.\tN\t<DEL>\t60\tPASS\tSVTYPE=DEL;END=3500
chr1\t5000\t.\tN\t<DEL>\t60\tPASS\tSVTYPE=DEL;END=6000
chr1\t1200\t.\tA\tG\t99\tPASS\tBCSQ=missense|GENE_A|rna-1|protein_coding|+|10K>10R|1200A>G
"""
    vcf_in = tmp_path / "in.vcf"
    vcf_in.write_text(vcf_text)
    vcf_out = tmp_path / "out.vcf"

    stats = sv_annotation_runner.annotate_sv_vcf(vcf_in, vcf_out, gff_path)
    assert stats["annotated"] == 2
    assert stats["total"] == 3

    out_text = vcf_out.read_text()

    # Header injected
    assert "##INFO=<ID=BCSQ," in out_text

    lines = [line for line in out_text.splitlines() if not line.startswith("#")]
    assert len(lines) == 3

    # Multi-gene deletion (GENE_A and GENE_B)
    assert "BCSQ=structural_variant_overlap|GENE_A,GENE_B||protein_coding" in lines[0]
    csq_0 = csq_parse.parse_bcsq(lines[0].split("\t")[7].split("BCSQ=")[1].split(";")[0])
    assert csq_0 is not None
    assert csq_0.consequence == "structural_variant_overlap"
    assert csq_0.gene == "GENE_A,GENE_B"

    # Intergenic deletion
    assert "BCSQ=intergenic||||||" in lines[1]
    csq_1 = csq_parse.parse_bcsq(lines[1].split("\t")[7].split("BCSQ=")[1].split(";")[0])
    assert csq_1 is not None
    assert csq_1.consequence == "intergenic"

    # Small variant pre-annotated by bcftools csq is preserved
    assert "BCSQ=missense|GENE_A|rna-1|protein_coding" in lines[2]
