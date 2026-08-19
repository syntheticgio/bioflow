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
