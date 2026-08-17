"""SRA download: staging layout, disk pre-flight, and failure classification.

The pure decisions, without the network or a worker. What fasterq-dump does
with a real accession is covered by the live smoke test; what matters here is
that its output is described correctly and its failures are sorted into
retryable and permanent, since that decides whether a user waits through three
attempts for an error that will never change.
"""

import sys
import textwrap
import threading
from pathlib import Path

import pytest
from app.errors import JobCancelled, PermanentError, RetryableError
from app.pipelines.tools import Tool
from app.queue import sra_handlers
from app.queue.registry import JobContext


def _ctx(**kwargs) -> JobContext:
    return JobContext(job_id="sra-j1", payload={}, epoch=0, attempts=0, owner="local", **kwargs)


FASTQ = b"@read\nACGTACGTACGTACGTACGTACGTACGTACGT\n+\nIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIII\n" * 20000


class TestDescribeStaging:
    def test_a_paired_run_labels_both_mates(self):
        """fasterq-dump --split-files says which file is which, so the applier
        can set a real mate link rather than inferring one from the name."""
        staged = sra_handlers._describe(
            {Path("/tmp/SRR1_1.fastq"): None, Path("/tmp/SRR1_2.fastq"): None}
        )
        assert [s["mate"] for s in staged] == ["R1", "R2"]
        assert [s["name"] for s in staged] == ["SRR1_1.fastq", "SRR1_2.fastq"]

    def test_a_single_end_run_has_no_mate(self):
        """None rather than "R1": there is no pair, and claiming one would
        make the applier look for a partner that does not exist."""
        staged = sra_handlers._describe({Path("/tmp/SRR1.fastq"): None})
        assert staged[0]["mate"] is None

    def test_an_orphan_beside_a_pair_is_marked_unpaired(self):
        """fasterq-dump emits a bare <acc>.fastq for reads whose mate was
        filtered upstream. Real data, but not part of the pair -- linking it as
        one would corrupt the pairing."""
        staged = sra_handlers._describe(
            {
                Path("/tmp/SRR1.fastq"): None,
                Path("/tmp/SRR1_1.fastq"): None,
                Path("/tmp/SRR1_2.fastq"): None,
            }
        )
        by_name = {s["name"]: s["mate"] for s in staged}
        assert by_name["SRR1.fastq"] == "unpaired"
        assert by_name["SRR1_1.fastq"] == "R1"
        assert by_name["SRR1_2.fastq"] == "R2"

    def test_the_absolute_path_rides_along(self):
        """The applier consumes these paths directly; a bare name would leave
        it guessing at the staging directory."""
        staged = sra_handlers._describe({Path("/data/tmp/sra_download/j1/SRR1.fastq"): None})
        assert staged[0]["path"] == "/data/tmp/sra_download/j1/SRR1.fastq"

    def test_a_compressed_names_pair_correctly_via_the_stripped_suffix(self):
        """A run _compress_staged already bgzip'd carries a .gz suffix that
        must not defeat the _1/_2 pairing match."""
        staged = sra_handlers._describe(
            {Path("/tmp/SRR1_1.fastq.gz"): "aaa", Path("/tmp/SRR1_2.fastq.gz"): "bbb"}
        )
        assert [s["mate"] for s in staged] == ["R1", "R2"]
        assert [s["name"] for s in staged] == ["SRR1_1.fastq.gz", "SRR1_2.fastq.gz"]

    def test_content_sha256_rides_along_from_compression(self):
        """What ingest_local_file's dedup-by-content lookup needs, since it
        never sees the plaintext this hash was computed from."""
        staged = sra_handlers._describe(
            {Path("/tmp/SRR1.fastq.gz"): "deadbeef", Path("/tmp/SRR2.fastq"): None}
        )
        by_name = {s["name"]: s["content_sha256"] for s in staged}
        assert by_name["SRR1.fastq.gz"] == "deadbeef"
        assert by_name["SRR2.fastq"] is None


class TestCompressStaged:
    """`_compress_staged` runs inside the SUBPROCESS handler specifically so
    it has a JobContext to report progress and check cancellation through --
    the async applier that calls ingest_local_file later never gets one. See
    docs/superpowers/specs/2026-08-05-object-compression-design.md."""

    def test_compresses_and_renames_with_gz(self, tmp_path):
        path = tmp_path / "SRR1.fastq"
        path.write_bytes(FASTQ)

        result = sra_handlers._compress_staged(_ctx(), [path], "SRR1")

        [(gz_path, content_sha256)] = result.items()
        assert gz_path.name == "SRR1.fastq.gz"
        assert gz_path.exists()
        assert not path.exists()
        assert gz_path.stat().st_size < len(FASTQ)
        assert content_sha256 is not None

    def test_compressed_content_hash_matches_the_plaintext(self, tmp_path):
        import hashlib

        path = tmp_path / "SRR1.fastq"
        path.write_bytes(FASTQ)
        expected = hashlib.sha256(FASTQ).hexdigest()

        result = sra_handlers._compress_staged(_ctx(), [path], "SRR1")

        [content_sha256] = result.values()
        assert content_sha256 == expected

    def test_compressed_output_round_trips(self, tmp_path):
        import gzip

        path = tmp_path / "SRR1.fastq"
        path.write_bytes(FASTQ)

        result = sra_handlers._compress_staged(_ctx(), [path], "SRR1")

        [gz_path] = result
        with gzip.open(gz_path, "rb") as f:
            assert f.read() == FASTQ

    def test_reports_a_compressing_phase(self, tmp_path):
        path = tmp_path / "SRR1.fastq"
        path.write_bytes(FASTQ)
        calls: list[dict] = []

        sra_handlers._compress_staged(_ctx(_progress_cb=calls.append), [path], "SRR1")

        phases = [c.get("phase") for c in calls]
        assert "compressing" in phases

    def test_honours_cancellation(self, tmp_path):
        path = tmp_path / "SRR1.fastq"
        path.write_bytes(FASTQ)
        event = threading.Event()
        event.set()

        with pytest.raises(JobCancelled):
            sra_handlers._compress_staged(_ctx(cancel_event=event), [path], "SRR1")

    def test_leaves_a_format_outside_the_allowlist_untouched(self, tmp_path):
        """fasterq-dump only ever writes plain FASTQ, but the should_compress
        check still runs rather than being assumed -- this is what proves it
        actually gates rather than compressing unconditionally."""
        path = tmp_path / "notes.txt"
        path.write_bytes(b"not a bioinformatics format\n" * 5)

        result = sra_handlers._compress_staged(_ctx(), [path], "SRR1")

        [(kept_path, content_sha256)] = result.items()
        assert kept_path == path
        assert kept_path.exists()
        assert content_sha256 is None


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


class TestPrefetchProgress:
    """prefetch's own bar is `\\r`-redrawn like fasterq-dump's, but its wording
    is not stable enough across toolkit versions to parse a percentage out of
    -- so this only checks that the phase keeps reporting activity via
    `message` as output arrives, rather than sitting frozen until it exits.

    `prefetch_tool.path` is pointed at a fake executable script (a shebang'd
    Python file) so `_prefetch`'s own argv-building is exercised unchanged --
    the fake just ignores the `--output-directory`/`--max-size`/accession
    arguments it is called with and prints a `\\r`-redrawn bar.
    """

    def _fake_prefetch(self, tmp_path: Path) -> str:
        script = tmp_path / "fake_prefetch.py"
        script.write_text(
            "#!"
            + sys.executable
            + "\n"
            + textwrap.dedent(
                """
                import sys, time
                sys.stdout.write("\\rSRR1: downloaded 10%")
                sys.stdout.flush()
                time.sleep(0.2)
                sys.stdout.write("\\rSRR1: downloaded 100%")
                sys.stdout.flush()
                sys.stdout.write("\\n")
                """
            )
        )
        script.chmod(0o755)
        return str(script)

    def test_progress_arrives_before_the_process_exits(self, tmp_path, monkeypatch):
        fake_path = self._fake_prefetch(tmp_path)
        monkeypatch.setattr(
            sra_handlers.tools,
            "prefetch",
            lambda: Tool(name="prefetch", path=fake_path, version="3.0.0"),
        )

        received: list[dict] = []
        ctx = _ctx()
        ctx._progress_cb = received.append

        sra_handlers._prefetch(ctx, "SRR1", tmp_path, tmp_path / "job.log", {})

        messages = [u.get("message") for u in received if u.get("message")]
        assert any("10%" in m for m in messages)
        assert any("100%" in m for m in messages)
