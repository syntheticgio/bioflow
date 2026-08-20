"""Launch-time wiring for transfer_annotation (Liftoff): the enqueue payload,
reference resolution, and the declared-budget refusal."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.errors import ValidationError
from app.models import FormatKind, ObjectRole, ObjectStatus
from app.services import pipeline_service


def _target(organism="Saccharomyces cerevisiae"):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name="myasm.fa",
        format=SimpleNamespace(kind=FormatKind.FASTA),
        role=None,
        metadata={"organism": organism},
        facts={},
        status=ObjectStatus.READY,
        project_id=PydanticObjectId(),
        owner="local",
    )


def _reference(project_id, owner):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name="ref.fa",
        format=SimpleNamespace(kind=FormatKind.FASTA),
        role=ObjectRole.REFERENCE,
        metadata={},
        facts={},
        status=ObjectStatus.READY,
        project_id=project_id,
        owner=owner,
    )


def _annotation(project_id, owner):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name="ref.gff3",
        format=SimpleNamespace(kind=FormatKind.GFF),
        role=ObjectRole.ANNOTATION,
        metadata={},
        facts={},
        status=ObjectStatus.READY,
        project_id=project_id,
        owner=owner,
        derived_from=[],
    )


def _available_liftoff():
    return SimpleNamespace(available=True, path="liftoff", name="liftoff", error=None)


class TestTransferAnnotationLaunch:
    async def test_launch_reaches_the_queue_with_resolved_reference(self):
        """reference_id=None resolves the project's single REFERENCE FASTA and
        the launch reaches the queue with the right payload and dedup key."""
        obj = _target()
        ref = _reference(obj.project_id, obj.owner)
        ann = _annotation(obj.project_id, obj.owner)

        enqueued = {}

        async def _enqueue(job_type, **kwargs):
            enqueued["type"] = job_type
            enqueued.update(kwargs)
            return SimpleNamespace(id=PydanticObjectId())

        with (
            patch("app.services.object_service.get_object", AsyncMock(return_value=obj)),
            patch("app.services.object_service.list_objects", AsyncMock(return_value=[ref])),
            patch(
                "app.services.pipeline_service.resolve_annotation",
                AsyncMock(return_value=ann),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=("a" * 64, None)),
            ),
            patch("app.pipelines.tools.liftoff", _available_liftoff),
            patch(
                "app.services.pipeline_service.current_admission_budget_mb",
                AsyncMock(return_value=10_000_000),
            ),
            patch(
                "app.services.run_service.create_run",
                AsyncMock(return_value=SimpleNamespace(id="run1", owner="local")),
            ),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            await pipeline_service.launch_transfer_annotation(
                object_id=obj.id,
                owner="local",
                reference_id=None,
                resource_override=True,
            )

        assert enqueued["type"] == "transfer_annotation"
        assert enqueued["resource_override"] is True
        payload = enqueued["payload"]
        assert payload["object_id"] == str(obj.id)
        # Inputs are resolved by digest/path, not object id (the handler
        # resolves them via _resolve_input). sha256 here because the patched
        # _resolve_readable returns a digest.
        assert payload["reference_sha256"] == "a" * 64
        assert payload["annotation_sha256"] == "a" * 64
        assert payload["target_name"] == "myasm.fa"
        # dedup key keeps a re-launch from double-queueing.
        assert "dedup_key" in enqueued

    async def test_launch_refuses_when_declared_budget_exceeds_admission(self):
        """The declared 16 GiB reservation refuses against a smaller admission
        budget unless the user overrides -- matching launch_annotate_genome."""
        obj = _target()

        with (
            patch("app.services.object_service.get_object", AsyncMock(return_value=obj)),
            patch("app.pipelines.tools.liftoff", _available_liftoff),
            patch(
                "app.services.pipeline_service.current_admission_budget_mb",
                AsyncMock(return_value=1000),
            ),
            patch("app.queue.queue.enqueue", AsyncMock()),
        ):
            with pytest.raises(ValidationError):
                await pipeline_service.launch_transfer_annotation(
                    object_id=obj.id,
                    owner="local",
                    reference_id=None,
                )
