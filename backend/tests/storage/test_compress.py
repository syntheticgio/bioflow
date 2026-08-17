"""Ingest-time compression: the allowlist and the two-hash bgzip/stdlib seam."""

import gzip
import hashlib
import subprocess

import pytest
from app.errors import JobCancelled
from app.models import Compression, FormatKind
from app.pipelines import tools
from app.storage import compress

FASTQ = (
    b"@SEQ_ID_%d\nACGTACGTACGTACGTACGTACGTACGTACGT\n+\nIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIII\n" * 20000
)


@pytest.fixture(autouse=True)
def clear_tool_cache():
    """bgzip's probe result is cached for the process lifetime, like every
    other tool -- tests that patch its availability must not leak."""
    tools.reset_cache()
    yield
    tools.reset_cache()


class TestShouldCompress:
    @pytest.mark.parametrize(
        ("kind", "compression", "expected"),
        [
            (FormatKind.FASTQ, Compression.NONE, True),
            (FormatKind.FASTA, Compression.NONE, True),
            (FormatKind.VCF, Compression.NONE, True),
            (FormatKind.SAM, Compression.NONE, True),
            (FormatKind.GFF, Compression.NONE, True),
            (FormatKind.GTF, Compression.NONE, True),
            (FormatKind.BED, Compression.NONE, True),
            (FormatKind.GFA, Compression.NONE, True),
            (FormatKind.GENBANK, Compression.NONE, True),
            # Never re-wrap something that already arrived compressed.
            (FormatKind.FASTQ, Compression.GZIP, False),
            (FormatKind.FASTQ, Compression.BGZF, False),
            # Already block-compressed, or read by an access pattern
            # compression would break.
            (FormatKind.BAM, Compression.BGZF, False),
            (FormatKind.CRAM, Compression.NONE, False),
            (FormatKind.BCF, Compression.BGZF, False),
            (FormatKind.FAI, Compression.NONE, False),
            (FormatKind.TEXT, Compression.NONE, False),
            # Aligner indexes detect as UNKNOWN -- excluded by construction,
            # not by an extension list.
            (FormatKind.UNKNOWN, Compression.NONE, False),
        ],
    )
    def test_allowlist(self, kind, compression, expected):
        assert compress.should_compress(kind, compression) is expected


class TestCompressAndHashWithBgzip:
    """Runs against the real `bgzip` binary shipped in the image."""

    def _require_bgzip(self):
        if not tools.bgzip().available:
            pytest.skip("bgzip not installed in this environment")

    def test_content_hash_matches_plaintext(self, tmp_path):
        self._require_bgzip()
        src = tmp_path / "reads.fastq"
        src.write_bytes(FASTQ)

        result = compress.compress_and_hash(src, dest_dir=tmp_path)

        assert result.content_sha256 == hashlib.sha256(FASTQ).hexdigest()
        assert result.seekable is True

    def test_compressed_hash_matches_the_written_file(self, tmp_path):
        self._require_bgzip()
        src = tmp_path / "reads.fastq"
        src.write_bytes(FASTQ)

        result = compress.compress_and_hash(src, dest_dir=tmp_path)

        on_disk = hashlib.sha256(result.path.read_bytes()).hexdigest()
        assert result.compressed_sha256 == on_disk
        assert result.compressed_size == result.path.stat().st_size

    def test_output_is_smaller_and_round_trips(self, tmp_path):
        self._require_bgzip()
        src = tmp_path / "reads.fastq"
        src.write_bytes(FASTQ)

        result = compress.compress_and_hash(src, dest_dir=tmp_path)

        assert result.compressed_size < len(FASTQ)
        roundtrip = subprocess.run(
            ["zcat", str(result.path)], capture_output=True, check=True
        ).stdout
        assert roundtrip == FASTQ

    def test_output_is_valid_bgzf_not_just_gzip(self, tmp_path):
        """BGZF is what makes samtools faidx / tabix seeking possible -- the
        whole reason bgzip was chosen over pigz. A plain-gzip fallback would
        pass every other assertion here and still silently lose that."""
        self._require_bgzip()
        src = tmp_path / "reads.fastq"
        src.write_bytes(FASTQ)

        result = compress.compress_and_hash(src, dest_dir=tmp_path)

        check = subprocess.run(
            ["bgzip", "-t", str(result.path)], capture_output=True, check=False
        )
        assert check.returncode == 0, check.stderr.decode()

    def test_source_is_untouched(self, tmp_path):
        self._require_bgzip()
        src = tmp_path / "reads.fastq"
        src.write_bytes(FASTQ)

        compress.compress_and_hash(src, dest_dir=tmp_path)

        assert src.exists()
        assert src.read_bytes() == FASTQ

    def test_cancellation_is_honoured(self, tmp_path, monkeypatch):
        self._require_bgzip()
        import threading

        src = tmp_path / "reads.fastq"
        src.write_bytes(FASTQ)
        monkeypatch.setattr(compress, "CANCEL_CHECK_BYTES", 1024)

        event = threading.Event()
        event.set()
        with pytest.raises(JobCancelled):
            compress.compress_and_hash(src, dest_dir=tmp_path, cancel_event=event)


class TestCompressAndHashFallsBackToStdlibGzip:
    """Per CLAUDE.md: the image ships bgzip as installed, so a test asserting
    the seam is *available* passes whether or not the fallback path works.
    These patch the probe off and assert the fallback actually engages."""

    def _force_bgzip_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            tools, "bgzip", lambda: tools.Tool(name="bgzip", path=None, version=None, error="x")
        )

    def test_falls_back_when_bgzip_unavailable(self, tmp_path, monkeypatch):
        self._force_bgzip_unavailable(monkeypatch)
        src = tmp_path / "reads.fastq"
        src.write_bytes(FASTQ)

        result = compress.compress_and_hash(src, dest_dir=tmp_path)

        assert result.seekable is False
        assert result.content_sha256 == hashlib.sha256(FASTQ).hexdigest()

    def test_fallback_output_is_readable_gzip(self, tmp_path, monkeypatch):
        self._force_bgzip_unavailable(monkeypatch)
        src = tmp_path / "reads.fastq"
        src.write_bytes(FASTQ)

        result = compress.compress_and_hash(src, dest_dir=tmp_path)

        with gzip.open(result.path, "rb") as f:
            assert f.read() == FASTQ

    def test_fallback_compressed_hash_matches_the_written_file(self, tmp_path, monkeypatch):
        self._force_bgzip_unavailable(monkeypatch)
        src = tmp_path / "reads.fastq"
        src.write_bytes(FASTQ)

        result = compress.compress_and_hash(src, dest_dir=tmp_path)

        on_disk = hashlib.sha256(result.path.read_bytes()).hexdigest()
        assert result.compressed_sha256 == on_disk

    def test_fallback_is_deterministic_across_runs(self, tmp_path, monkeypatch):
        """mtime=0 keeps the compressed digest stable for identical input --
        otherwise dedup would depend on when a file happened to be ingested,
        since a gzip header normally embeds the current time."""
        self._force_bgzip_unavailable(monkeypatch)
        src = tmp_path / "reads.fastq"
        src.write_bytes(FASTQ)

        first = compress.compress_and_hash(src, dest_dir=tmp_path)
        second = compress.compress_and_hash(src, dest_dir=tmp_path)

        assert first.compressed_sha256 == second.compressed_sha256

    def test_two_compressors_agree_on_content_hash_for_identical_input(self, tmp_path, monkeypatch):
        """The reason dedup keys on content_sha256 rather than the CAS digest:
        bgzip and the stdlib fallback write different compressed bytes for
        identical plaintext, so only the plaintext hash lets two ingests of
        the same file converge on one blob regardless of which one wrote it."""
        src = tmp_path / "reads.fastq"
        src.write_bytes(FASTQ)

        via_bgzip = compress.compress_and_hash(src, dest_dir=tmp_path)

        self._force_bgzip_unavailable(monkeypatch)
        via_stdlib = compress.compress_and_hash(src, dest_dir=tmp_path)

        assert via_bgzip.content_sha256 == via_stdlib.content_sha256
        assert via_bgzip.compressed_sha256 != via_stdlib.compressed_sha256
