"""The transfer_annotation (Liftoff) handler execution and result applier."""

import types
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId

from app.config import settings
from app.models import ObjectRole
from app.queue import results
from app.queue.assembly_qc_handlers import transfer_annotation
from app.services import object_service, project_service, run_service


@pytest.fixture(autouse=True)
def _tmp_settings(tmp_path, monkeypatch):
    """Keep _prepare_workdir out of the real tmp/ tree (same convention as
    test_annotation_export_handler.py)."""
    from app.config import settings

    monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
    return settings


class _Ctx:
    """The parts of JobContext the handler touches."""

    def __init__(self, payload, job_id="test-transfer-job"):
        self.payload = payload
        self.job_id = job_id

    def check_cancel(self):
        return None

    def progress(self, **kw):
        return None

    def extend_lease(self, seconds):
        return None


def _fake_liftoff():
    return types.SimpleNamespace(available=True, path="liftoff", error=None, name="liftoff")


def test_transfer_annotation_handler_is_registered():
    """The handler is hand-registered in queue/registry.py; a missing entry
    means the launch enqueues a job no handler can run."""
    from app.queue.registry import get_handler

    assert get_handler("transfer_annotation") is not None


def test_transfer_annotation_handler_runs_liftoff(tmp_path, monkeypatch):
    """Liftoff is invoked with target before reference and the annotation via
    -g; the lifted GFF3 it writes becomes the handler's single output."""
    target = tmp_path / "target.fa"
    target.write_text(">t1\nACGTACGT\n")
    reference = tmp_path / "ref.fa"
    reference.write_text(">r1\nACGTACGT\n")
    annotation = tmp_path / "ref.gff3"
    annotation.write_text("##gff-version 3\n")

    produced = {}

    def _fake_run(ctx, cmd, log_path=None):
        # Liftoff writes the output; stand in for it here.
        out = Path(cmd[cmd.index("-o") + 1])
        out.write_text("##gff-version 3\nchr1\tliftoff\tgene\t1\t10\t.\t+\t.\tID=g1\n")
        produced["cmd"] = cmd
        return 0

    monkeypatch.setattr(
        "app.queue.assembly_qc_handlers.tools.liftoff", _fake_liftoff
    )
    monkeypatch.setattr(
        "app.queue.assembly_qc_handlers.run_subprocess", _fake_run
    )

    ctx = _Ctx(
        {
            "object_id": "target-obj-1",
            "target_path": str(target),
            "reference_path": str(reference),
            "annotation_path": str(annotation),
            "target_name": "myasm",
            "threads": 4,
            "copies": True,
        }
    )

    result = transfer_annotation(ctx)

    assert result["object_id"] == "target-obj-1"
    assert result["output"]["name"] == "myasm_liftoff.gff3"
    out = Path(result["output"]["tmp_path"])
    assert out.exists()
    assert "ID=g1" in out.read_text()

    cmd = produced["cmd"]
    assert cmd[0] == "liftoff"
    assert cmd[1] == str(target)  # target positional, first
    assert cmd[2] == str(reference)  # reference positional, second
    assert "-copies" in cmd  # copies flag present


@pytest.mark.usefixtures("beanie_models")
@pytest.mark.asyncio(loop_scope="module")
class TestTransferAnnotationApplier:
    """_apply_transfer_annotation must register the lifted GFF3 as a new
    ANNOTATION object deriving from the target assembly -- otherwise the
    transfer produces no object the explorer can open."""

    async def test_registers_lifted_annotation_deriving_from_target(
        self, tmp_path, monkeypatch
    ):
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        settings.sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        settings.sentinel_path.write_text("biopipe-home-v1\n")
        owner = "transfer-annotation-owner"

        async def _skip_ingest(obj, **kwargs):
            return ""

        async def _skip_enqueue(*args, **kwargs):
            return None

        monkeypatch.setattr(object_service, "enqueue_ingest", _skip_ingest)
        monkeypatch.setattr("app.queue.queue.enqueue", _skip_enqueue)

        project = await project_service.create_project(
            name=f"{owner}-project", owner=owner
        )
        target_path = tmp_path / f"target-{_uuid_hex()}.fa"
        target_path.write_text(">t1\nACGTACGT\n")
        target = await object_service.ingest_local_file(
            owner=owner,
            project_id=project.id,
            path=target_path,
            name="target.fa",
            role=None,
        )

        output_path = tmp_path / f"lifted-{_uuid_hex()}.gff3"
        output_path.write_text(
            "##gff-version 3\nchr1\tliftoff\tgene\t1\t10\t.\t+\t.\tID=g1\n"
        )

        monkeypatch.setattr(run_service, "run_for_job", AsyncMock(return_value=None))
        monkeypatch.setattr(run_service, "record_outputs", AsyncMock())

        await results._apply_transfer_annotation(
            {
                "object_id": str(target.id),
                "job_id": str(PydanticObjectId()),
                "output": {"tmp_path": str(output_path), "name": "lifted.gff3"},
            },
            owner=owner,
        )

        lifted = [
            o
            for o in await object_service.list_objects(project.id, owner=owner)
            if o.role is ObjectRole.ANNOTATION
        ]
        assert len(lifted) == 1
        # The store gzip-compresses on ingest; the explorer decompresses.
        assert lifted[0].name == "lifted.gff3.gz"
        assert lifted[0].derived_from == [target.id]
        assert lifted[0].facts == {}


def _uuid_hex():
    return uuid.uuid4().hex
