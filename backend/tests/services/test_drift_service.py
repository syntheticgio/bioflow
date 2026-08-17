from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

from app.models.blob import Blob, BlobState, BlobStorage
from app.models.drift import DriftCategory, DriftEntry, DriftReport
from app.services import drift_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


@pytest.fixture
def objects_dir(tmp_path: Path) -> Path:
    """A throwaway objects/ root, patched in for drift_service.settings.

    The real settings.objects_dir points at /data/objects, the storage home
    shared with the running stack (deliberately, per CLAUDE.md) -- walking it
    for real would pick up whatever the main stack has stored, not just what
    this test placed. Same pattern as test_blob_transfer_api.py's
    _patch_objects_dir.
    """
    d = tmp_path / "data" / "objects"
    d.mkdir(parents=True, exist_ok=True)
    with patch("app.services.drift_service.settings") as mock_settings:
        mock_settings.objects_dir = d
        yield d


def _place_blob_file(objects_dir: Path, digest: str, content: bytes = b"hello") -> None:
    """Write a file where a managed blob of this digest would live."""
    path = objects_dir / digest[:2] / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class TestDriftReportModel:
    async def test_load_creates_the_singleton_when_absent(self):
        report = await DriftReport.load()
        assert report.id == DriftReport.SINGLETON_ID
        assert report.counts == {}
        assert report.entries == []
        assert report.reclaimable_bytes == 0

    async def test_load_returns_the_same_document_twice(self):
        first = await DriftReport.load()
        first.reclaimable_bytes = 4096
        await first.save()

        second = await DriftReport.load()
        assert second.reclaimable_bytes == 4096

    async def test_entry_carries_its_category_and_size(self):
        entry = DriftEntry(
            category=DriftCategory.ORPHANED_FILE,
            path="ab/abc123",
            size_bytes=1024,
        )
        assert entry.category is DriftCategory.ORPHANED_FILE
        assert entry.object_id is None
        assert entry.digest is None


class TestFindOrphanedFiles:
    @pytest_asyncio.fixture(autouse=True, loop_scope="module")
    async def clean(self):
        await Blob.find_all().delete()

    async def test_file_with_no_blob_record_is_an_orphan(self, objects_dir):
        _place_blob_file(objects_dir, DIGEST_A)

        entries = await drift_service.find_orphaned_files()

        orphans = [e for e in entries if e.category is DriftCategory.ORPHANED_FILE]
        assert [e.digest for e in orphans] == [DIGEST_A]
        assert orphans[0].size_bytes == 5

    async def test_old_pending_record_is_a_stalled_ingest(self, objects_dir):
        _place_blob_file(objects_dir, DIGEST_B)
        stale = datetime.now(UTC) - timedelta(hours=3)
        await Blob(
            id=DIGEST_B,
            size=5,
            state=BlobState.PENDING,
            storage=BlobStorage.MANAGED,
            rel_path=f"{DIGEST_B[:2]}/{DIGEST_B}",
            created_at=stale,
            updated_at=stale,
        ).insert()

        entries = await drift_service.find_orphaned_files()

        stalled = [e for e in entries if e.category is DriftCategory.STALLED_INGEST]
        assert [e.digest for e in stalled] == [DIGEST_B]

    async def test_recent_pending_record_is_not_reported(self, objects_dir):
        """An ingest in flight right now. The whole false-positive guard."""
        _place_blob_file(objects_dir, DIGEST_C)
        await Blob(
            id=DIGEST_C,
            size=5,
            state=BlobState.PENDING,
            storage=BlobStorage.MANAGED,
            rel_path=f"{DIGEST_C[:2]}/{DIGEST_C}",
        ).insert()

        entries = await drift_service.find_orphaned_files()

        assert entries == []

    async def test_present_record_is_not_reported(self, objects_dir):
        _place_blob_file(objects_dir, DIGEST_A)
        await Blob(
            id=DIGEST_A,
            size=5,
            state=BlobState.PRESENT,
            storage=BlobStorage.MANAGED,
            rel_path=f"{DIGEST_A[:2]}/{DIGEST_A}",
        ).insert()

        entries = await drift_service.find_orphaned_files()

        assert entries == []


class TestFindMissingBlobs:
    @pytest_asyncio.fixture(autouse=True, loop_scope="module")
    async def clean(self):
        await Blob.find_all().delete()

    async def test_missing_managed_blob_is_reported(self):
        await Blob(
            id=DIGEST_A,
            size=2048,
            state=BlobState.MISSING,
            storage=BlobStorage.MANAGED,
            rel_path=f"{DIGEST_A[:2]}/{DIGEST_A}",
        ).insert()

        entries = await drift_service.find_missing_blobs()

        assert [e.digest for e in entries] == [DIGEST_A]
        assert entries[0].category is DriftCategory.MISSING_BLOB
        assert entries[0].size_bytes == 2048

    async def test_missing_external_blob_is_never_reported(self):
        """Registered in place; outside BIOINFO_HOME, never ours to reclaim."""
        await Blob(
            id=DIGEST_B,
            size=2048,
            state=BlobState.MISSING,
            storage=BlobStorage.EXTERNAL,
            external_path="/somewhere/else/reads.fastq",
        ).insert()

        entries = await drift_service.find_missing_blobs()

        assert entries == []

    async def test_present_blob_is_not_reported(self):
        await Blob(
            id=DIGEST_C,
            size=2048,
            state=BlobState.PRESENT,
            storage=BlobStorage.MANAGED,
            rel_path=f"{DIGEST_C[:2]}/{DIGEST_C}",
        ).insert()

        entries = await drift_service.find_missing_blobs()

        assert entries == []
