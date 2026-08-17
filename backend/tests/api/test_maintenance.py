import pytest

from app.models.drift import DriftCategory, DriftEntry, DriftReport

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


class TestDriftRoute:
    async def test_returns_the_stored_report(self, client):
        await DriftReport.find_all().delete()
        report = await DriftReport.load()
        report.counts = {"orphaned_file": 2}
        report.reclaimable_bytes = 8192
        report.entries = [
            DriftEntry(
                category=DriftCategory.ORPHANED_FILE,
                path="ab/abc",
                digest="a" * 64,
                size_bytes=4096,
            )
        ]
        await report.save()

        res = await client.get("/api/v1/maintenance/drift")

        assert res.status_code == 200
        body = res.json()
        assert body["counts"]["orphaned_file"] == 2
        assert body["reclaimable_bytes"] == 8192
        assert body["entries"][0]["category"] == "orphaned_file"

    async def test_returns_an_empty_report_before_any_sweep(self, client):
        await DriftReport.find_all().delete()
        res = await client.get("/api/v1/maintenance/drift")

        assert res.status_code == 200
        assert res.json()["counts"] == {}
