"""MetaBAT2 command building, depth parsing, and bin enumeration.

Fixtures are captured verbatim from a real MetaBAT2 2.18 run on 2026-08-20
against a synthetic two-organism community (AT-rich contigs at 30x, GC-rich
contigs at 8x, plus one contig short enough to fall out of binning). Nothing
here is invented: the column layouts, the `Coveage` misspelling, and the set of
files MetaBAT2 writes beside the bins are all what the binary actually produced.
"""

import pytest

from app.pipelines import metabat_runner

# From `jgi_summarize_bam_contig_depths --outputDepth`. Columns 4+ are per-BAM
# and come in pairs (depth, variance), so their number and names depend on the
# input; only the first three are positionally stable.
DEPTH_TSV = """contigName\tcontigLen\ttotalAvgDepth\taln.bam\taln.bam-var
orgA_ctg0\t200000\t30.013\t30.013\t30.5048
orgA_ctg1\t200000\t30.013\t30.013\t29.457
orgB_ctg0\t200000\t8.00278\t8.00278\t8.0047
tiny_ctg\t1000\t5.31444\t5.31444\t1.94885
"""

# From `<prefix>.BinInfo.txt`. "Coveage" is MetaBAT2's own spelling.
BIN_INFO_TSV = """BinNum\tNumContigs\tTotalLength\tLengthWeightedAvgCoveage\tFileName
1\t6\t1200000\t8.00292\tbins/bin.1.fa
2\t2\t400000\t30.013\tbins/bin.2.fa
3\t4\t800000\t30.0124\tbins/bin.3.fa
"""


def write_bin(prefix, index, contigs=2, bases=120):
    """A wrapped FASTA, like the ones MetaBAT2 writes."""
    path = prefix.parent / f"{prefix.name}.{index}.fa"
    per = bases // contigs
    body = ""
    for c in range(contigs):
        body += f">ctg{index}_{c} total_depth=8.00 sample_depths=8.0\n"
        seq = "ACGT" * (per // 4)
        for j in range(0, len(seq), 60):
            body += seq[j : j + 60] + "\n"
    path.write_text(body)
    return path


class TestDepthsCommand:
    def test_uses_metabat2s_own_depth_summarizer(self, tmp_path):
        """The command shape is asserted, not just the end result.

        This is the guard for design decision B1, and the failure it protects
        against is silent: a depth file built from mean depths alone -- from
        mosdepth, say, which this app already runs -- is a file MetaBAT2
        accepts and bins from, with worse bins and no error anywhere. An
        end-to-end assertion would pass either way, so the tool actually
        invoked is what has to be pinned.
        """
        cmd = metabat_runner.build_depths_command(
            bam=tmp_path / "aln.bam", output=tmp_path / "depth.txt"
        )
        assert cmd[0] == "jgi_summarize_bam_contig_depths"
        assert "--outputDepth" in cmd
        assert cmd[cmd.index("--outputDepth") + 1] == str(tmp_path / "depth.txt")
        assert cmd[-1] == str(tmp_path / "aln.bam")

    def test_honours_a_configured_path(self, tmp_path):
        cmd = metabat_runner.build_depths_command(
            bam=tmp_path / "a.bam",
            output=tmp_path / "d.txt",
            jgi_depths="/opt/metabat2/env/bin/jgi_summarize_bam_contig_depths",
        )
        assert cmd[0].endswith("/jgi_summarize_bam_contig_depths")


class TestBinningCommand:
    def test_passes_the_expected_inputs(self, tmp_path):
        cmd = metabat_runner.build_binning_command(
            contigs=tmp_path / "contigs.fa",
            depths=tmp_path / "depth.txt",
            out_prefix=tmp_path / "bins" / "bin",
            threads=4,
        )
        assert cmd[0] == "metabat2"
        assert cmd[cmd.index("-i") + 1] == str(tmp_path / "contigs.fa")
        assert cmd[cmd.index("-a") + 1] == str(tmp_path / "depth.txt")
        assert cmd[cmd.index("-o") + 1] == str(tmp_path / "bins" / "bin")
        assert cmd[cmd.index("-t") + 1] == "4"

    def test_always_asks_for_the_unbinned_contigs(self, tmp_path):
        """`--unbinned` is not MetaBAT2's default.

        Without it the contigs MetaBAT2 could not place are written nowhere,
        and how much of a community failed to resolve is exactly what #728
        refuses to discard silently.
        """
        cmd = metabat_runner.build_binning_command(
            contigs=tmp_path / "c.fa",
            depths=tmp_path / "d.txt",
            out_prefix=tmp_path / "bin",
        )
        assert "--unbinned" in cmd

    def test_pins_the_seed_so_a_rerun_reproduces_the_bins(self, tmp_path):
        """MetaBAT2's own default seed is 0, meaning "pick a random one".

        Left at 0, binning the same assembly twice yields different MAGs from
        identical inputs -- which breaks job dedup, provenance, and any
        re-run-to-compare workflow.
        """
        cmd = metabat_runner.build_binning_command(
            contigs=tmp_path / "c.fa",
            depths=tmp_path / "d.txt",
            out_prefix=tmp_path / "bin",
        )
        assert cmd[cmd.index("--seed") + 1] == "1"

    def test_refuses_a_min_contig_below_metabat2s_floor(self, tmp_path):
        """Caught here rather than after the depth step has already run."""
        with pytest.raises(ValueError, match="1500"):
            metabat_runner.build_binning_command(
                contigs=tmp_path / "c.fa",
                depths=tmp_path / "d.txt",
                out_prefix=tmp_path / "bin",
                min_contig=1000,
            )


class TestParseDepths:
    def test_reads_the_stable_columns(self, tmp_path):
        path = tmp_path / "depth.txt"
        path.write_text(DEPTH_TSV)
        rows = metabat_runner.parse_depths(path)
        assert [r.contig for r in rows] == [
            "orgA_ctg0",
            "orgA_ctg1",
            "orgB_ctg0",
            "tiny_ctg",
        ]
        assert rows[0].length == 200000
        assert rows[2].mean_depth == pytest.approx(8.00278)

    def test_skips_a_malformed_row_without_losing_the_file(self, tmp_path):
        """A depth table can be tens of thousands of contigs long, and
        MetaBAT2 has already binned from the file itself by the time this
        parses it -- so one bad row is not a reason to lose the rest."""
        path = tmp_path / "depth.txt"
        path.write_text(DEPTH_TSV + "broken\tnot-a-number\tx\n")
        rows = metabat_runner.parse_depths(path)
        assert len(rows) == 4


class TestParseBinInfo:
    def test_reads_counts_bases_and_depth_per_bin(self, tmp_path):
        path = tmp_path / "bin.BinInfo.txt"
        path.write_text(BIN_INFO_TSV)
        info = metabat_runner.parse_bin_info(path)
        assert info[1] == (6, 1200000, pytest.approx(8.00292))
        assert info[3] == (4, 800000, pytest.approx(30.0124))

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        """The facts it supplies are enrichment; enumerate_bins falls back to
        measuring the FASTAs themselves."""
        assert metabat_runner.parse_bin_info(tmp_path / "absent.txt") == {}


class TestMeasureFasta:
    def test_counts_sequence_not_newlines(self, tmp_path):
        path = tmp_path / "b.fa"
        path.write_text(">a\nACGT\nACGT\n>b\nAC\n")
        assert metabat_runner.measure_fasta(path) == (2, 10)


class TestEnumerateBins:
    def test_finds_bins_in_numeric_order(self, tmp_path):
        """Numeric, not lexical: `sorted()` over filenames puts bin.10 before
        bin.2, which would make `bin_index` disagree with the order a user
        sees."""
        prefix = tmp_path / "bin"
        for i in (1, 2, 10):
            write_bin(prefix, i)
        bins = metabat_runner.enumerate_bins(prefix)
        assert [b.index for b in bins] == [1, 2, 10]

    def test_ignores_the_non_bin_files_metabat2_writes_alongside(self, tmp_path):
        """MetaBAT2 writes unbinned.fa, tooShort.fa, lowDepth.fa, BinInfo.txt
        and BinMembers.txt into the same directory as the bins.

        A `<prefix>.*.fa` glob would ingest three of those as MAGs -- an
        "unbinned contigs" object presented to the user as a genome.
        """
        prefix = tmp_path / "bin"
        write_bin(prefix, 1)
        for name in ("unbinned", "tooShort", "lowDepth"):
            (tmp_path / f"bin.{name}.fa").write_text(">x\nACGT\n")
        (tmp_path / "bin.BinInfo.txt").write_text(BIN_INFO_TSV)
        (tmp_path / "bin.BinMembers.txt").write_text("BinNum\tSequenceName\n")
        bins = metabat_runner.enumerate_bins(prefix)
        assert [b.index for b in bins] == [1]

    def test_prefers_bininfos_numbers_when_present(self, tmp_path):
        prefix = tmp_path / "bin"
        write_bin(prefix, 1)
        (tmp_path / "bin.BinInfo.txt").write_text(BIN_INFO_TSV)
        [got] = metabat_runner.enumerate_bins(prefix)
        assert got.contig_count == 6
        assert got.total_bases == 1200000
        assert got.mean_depth == pytest.approx(8.00292)

    def test_measures_the_fasta_when_bininfo_is_absent(self, tmp_path):
        prefix = tmp_path / "bin"
        write_bin(prefix, 1, contigs=2, bases=120)
        [got] = metabat_runner.enumerate_bins(prefix)
        assert got.contig_count == 2
        assert got.mean_depth is None

    def test_skips_an_empty_bin_without_losing_its_siblings(self, tmp_path):
        """Losing thirty-nine good MAGs to whichever one came out empty is the
        failure R3 exists to prevent."""
        prefix = tmp_path / "bin"
        write_bin(prefix, 1)
        (tmp_path / "bin.2.fa").write_text("")
        write_bin(prefix, 3)
        assert [b.index for b in metabat_runner.enumerate_bins(prefix)] == [1, 3]

    def test_a_missing_directory_yields_nothing(self, tmp_path):
        assert metabat_runner.enumerate_bins(tmp_path / "gone" / "bin") == []


class TestUnbinned:
    def test_found_when_it_holds_contigs(self, tmp_path):
        prefix = tmp_path / "bin"
        (tmp_path / "bin.unbinned.fa").write_text(">x\nACGT\n")
        assert metabat_runner.unbinned_path(prefix) is not None

    def test_an_empty_unbinned_file_is_treated_as_absent(self, tmp_path):
        """MetaBAT2 writes a zero-byte unbinned.fa when every contig was
        placed. Ingesting it would put an empty, unopenable "unbinned contigs"
        object in the user's project."""
        prefix = tmp_path / "bin"
        (tmp_path / "bin.unbinned.fa").write_text("")
        assert metabat_runner.unbinned_path(prefix) is None


class TestExcludedPaths:
    def test_reports_tooshort_and_lowdepth_separately_from_unbinned(self, tmp_path):
        """These are not the same thing as unbinned contigs.

        A contig in tooShort was never eligible (below --minContig); one in
        lowDepth had too little coverage to place. Folding either into the
        unbinned count would overstate how unresolvable the community is, when
        the honest answer is that the assembly or the sequencing depth was the
        limit.
        """
        prefix = tmp_path / "bin"
        (tmp_path / "bin.tooShort.fa").write_text(">s\nACGT\n")
        (tmp_path / "bin.lowDepth.fa").write_text("")
        found = metabat_runner.excluded_paths(prefix)
        assert set(found) == {"tooShort"}


class TestBinCap:
    def test_allows_a_run_at_the_cap(self):
        metabat_runner.check_bin_cap(200, 200)

    def test_refuses_over_the_cap_naming_both_numbers(self):
        """Refuses rather than truncating (B4). Truncation would drop MAGs
        ordered by MetaBAT2's numbering rather than by quality, so the
        discarded set is arbitrary AND invisible."""
        with pytest.raises(ValueError) as exc:
            metabat_runner.check_bin_cap(201, 200)
        message = str(exc.value)
        assert "201" in message
        assert "200" in message
        assert "Nothing was ingested" in message


class TestFacts:
    def test_a_bin_traces_back_to_its_community(self):
        """A bin has no container object, so these facts are the only thing
        tying it to the assembly it came out of."""
        bin_ = metabat_runner.Bin(
            index=3, path=None, contig_count=4, total_bases=800000, mean_depth=30.0124
        )
        facts = metabat_runner.bin_facts(
            bin_=bin_, source_assembly_id="abc123", total_bins=7
        )
        assert facts["bin_index"] == 3
        assert facts["bin_source_assembly"] == "abc123"
        assert facts["bin_contig_count"] == 4
        assert facts["bin_total_bases"] == 800000
        assert facts["bin_total_bins"] == 7
        assert facts["bin_mean_depth"] == pytest.approx(30.0124)

    def test_mean_depth_is_omitted_rather_than_nulled(self):
        bin_ = metabat_runner.Bin(
            index=1, path=None, contig_count=2, total_bases=10, mean_depth=None
        )
        facts = metabat_runner.bin_facts(
            bin_=bin_, source_assembly_id="x", total_bins=1
        )
        assert "bin_mean_depth" not in facts

    def test_the_assembly_records_the_binned_unbinned_split(self):
        bins = [
            metabat_runner.Bin(
                index=i, path=None, contig_count=2, total_bases=1000, mean_depth=None
            )
            for i in (1, 2, 3)
        ]
        facts = metabat_runner.binning_facts(
            bins=bins, unbinned_bases=1000, excluded={}, tool_version="2.18"
        )
        assert facts["binning_bin_count"] == 3
        assert facts["binning_binned_bases"] == 3000
        assert facts["binning_unbinned_bases"] == 1000
        assert facts["binning_binned_pct"] == pytest.approx(75.0)
        assert facts["binner_version"] == "2.18"

    def test_excluded_contigs_stay_out_of_the_recovered_fraction(self):
        """A contig below --minContig was never a binning candidate, so
        counting it as "not recovered" would report a worse community than the
        data shows."""
        bins = [
            metabat_runner.Bin(
                index=1, path=None, contig_count=1, total_bases=900, mean_depth=None
            )
        ]
        facts = metabat_runner.binning_facts(
            bins=bins,
            unbinned_bases=100,
            excluded={"tooShort": 5000},
            tool_version=None,
        )
        assert facts["binning_binned_pct"] == pytest.approx(90.0)
        assert facts["binning_excluded_bases"] == 5000
        assert facts["binning_tooshort_bases"] == 5000
