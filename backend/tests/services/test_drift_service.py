import pytest

from app.models.drift import DriftCategory, DriftEntry, DriftReport

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


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
