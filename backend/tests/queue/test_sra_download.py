"""SRA download: staging layout, disk pre-flight, and failure classification.

The pure decisions, without the network or a worker. What fasterq-dump does
with a real accession is covered by the live smoke test; what matters here is
that its output is described correctly and its failures are sorted into
retryable and permanent, since that decides whether a user waits through three
attempts for an error that will never change.
"""

from pathlib import Path

import pytest

from app.errors import PermanentError, RetryableError
from app.queue import sra_handlers


class TestDescribeStaging:
    def test_a_paired_run_labels_both_mates(self):
        """fasterq-dump --split-files says which file is which, so the applier
        can set a real mate link rather than inferring one from the name."""
        staged = sra_handlers._describe(
            [Path("/tmp/SRR1_1.fastq"), Path("/tmp/SRR1_2.fastq")]
        )
        assert [s["mate"] for s in staged] == ["R1", "R2"]
        assert [s["name"] for s in staged] == ["SRR1_1.fastq", "SRR1_2.fastq"]

    def test_a_single_end_run_has_no_mate(self):
        """None rather than "R1": there is no pair, and claiming one would
        make the applier look for a partner that does not exist."""
        staged = sra_handlers._describe([Path("/tmp/SRR1.fastq")])
        assert staged[0]["mate"] is None

    def test_an_orphan_beside_a_pair_is_marked_unpaired(self):
        """fasterq-dump emits a bare <acc>.fastq for reads whose mate was
        filtered upstream. Real data, but not part of the pair -- linking it as
        one would corrupt the pairing."""
        staged = sra_handlers._describe(
            [
                Path("/tmp/SRR1.fastq"),
                Path("/tmp/SRR1_1.fastq"),
                Path("/tmp/SRR1_2.fastq"),
            ]
        )
        by_name = {s["name"]: s["mate"] for s in staged}
        assert by_name["SRR1.fastq"] == "unpaired"
        assert by_name["SRR1_1.fastq"] == "R1"
        assert by_name["SRR1_2.fastq"] == "R2"

    def test_the_absolute_path_rides_along(self):
        """The applier consumes these paths directly; a bare name would leave
        it guessing at the staging directory."""
        staged = sra_handlers._describe([Path("/data/tmp/sra_download/j1/SRR1.fastq")])
        assert staged[0]["path"] == "/data/tmp/sra_download/j1/SRR1.fastq"


class TestDiskPreflight:
    def test_passes_when_there_is_room(self, tmp_path):
        sra_handlers._check_disk_space(tmp_path, 1000, "SRR1")

    def test_refuses_before_spending_an_hour_on_a_transfer_that_cannot_land(
        self, tmp_path, monkeypatch
    ):
        """Checked up front rather than after: discovering the disk is full
        once the files exist means the space is already spent."""
        import shutil

        monkeypatch.setattr(
            shutil, "disk_usage", lambda p: type("U", (), {"free": 1_000_000})()
        )
        with pytest.raises(PermanentError) as exc:
            sra_handlers._check_disk_space(tmp_path, 10_000_000, "SRR1")
        assert "SRR1" in str(exc.value)

    def test_accounts_for_extraction_not_just_the_archive(self, tmp_path, monkeypatch):
        """The estimate is the compressed archive; fasterq-dump writes plain
        FASTQ several times that size while prefetch still holds the archive.
        A check against the archive size alone would pass and then fill the
        disk."""
        import shutil

        # Comfortably larger than the archive, smaller than the extraction.
        monkeypatch.setattr(
            shutil, "disk_usage", lambda p: type("U", (), {"free": 2_000_000})()
        )
        with pytest.raises(PermanentError):
            sra_handlers._check_disk_space(tmp_path, 1_000_000, "SRR1")

    def test_a_missing_estimate_does_not_block_the_download(self, tmp_path, monkeypatch):
        """NCBI does not always report a size. An absent figure is not evidence
        of a problem, and refusing on it would block a legitimate download."""
        import shutil

        monkeypatch.setattr(
            shutil, "disk_usage", lambda p: type("U", (), {"free": 1})()
        )
        sra_handlers._check_disk_space(tmp_path, None, "SRR1")
        sra_handlers._check_disk_space(tmp_path, 0, "SRR1")


class TestFailureClassification:
    """Retryable vs permanent decides whether a user waits out three attempts."""

    def _fail(self, tmp_path, message: str, code: int = 3):
        log = tmp_path / "job.log"
        log.write_text(message)
        return sra_handlers._download_failure(code, log, "SRR1")

    def test_a_retracted_accession_is_permanent(self, tmp_path):
        """It will fail identically forever; three attempts only delay the
        error the user needs to see."""
        err = self._fail(tmp_path, "err: accession SRR1 not found")
        assert isinstance(err, PermanentError)

    def test_an_invalid_accession_is_permanent(self, tmp_path):
        assert isinstance(self._fail(tmp_path, "invalid accession"), PermanentError)

    def test_a_full_disk_is_permanent(self, tmp_path):
        """Retrying into a full disk fails the same way and wastes the transfer."""
        err = self._fail(tmp_path, "write failed: disk full")
        assert isinstance(err, PermanentError)

    @pytest.mark.parametrize(
        "message",
        [
            "connection reset by peer",
            "operation timed out",
            "network unreachable",
            "server returned 503",
            "429 too many requests",
        ],
    )
    def test_transient_network_trouble_is_retryable(self, tmp_path, message):
        """SRA rate-limits and goes intermittently unavailable; these are the
        cases the third attempt genuinely fixes."""
        assert isinstance(self._fail(tmp_path, message), RetryableError)

    def test_an_oom_kill_is_retryable(self, tmp_path):
        err = self._fail(tmp_path, "", code=137)
        assert isinstance(err, RetryableError)
        assert "memory" in str(err)

    def test_an_unrecognized_failure_is_retryable(self, tmp_path):
        """Opposite of the pipeline handlers' default, and deliberate: a fastp
        failure is usually the input, a download failure usually the network.
        A genuinely permanent one still stops after three attempts."""
        assert isinstance(self._fail(tmp_path, "something odd happened"), RetryableError)

    def test_the_message_carries_the_log_tail(self, tmp_path):
        """"fasterq-dump exited 3" alone tells the user nothing actionable."""
        err = self._fail(tmp_path, "vdb error: cannot open cache")
        assert "cannot open cache" in str(err)


class TestProgressParsing:
    @pytest.mark.parametrize(
        "line,phase,pct",
        [
            ("lookup :|------------------              48.00%", "lookup", 48.0),
            ("join   :|--------------------------------100.00%", "join", 100.0),
            ("concat :|-----                             5.50%", "concat", 5.5),
        ],
    )
    def test_reads_the_toolkit_progress_bar(self, line, phase, pct):
        match = sra_handlers._PROGRESS_RE.match(line)
        assert match is not None
        assert match.group(1) == phase
        assert float(match.group(2)) == pct

    @pytest.mark.parametrize(
        "line",
        ["", "spots read      : 1,234,567", "2026-07-28T01:00:00 fasterq-dump.3.2.1"],
    )
    def test_ignores_everything_else(self, line):
        """Other output lines must not be mistaken for progress."""
        assert sra_handlers._PROGRESS_RE.match(line) is None
