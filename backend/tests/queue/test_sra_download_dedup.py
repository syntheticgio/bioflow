"""`_apply_sra_download` must not create a second file-list entry for an
accession the project already holds.

Job-level dedup (`sra_service.launch_download`'s `dedup_key`) only collapses a
second request while the first job is still in flight. Once that job has
finished, a second request for the same accession sails through -- the queue
sees no in-flight job to collide with -- and reaches this applier. Before this
fix, `_apply_sra_download` called `ingest_local_file` unconditionally, and
`ingest_local_file` dedups only the underlying blob bytes, never the
`DataObject` record -- so two downloads of the same run produced two visible
files, exactly the symptom in bug #81.
"""

import uuid
from pathlib import Path

import pytest
from app.config import settings
from app.models import DataObject, ObjectStatus
from app.queue.results import _apply_sra_download
from app.services import object_service, project_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch):
    """Stub the downstream QC/header enqueues; this test only cares about the
    object records _apply_sra_download writes, not what runs after."""

    async def _skip(*args, **kwargs):
        return ""

    monkeypatch.setattr(object_service, "enqueue_ingest", _skip)

    from app.queue import queue

    monkeypatch.setattr(queue, "enqueue", _skip)


_scratch_files: list[Path] = []


@pytest.fixture(autouse=True)
def _reclaim_scratch_files():
    _scratch_files.clear()
    yield
    for path in _scratch_files:
        path.unlink(missing_ok=True)
    _scratch_files.clear()


def _staged_file(name: str, content: bytes) -> Path:
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    path = settings.tmp_dir / f"sra-dedup-test-{uuid.uuid4().hex}-{name}"
    path.write_bytes(content)
    _scratch_files.append(path)
    return path


def _download_result(project_id, accession: str, staged: list[dict]) -> dict:
    return {
        "project_id": str(project_id),
        "accession": accession,
        "job_id": None,
        "platform": "ILLUMINA",
        "metadata": {"sra_run": accession},
        "staged": staged,
    }


class TestSraDownloadSkipsAlreadyPresentAccession:
    async def test_a_second_completed_download_does_not_create_a_second_object(self):
        project = await project_service.create_project(
            name="sra-dedup-test", owner="local"
        )

        first_path = _staged_file("acc.fastq", b"@acc.1\nACGT\n+\nIIII\n")
        await _apply_sra_download(
            _download_result(
                project.id,
                "ACC999",
                [{"path": str(first_path), "name": "acc.fastq", "mate": None}],
            ),
            owner="local",
        )

        objects_after_first = await DataObject.find(
            DataObject.project_id == project.id
        ).to_list()
        assert len(objects_after_first) == 1
        assert objects_after_first[0].status != ObjectStatus.ERROR

        # A second, independent completed download of the same accession --
        # the job-level dedup_key cannot catch this because the first job is
        # no longer in flight by the time this one lands.
        second_path = _staged_file("acc.fastq", b"@acc.1\nACGT\n+\nIIII\n")
        await _apply_sra_download(
            _download_result(
                project.id,
                "ACC999",
                [{"path": str(second_path), "name": "acc.fastq", "mate": None}],
            ),
            owner="local",
        )

        objects_after_second = await DataObject.find(
            DataObject.project_id == project.id
        ).to_list()
        assert len(objects_after_second) == 1, (
            "a second completed download of an accession already present "
            "must not add a second file-list entry"
        )

    async def test_a_different_accession_still_ingests_normally(self):
        """The skip is keyed on the accession, not the project -- a project
        that already holds one run must still accept a different one."""
        project = await project_service.create_project(
            name="sra-dedup-different-accession-test", owner="local"
        )

        first_path = _staged_file("acc1.fastq", b"@acc1.1\nACGT\n+\nIIII\n")
        await _apply_sra_download(
            _download_result(
                project.id,
                "ACC001",
                [{"path": str(first_path), "name": "acc1.fastq", "mate": None}],
            ),
            owner="local",
        )

        second_path = _staged_file("acc2.fastq", b"@acc2.1\nTTTT\n+\nIIII\n")
        await _apply_sra_download(
            _download_result(
                project.id,
                "ACC002",
                [{"path": str(second_path), "name": "acc2.fastq", "mate": None}],
            ),
            owner="local",
        )

        objects = await DataObject.find(DataObject.project_id == project.id).to_list()
        assert len(objects) == 2

    async def test_an_errored_prior_object_does_not_block_a_retry(self):
        """The frontend deliberately lets a user re-request an accession
        whose file was deleted or came in corrupted -- that recovery path
        must keep working. A non-READY record for the accession must not be
        treated as "already have it"."""
        project = await project_service.create_project(
            name="sra-dedup-errored-retry-test", owner="local"
        )

        stale = DataObject(
            project_id=project.id,
            owner="local",
            name="stale.fastq.gz",
            status=ObjectStatus.ERROR,
            metadata={"sra_run": "ACC777"},
        )
        await stale.insert()

        retry_path = _staged_file("acc.fastq", b"@acc.1\nACGT\n+\nIIII\n")
        await _apply_sra_download(
            _download_result(
                project.id,
                "ACC777",
                [{"path": str(retry_path), "name": "acc.fastq", "mate": None}],
            ),
            owner="local",
        )

        non_error = await DataObject.find(
            DataObject.project_id == project.id,
            DataObject.status != ObjectStatus.ERROR,
        ).to_list()
        assert len(non_error) == 1, "a retry after a prior error must still ingest"
