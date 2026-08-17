"""`_apply_sra_download` sets read_number alongside mate_object_id.

Before this, the SRA path linked mate_object_id but never set read_number --
`_link_mate` sets both together, but the SRA path bypasses `_link_mate` because
fasterq-dump's own R1/R2 labelling is more authoritative than an inference.
That labelling was thrown away instead of being written down.
"""

import uuid
from pathlib import Path

import pytest
from app.config import settings
from app.models import DataObject
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
    path = settings.tmp_dir / f"sra-mate-test-{uuid.uuid4().hex}-{name}"
    path.write_bytes(content)
    _scratch_files.append(path)
    return path


class TestSraDownloadSetsReadNumber:
    async def test_paired_run_gets_read_numbers_1_and_2(self):
        project = await project_service.create_project(name="sra-mate-test", owner="local")

        r1_path = _staged_file("acc_1.fastq", b"@acc.1\nACGT\n+\nIIII\n")
        r2_path = _staged_file("acc_2.fastq", b"@acc.1\nACGT\n+\nIIII\n")

        result = {
            "project_id": str(project.id),
            "accession": "ACC123",
            "job_id": None,
            "platform": "ILLUMINA",
            "metadata": {},
            "staged": [
                {"path": str(r1_path), "name": "acc_1.fastq", "mate": "R1"},
                {"path": str(r2_path), "name": "acc_2.fastq", "mate": "R2"},
            ],
        }

        await _apply_sra_download(result, owner="local")

        # .gz: FASTQ compresses at ingest -- see docs/superpowers/specs/
        # 2026-08-05-object-compression-design.md. Each mate keeps its own
        # DataObject even though the two fixtures' identical bytes compress
        # to one shared Blob underneath (same as pre-compression dedup on
        # identical raw content -- one record per file, one blob for both).
        r1 = await DataObject.find_one(
            DataObject.project_id == project.id, DataObject.name == "acc_1.fastq.gz"
        )
        r2 = await DataObject.find_one(
            DataObject.project_id == project.id, DataObject.name == "acc_2.fastq.gz"
        )

        assert r1.mate_object_id == r2.id
        assert r2.mate_object_id == r1.id
        assert r1.read_number == 1
        assert r2.read_number == 2
