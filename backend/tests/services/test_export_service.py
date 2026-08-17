from app.config import settings
from app.services import export_service


def test_exports_dir_is_under_bioinfo_home():
    assert settings.exports_dir == settings.bioinfo_home / "exports"


def test_export_format_constants():
    assert export_service.BIOFLOW_EXPORT_VERSION == 1
    assert export_service.DEFAULT_BLOB_THRESHOLD_BYTES == 100 * 1024 * 1024
