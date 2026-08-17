import pytest

from app.config import settings
from app.services import export_service
from tests.services.helpers import TEST_OWNER, make_project


def test_exports_dir_is_under_bioinfo_home():
    assert settings.exports_dir == settings.bioinfo_home / "exports"


def test_export_format_constants():
    assert export_service.BIOFLOW_EXPORT_VERSION == 1
    assert export_service.DEFAULT_BLOB_THRESHOLD_BYTES == 100 * 1024 * 1024


@pytest.mark.usefixtures("beanie_models")
@pytest.mark.asyncio(loop_scope="module")
class TestCollect:
    async def test_includes_descendant_projects(self):
        parent = await make_project("export-collect-parent")
        child = await make_project("export-collect-child", parent)

        bundle = await export_service.collect(parent.id, owner=TEST_OWNER)

        ids = {p.id for p in bundle.projects}
        assert ids == {parent.id, child.id}

    async def test_excludes_unrelated_projects(self):
        target = await make_project("export-collect-target")
        await make_project("export-collect-other")

        bundle = await export_service.collect(target.id, owner=TEST_OWNER)

        assert {p.id for p in bundle.projects} == {target.id}
