"""The one pure function in transcript_qc_handlers.py worth testing directly.

The handler itself shells out to pysam and the queue and is deliberately
untested, matching run_bam_stats/align_handlers.py's pattern -- but _is_gzip
is a pure function over bytes, and it exists specifically because a real
gzip-compressed GTF silently produced zero parsed transcripts when opened as
plain text. Mirrors TestIsGzip in test_align_handlers_star_gzip.py, the same
check against the same class of bug in a sibling handler.
"""

import gzip

from app.queue.transcript_qc_handlers import _is_gzip


class TestIsGzip:
    def test_detects_gzip_magic_bytes(self, tmp_path):
        path = tmp_path / "genes.gtf.gz"
        with gzip.open(path, "wb") as f:
            f.write(b'chr1\tx\texon\t1\t100\t.\t+\t.\tgene_id "G1";\n')
        assert _is_gzip(path) is True

    def test_plain_text_is_not_gzip(self, tmp_path):
        path = tmp_path / "genes.gtf"
        path.write_bytes(b'chr1\tx\texon\t1\t100\t.\t+\t.\tgene_id "G1";\n')
        assert _is_gzip(path) is False

    def test_mismatched_extension_is_sniffed_by_content(self, tmp_path):
        # The stored name is a user-facing label, not a format guarantee --
        # this app's own upload path compresses files regardless of the
        # extension the user gave them.
        path = tmp_path / "genes.gtf"
        with gzip.open(path, "wb") as f:
            f.write(b'chr1\tx\texon\t1\t100\t.\t+\t.\tgene_id "G1";\n')
        assert _is_gzip(path) is True
