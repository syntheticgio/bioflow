"""kraken_runner: pure functions, no binaries -- the quast_runner split."""

from pathlib import Path

from app.pipelines import kraken_runner


def test_kraken2_single_end_command():
    cmd = kraken_runner.build_kraken2_command(
        kraken2_path="kraken2",
        db_dir=Path("/data/kraken_dbs/standard-8"),
        reads=Path("/work/reads.fastq"),
        mate=None,
        report=Path("/work/report.txt"),
        output=Path("/dev/null"),
        threads=4,
        gzipped=False,
    )
    assert cmd[0] == "kraken2"
    assert "--db" in cmd and cmd[cmd.index("--db") + 1] == "/data/kraken_dbs/standard-8"
    assert "--report" in cmd
    assert "--paired" not in cmd
    assert "--gzip-compressed" not in cmd
    assert cmd[-1] == "/work/reads.fastq"


def test_kraken2_paired_gzipped_command():
    cmd = kraken_runner.build_kraken2_command(
        kraken2_path="kraken2",
        db_dir=Path("/db"),
        reads=Path("/work/r1.fastq.gz"),
        mate=Path("/work/r2.fastq.gz"),
        report=Path("/work/report.txt"),
        output=Path("/dev/null"),
        threads=8,
        gzipped=True,
    )
    assert "--paired" in cmd
    assert "--gzip-compressed" in cmd
    assert cmd[-2:] == ["/work/r1.fastq.gz", "/work/r2.fastq.gz"]
    assert "--memory-mapping" not in cmd  # memory is budgeted, not mapped (spec K2-R2)


def test_bracken_command():
    cmd = kraken_runner.build_bracken_command(
        bracken_path="bracken",
        db_dir=Path("/db"),
        report=Path("/work/report.txt"),
        output=Path("/work/bracken.tsv"),
        read_len=150,
    )
    assert cmd[0] == "bracken"
    assert cmd[cmd.index("-d") + 1] == "/db"
    assert cmd[cmd.index("-i") + 1] == "/work/report.txt"
    assert cmd[cmd.index("-o") + 1] == "/work/bracken.tsv"
    assert cmd[cmd.index("-r") + 1] == "150"
    assert cmd[cmd.index("-l") + 1] == "S"


KRAKEN_REPORT = """\
 12.50\t1250\t1250\tU\t0\tunclassified
 87.50\t8750\t0\tR\t1\troot
 87.40\t8740\t12\tR1\t131567\t  cellular organisms
 87.00\t8700\t0\tD\t2\t    Bacteria
 86.20\t8620\t8000\tS\t562\t          Escherichia coli
  1.10\t110\t110\tS\t1280\t          Staphylococcus aureus
"""

BRACKEN_OUTPUT = """\
name\ttaxonomy_id\ttaxonomy_lvl\tkraken_assigned_reads\tadded_reads\tnew_est_reads\tfraction_total_reads
Escherichia coli\t562\tS\t8000\t620\t8620\t0.98514
Staphylococcus aureus\t1280\tS\t110\t20\t130\t0.01486
"""


def test_parse_kraken_report():
    rows = kraken_runner.parse_kraken_report(KRAKEN_REPORT)
    assert len(rows) == 6
    unclassified = rows[0]
    assert unclassified == {
        "pct": 12.5, "clade_reads": 1250, "direct_reads": 1250,
        "rank": "U", "taxid": 0, "name": "unclassified",
    }
    ecoli = next(r for r in rows if r["taxid"] == 562)
    assert ecoli["name"] == "Escherichia coli"  # indentation stripped
    assert ecoli["rank"] == "S"
    assert ecoli["pct"] == 86.2


def test_parse_kraken_report_garbage_is_empty():
    assert kraken_runner.parse_kraken_report("") == []
    assert kraken_runner.parse_kraken_report("not\ta\treport") == []
    # A malformed line is skipped, not fatal
    assert len(kraken_runner.parse_kraken_report(KRAKEN_REPORT + "bad line\n")) == 6


def test_parse_bracken_output():
    rows = kraken_runner.parse_bracken_output(BRACKEN_OUTPUT)
    assert rows == [
        {"name": "Escherichia coli", "taxid": 562, "fraction": 0.98514},
        {"name": "Staphylococcus aureus", "taxid": 1280, "fraction": 0.01486},
    ]


def test_parse_bracken_garbage_is_empty():
    assert kraken_runner.parse_bracken_output("") == []
    assert kraken_runner.parse_bracken_output("no\ttabs\there\n") == []


def _kr(pct, clade, direct, rank, taxid, name):
    return {"pct": pct, "clade_reads": clade, "direct_reads": direct,
            "rank": rank, "taxid": taxid, "name": name}


def test_top_taxa_prefers_bracken():
    kraken = [
        _kr(12.5, 1250, 1250, "U", 0, "unclassified"),
        _kr(86.2, 8620, 8000, "S", 562, "Escherichia coli"),
    ]
    bracken = [
        {"name": "Escherichia coli", "taxid": 562, "fraction": 0.985},
        {"name": "Staphylococcus aureus", "taxid": 1280, "fraction": 0.015},
    ]
    result = kraken_runner.top_taxa(kraken, bracken)
    assert result["bracken_used"] is True
    assert result["unclassified_pct"] == 12.5
    assert result["taxa"][0] == {
        "name": "Escherichia coli", "rank": "S", "taxid": 562, "pct": 98.5,
    }


def test_top_taxa_falls_back_to_kraken_species():
    kraken = [
        _kr(12.5, 1250, 1250, "U", 0, "unclassified"),
        _kr(86.2, 8620, 8000, "S", 562, "Escherichia coli"),
        _kr(0.5, 50, 50, "S", 1280, "Staphylococcus aureus"),
    ]
    result = kraken_runner.top_taxa(kraken, [])
    assert result["bracken_used"] is False
    assert [t["name"] for t in result["taxa"]] == [
        "Escherichia coli", "Staphylococcus aureus",
    ]


def test_top_taxa_keeps_top_ten_plus_one_percent():
    # 12 species at 0.5% each after two big ones: top 10 kept, plus all >=1%
    kraken = [_kr(30.0, 3000, 3000, "S", 100 + i, f"Species {i}") for i in range(2)]
    kraken += [_kr(0.5, 50, 50, "S", 200 + i, f"Minor {i}") for i in range(12)]
    result = kraken_runner.top_taxa(kraken, [])
    assert len(result["taxa"]) == 10


def test_mismatch_fires_when_genus_absent():
    kraken = [
        _kr(94.0, 9400, 9000, "S", 1280, "Staphylococcus aureus"),
        _kr(2.0, 200, 200, "S", 562, "Escherichia coli"),
    ]
    result = kraken_runner.organism_mismatch("Escherichia coli", kraken)
    assert result == {
        "claimed": "Escherichia coli",
        "dominant": [{"name": "Staphylococcus aureus", "pct": 94.0}],
    }


def test_mismatch_silent_when_genus_dominant():
    kraken = [_kr(94.0, 9400, 9000, "S", 562, "Escherichia coli")]
    assert kraken_runner.organism_mismatch("Escherichia coli K-12", kraken) is None


def test_mismatch_silent_without_metadata():
    kraken = [_kr(94.0, 9400, 9000, "S", 1280, "Staphylococcus aureus")]
    assert kraken_runner.organism_mismatch(None, kraken) is None
    assert kraken_runner.organism_mismatch("  ", kraken) is None
