"""Dedup by content_sha256: the compressed-format lookup added alongside
find_present_blob, which stays keyed on the CAS digest. See
docs/superpowers/specs/2026-08-05-object-compression-design.md.
"""

import pytest
from app.models import BlobState, DataObject
from app.services import blob_service
from beanie import PydanticObjectId

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


async def _object(name: str = "reads.fastq.gz") -> DataObject:
    obj = DataObject(project_id=PydanticObjectId(), name=name, owner="b")
    await obj.insert()
    return obj


async def test_attach_blob_to_object_persists_content_sha256():
    digest = "a" * 64
    content_digest = "1" * 64
    obj = await _object()

    await blob_service.attach_blob_to_object(
        object_id=obj.id, digest=digest, size=100, content_sha256=content_digest
    )

    from app.models import Blob

    blob = await Blob.get(digest)
    assert blob.content_sha256 == content_digest


async def test_content_sha256_defaults_to_none_for_an_uncompressed_blob():
    digest = "b" * 64
    obj = await _object()

    await blob_service.attach_blob_to_object(object_id=obj.id, digest=digest, size=100)

    from app.models import Blob

    blob = await Blob.get(digest)
    assert blob.content_sha256 is None


async def test_find_present_blob_by_content_finds_a_dedup_candidate():
    digest = "c" * 64
    content_digest = "2" * 64
    obj = await _object()
    await blob_service.attach_blob_to_object(
        object_id=obj.id, digest=digest, size=100, content_sha256=content_digest
    )

    found = await blob_service.find_present_blob_by_content(content_digest)

    assert found is not None
    assert found.id == digest


async def test_find_present_blob_by_content_ignores_non_present_state():
    """Quarantined or missing content must be re-placed rather than trusted,
    same rule find_present_blob already applies to the CAS-key lookup."""
    digest = "d" * 64
    content_digest = "3" * 64
    obj = await _object()
    await blob_service.attach_blob_to_object(
        object_id=obj.id, digest=digest, size=100, content_sha256=content_digest
    )
    from app.models import Blob

    blob = await Blob.get(digest)
    blob.state = BlobState.QUARANTINED
    await blob.save()

    found = await blob_service.find_present_blob_by_content(content_digest)

    assert found is None


async def test_find_present_blob_by_content_returns_none_when_absent():
    found = await blob_service.find_present_blob_by_content("e" * 64)
    assert found is None


async def test_two_compressors_writing_different_bytes_still_dedup_by_content():
    """The scenario content_sha256 exists for: bgzip and the stdlib fallback
    produce different compressed bytes (and so different CAS digests) from
    identical plaintext. A caller that looked up by CAS digest alone would
    never find the first ingest's blob and would store the bytes twice."""
    plaintext_digest = "f6" * 32
    bgzip_digest = "f7" * 32
    stdlib_digest = "f8" * 32

    obj1 = await _object("reads.fastq.gz")
    await blob_service.attach_blob_to_object(
        object_id=obj1.id, digest=bgzip_digest, size=100, content_sha256=plaintext_digest
    )

    # A second ingest of the same plaintext, compressed by the stdlib
    # fallback this time, looks up by content hash first.
    existing = await blob_service.find_present_blob_by_content(plaintext_digest)
    assert existing is not None
    assert existing.id == bgzip_digest
    assert existing.id != stdlib_digest
