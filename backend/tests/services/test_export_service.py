import json
import tarfile

import pytest

from app.config import settings
from app.models import Blob, BlobState, JobRunTiming, RunKind, SourceInfo, SourceMode
from app.models.timing import RunMachine
from app.services import export_service, run_service
from tests.services.helpers import TEST_OWNER, make_blob, make_object, make_project


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

    async def test_includes_project_scoped_timing_with_no_object(self):
        """A job that never attached to one object still ran.

        executor.py records object_id only when the job has one, so
        project-level work carries project_id alone. Keying the query on
        object_id dropped it from every archive, and nothing in the
        manifest could say so -- the absence read as "this project ran
        nothing".
        """
        project = await make_project("export-collect-project-timing")
        await JobRunTiming(
            job_type="export",
            input_bytes=1_000,
            job_id="j-project-scoped",
            duration_ms=5_000,
            object_id=None,
            project_id=str(project.id),
        ).insert()

        bundle = await export_service.collect(project.id, owner=TEST_OWNER)

        assert [t.job_id for t in bundle.timings] == ["j-project-scoped"]

    async def test_includes_project_scoped_timing_from_a_descendant(self):
        parent = await make_project("export-collect-timing-parent")
        child = await make_project("export-collect-timing-child", parent)
        await JobRunTiming(
            job_type="export",
            input_bytes=1_000,
            job_id="j-child-scoped",
            duration_ms=5_000,
            project_id=str(child.id),
        ).insert()

        bundle = await export_service.collect(parent.id, owner=TEST_OWNER)

        assert [t.job_id for t in bundle.timings] == ["j-child-scoped"]

    async def test_a_timing_matching_both_keys_appears_once(self):
        """The $or must not turn one run into two rows in the archive."""
        project = await make_project("export-collect-timing-both")
        obj = await make_object(project, "reads.fastq")
        await JobRunTiming(
            job_type="align",
            input_bytes=1_000,
            job_id="j-both-keys",
            duration_ms=5_000,
            object_id=str(obj.id),
            project_id=str(project.id),
        ).insert()

        bundle = await export_service.collect(project.id, owner=TEST_OWNER)

        assert [t.job_id for t in bundle.timings] == ["j-both-keys"]

    async def test_excludes_another_projects_timing(self):
        project = await make_project("export-collect-timing-mine")
        other = await make_project("export-collect-timing-theirs")
        await JobRunTiming(
            job_type="export",
            input_bytes=1_000,
            job_id="j-not-mine",
            duration_ms=5_000,
            project_id=str(other.id),
        ).insert()

        bundle = await export_service.collect(project.id, owner=TEST_OWNER)

        assert bundle.timings == []


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
        id="b" * 64,
        size=10_000,
        state=BlobState.PRESENT,
        rel_path="cd/large",
        content_sha256="b" * 64,
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


@pytest.mark.usefixtures("beanie_models")
@pytest.mark.asyncio(loop_scope="module")
class TestRenderReport:
    async def test_names_the_project_and_its_description(self):
        project = await make_project("export-report-ecoli")
        project.description = "Nanopore run"
        await project.save()
        bundle = export_service.ExportBundle(root=project, projects=[project])

        report = await export_service.render_report(bundle, owner=TEST_OWNER)

        assert project.name in report
        assert "Nanopore run" in report

    async def test_states_the_archive_is_not_importable(self):
        project = await make_project("export-report-not-importable")
        bundle = export_service.ExportBundle(root=project, projects=[project])

        report = await export_service.render_report(bundle, owner=TEST_OWNER)

        assert "cannot be imported" in report.lower()

    async def test_includes_each_object_with_its_provenance(self):
        project = await make_project("export-report-objects")
        obj = await make_object(project, "reads.fastq.gz")
        bundle = export_service.ExportBundle(
            root=project, projects=[project], objects=[obj]
        )

        report = await export_service.render_report(bundle, owner=TEST_OWNER)

        assert obj.name in report
        assert "## Provenance" in report

    async def test_includes_run_history(self):
        project = await make_project("export-report-runs")
        run = await run_service.create_run(
            kind=RunKind.ALIGNMENT,
            project_id=project.id,
            label="reads -> ref",
            inputs=[],
            params={},
            owner=TEST_OWNER,
        )
        bundle = export_service.ExportBundle(root=project, projects=[project], runs=[run])

        report = await export_service.render_report(bundle, owner=TEST_OWNER)

        assert "## Run history" in report
        assert run.label in report

    async def test_lists_sub_projects(self):
        parent = await make_project("export-report-parent")
        child = await make_project("export-report-child", parent)
        # `projects` is deliberately given in child-before-parent order --
        # the same shape a Mongo `$in` query can return, since `$in` does
        # not preserve array order. Sub-projects must be identified by
        # `root.id`, not by list position, or this test would pass by luck.
        bundle = export_service.ExportBundle(root=parent, projects=[child, parent])

        report = await export_service.render_report(bundle, owner=TEST_OWNER)

        assert "## Sub-projects" in report
        assert child.name in report
        assert parent.name not in report.split("## Sub-projects")[1]


@pytest.mark.usefixtures("beanie_models")
@pytest.mark.asyncio(loop_scope="module")
class TestExportProject:
    async def test_archive_contains_the_expected_members(self):
        project = await make_project("export-archive-members")

        result = await export_service.export_project(project.id, owner=TEST_OWNER)

        with tarfile.open(result.path) as tar:
            names = set(tar.getnames())
        assert {"manifest.json", "data-manifest.tsv", "report.md", "README.md"} <= names
        assert any(n.startswith("metadata/") for n in names)

    async def test_manifest_json_carries_the_version_envelope(self):
        project = await make_project("export-archive-envelope")

        result = await export_service.export_project(project.id, owner=TEST_OWNER)

        with tarfile.open(result.path) as tar:
            manifest = json.loads(tar.extractfile("manifest.json").read())
        assert manifest["bioflow_export_version"] == export_service.BIOFLOW_EXPORT_VERSION
        assert manifest["redaction_profile"] == "secrets+paths+machine"

    async def test_objectids_are_preserved_for_a_future_importer(self):
        project = await make_project("export-archive-objectids")

        result = await export_service.export_project(project.id, owner=TEST_OWNER)

        with tarfile.open(result.path) as tar:
            docs = json.loads(tar.extractfile("metadata/projects.json").read())
        assert docs[0]["_id"] == str(project.id)

    async def test_a_project_scoped_timing_reaches_job_timings_json(self):
        """End-to-end form of the collect() fix, at the archive boundary.

        A project-level run has no object_id to key off; before #538 it was
        absent from the archive with nothing saying it had been left out.
        """
        project = await make_project("export-archive-project-timing")
        await JobRunTiming(
            job_type="export",
            input_bytes=1_000,
            job_id="j-archive-project-scoped",
            duration_ms=7_000,
            object_id=None,
            project_id=str(project.id),
        ).insert()

        result = await export_service.export_project(project.id, owner=TEST_OWNER)

        with tarfile.open(result.path) as tar:
            docs = json.loads(tar.extractfile("metadata/job_timings.json").read())
        assert [d["job_id"] for d in docs] == ["j-archive-project-scoped"]
        assert docs[0]["duration_ms"] == 7_000

    async def test_archive_contains_no_secrets_no_paths_no_machine_names(self):
        """The assertion that outlives whoever wrote the exporter.

        Greps the whole produced archive. This is what keeps the redaction
        rule true after someone edits export_service.py a year from now.

        The blob carrying the secret path must be reachable from the project
        (referenced by an object) -- collect() only pulls in blobs that
        objects point to, so an orphan Blob().insert() would never enter the
        bundle at all and the grep below would pass vacuously.
        """
        project = await make_project("export-archive-redaction")
        digest = "d" * 64
        await make_blob(digest)
        blob = await Blob.get(digest)
        blob.external_path = "/Users/gio/private/reads.fastq"
        blob.state = BlobState.PRESENT
        await blob.save()
        obj = await make_object(project, "reads.fastq", digest=digest)

        # C1: DataObject.source.original_path is a second, nested path field
        # (SourceInfo, set for every register-in-place object by
        # object_service.py) that _strip_paths must also clear -- the
        # top-level-only version of this function was a complete no-op for
        # objects.
        obj.source = SourceInfo(
            mode=SourceMode.REGISTER_IN_PLACE,
            original_path="/Users/gio/private/other-reads.fastq",
        )
        await obj.save()

        # C2: JobRunTiming.worker_id defaults to
        # f"{socket.gethostname()}:{os.getpid()}" (queue/worker.py) and is a
        # sibling of `machine` -- both are machine-identity facts, and
        # redact() must clear both, not just `machine`.
        await JobRunTiming(
            job_type="align",
            input_bytes=1_000_000,
            job_id="j-redaction",
            duration_ms=1_000,
            object_id=str(obj.id),
            machine=RunMachine(machine_id="gio-workstation.local"),
            worker_id="gio-workstation.local:4242",
        ).insert()

        result = await export_service.export_project(project.id, owner=TEST_OWNER)

        with tarfile.open(result.path) as tar:
            blob = b""
            for member in tar.getmembers():
                if member.isfile():
                    blob += tar.extractfile(member).read()

        for forbidden in (
            b"/Users/gio",
            b"secret.key",
            b"fernet",
            b"ai_providers",
            b"gio-workstation",
        ):
            assert forbidden.lower() not in blob.lower(), f"{forbidden!r} leaked"


def test_export_launcher_is_excluded_from_node_types():
    """Export is a project-level operation, not a pipeline node.

    node_types.py asserts every launch_* is either a NodeTypeSpec or
    explicitly excluded. #355 added both for one launcher in separate
    commits, satisfying the test its issue named while silently failing
    test_no_launcher_is_both_used_and_excluded in the same class.

    Compares against spec.launch_name (the "module.function_name" string),
    not spec.launch (the callable) -- a string can never be "in" a set of
    function objects, so comparing against spec.launch would make this
    assertion vacuously true regardless of whether the exclusion is correct.
    """
    from app.pipelines.node_types import EXCLUDED_LAUNCHES, NODE_TYPES

    name = "pipeline_service.launch_project_export"
    assert name in EXCLUDED_LAUNCHES
    launch_names = {spec.launch_name for spec in NODE_TYPES.values()}
    assert name not in launch_names


@pytest.mark.usefixtures("beanie_models")
@pytest.mark.asyncio(loop_scope="module")
class TestLaunchProjectExport:
    """`launch_project_export`'s own enqueue contract.

    Nothing else touches this launcher directly -- the exhaustiveness test
    above only checks node_types.py's registry, not that the function
    actually queues a job. That gap is exactly what let a missing
    `owner=owner` kwarg on the `queue.enqueue` call through: every call
    raised `TypeError: enqueue() missing 1 required keyword-only argument:
    'owner'` and nothing caught it.
    """

    @pytest.fixture(autouse=True)
    def _no_redis(self, monkeypatch):
        """Keep the Mongo insert and stub the rest, same as test_queue_owner.py.

        This process has no live Redis; `enqueue` writes Mongo first (what
        these tests actually check) and only then pushes to Redis and
        publishes an event, both of which are stubbed out here.
        """
        from app.queue import queue

        async def _skip(*args, **kwargs):
            return None

        monkeypatch.setattr(queue, "_push_to_redis", _skip)
        monkeypatch.setattr(queue, "publish_event", _skip)

    async def test_queues_a_job_for_the_project(self):
        from app.models import Job, JobClass
        from app.services import pipeline_service

        project = await make_project("export-launch")

        job = await pipeline_service.launch_project_export(
            project_id=project.id, owner=TEST_OWNER
        )

        assert isinstance(job, Job)
        assert job.project_id == project.id
        assert job.job_class == JobClass.USER_BACKGROUND
        assert job.type == "project_export"
        assert job.payload["project_id"] == str(project.id)
        assert job.payload["owner"] == TEST_OWNER
        assert job.payload["threshold_bytes"] == export_service.DEFAULT_BLOB_THRESHOLD_BYTES

    async def test_a_custom_threshold_is_passed_through(self):
        from app.services import pipeline_service

        project = await make_project("export-launch-threshold")

        job = await pipeline_service.launch_project_export(
            project_id=project.id, owner=TEST_OWNER, threshold_bytes=1234
        )

        assert job.payload["threshold_bytes"] == 1234

    async def test_a_wrong_owner_is_treated_as_not_found(self):
        from app.errors import NotFoundError
        from app.services import pipeline_service

        project = await make_project("export-launch-wrong-owner")

        with pytest.raises(NotFoundError):
            await pipeline_service.launch_project_export(
                project_id=project.id, owner="someone-else"
            )
