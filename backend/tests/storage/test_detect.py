"""Format detection tests, driven by byte fixtures rather than real files."""

import gzip
import struct
from pathlib import Path

import pytest

from app.models import Compression, FormatConfidence, FormatKind
from app.storage.detect import (
    EXTENSION_MAP,
    detect,
    detect_from_extension,
    detect_from_magic,
)

FASTQ = b"""@SEQ_ID_1
GATTTGGGGTTCAAAGCAGTATCGATCAAATAGTAAATCCATTTGTTCAA
+
!''*((((***+))%%%++)(%%%%).1***-+*''))**55CCF>>>>>
@SEQ_ID_2
AATTGGCCAATTGGCCAATTGGCCAATTGGCCAATTGGCCAATTGGCCAA
+
IIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIII
"""

FASTA = b""">chr1 test contig
ACGTACGTACGTACGTACGTACGTACGTACGT
ACGTACGTACGTACGTACGTACGTACGTACGT
>chr2
TTTTGGGGCCCCAAAA
"""

VCF = b"""##fileformat=VCFv4.3
##contig=<ID=chr1,length=248956422>
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO
chr1\t100\t.\tA\tG\t50\tPASS\t.
"""

SAM = b"""@HD\tVN:1.6\tSO:coordinate
@SQ\tSN:chr1\tLN:248956422
read1\t0\tchr1\t100\t60\t50M\t*\t0\t0\tACGT\tIIII
"""

BED = b"""chr1\t100\t200\tfeature1\t0\t+
chr1\t300\t400\tfeature2\t0\t-
chr2\t500\t600\tfeature3\t0\t+
"""

GFF = b"""##gff-version 3
chr1\tsrc\tgene\t100\t200\t.\t+\t.\tID=gene1
"""

# samtools FASTA index: name, length, offset, linebases, linewidth. Column 1
# (offset) is not ordered relative to column 2 (length) the way BED's
# chromStart < chromEnd is, which is exactly what used to make this
# misdetect as BED (#48).
FAI = b"""chr1\t230218\t6\t60\t61
chr2\t1000\t230300\t60\t61
"""


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def make_bgzf(payload: bytes) -> bytes:
    """A minimal BGZF member: gzip with the 'BC' extra subfield.

    BGZF must be distinguishable from plain gzip because it is block-compressed
    and therefore seekable -- that property is what makes indexing possible.
    """
    import zlib

    compressor = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
    deflated = compressor.compress(payload) + compressor.flush()
    xlen = 6
    bsize = len(deflated) + 25 + 1
    header = (
        b"\x1f\x8b\x08\x04"
        + b"\x00\x00\x00\x00"
        + b"\x00\xff"
        + struct.pack("<H", xlen)
        + b"BC"
        + struct.pack("<H", 2)
        + struct.pack("<H", bsize)
    )
    return (
        header
        + deflated
        + struct.pack("<I", zlib.crc32(payload))
        + struct.pack("<I", len(payload))
    )


class TestExtension:
    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("sample.fastq", FormatKind.FASTQ),
            ("sample.fq.gz", FormatKind.FASTQ),
            ("ref.fasta", FormatKind.FASTA),
            ("ref.fa.gz", FormatKind.FASTA),
            ("aln.bam", FormatKind.BAM),
            ("calls.vcf.gz", FormatKind.VCF),
            ("regions.bed", FormatKind.BED),
            ("genes.gff3", FormatKind.GFF),
            ("ref.fna.fai", FormatKind.FAI),
            ("mystery", None),
            ("archive.gz", None),
        ],
    )
    def test_extension(self, filename, expected):
        assert detect_from_extension(filename) == expected

    def test_every_format_kind_has_an_extension(self):
        """A new FormatKind must say what it looks like on disk.

        EXTENSION_MAP's *keys* are an open vocabulary -- `fq` and `fastq` both
        mean FASTQ, and no enum can generate that list. Its values are not: a
        kind absent from here is one only magic-byte detection can ever
        produce, which is a real claim rather than an oversight, and this is
        what makes someone state it deliberately.

        UNKNOWN is excluded because it is the absence of an answer rather than
        a format. There is no other exception on purpose -- an escape hatch
        built before it has a member tends to become a dumping ground.
        """
        assert set(EXTENSION_MAP.values()) == set(FormatKind) - {FormatKind.UNKNOWN}


class TestMagic:
    def test_plain_fastq(self, tmp_path):
        r = detect(write(tmp_path, "x.fastq", FASTQ))
        assert r.kind is FormatKind.FASTQ
        assert r.compression is Compression.NONE
        assert r.confidence is FormatConfidence.MAGIC

    def test_fasta(self, tmp_path):
        assert detect(write(tmp_path, "x.fa", FASTA)).kind is FormatKind.FASTA

    def test_vcf(self, tmp_path):
        assert detect(write(tmp_path, "x.vcf", VCF)).kind is FormatKind.VCF

    def test_sam_not_confused_with_fastq(self, tmp_path):
        """Both start with '@'. FASTQ is distinguished by the '+' on line 3."""
        assert detect(write(tmp_path, "x.sam", SAM)).kind is FormatKind.SAM

    def test_gff(self, tmp_path):
        assert detect(write(tmp_path, "x.gff", GFF)).kind is FormatKind.GFF

    def test_bed(self, tmp_path):
        assert detect(write(tmp_path, "x.bed", BED)).kind is FormatKind.BED

    def test_fai_not_misdetected_as_bed(self, tmp_path):
        """Regression for #48: a .fai's (length, offset) columns are both
        int but not ordered like BED's (chromStart, chromEnd), so the
        tabular sniffer must not call it BED. The .fai extension carries
        the answer instead."""
        r = detect(write(tmp_path, "ref.fna.fai", FAI))
        assert r.kind is FormatKind.FAI
        assert r.magic_says is None

    def test_gzipped_fastq(self, tmp_path):
        r = detect(write(tmp_path, "x.fastq.gz", gzip.compress(FASTQ)))
        assert r.kind is FormatKind.FASTQ
        assert r.compression is Compression.GZIP

    def test_bgzf_detected_distinctly_from_gzip(self, tmp_path):
        r = detect(write(tmp_path, "x.vcf.gz", make_bgzf(VCF)))
        assert r.compression is Compression.BGZF
        assert r.kind is FormatKind.VCF

    def test_bam(self, tmp_path):
        r = detect(write(tmp_path, "x.bam", make_bgzf(b"BAM\x01" + b"\x00" * 64)))
        assert r.kind is FormatKind.BAM
        assert r.compression is Compression.BGZF

    def test_bgzf_multi_member_fastq_sniffs_from_magic(self, tmp_path):
        """Regression: a real bgzip'd file is many small BGZF members back to
        back, and its first member alone (as real `bgzip` emits it) is far
        short of a full FASTQ record. Decoding only the first member used to
        leave too little for `_sniff_text` to recognize, silently downgrading
        confidence from MAGIC to EXTENSION for every bgzip'd upload."""
        record = FASTQ * 50  # several BGZF members once each is capped small
        stream = b"".join(make_bgzf(record[i : i + 200]) for i in range(0, len(record), 200))
        r = detect(write(tmp_path, "x.fastq.gz", stream))
        assert r.kind is FormatKind.FASTQ
        assert r.compression is Compression.BGZF
        assert r.confidence is FormatConfidence.MAGIC
        assert r.magic_says is FormatKind.FASTQ


class TestDisagreement:
    def test_contents_win_but_extension_is_recorded(self, tmp_path):
        """A .bam holding FASTQ must surface both signals, not silently pick one."""
        r = detect(write(tmp_path, "mislabeled.bam", FASTQ))
        assert r.kind is FormatKind.FASTQ
        assert r.magic_says is FormatKind.FASTQ
        assert r.extension_says is FormatKind.BAM
        assert r.confidence is FormatConfidence.MAGIC

    def test_unknown_falls_back_to_extension(self, tmp_path):
        r = detect(write(tmp_path, "weird.fastq", b"\x00\x01\x02\x03binary junk"))
        assert r.kind is FormatKind.FASTQ
        assert r.confidence is FormatConfidence.EXTENSION

    def test_no_signal_at_all(self, tmp_path):
        r = detect(write(tmp_path, "mystery", b"\x00\x01\x02\x03"))
        assert r.kind is FormatKind.UNKNOWN
        assert r.confidence is FormatConfidence.NONE

    def test_empty_file(self, tmp_path):
        r = detect(write(tmp_path, "empty.txt", b""))
        assert r.kind is FormatKind.TEXT

    def test_missing_file_does_not_raise(self, tmp_path):
        r = detect(tmp_path / "nope.fastq")
        assert r.kind is FormatKind.FASTQ
        assert r.confidence is FormatConfidence.EXTENSION

    @pytest.mark.parametrize(
        "payload",
        [
            b"\x1f\x8b\x08\x04" + b"\x00" * 200,  # BGZF-ish header, invalid body
            b"\x1f\x8b\x08\x00" + b"\xff" * 100,  # gzip header, garbage deflate
            b"\x1f\x8b",  # gzip magic and nothing else
        ],
    )
    def test_corrupt_gzip_never_raises(self, tmp_path, payload):
        """zlib.error is not an OSError subclass. Letting it escape turns an
        unreadable file into an ingest job that retries forever instead of
        being flagged once."""
        r = detect(write(tmp_path, "truncated.bam", payload))
        assert r.kind in (FormatKind.BAM, FormatKind.UNKNOWN)
        assert r.compression in (Compression.GZIP, Compression.BGZF)


class TestMagicDirect:
    def test_detect_from_magic_on_bytes(self):
        kind, comp = detect_from_magic(FASTQ)
        assert kind is FormatKind.FASTQ
        assert comp is Compression.NONE
