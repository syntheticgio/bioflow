"""Tests for bakta_runner: command construction, GFF3 parsing, gene-density
computation. All pure functions -- no container, queue, or binary needed.
"""

from app.pipelines import bakta_runner as runner


# ── build_bakta_command ──────────────────────────────────────────────


class TestBuildBaktaCommand:
    def test_minimal_invocation(self):
        cmd = runner.build_bakta_command(
            bakta_path="/usr/bin/bakta",
            assembly="/data/ecoli.fna",
            out_dir="/data/out",
            threads=4,
        )
        assert cmd == [
            "/usr/bin/bakta",
            "/data/ecoli.fna",
            "--output", "/data/out",
            "--threads", "4",
        ]

    def test_with_full_organism(self):
        cmd = runner.build_bakta_command(
            bakta_path="bakta",
            assembly="assembly.fna",
            out_dir="out",
            threads=8,
            genus="Escherichia",
            species="coli",
            strain="K-12",
        )
        assert cmd == [
            "bakta",
            "assembly.fna",
            "--output", "out",
            "--threads", "8",
            "--genus", "Escherichia",
            "--species", "coli",
            "--strain", "K-12",
        ]

    def test_omits_none_taxonomy_fields(self):
        cmd = runner.build_bakta_command(
            bakta_path="bakta",
            assembly="assembly.fna",
            out_dir="out",
            threads=1,
            genus=None,
            species="coli",
            strain=None,
        )
        assert cmd == [
            "bakta",
            "assembly.fna",
            "--output", "out",
            "--threads", "1",
            "--species", "coli",
        ]


# ── parse_gff3 ───────────────────────────────────────────────────────

# Minimal GFF3 from a real Bakta run on a small contig.
_SAMPLE_GFF3 = """##gff-version 3
##sequence-region contig_1 1 10000
contig_1\tBakta\tgene\t100\t900\t.\t+\t0\tID=gene_1;Name=dnaA
contig_1\tBakta\tCDS\t100\t900\t.\t+\t0\tID=cds_1;Parent=gene_1
contig_1\tBakta\tgene\t1000\t2500\t.\t-\t0\tID=gene_2;Name=dnaN
contig_1\tBakta\ttRNA\t3000\t3076\t.\t+\t.\tID=trna_1
contig_2\tBakta\tgene\t1\t500\t.\t+\t0\tID=gene_3
"""


class TestParseGff3:
    def test_parses_gene_features(self):
        genes = runner.parse_gff3(_SAMPLE_GFF3)
        assert "contig_1" in genes
        assert "contig_2" in genes
        assert len(genes["contig_1"]) == 2  # two genes, one tRNA skipped
        assert len(genes["contig_2"]) == 1

    def test_gene_coordinates(self):
        genes = runner.parse_gff3(_SAMPLE_GFF3)
        g1 = genes["contig_1"][0]
        assert g1["start"] == 100
        assert g1["end"] == 900
        assert g1["strand"] == "+"

    def test_negative_strand(self):
        genes = runner.parse_gff3(_SAMPLE_GFF3)
        g2 = genes["contig_1"][1]
        assert g2["strand"] == "-"

    def test_skips_trna_features(self):
        genes = runner.parse_gff3(_SAMPLE_GFF3)
        # contig_1 has 2 genes, 1 CDS, 1 tRNA → only genes counted (2)
        assert len(genes["contig_1"]) == 2

    def test_empty_input_returns_empty_dict(self):
        assert runner.parse_gff3("") == {}

    def test_comment_only_returns_empty_dict(self):
        assert runner.parse_gff3("##gff-version 3\n# a comment\n") == {}

    def test_malformed_lines_are_skipped(self):
        bad = "contig_1\tBakta\tgene\tnot_a_number\t900\n"
        assert runner.parse_gff3(bad) == {}


# ── compute_gene_density ─────────────────────────────────────────────


class TestComputeGeneDensity:
    def test_one_contig_one_window(self):
        genes = {"contig_1": [{"start": 1, "end": 500, "strand": "+"}]}
        lengths = {"contig_1": 1000}
        result = runner.compute_gene_density(genes, lengths, window_count=1)
        assert len(result["contigs"]) == 1
        c = result["contigs"][0]
        assert c["name"] == "contig_1"
        assert c["count"] == [1]
        assert c["density"] == [1.0]  # 1 gene / (1000/1000) kb = 1.0

    def test_multiple_genes_per_window(self):
        genes = {
            "contig_1": [
                {"start": 1, "end": 100, "strand": "+"},
                {"start": 150, "end": 250, "strand": "-"},
            ]
        }
        lengths = {"contig_1": 1000}
        result = runner.compute_gene_density(genes, lengths, window_count=1)
        c = result["contigs"][0]
        assert c["count"] == [2]
        assert c["density"] == [2.0]  # 2 genes per kb

    def test_gene_midpoint_binning(self):
        """A gene at position 750 should land in the first window when
        window_bases=500 (midpoint 375 → window 0), and a gene at 1250
        (midpoint 625) should land in the second window."""
        genes = {
            "contig_1": [
                {"start": 1, "end": 750, "strand": "+"},    # midpoint 375
                {"start": 1000, "end": 1500, "strand": "+"}, # midpoint 1250
            ]
        }
        lengths = {"contig_1": 2000}
        result = runner.compute_gene_density(genes, lengths, window_count=2)
        c = result["contigs"][0]
        assert c["window_bases"] == 1000
        assert c["count"] == [1, 1]

    def test_contig_with_no_genes_gets_null_windows(self):
        genes = {}
        lengths = {"contig_1": 10000}
        result = runner.compute_gene_density(genes, lengths, window_count=10)
        c = result["contigs"][0]
        assert c["count"] == [None] * 10
        assert c["density"] == [None] * 10

    def test_null_vs_zero_distinction(self):
        """Zero genes and unannotated are different claims. A contig in the
        lengths dict that has gene hits but zero in one window gets a zero,
        not null."""
        genes = {
            "contig_1": [
                {"start": 1, "end": 100, "strand": "+"},
            ]
        }
        lengths = {"contig_1": 2000}
        result = runner.compute_gene_density(genes, lengths, window_count=2)
        c = result["contigs"][0]
        # Gene midpoint is 50, window_bases=1000 → window 0
        assert c["count"] == [1, 0]
        assert c["density"] == [1.0, 0.0]

    def test_short_contigs_are_skipped(self):
        """A contig with fewer bases than MIN_WINDOW_BASES per window is
        dropped."""
        genes = {"tiny": [{"start": 1, "end": 50, "strand": "+"}]}
        lengths = {"tiny": 50}  # 50 < MIN_WINDOW_BASES (100)
        result = runner.compute_gene_density(genes, lengths, window_count=1)
        assert result == {}

    def test_empty_input_returns_empty_dict(self):
        assert runner.compute_gene_density({}, {}) == {}

    def test_partial_flag_when_contigs_dropped(self):
        """When there are more contigs than MAX_STORED_CONTIGS, the shorter
        ones are dropped and gene_density_partial is set."""
        # Create 51 contigs, each 5000 bp, each with one gene.
        genes = {}
        lengths = {}
        for i in range(51):
            name = f"contig_{i}"
            genes[name] = [{"start": 1, "end": 100, "strand": "+"}]
            lengths[name] = 5000
        result = runner.compute_gene_density(genes, lengths, window_count=5)
        assert len(result["contigs"]) == 50  # MAX_STORED_CONTIGS
        assert result.get("gene_density_partial") is True
