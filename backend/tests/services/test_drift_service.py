from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from app.config import settings
from app.models.blob import Blob, BlobState, BlobStorage
from app.models.drift import DriftCategory, DriftEntry, DriftReport
from app.models.object import DataObject
from app.services import drift_service
from app.storage.home import HomeStatus

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


class TestWalkBatching:
    """#499: the walk streams and each Blob lookup is bounded.

    The failure this guards is a single `$in` naming every digest in the tree,
    which exceeds MongoDB's 16MB BSON command limit long before the few
    hundred thousand files the design doc targets. Measured on a 300k-file
    tree: the unbatched command encodes to 23.0MB, the largest batched one to
    0.149MB.
    """

    @pytest_asyncio.fixture(autouse=True, loop_scope="module")
    async def clean(self):
        await Blob.find_all().delete()

    async def test_walk_yields_batches_without_materializing_the_tree(self, objects_dir):
        for i in range(7):
            _place_blob_file(objects_dir, f"{i:064x}")

        batches = drift_service._iter_object_files(batch_size=3)

        # A generator, so nothing has been read from disk yet.
        assert not isinstance(batches, list)
        assert [len(b) for b in batches] == [3, 3, 1]

    async def test_walk_of_a_missing_root_yields_nothing(self, objects_dir, tmp_path):
        with patch.object(drift_service.settings, "objects_dir", tmp_path / "nope"):
            assert list(drift_service._iter_object_files()) == []

    async def test_each_blob_lookup_is_bounded_by_the_batch_size(self, objects_dir):
        """No `$in` may name more digests than one batch holds."""
        for i in range(25):
            _place_blob_file(objects_dir, f"{i:064x}")

        seen: list[int] = []
        real_find = Blob.find

        def spy(*args, **kwargs):
            for arg in args:
                if isinstance(arg, dict) and "_id" in arg:
                    seen.append(len(arg["_id"]["$in"]))
            return real_find(*args, **kwargs)

        with patch.object(drift_service, "WALK_BATCH_SIZE", 10):
            with patch.object(Blob, "find", spy):
                entries = await drift_service.find_orphaned_files()

        assert seen == [10, 10, 5]
        # Batching must not change what is found.
        assert len(entries) == 25

    async def test_batching_finds_the_same_entries_as_a_single_pass(self, objects_dir):
        for i in range(12):
            _place_blob_file(objects_dir, f"{i:064x}")

        with patch.object(drift_service, "WALK_BATCH_SIZE", 5):
            batched = await drift_service.find_orphaned_files()
        with patch.object(drift_service, "WALK_BATCH_SIZE", 10_000):
            single = await drift_service.find_orphaned_files()

        assert sorted(e.digest for e in batched) == sorted(e.digest for e in single)

    async def test_a_record_is_matched_even_when_it_lands_in_a_later_batch(self, objects_dir):
        """Each batch queries only its own digests, so per-batch lookups must
        still resolve records for the files in that batch."""
        digests = [f"{i:064x}" for i in range(6)]
        for digest in digests:
            _place_blob_file(objects_dir, digest)
        present = digests[-1]
        await Blob(
            id=present,
            size=5,
            state=BlobState.PRESENT,
            storage=BlobStorage.MANAGED,
            rel_path=f"{present[:2]}/{present}",
        ).insert()

        with patch.object(drift_service, "WALK_BATCH_SIZE", 2):
            entries = await drift_service.find_orphaned_files()

        assert present not in {e.digest for e in entries}
        assert len(entries) == 5


class TestDriftAccumulator:
    """#499: examples are capped as they arrive; counts and bytes stay exact."""

    def _entry(self, category, size=0):
        return DriftEntry(category=category, path="ab/abc", size_bytes=size)

    async def test_counts_stay_exact_above_the_cap(self):
        acc = drift_service.DriftAccumulator(cap=3)
        for _ in range(10):
            acc.add(self._entry(DriftCategory.ORPHANED_FILE))

        assert acc.counts["orphaned_file"] == 10
        assert len(acc.entries) == 3

    async def test_reclaimable_bytes_stay_exact_above_the_cap(self):
        """The sum must count entries the cap discarded -- otherwise the
        reported free space shrinks the worse the drift gets."""
        acc = drift_service.DriftAccumulator(cap=2)
        for _ in range(10):
            acc.add(self._entry(DriftCategory.ORPHANED_FILE, size=100))

        assert acc.reclaimable_bytes == 1000

    async def test_missing_blob_bytes_are_never_reclaimable(self):
        acc = drift_service.DriftAccumulator(cap=10)
        acc.add(self._entry(DriftCategory.MISSING_BLOB, size=2048))

        assert acc.reclaimable_bytes == 0
        assert acc.counts["missing_blob"] == 1

    async def test_the_cap_is_per_category(self):
        """A flood of one category must not hide every example of another."""
        acc = drift_service.DriftAccumulator(cap=2)
        for _ in range(10):
            acc.add(self._entry(DriftCategory.ORPHANED_FILE))
        acc.add(self._entry(DriftCategory.MISSING_BLOB))

        kept = [e.category for e in acc.entries]
        assert kept.count(DriftCategory.ORPHANED_FILE) == 2
        assert kept.count(DriftCategory.MISSING_BLOB) == 1


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


@pytest.fixture
def report_roots(tmp_path: Path):
    """Throwaway report-root directories, patched in for both settings and
    drift_service's REPORT_ROOTS.

    REPORT_ROOTS is built once at drift_service import time from
    settings.qc_reports_dir et al. (each a property derived from
    settings.bioinfo_home), so patching settings.bioinfo_home alone would not
    move the already-captured Path objects inside REPORT_ROOTS. Both need
    patching so settings.qc_reports_dir (read directly by the test, per the
    brief) and REPORT_ROOTS (read by find_missing_report_dirs) agree on the
    same tmp_path root. Same rationale as the objects_dir fixture above, one
    level up the dependency chain.
    """
    home = tmp_path / "data"
    home.mkdir(parents=True, exist_ok=True)
    with patch.object(settings, "bioinfo_home", home):
        patched_roots = {
            "qc_tool": settings.qc_reports_dir,
            "bam_stats_summary": settings.bam_stats_dir,
            "vcf_stats_summary": settings.vcf_stats_dir,
            "annotation_stats_status": settings.annotation_stats_dir,
        }
        with patch.object(drift_service, "REPORT_ROOTS", patched_roots):
            yield


class TestFindMissingReportDirs:
    @pytest_asyncio.fixture(autouse=True, loop_scope="module")
    async def clean(self):
        await DataObject.find_all().delete()

    async def test_object_claiming_qc_with_no_directory_is_reported(self, report_roots):
        obj = await _make_object({"qc_tool": "fastp"})

        entries = await drift_service.find_missing_report_dirs()

        assert [e.object_id for e in entries] == [str(obj.id)]
        assert entries[0].category is DriftCategory.MISSING_REPORT_DIR

    async def test_object_with_its_directory_present_is_not_reported(self, report_roots):
        obj = await _make_object({"qc_tool": "fastp"})
        (settings.qc_reports_dir / str(obj.id)).mkdir(parents=True, exist_ok=True)

        entries = await drift_service.find_missing_report_dirs()

        assert entries == []

    async def test_object_claiming_no_report_is_not_reported(self, report_roots):
        await _make_object({"qc_total_reads": 1000})

        entries = await drift_service.find_missing_report_dirs()

        assert entries == []

    async def test_failed_annotation_status_is_not_a_claim(self, report_roots):
        """`annotation_stats_status` gates on == "ok", matching the UI."""
        await _make_object({"annotation_stats_status": "failed"})

        entries = await drift_service.find_missing_report_dirs()

        assert entries == []

    async def test_transcript_qc_has_no_directory_and_is_never_reported(self, report_roots):
        await _make_object({"transcript_qc_status": "ok"})

        entries = await drift_service.find_missing_report_dirs()

        assert entries == []

    async def test_only_the_id_and_tested_fact_are_fetched(self, report_roots):
        """#499: full DataObject documents are never loaded to run a predicate."""
        obj = await _make_object({"qc_tool": "fastp", "qc_total_reads": 1000})

        seen: list[dict] = []
        collection = DataObject.get_pymongo_collection()
        real_find = collection.find

        def spy(*args, **kwargs):
            seen.append(kwargs.get("projection"))
            return real_find(*args, **kwargs)

        class SpyCollection:
            """Wraps the real collection, recording each find()'s projection.

            The spy has to sit on an instance: patching find() on the pymongo
            class turns it into an unbound function, and `self` then collides
            with the positional filter argument.
            """

            find = staticmethod(spy)

            def __getattr__(self, name):
                return getattr(collection, name)

        with patch.object(DataObject, "get_pymongo_collection", lambda: SpyCollection()):
            entries = await drift_service.find_missing_report_dirs()

        assert [e.object_id for e in entries] == [str(obj.id)]
        assert {"_id": 1, "facts.qc_tool": 1} in seen
        assert all(p is not None for p in seen)


async def _make_object(facts: dict) -> DataObject:
    from beanie import PydanticObjectId

    obj = DataObject(
        project_id=PydanticObjectId(),
        name="sample.fastq.gz",
        facts=facts,
    )
    await obj.insert()
    return obj


class TestSweep:
    @pytest_asyncio.fixture(autouse=True, loop_scope="module")
    async def clean(self):
        await Blob.find_all().delete()
        await DataObject.find_all().delete()
        await DriftReport.find_all().delete()

    @pytest.fixture(autouse=True)
    def home_ok(self):
        """sweep() checks the real check_home() against the container's own
        BIOINFO_HOME, which carries no sentinel in the test environment.
        Patch it ok by default; the one skip test overrides it locally.
        """
        with patch(
            "app.services.drift_service.check_home",
            return_value=HomeStatus(True, "ok", "/data"),
        ):
            yield

    async def test_sweep_counts_each_category_and_sums_reclaimable(
        self, objects_dir, report_roots
    ):
        _place_blob_file(objects_dir, DIGEST_A, b"0123456789")  # orphan, 10 bytes
        await Blob(
            id=DIGEST_B,
            size=2048,
            state=BlobState.MISSING,
            storage=BlobStorage.MANAGED,
            rel_path=f"{DIGEST_B[:2]}/{DIGEST_B}",
        ).insert()

        report = await drift_service.sweep()

        assert report.counts["orphaned_file"] == 1
        assert report.counts["missing_blob"] == 1
        # Only categories 1 and 2 are reclaimable: a missing blob's bytes are
        # already gone, so counting them would promise space that does not exist.
        assert report.reclaimable_bytes == 10

    async def test_sweep_is_stored_and_readable(self, objects_dir, report_roots):
        _place_blob_file(objects_dir, DIGEST_A)

        await drift_service.sweep()
        stored = await DriftReport.load()

        assert stored.counts["orphaned_file"] == 1
        assert stored.swept_at is not None

    async def test_sweep_skips_when_home_is_not_mounted(self, objects_dir, report_roots):
        """Every blob looks missing when the drive is gone."""
        with patch(
            "app.services.drift_service.check_home",
            return_value=HomeStatus(False, "sentinel missing", "/data"),
        ):
            report = await drift_service.sweep()

        assert report.skipped is True
        assert report.skip_reason == "sentinel missing"
        assert report.counts == {}

    async def test_entries_are_capped_but_counts_stay_exact(self, objects_dir, report_roots):
        for i in range(drift_service.MAX_ENTRIES_PER_CATEGORY + 5):
            digest = f"{i:064x}"
            _place_blob_file(objects_dir, digest)

        report = await drift_service.sweep()

        assert report.counts["orphaned_file"] == drift_service.MAX_ENTRIES_PER_CATEGORY + 5
        capped = [e for e in report.entries if e.category is DriftCategory.ORPHANED_FILE]
        assert len(capped) == drift_service.MAX_ENTRIES_PER_CATEGORY

    async def test_entries_are_never_all_held_at_once(self, objects_dir, report_roots):
        """#499: the cap applies as entries arrive, not after a full list is
        built -- that discarded list was the sweep's peak allocation (212MB at
        300k orphans, against a job provisioned at mem_mb=128; 36MB after).

        Watching the accumulator rather than measuring bytes: the invariant is
        that nothing retains more than the cap at any moment, which a memory
        reading can only ever approximate.
        """
        over = drift_service.MAX_ENTRIES_PER_CATEGORY * 3
        for i in range(over):
            _place_blob_file(objects_dir, f"{i:064x}")

        high_water = 0
        real_add = drift_service.DriftAccumulator.add

        def watched_add(self, entry):
            nonlocal high_water
            real_add(self, entry)
            high_water = max(high_water, len(self.entries))

        with patch.object(drift_service.DriftAccumulator, "add", watched_add):
            report = await drift_service.sweep()

        assert report.counts["orphaned_file"] == over
        assert high_water == drift_service.MAX_ENTRIES_PER_CATEGORY


class TestSweepHandler:
    @pytest_asyncio.fixture(autouse=True, loop_scope="module")
    async def clean(self):
        await Blob.find_all().delete()
        await DataObject.find_all().delete()
        await DriftReport.find_all().delete()

    @pytest.fixture(autouse=True)
    def home_ok(self):
        """sweep() checks the real check_home(), which carries no sentinel in
        the test environment. Same patch TestSweep uses."""
        with patch(
            "app.services.drift_service.check_home",
            return_value=HomeStatus(True, "ok", "/data"),
        ):
            yield

    def _ctx(self):
        """Minimal JobContext stand-in: the handler only needs check_cancel."""
        from unittest.mock import MagicMock

        ctx = MagicMock()
        ctx.payload = {}
        ctx.check_cancel = MagicMock(return_value=None)
        return ctx

    async def test_handler_runs_the_sweep_and_returns_counts(self, objects_dir, report_roots):
        from app.queue.handlers import sweep_storage_drift

        _place_blob_file(objects_dir, DIGEST_A)

        result = await sweep_storage_drift(self._ctx())

        assert result["counts"]["orphaned_file"] == 1
        assert "reclaimable_bytes" in result

    async def test_handler_is_registered_with_the_queue(self):
        from app.queue.registry import get_handler

        assert get_handler("sweep_storage_drift") is not None

    async def test_schedule_and_resources_are_seeded(self):
        from app.queue.scheduler import DEFAULT_SCHEDULES, RESOURCES

        ids = {s["_id"] for s in DEFAULT_SCHEDULES}
        assert "sweep_storage_drift" in ids
        assert "sweep_storage_drift" in RESOURCES
