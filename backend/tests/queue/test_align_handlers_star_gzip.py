"""Two index builders need a plain-text FASTA/GTF: STAR's `genomeGenerate`
and `hisat2-build`. See `align_handlers._ensure_uncompressed` for the failures
this guards against -- an NCBI-downloaded reference (always gzip) reached STAR
unusable and it failed with "is not fasta", and reached hisat2-build which
exited 1 partway through, deleting its own partial .ht2 files (#560).

Which builders can cope is declared per aligner by
`AlignerSpec.builder_accepts_gzip` and asserted in
`tests/pipelines/test_aligner_registry.py`; this file covers the mechanics of
the decompression itself.
"""

import gzip

from app.queue.align_handlers import _ensure_uncompressed, _is_gzip


class TestIsGzip:
    def test_detects_gzip_magic_bytes(self, tmp_path):
        path = tmp_path / "genome.fna.gz"
        with gzip.open(path, "wb") as f:
            f.write(b">chr1\nACGT\n")
        assert _is_gzip(path) is True

    def test_plain_text_is_not_gzip(self, tmp_path):
        path = tmp_path / "genome.fna"
        path.write_bytes(b">chr1\nACGT\n")
        assert _is_gzip(path) is False

    def test_mismatched_extension_is_sniffed_by_content(self, tmp_path):
        # The stored name is a user-facing label, not a format guarantee --
        # a registered or renamed file can carry the wrong extension.
        path = tmp_path / "genome.fna"
        with gzip.open(path, "wb") as f:
            f.write(b">chr1\nACGT\n")
        assert _is_gzip(path) is True


class TestEnsureUncompressed:
    def test_decompresses_gzip_into_dest_dir(self, tmp_path):
        src_dir = tmp_path / "ref"
        src_dir.mkdir()
        gz = src_dir / "genome.fna.gz"
        with gzip.open(gz, "wb") as f:
            f.write(b">chr1\nACGTACGT\n")

        dest_dir = tmp_path / "build-input"
        out = _ensure_uncompressed(gz, dest_dir)

        assert out.parent == dest_dir
        assert out.name == "genome.fna"
        assert out.read_bytes() == b">chr1\nACGTACGT\n"

    def test_plain_file_passes_through_unchanged(self, tmp_path):
        src_dir = tmp_path / "ref"
        src_dir.mkdir()
        plain = src_dir / "genome.fna"
        plain.write_bytes(b">chr1\nACGT\n")

        dest_dir = tmp_path / "build-input"
        out = _ensure_uncompressed(plain, dest_dir)

        assert out == plain
        assert not dest_dir.exists()
