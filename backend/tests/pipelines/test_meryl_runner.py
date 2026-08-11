"""Tests for meryl_runner — histogram parsing, genome size, repeat density."""

import pytest

from app.pipelines import meryl_runner
from app.storage.parsers import MAX_STORED_CONTIGS


# ── histogram parsing ─────────────────────────────────────────────────

def test_parse_histogram():
    text = "1\t12345\n2\t8901\n3\t4567\n"
    result = meryl_runner.parse_meryl_histogram(text)
    assert result == [[1, 12345], [2, 8901], [3, 4567]]


def test_parse_histogram_skips_blank_lines():
    text = "\n1\t12345\n\n2\t8901\n\n"
    result = meryl_runner.parse_meryl_histogram(text)
    assert len(result) == 2


def test_parse_histogram_skips_short_lines():
    text = "1\n1\t12345\n"
    result = meryl_runner.parse_meryl_histogram(text)
    assert result == [[1, 12345]]


def test_parse_histogram_skips_non_numeric():
    text = "abc\tdef\n1\t12345\n"
    result = meryl_runner.parse_meryl_histogram(text)
    assert result == [[1, 12345]]


def test_parse_histogram_empty():
    assert meryl_runner.parse_meryl_histogram("") == []


# ── genome size estimation ────────────────────────────────────────────

def test_genome_size_unimodal():
    # A clean homozygous genome: one clear peak at coverage 20.
    hist = [[1, 5_000_000], [2, 2_000_000], [5, 1_000_000],
            [10, 3_000_000], [20, 16_000_000], [25, 2_000_000],
            [30, 1_000_000], [40, 500_000]]
    result = meryl_runner.compute_genome_size(hist, k=21)
    assert result["heterozygosity"] is None
    assert result["genome_size_est"] is not None
    assert result["genome_size_est"] > 0
    assert "total_kmers" in result
    assert "distinct_kmers" in result


def test_genome_size_bimodal():
    # Heterozygous diploid: peaks at 10x and 20x.
    hist = [[1, 1_000], [5, 2_000], [10, 8_000], [15, 3_000],
            [20, 16_000], [25, 2_000]]
    result = meryl_runner.compute_genome_size(hist, k=21)
    assert result["heterozygosity"] is not None
    assert result["heterozygosity"] > 0.0
    assert "genome_size_est" in result


def test_genome_size_no_clear_peak():
    # Error-peak that dominates the histogram — k-mer spectra from reads
    # with a strong sequencing error tail have a huge peak at frequency 1
    # that dwarfs the true coverage peak. This is not a meaningful
    # genome-size signal.
    hist = [[1, 1_000_000], [2, 5], [3, 6], [4, 5], [5, 10], [6, 6]]
    result = meryl_runner.compute_genome_size(hist, k=21)
    assert "genome_size_est" not in result
    assert "total_kmers" in result


def test_genome_size_empty():
    result = meryl_runner.compute_genome_size([])
    assert result == {"total_kmers": 0, "distinct_kmers": 0}


# ── repeat density ────────────────────────────────────────────────────

def test_repeat_density_simple():
    lines = [
        ">chrI:0-21\tAAAAAAAAAAAAAAAAAAAAA",
        ">chrI:0-21\tTTTTTTTTTTTTTTTTTTTTT",
        ">chrI:500-521\tGGGGGGGGGGGGGGGGGGGGG",
    ]
    lengths = {"chrI": 1000}
    result = meryl_runner.compute_repeat_density(lines, lengths, window_count=2)
    c = result["contigs"]
    assert len(c) == 1
    assert c[0]["name"] == "chrI"
    assert c[0]["window_bases"] == 500
    # Window 0 has 2 hits, window 1 has 1 hit.
    assert c[0]["density"][0] > c[0]["density"][1]
    assert c[0]["count"][0] == 2
    assert c[0]["count"][1] == 1


def test_repeat_density_all_nulls_for_missing_contig():
    lines = [">chrI:0-21\tAAAAAAAAAAAAAAAAAAAAA"]
    lengths = {"chrI": 1000, "chrN": 500}
    result = meryl_runner.compute_repeat_density(lines, lengths, window_count=2)
    names = [c["name"] for c in result["contigs"]]
    assert "chrN" in names and "chrI" in names
    n_contig = [c for c in result["contigs"] if c["name"] == "chrN"][0]
    assert all(v is None for v in n_contig["density"])
    assert all(v is None for v in n_contig["count"])


def test_repeat_density_keeps_longest_and_flags_partial():
    lengths = {f"c{i}": 2000 for i in range(60)}
    lengths["longest"] = 10000
    lines = [f">longest:{i*100}-{i*100+21}\tAAAAAAAAAAAAAAAAAAAAA" for i in range(20)]
    result = meryl_runner.compute_repeat_density(lines, lengths, window_count=2)
    assert len(result["contigs"]) <= MAX_STORED_CONTIGS
    assert any(c["name"] == "longest" for c in result["contigs"])
    assert result["repeat_density_partial"] is True


def test_repeat_density_no_partial_when_under_cap():
    lengths = {"c1": 2000, "c2": 3000}
    lines = [">c1:0-21\tAAAAAAAAAAAAAAAAAAAAA"]
    result = meryl_runner.compute_repeat_density(lines, lengths, window_count=2)
    assert "repeat_density_partial" not in result


def test_repeat_density_short_contig_gets_fewer_windows():
    # 2000 bp → 500 windows = 4 bp/window, below the 100 bp floor.
    # Should get 2000 // 100 = 20 windows.
    lengths = {"small": 2000}
    lines = [f">small:{i*100}-{i*100+21}\tAAAAAAAAAAAAAAAAAAAAA" for i in range(10)]
    result = meryl_runner.compute_repeat_density(lines, lengths, window_count=500)
    c = result["contigs"][0]
    assert c["window_bases"] >= meryl_runner.MIN_WINDOW_BASES
    assert len(c["density"]) == 20


def test_repeat_density_skips_bad_lines():
    lines = [
        "not a fasta line",
        ">bad:format\tAAAAAAAAAAAAAAAAAAAAA",
        ">chrI:0-21\tAAAAAAAAAAAAAAAAAAAAA",
    ]
    lengths = {"chrI": 1000}
    result = meryl_runner.compute_repeat_density(lines, lengths, window_count=2)
    assert len(result["contigs"]) == 1
    # chrI has 1 hit.
    c = result["contigs"][0]
    assert sum(v for v in c["count"] if v is not None) == 1


def test_repeat_density_empty_input():
    # No k-mer lines at all — the contig in lengths has zero hits,
    # so it gets null windows rather than returning empty.
    result = meryl_runner.compute_repeat_density([], {"chrI": 1000})
    assert len(result["contigs"]) == 1
    c = result["contigs"][0]
    assert c["name"] == "chrI"
    assert all(v is None for v in c["density"])
    assert all(v is None for v in c["count"])
