import pytest

from app.config import settings
from app.models import Blob, BlobState, JobRunTiming
from app.models.timing import RunMachine
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


def test_redact_strips_external_path():
    bundle = export_service.ExportBundle(
        blobs=[
            Blob(
                id="a" * 64,
                size=10,
                state=BlobState.PRESENT,
                external_path="/Users/gio/secret-dir/reads.fastq",
            )
        ]
    )

    docs, summary = export_service.redact(bundle)

    assert docs["blobs"][0]["external_path"] is None
    assert summary.paths_relativized == 1


def test_redact_clears_machine_identity_but_keeps_durations():
    # Controller correction: JobRunTiming has no duration_seconds field --
    # the real field is duration_ms (backend/app/models/timing.py). Using
    # duration_ms=2_400_000 (same 2400s duration, in milliseconds).
    timing = JobRunTiming(
        job_type="align",
        input_bytes=1_000_000,
        job_id="j1",
        duration_ms=2_400_000,
        machine=RunMachine(machine_id="gio-workstation.local"),
    )
    bundle = export_service.ExportBundle(timings=[timing])

    docs, summary = export_service.redact(bundle)

    assert docs["job_timings"][0]["machine"] == {}
    assert docs["job_timings"][0]["duration_ms"] == 2_400_000
    assert summary.machine_records_cleared == 1


def test_redact_does_not_count_an_unset_machine_as_cleared():
    # RunMachine() with no fields set still dumps to a dict of all-None
    # values, which is truthy -- the count must not fire for a timing that
    # never actually recorded machine identity.
    timing = JobRunTiming(
        job_type="align",
        input_bytes=1_000_000,
        job_id="j2",
        duration_ms=1_000,
        machine=RunMachine(),
    )
    bundle = export_service.ExportBundle(timings=[timing])

    docs, summary = export_service.redact(bundle)

    assert summary.machine_records_cleared == 0
    assert all(v is None for v in docs["job_timings"][0]["machine"].values())


def test_serialized_collections_excludes_secret_bearing_collections():
    """Exclusion by construction: the allowlist is the guarantee.

    Inverts #411 deliberately -- backup fails safe by including every
    collection, export fails safe by naming the ones it serializes.
    """
    forbidden = {"ai_providers", "app_settings", "nodes", "profiles"}
    assert forbidden.isdisjoint(set(export_service.SERIALIZED_COLLECTIONS))


def test_manifest_lists_excluded_blobs_as_excluded():
    small = Blob(
        id="a" * 64, size=100, state=BlobState.PRESENT, rel_path="ab/small", content_sha256="a" * 64
    )
    large = Blob(
        id="b" * 64, size=10_000, state=BlobState.PRESENT, rel_path="cd/large", content_sha256="b" * 64
    )
    bundle = export_service.ExportBundle(blobs=[small, large])

    tsv, included = export_service.build_manifest(bundle, threshold_bytes=1_000)

    rows = [line.split("\t") for line in tsv.strip().splitlines()[1:]]
    by_size = {r[1]: r for r in rows}
    assert by_size["100"][-1] == "included"
    assert by_size["10000"][-1] == "excluded"
    assert [b.id for b in included] == [small.id]


def test_manifest_has_a_header_row():
    bundle = export_service.ExportBundle(blobs=[])
    tsv, included = export_service.build_manifest(bundle, threshold_bytes=1_000)
    assert tsv.splitlines()[0].split("\t") == [
        "blob_id",
        "size",
        "content_sha256",
        "state",
        "rel_path",
        "bytes",
    ]
    assert included == []


def test_manifest_falls_back_to_id_when_content_sha256_is_unset():
    # content_sha256 is only populated for compressed blobs whose stored
    # bytes differ from the plaintext hash (backend/app/models/blob.py). The
    # common case -- an uncompressed blob, or one predating compression --
    # leaves it None, and the manifest must still report a real digest by
    # falling back to `id`, which is always the stored-bytes hash.
    blob = Blob(id="c" * 64, size=10, state=BlobState.PRESENT, rel_path="ef/plain")
    bundle = export_service.ExportBundle(blobs=[blob])

    tsv, _ = export_service.build_manifest(bundle, threshold_bytes=1_000)

    row = tsv.strip().splitlines()[1].split("\t")
    assert row[2] == "c" * 64
