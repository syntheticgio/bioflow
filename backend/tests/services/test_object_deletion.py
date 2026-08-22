"""Object deletion: the per-object report directories written outside objects/.

Compute jobs write Results artifacts to qc_reports/, bam_stats/, coverage/ and
the rest of `object_service._REPORT_ROOTS`, keyed by object id. Those live
outside the content-addressed store, so nothing refcounts them and blob GC
never sees them -- deletion has to remove them by hand or they leak
permanently.
"""

import pytest

from app.config import settings
from app.services import object_service, project_service
from tests.services.helpers import TEST_OWNER

pytestmark = pytest.mark.usefixtures("beanie_models", "private_report_roots")
# Applied per class: TestReportDirCleanup holds one sync test.
asyncio_module_loop = pytest.mark.asyncio(loop_scope="module")

_ROOT_NAMES = (
    "qc_reports_dir",
    "bam_stats_dir",
    "vcf_stats_dir",
    "annotation_stats_dir",
    "sv_stats_dir",
    "feature_coverage_dir",
    "variants_in_regions_dir",
    "annotation_comparison_dir",
    "coverage_dir",
    "gc_bias_dir",
    "methylation_dir",
)


@pytest.fixture(scope="module", autouse=True)
def private_report_roots(tmp_path_factory):
    """Give this module its own report roots instead of the real ones.

    `reap_report_dirs` scans an entire root and deletes any directory whose
    name is not an object id in *its own* database. Pointed at the shared
    /data roots, that makes the module hostile to anything else running at
    the same time: another test run's live report directory is, from here,
    an orphan to be swept. An `xdist_group` mark only serializes workers
    within one run, so two concurrent runs still destroyed each other's
    fixtures -- which is what the two-simultaneous-suites check caught.

    Both the settings properties and `object_service._REPORT_ROOTS` are
    redirected: the latter is a module-level tuple built at import time, so
    patching settings alone would leave the code under test on the real
    roots while the assertions looked at the private ones.
    """
    base = tmp_path_factory.mktemp("report-roots")
    with pytest.MonkeyPatch.context() as mp:
        roots = []
        for name in _ROOT_NAMES:
            path = base / name
            path.mkdir(parents=True, exist_ok=True)
            mp.setattr(type(settings), name, property(lambda _s, p=path: p))
            roots.append(path)
        mp.setattr(object_service, "_REPORT_ROOTS", tuple(roots))
        yield


def report_dirs():
    return tuple(getattr(settings, name) for name in _ROOT_NAMES)


class TestReportDirCleanup:
    def test_report_dirs_matches_object_services_report_roots(self):
        """A settings dir added to this test's own `report_dirs()` but missed
        in `object_service._REPORT_ROOTS` (or vice versa) would pass every
        other test here while leaking that root's directories on delete and
        never copying them on share -- exactly the gap `sv_stats_dir` had
        until this test was added. Compared as sets since ordering is not a
        contract either tuple makes.
        """
        assert set(report_dirs()) == set(object_service._REPORT_ROOTS)


    @asyncio_module_loop
    async def test_removes_every_report_dir_for_the_deleted_object(self):
        from tests.services.helpers import make_object

        root = await project_service.create_project(name="reports-cleanup", owner=TEST_OWNER)
        obj = await make_object(root, "sample.vcf.gz")

        made = []
        for parent in report_dirs():
            d = parent / str(obj.id)
            d.mkdir(parents=True, exist_ok=True)
            (d / "artifact.txt").write_text("generated")
            made.append(d)

        await object_service.delete_object(obj.id, owner=TEST_OWNER)

        for d in made:
            assert not d.exists(), f"leaked {d}"

    @asyncio_module_loop
    async def test_logs_caller_and_reason_for_an_inline_delete(self, monkeypatch):
        """Issue #10: a removal with no caller/reason on the log line is
        unattributable after the fact. delete_object must stamp both."""
        from tests.services.helpers import make_object

        infos: list[tuple] = []
        monkeypatch.setattr(
            object_service.log, "info", lambda event, **kw: infos.append((event, kw))
        )

        root = await project_service.create_project(name="reports-attribution", owner=TEST_OWNER)
        obj = await make_object(root, "sample.vcf.gz")
        d = settings.vcf_stats_dir / str(obj.id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "artifact.txt").write_text("generated")

        await object_service.delete_object(obj.id, owner=TEST_OWNER)

        removed = [kw for event, kw in infos if event == "report_dir_removed"]
        assert removed, "expected a report_dir_removed log line"
        assert removed[0]["caller"] == "delete_object"
        assert removed[0]["reason"] == "object_deleted"

    @asyncio_module_loop
    async def test_leaves_other_objects_reports_alone(self):
        """The removal is keyed by object id, so a sibling's identically-shaped
        directory next to it must survive."""
        from tests.services.helpers import make_object

        root = await project_service.create_project(name="reports-sibling", owner=TEST_OWNER)
        target = await make_object(root, "target.vcf.gz")
        keeper = await make_object(root, "keeper.vcf.gz")

        kept = settings.vcf_stats_dir / str(keeper.id)
        kept.mkdir(parents=True, exist_ok=True)
        (kept / "variants.tsv").write_text("keep me")
        doomed = settings.vcf_stats_dir / str(target.id)
        doomed.mkdir(parents=True, exist_ok=True)

        await object_service.delete_object(target.id, owner=TEST_OWNER)

        assert not doomed.exists()
        assert (kept / "variants.tsv").read_text() == "keep me"

    @asyncio_module_loop
    async def test_deletes_cleanly_when_no_reports_were_ever_computed(self):
        """The normal case: most objects never have Results computed, so a
        missing directory is expected and must not fail the delete."""
        from app.models import DataObject
        from tests.services.helpers import make_object

        root = await project_service.create_project(name="reports-absent", owner=TEST_OWNER)
        obj = await make_object(root, "plain.fastq.gz")
        for parent in report_dirs():
            assert not (parent / str(obj.id)).exists()

        await object_service.delete_object(obj.id, owner=TEST_OWNER)

        assert await DataObject.get(obj.id) is None

    @asyncio_module_loop
    async def test_removes_a_sidecars_reports_too(self):
        """Sidecars are deleted by recursion, so their reports have to ride the
        same path -- a .bai's own stats directory would otherwise outlive it."""
        from app.models import SidecarRole
        from tests.services.helpers import make_object

        root = await project_service.create_project(name="reports-sidecar", owner=TEST_OWNER)
        bam = await make_object(root, "sample.bam")
        bai = await make_object(
            root,
            "sample.bam.bai",
            sidecar_of=bam.id,
            sidecar_role=SidecarRole.BAI,
        )
        d = settings.bam_stats_dir / str(bai.id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "contigs.tsv").write_text("x")

        await object_service.delete_object(bam.id, owner=TEST_OWNER)

        assert not d.exists()


class TestCopyReportDirs:
    pytestmark = asyncio_module_loop

    async def test_copies_annotation_stats_dir(self):
        """Sharing an object copies the annotation feature database too."""
        from app.services import project_service
        from tests.services.helpers import make_object

        root = await project_service.create_project(name="copy-annot", owner=TEST_OWNER)
        src = await make_object(root, "source.gff")
        dst = await make_object(root, "copy.gff")

        d = settings.annotation_stats_dir / str(src.id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "features.db").write_text("annotation data")

        object_service.copy_report_dirs(src.id, dst.id)

        copied = settings.annotation_stats_dir / str(dst.id)
        assert (copied / "features.db").read_text() == "annotation data"

    async def test_skips_missing_source_dirs(self):
        """A source with no annotation results copies without error."""
        from app.services import project_service
        from tests.services.helpers import make_object

        root = await project_service.create_project(name="copy-skip", owner=TEST_OWNER)
        src = await make_object(root, "source.fasta")
        dst = await make_object(root, "copy.fasta")

        # No annotation_stats dir exists for src — should not raise.
        object_service.copy_report_dirs(src.id, dst.id)

        assert not (settings.annotation_stats_dir / str(dst.id)).exists()


class TestReapReportDirs:
    pytestmark = asyncio_module_loop

    """The sweep for directories stranded before deletion cleaned up inline."""

    @staticmethod
    def ctx(**payload):
        from app.queue.registry import JobContext

        return JobContext(job_id="reap-1", payload=payload, epoch=1, attempts=1, owner="local")

    @staticmethod
    def age(path, hours=48):
        """Backdate mtime past the grace window."""
        import os
        import time

        old = time.time() - hours * 3600
        os.utime(path, (old, old))

    async def test_removes_a_directory_whose_object_is_gone(self):
        from bson import ObjectId

        from app.queue.handlers import reap_report_dirs

        gone = ObjectId()
        d = settings.vcf_stats_dir / str(gone)
        d.mkdir(parents=True, exist_ok=True)
        (d / "variants.db").write_bytes(b"x" * 2048)
        self.age(d)

        result = await reap_report_dirs(self.ctx())

        assert not d.exists()
        assert result["removed"] >= 1
        assert result["bytes_reclaimed"] >= 2048

    async def test_sweeps_every_root_in_the_shared_tuple(self):
        """The reaper carried its own hardcoded copy of the root list, so the
        roots added after it was written (coverage/, gc_bias/ and the rest)
        were removed on delete but never reaped as orphans -- #787. Asserting
        an orphan dies under *every* root is what makes the two lists provably
        the same list.
        """
        from bson import ObjectId

        from app.queue.handlers import reap_report_dirs

        orphans = []
        for parent in report_dirs():
            d = parent / str(ObjectId())
            d.mkdir(parents=True, exist_ok=True)
            (d / "artifact.json").write_text("orphaned")
            self.age(d)
            orphans.append(d)

        await reap_report_dirs(self.ctx())

        for d in orphans:
            assert not d.exists(), f"unreaped {d}"

    async def test_keeps_a_directory_whose_object_still_exists(self):
        """The check that matters: a live object's Results must survive a sweep
        that is running specifically to delete directories like it."""
        from app.queue.handlers import reap_report_dirs
        from tests.services.helpers import make_object

        root = await project_service.create_project(name="reap-live", owner=TEST_OWNER)
        obj = await make_object(root, "live.vcf.gz")
        d = settings.vcf_stats_dir / str(obj.id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "variants.tsv").write_text("live")
        self.age(d)

        await reap_report_dirs(self.ctx())

        assert (d / "variants.tsv").read_text() == "live"

    async def test_spares_a_recent_orphan(self):
        """A directory is created before the compute job writes into it, so a
        just-made one may have no object row yet. The grace window covers it."""
        from bson import ObjectId

        from app.queue.handlers import reap_report_dirs

        fresh = settings.bam_stats_dir / str(ObjectId())
        fresh.mkdir(parents=True, exist_ok=True)

        await reap_report_dirs(self.ctx())

        assert fresh.exists()
        fresh.rmdir()

    async def test_ignores_entries_that_are_not_object_ids(self):
        from app.queue.handlers import reap_report_dirs

        stray = settings.qc_reports_dir / "not-an-object-id"
        stray.mkdir(parents=True, exist_ok=True)
        self.age(stray)

        await reap_report_dirs(self.ctx())

        assert stray.exists()
        stray.rmdir()

    async def test_logs_a_candidate_line_and_attribution_for_every_decision(self, monkeypatch):
        """Issue #10: the reaper previously logged only an aggregate count, so
        a wrongly-reaped directory left no trace of which object was checked,
        what the DB lookup returned, or why it was judged eligible. Every
        candidate -- reaped, spared as young, or spared as live -- must now
        produce a `report_dir_reap_candidate` line, and an actual removal must
        be attributed to the reaper by name."""
        from bson import ObjectId

        from app.queue import handlers as handlers_module
        from app.queue.handlers import reap_report_dirs
        from tests.services.helpers import make_object

        infos: list[tuple] = []
        monkeypatch.setattr(
            handlers_module.log, "info", lambda event, **kw: infos.append((event, kw))
        )
        monkeypatch.setattr(
            object_service.log, "info", lambda event, **kw: infos.append((event, kw))
        )

        root = await project_service.create_project(name="reap-attribution", owner=TEST_OWNER)
        live_obj = await make_object(root, "live.vcf.gz")

        gone = ObjectId()
        reaped_dir = settings.vcf_stats_dir / str(gone)
        reaped_dir.mkdir(parents=True, exist_ok=True)
        self.age(reaped_dir)

        live_dir = settings.vcf_stats_dir / str(live_obj.id)
        live_dir.mkdir(parents=True, exist_ok=True)
        self.age(live_dir)

        young = ObjectId()
        young_dir = settings.qc_reports_dir / str(young)
        young_dir.mkdir(parents=True, exist_ok=True)

        await reap_report_dirs(self.ctx())

        candidates = {
            kw["object_id"]: kw for event, kw in infos if event == "report_dir_reap_candidate"
        }
        assert candidates[str(gone)]["action"] == "reap"
        assert candidates[str(gone)]["db_lookup_result"] == "not_found"
        assert candidates[str(live_obj.id)]["action"] == "skip_live_object"
        assert candidates[str(live_obj.id)]["db_lookup_result"] == "found"
        assert candidates[str(young)]["action"] == "skip_too_young"

        removed = [
            kw
            for event, kw in infos
            if event == "report_dir_removed" and kw["object_id"] == str(gone)
        ]
        assert removed, "expected a report_dir_removed line for the reaped directory"
        assert removed[0]["caller"] == "reap_report_dirs"
        assert removed[0]["reason"] == "orphaned_no_db_record"

        young_dir.rmdir()
