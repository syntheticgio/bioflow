"""Tests for meryl_runner — histogram parsing, genome size, repeat density.

The fixture-replay tests at the bottom parse output captured from the real
meryl 1.4.2 binary in the worker image (see the fixture files' provenance in
tests/fixtures/tool_logs). They exist because the original hand-built
fixtures here invented formats meryl never emits — tab-separated histograms
and positional FASTA headers — and stayed green while every real run parsed
to nothing (#612).
"""

from pathlib import Path

from app.pipelines import meryl_runner
from app.storage.parsers import MAX_STORED_CONTIGS

FIXTURES = Path(__file__).parent.parent / "fixtures" / "tool_logs"

# ── command builders ──────────────────────────────────────────────────

def test_print_gt_command_uses_two_token_filter():
    # meryl 1.4.2 misparses "greater-than=N" as opLessThan and prints
    # nothing; the two-token form is the one that works (#612).
    cmd = meryl_runner.build_meryl_print_gt_command(
        meryl_path="meryl", database=Path("/db.meryl"), threshold=3
    )
    assert cmd == ["meryl", "print", "greater-than", "3", "/db.meryl"]
    assert not any("greater-than=" in part for part in cmd)


# ── histogram parsing ─────────────────────────────────────────────────

def test_parse_histogram_space_aligned():
    # The real shape: space-aligned five-column table rows.
    text = (
        "        1         8773       0.9951       0.9813   111.856823\n"
        "        2            2       0.9953       0.9818   223.713647\n"
    )
    result = meryl_runner.parse_meryl_histogram(text)
    assert result == [[1, 8773], [2, 2]]


def test_parse_histogram_skips_preamble_and_headers():
    text = (
        "Number of 21-mers that are:\n"
        "  unique                   8773  (exactly one instance of the kmer is in the input)\n"
        "  missing         4398046502288  (non-redundant kmer sequences not in the input)\n"
        "\n"
        "             number of   cumulative   cumulative     presence\n"
        "frequency        kmers     distinct        total       (1e-6)\n"
        "--------- ------------ ------------ ------------ ------------\n"
        "        1         8773       0.9951       0.9813   111.856823\n"
    )
    result = meryl_runner.parse_meryl_histogram(text)
    assert result == [[1, 8773]]


def test_parse_histogram_skips_short_lines():
    result = meryl_runner.parse_meryl_histogram("1\n1 12345\n")
    assert result == [[1, 12345]]


def test_parse_histogram_empty():
    assert meryl_runner.parse_meryl_histogram("") == []


def test_parse_histogram_real_fixture():
    raw = (FIXTURES / "meryl-1.4.2-statistics.log").read_text()
    result = meryl_runner.parse_meryl_histogram(raw)
    assert result == [[1, 8773], [2, 2], [3, 1], [4, 40]]


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


# ── meryl print parsing ───────────────────────────────────────────────

def test_parse_print_kmers():
    text = "AACCCCCTGCGTCTACAGGTT\t4\nACGCAGGGGGTTTGGTCAGAT\t4\n"
    kmers = meryl_runner.parse_meryl_print_kmers(text)
    assert kmers == {"AACCCCCTGCGTCTACAGGTT", "ACGCAGGGGGTTTGGTCAGAT"}


def test_parse_print_kmers_skips_banner_lines():
    # run_subprocess merges stderr into the same stream, so the parser sees
    # meryl's banners between (or before) the k-mer lines.
    text = (
        "\nFound 1 command tree.\n\n"
        "PROCESSING TREE #1 using 24 threads.\n"
        "  opGreaterThan\n"
        "    threshold=3\n"
        "    asm.meryl\n"
        "    print to (stdout)\n"
        "\nCleaning up.\n\nBye.\n"
        "AACCCCCTGCGTCTACAGGTT\t4\n"
    )
    assert meryl_runner.parse_meryl_print_kmers(text) == {"AACCCCCTGCGTCTACAGGTT"}


def test_parse_print_kmers_real_fixture():
    raw = (FIXTURES / "meryl-1.4.2-print-greater-than.log").read_text()
    kmers = meryl_runner.parse_meryl_print_kmers(raw)
    # The fixture's input planted one 60 bp repeat 4 times: 40 distinct
    # canonical 21-mers at frequency 4.
    assert len(kmers) == 40
    assert all(len(kmer) == 21 for kmer in kmers)
    assert all(set(kmer) <= set("ACGT") for kmer in kmers)


def test_expand_kmer_orientations():
    expanded = meryl_runner.expand_kmer_orientations({"AAC"})
    assert expanded == {"AAC", "GTT"}


# ── repeat density ────────────────────────────────────────────────────

def _flat(seq_parts: list[str]) -> str:
    return "".join(seq_parts)


def test_repeat_density_bins_hits_by_position():
    # 1000 bp contig, 2 windows of 500. Plant a repetitive 21-mer twice in
    # the first window and once in the second; the background is a
    # homopolymer stretch that shares no 21-mer with the repeat.
    rep = "ACGTACGTACGTACGTACGTA"  # 21 bp
    background = "C" * 1000
    seq = (
        background[:100] + rep + background[121:300] + rep + background[321:700]
        + rep + background[721:]
    )
    assert len(seq) == 1000
    result = meryl_runner.compute_repeat_density(
        [("chrI", seq)], {rep}, window_count=2
    )
    c = result["contigs"]
    assert len(c) == 1
    assert c[0]["name"] == "chrI"
    assert c[0]["window_bases"] == 500
    assert c[0]["count"][0] == 2
    assert c[0]["count"][1] == 1
    assert c[0]["density"][0] > c[0]["density"][1] > 0


def test_repeat_density_matches_reverse_complement():
    # Meryl stores the canonical orientation; the assembly can carry the
    # repeat on the other strand. GATC's revcomp shares no forward match.
    rep = "AAAAAAAAAAAAAAAAAAAAG"  # 21 bp, revcomp = CTTTTTTTTTTTTTTTTTTTT
    revcomp = "CTTTTTTTTTTTTTTTTTTTT"
    background = "G" * 1000
    seq = background[:400] + revcomp + background[421:]
    result = meryl_runner.compute_repeat_density(
        [("chrI", seq)], {rep}, window_count=2
    )
    assert sum(result["contigs"][0]["count"]) == 1


def test_repeat_density_zero_hits_are_measured_zero():
    # A scanned contig with no repetitive k-mers is a real measurement of
    # zero, not missing data.
    result = meryl_runner.compute_repeat_density(
        [("chrN", "A" * 1000)], {"ACGTACGTACGTACGTACGTA"}, window_count=2
    )
    c = result["contigs"][0]
    assert c["count"] == [0, 0]
    assert c["density"] == [0.0, 0.0]


def test_repeat_density_respects_k():
    # k flows into the output dict and the density denominator instead of
    # a hard-coded 21 (#612).
    rep = "ACGTACGTACGTACG"  # 15 bp
    seq = "C" * 200 + rep + "C" * 785
    result = meryl_runner.compute_repeat_density(
        [("chrI", seq)], {rep}, k=15, window_count=2
    )
    assert result["k"] == 15
    c = result["contigs"][0]
    assert c["count"][0] == 1
    assert c["density"][0] == round(1 / (c["window_bases"] - 15 + 1), 4)


def test_repeat_density_keeps_longest_and_flags_partial():
    contigs = [(f"c{i}", "A" * 2000) for i in range(60)]
    contigs.append(("longest", "A" * 10000))
    result = meryl_runner.compute_repeat_density(contigs, set(), window_count=2)
    assert len(result["contigs"]) <= MAX_STORED_CONTIGS
    assert any(c["name"] == "longest" for c in result["contigs"])
    assert result["repeat_density_partial"] is True


def test_repeat_density_no_partial_when_under_cap():
    result = meryl_runner.compute_repeat_density(
        [("c1", "A" * 2000), ("c2", "A" * 3000)], set(), window_count=2
    )
    assert "repeat_density_partial" not in result


def test_repeat_density_short_contig_gets_fewer_windows():
    # 2000 bp → 500 windows = 4 bp/window, below the 100 bp floor.
    # Should get 2000 // 100 = 20 windows.
    result = meryl_runner.compute_repeat_density(
        [("small", "A" * 2000)], set(), window_count=500
    )
    c = result["contigs"][0]
    assert c["window_bases"] >= meryl_runner.MIN_WINDOW_BASES
    assert len(c["density"]) == 20


def test_repeat_density_skips_tiny_contigs():
    # A contig below MIN_WINDOW_BASES has zero windows and is dropped.
    result = meryl_runner.compute_repeat_density(
        [("tiny", "A" * 50), ("chrI", "A" * 1000)], set(), window_count=2
    )
    names = [c["name"] for c in result["contigs"]]
    assert names == ["chrI"]


def test_repeat_density_empty_input():
    assert meryl_runner.compute_repeat_density([], set()) == {}


def test_repeat_density_full_chain_against_real_meryl_output():
    """Parse real `meryl print` output, then scan the very FASTA that
    produced it. The fixture input planted one 60 bp repeat four times —
    three sites in contig_1, one in contig_2 — so each site contributes
    exactly 40 matching 21-mer positions (60 - 21 + 1)."""
    kmers = meryl_runner.parse_meryl_print_kmers(
        (FIXTURES / "meryl-1.4.2-print-greater-than.log").read_text()
    )
    density = meryl_runner.compute_repeat_density(
        meryl_runner.iter_fasta_contigs(FIXTURES / "meryl-1.4.2-input.fasta"),
        kmers,
    )
    by_name = {c["name"]: c for c in density["contigs"]}
    assert set(by_name) == {"contig_1", "contig_2", "contig_3"}
    assert sum(by_name["contig_1"]["count"]) == 3 * 40
    assert sum(by_name["contig_2"]["count"]) == 40
    assert sum(by_name["contig_3"]["count"]) == 0


# ── FASTA contig iteration ────────────────────────────────────────────

def test_iter_fasta_contigs(tmp_path):
    fasta = tmp_path / "asm.fasta"
    fasta.write_text(">c1 description here\nacgt\nACGT\n>c2\nGGGG\n")
    contigs = list(meryl_runner.iter_fasta_contigs(fasta))
    assert contigs == [("c1", "ACGTACGT"), ("c2", "GGGG")]


def test_iter_fasta_contigs_gzip(tmp_path):
    import gzip

    fasta = tmp_path / "asm.fasta.gz"
    with gzip.open(fasta, "wt") as fh:
        fh.write(">c1\nACGT\n")
    assert list(meryl_runner.iter_fasta_contigs(fasta)) == [("c1", "ACGT")]


def test_iter_fasta_contigs_missing_file(tmp_path):
    assert list(meryl_runner.iter_fasta_contigs(tmp_path / "nope.fasta")) == []
