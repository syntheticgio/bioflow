"""Ingest-time compression wired into ingest_local_file and ingest_stream.

Step 3 of docs/superpowers/specs/2026-08-05-object-compression-design.md:
step 2 built the seam (storage/compress.py, Blob.content_sha256), this wires
it into the ingest paths so it actually runs. Drives the real production path
end to end, not compress.compress_and_hash in isolation -- test_compress.py
already covers that unit; this covers the decision to call it, the renamed
object, the dedup lookup, and the paths that must NOT compress.
"""

import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.models import Blob
from app.pipelines import tools
from app.services import object_service, project_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch):
    """ingest_local_file/ingest_stream both finish by queueing ingest_headers,
    which needs a live Redis connection this process never opens -- orthogonal
    to compression, so stubbed the same way test_object_service_owner.py does."""

    async def _skip(obj, **kwargs):
        return ""

    monkeypatch.setattr(object_service, "enqueue_ingest", _skip)


_scratch_files: list[Path] = []


@pytest.fixture(autouse=True)
def _reclaim_scratch_files():
    _scratch_files.clear()
    yield
    for path in _scratch_files:
        path.unlink(missing_ok=True)
    _scratch_files.clear()


def _scratch_file(name: str, content: bytes) -> Path:
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    path = settings.tmp_dir / f"compress-test-{uuid.uuid4().hex}-{name}"
    path.write_bytes(content)
    _scratch_files.append(path)
    return path


def _fastq(n: int) -> bytes:
    """Real FASTQ text, unique per call so two ingests do not collide."""
    return f"@read{n}\nACGTACGTACGT\n+\nIIIIIIIIIIII\n".encode() * 50


async def _project(owner: str):
    return await project_service.create_project(name=f"{owner}-proj", owner=owner)


class TestIngestLocalFileCompresses:
    async def test_fastq_output_is_stored_compressed_and_renamed(self):
        owner = "compress-a"
        project = await _project(owner)
        path = _scratch_file("reads.fastq", _fastq(1))
        original_size = path.stat().st_size

        obj = await object_service.ingest_local_file(
            project_id=project.id, path=path, name="reads.fastq", owner=owner
        )

        assert obj.name == "reads.fastq.gz"
        assert obj.size < original_size
        blob = await Blob.get(obj.blob_sha256)
        assert blob.content_sha256 is not None
        assert blob.size == obj.size

    async def test_bam_is_left_uncompressed(self):
        """Already block-compressed -- see compress.COMPRESSIBLE_KINDS."""
        owner = "compress-b"
        project = await _project(owner)
        # Not real BAM magic bytes, but the point under test is the name/size
        # path, and .bam is unambiguous by extension alone when content is
        # not itself gzip -- detect() falls back to EXTENSION confidence,
        # still FormatKind.BAM, still excluded from the allowlist.
        content = b"not actually bam but named like one" * 20
        path = _scratch_file("aln.bam", content)

        obj = await object_service.ingest_local_file(
            project_id=project.id, path=path, name="aln.bam", owner=owner
        )

        assert obj.name == "aln.bam"
        assert obj.size == len(content)
        blob = await Blob.get(obj.blob_sha256)
        assert blob.content_sha256 is None

    async def test_unknown_format_is_left_uncompressed(self):
        """An aligner-index member detects as UNKNOWN and must not compress --
        the allowlist excludes it by construction, not by an extension list."""
        owner = "compress-c"
        project = await _project(owner)
        content = b"\x00\x01\x02\x03" * 100  # binary, no recognizable format
        path = _scratch_file("index.bt2", content)

        obj = await object_service.ingest_local_file(
            project_id=project.id, path=path, name="index.bt2", owner=owner
        )

        assert obj.name == "index.bt2"
        assert obj.size == len(content)

    async def test_already_gzipped_fastq_is_not_double_compressed(self):
        """fastp and friends already emit real .fastq.gz -- must be a no-op,
        not a second layer of compression on top."""
        import gzip

        owner = "compress-d"
        project = await _project(owner)
        plaintext = _fastq(2)
        gzipped = gzip.compress(plaintext)
        path = _scratch_file("reads.trimmed.fastq.gz", gzipped)

        obj = await object_service.ingest_local_file(
            project_id=project.id, path=path, name="reads.trimmed.fastq.gz", owner=owner
        )

        assert obj.name == "reads.trimmed.fastq.gz"
        assert obj.size == len(gzipped)
        blob = await Blob.get(obj.blob_sha256)
        # Never compressed, so no plaintext hash was ever computed for it.
        assert blob.content_sha256 is None

    async def test_mislabeled_gz_with_plain_bytes_does_not_double_suffix(self):
        """A name claiming .gz whose bytes are not actually gzip -- content
        wins, so it compresses, and the stale suffix must not survive into
        name.gz.gz. Regression: this is exactly what test_results_owner.py's
        scratch fixtures (arbitrary bytes, a .gz-suffixed name) hit."""
        owner = "compress-e"
        project = await _project(owner)
        path = _scratch_file("calls.vcf.gz", _vcf_bytes())

        obj = await object_service.ingest_local_file(
            project_id=project.id, path=path, name="calls.vcf.gz", owner=owner
        )

        assert obj.name == "calls.vcf.gz"
        assert ".gz.gz" not in obj.name

    async def test_two_ingests_of_the_same_plaintext_dedup_across_compressors(
        self, monkeypatch
    ):
        """The scenario content_sha256 exists for: bgzip and the stdlib
        fallback write different compressed bytes for identical plaintext, so
        only a plaintext-keyed lookup lets the second ingest find the first
        blob rather than storing the bytes twice."""
        owner = "compress-f"
        project = await _project(owner)
        content = _fastq(3)

        path1 = _scratch_file("a.fastq", content)
        obj1 = await object_service.ingest_local_file(
            project_id=project.id, path=path1, name="a.fastq", owner=owner
        )

        monkeypatch.setattr(
            tools, "bgzip", lambda: tools.Tool(name="bgzip", path=None, version=None, error="x")
        )
        path2 = _scratch_file("b.fastq", content)
        obj2 = await object_service.ingest_local_file(
            project_id=project.id, path=path2, name="b.fastq", owner=owner
        )

        assert obj1.blob_sha256 == obj2.blob_sha256
        blob = await Blob.get(obj1.blob_sha256)
        assert blob.ref_count == 2


class TestIngestStreamCompresses:
    async def test_uploaded_fastq_is_stored_compressed_and_renamed(self):
        owner = "compress-stream-a"
        project = await _project(owner)
        content = _fastq(4)

        obj = await object_service.ingest_stream(
            owner=owner, project_id=project.id, filename="upload.fastq", stream=iter([content])
        )

        assert obj.name == "upload.fastq.gz"
        assert obj.size < len(content)
        blob = await Blob.get(obj.blob_sha256)
        assert blob.content_sha256 is not None

    async def test_uploaded_text_file_is_left_uncompressed(self):
        owner = "compress-stream-b"
        project = await _project(owner)
        content = b"just some notes, not a bioinformatics format\n" * 5

        obj = await object_service.ingest_stream(
            owner=owner, project_id=project.id, filename="notes.txt", stream=iter([content])
        )

        assert obj.name == "notes.txt"
        assert obj.size == len(content)

    async def test_upload_dedups_against_a_locally_ingested_compressed_blob(self):
        """Cross-path dedup: an upload of the exact bytes a pipeline already
        produced (and compressed) must land on that same blob rather than
        writing a second copy under a different CAS digest."""
        owner = "compress-stream-c"
        project = await _project(owner)
        content = _fastq(5)

        path = _scratch_file("produced.fastq", content)
        produced = await object_service.ingest_local_file(
            project_id=project.id, path=path, name="produced.fastq", owner=owner
        )

        uploaded = await object_service.ingest_stream(
            owner=owner, project_id=project.id, filename="uploaded.fastq", stream=iter([content])
        )

        assert uploaded.blob_sha256 == produced.blob_sha256


def _vcf_bytes() -> bytes:
    return (
        b"##fileformat=VCFv4.3\n"
        b"##contig=<ID=chr1,length=248956422>\n"
        b"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        b"chr1\t100\t.\tA\tG\t50\tPASS\t.\n"
    ) * 20
