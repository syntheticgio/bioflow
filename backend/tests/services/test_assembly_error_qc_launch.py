"""launch_assembly_error_qc's payload key contract with its handler.

The handler (`app/queue/assembly_qc_handlers.py`) resolves each BAM's index
via `_link_bam_index(ctx.payload, "ngs_bai", ...)` / `(..., "sms_bai", ...)`,
which reads `payload["ngs_bai_sha256"]`/`payload["ngs_bai_path"]` (and the sms
equivalents) -- NOT `f"{prefix}_bai_*"` where prefix is the BAM's own
`"ngs_bam"`/`"sms_bam"` key. A prior version of this launch wrote
`ngs_bam_bai_sha256`/`sms_bam_bai_sha256`, which the handler never reads: it
falls through to guessing a path next to the raw BAM, which cannot work for
BioFlow's content-addressed storage, and raises PermanentError for every
BioFlow-produced BAM -- the primary case this feature exists to handle.

This test locks the payload's BAI keys to the literal strings the handler
reads, so a future refactor that renames one side without the other fails
loudly instead of only failing at runtime against a real BAM.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.models import FormatKind, ObjectStatus, SidecarRole
from app.pipelines.tools import Tool
from app.services import pipeline_service
from beanie import PydanticObjectId

_CRAQ = Tool(name="craq", path="/usr/local/bin/craq", version="1.0.9")


def _obj(*, name, kind=FormatKind.BAM, status=ObjectStatus.READY, project_id=None,
         derived_from=None, owner="local", role=None):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name=name,
        format=SimpleNamespace(kind=kind),
        status=status,
        facts={},
        project_id=project_id or PydanticObjectId(),
        owner=owner,
        derived_from=derived_from or [],
        blob_sha256="a" * 64,
        role=role,
    )


def _assembly():
    return _obj(name="draft.fasta", kind=FormatKind.FASTA)


def _bam(assembly, *, name="reads.bam"):
    return _obj(name=name, kind=FormatKind.BAM, derived_from=[assembly.id],
                project_id=assembly.project_id)


def _bai_sidecar(bam):
    return _obj(name=f"{bam.name}.bai", kind=FormatKind.UNKNOWN, project_id=bam.project_id)


async def _run(*, assembly, ngs_bam=None, sms_bam=None, sidecars_by_bam):
    objects = {o.id: o for o in [assembly, ngs_bam, sms_bam] if o is not None}

    async def _get_object(object_id, *, owner):
        return objects[object_id]

    async def _list_sidecars(object_id, *, owner):
        for bam, sidecars in sidecars_by_bam:
            if bam.id == object_id:
                return sidecars
        return []

    enqueued = {}

    async def _enqueue(job_type, **kwargs):
        enqueued["type"] = job_type
        enqueued.update(kwargs)
        return SimpleNamespace(id="job1")

    with (
        patch("app.pipelines.tools.craq", return_value=_CRAQ),
        patch("app.services.object_service.get_object", AsyncMock(side_effect=_get_object)),
        patch(
            "app.services.object_service.list_sidecars",
            AsyncMock(side_effect=_list_sidecars),
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("a" * 64, None)),
        ),
        patch("app.queue.queue.enqueue", _enqueue),
    ):
        job = await pipeline_service.launch_assembly_error_qc(
            object_id=assembly.id,
            owner="local",
            ngs_bam_id=ngs_bam.id if ngs_bam else None,
            sms_bam_id=sms_bam.id if sms_bam else None,
        )
    return job, enqueued


class TestBaiPayloadKeysMatchWhatTheHandlerReads:
    async def test_ngs_bam_bai_keys_are_exactly_ngs_bai_prefixed(self):
        assembly = _assembly()
        ngs_bam = _bam(assembly, name="ngs.bam")
        bai = _bai_sidecar(ngs_bam)
        bai.sidecar_role = SidecarRole.BAI

        job, enqueued = await _run(
            assembly=assembly, ngs_bam=ngs_bam, sidecars_by_bam=[(ngs_bam, [bai])],
        )

        payload = enqueued["payload"]
        assert payload["ngs_bai_sha256"] == "a" * 64
        assert "ngs_bam_bai_sha256" not in payload
        assert "ngs_bam_bai_path" not in payload

        # The BAM's own keys are unaffected -- still prefixed "ngs_bam".
        assert payload["ngs_bam_sha256"] == "a" * 64
        assert payload["ngs_bam_object_id"] == str(ngs_bam.id)

    async def test_sms_bam_bai_keys_are_exactly_sms_bai_prefixed(self):
        assembly = _assembly()
        sms_bam = _bam(assembly, name="sms.bam")
        bai = _bai_sidecar(sms_bam)
        bai.sidecar_role = SidecarRole.BAI

        job, enqueued = await _run(
            assembly=assembly, sms_bam=sms_bam, sidecars_by_bam=[(sms_bam, [bai])],
        )

        payload = enqueued["payload"]
        assert payload["sms_bai_sha256"] == "a" * 64
        assert "sms_bam_bai_sha256" not in payload
        assert "sms_bam_bai_path" not in payload

        assert payload["sms_bam_sha256"] == "a" * 64
        assert payload["sms_bam_object_id"] == str(sms_bam.id)

    async def test_both_bams_get_their_own_bai_prefixed_keys(self):
        assembly = _assembly()
        ngs_bam = _bam(assembly, name="ngs.bam")
        sms_bam = _bam(assembly, name="sms.bam")
        ngs_bai = _bai_sidecar(ngs_bam)
        ngs_bai.sidecar_role = SidecarRole.BAI
        sms_bai = _bai_sidecar(sms_bam)
        sms_bai.sidecar_role = SidecarRole.BAI

        job, enqueued = await _run(
            assembly=assembly,
            ngs_bam=ngs_bam,
            sms_bam=sms_bam,
            sidecars_by_bam=[(ngs_bam, [ngs_bai]), (sms_bam, [sms_bai])],
        )

        payload = enqueued["payload"]
        assert payload["ngs_bai_sha256"] == "a" * 64
        assert payload["sms_bai_sha256"] == "a" * 64
        assert enqueued["type"] == "assess_assembly_errors"
