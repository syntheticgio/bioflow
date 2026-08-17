"""The chunked alignment launch must attribute its job to the reads it aligns.

`/jobs?object_id=...` is how every "is anything running on this file?" surface
in the UI answers the question -- the Actions tab's Launch buttons, the
detail panel's QC guard, the active-jobs indicator. A job enqueued without
`object_id` is invisible to all of them: the alignment runs for an hour while
the file's buttons sit enabled, claiming nothing is in flight.

The single-shot path has always passed `object_id=primary_set.r1.id`. The
chunked path -- taken when a reference is too large to index in one piece --
did not, so exactly the long-running alignments most worth showing as busy
were the ones that reported nothing.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from beanie import PydanticObjectId

from app.models import FormatKind, ObjectStatus
from app.services import memory_estimate, pipeline_service


def _memory_estimate(mb: int) -> memory_estimate.MemoryEstimate:
    return memory_estimate.MemoryEstimate(
        mb=mb,
        source=memory_estimate.EstimateSource.HEURISTIC,
        detail="from published tool coefficients",
    )


def _fastq_object(object_id, name, *, project_id):
    return SimpleNamespace(
        id=object_id,
        name=name,
        format=SimpleNamespace(kind=FormatKind.FASTQ),
        role=None,
        facts={},
        metadata={},
        status=ObjectStatus.READY,
        project_id=project_id,
        owner="local",
        size=1_000_000,
    )


class TestChunkedAlignmentAttribution:
    async def test_chunked_job_carries_the_reads_object_id(self, tmp_path):
        """Enqueued with object_id so the file's Launch buttons see it."""
        project_id = PydanticObjectId()
        primary = _fastq_object(
            PydanticObjectId(), "sample_R1.fastq", project_id=project_id
        )
        reference = SimpleNamespace(
            id=PydanticObjectId(),
            name="ref.fasta",
            format=SimpleNamespace(kind=FormatKind.FASTA),
            role=None,
            facts={},
            status=ObjectStatus.READY,
            project_id=project_id,
            owner="local",
            size=3_000_000_000,
            blob_sha256="abc123",
        )

        enqueued: dict = {}

        async def _enqueue(job_type, **kwargs):
            enqueued["type"] = job_type
            enqueued.update(kwargs)
            return SimpleNamespace(id=PydanticObjectId())

        async def _get_object(object_id, owner):
            for obj in (primary, reference):
                if obj.id == object_id:
                    return obj
            raise AssertionError(f"unexpected object id {object_id}")

        fasta_path = tmp_path / "ref.fasta"
        fasta_path.write_text(">chr1\nACGT\n")

        # Two buckets, so pack_buckets' "one bucket falls through to the
        # single-shot path" escape is not taken and the chunked enqueue below
        # is genuinely the one under test.
        buckets = [
            SimpleNamespace(
                index=0,
                sequences=["chr1"],
                total_bases=100,
                estimated_mb=10,
                fasta_path=tmp_path / "b0.fa",
            ),
            SimpleNamespace(
                index=1,
                sequences=["chr2"],
                total_bases=100,
                estimated_mb=10,
                fasta_path=tmp_path / "b1.fa",
            ),
        ]

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=_get_object),
            ),
            patch(
                "app.services.pipeline_service.reference_index_status",
                AsyncMock(return_value={"minimap2": True, "fai": True}),
            ),
            patch(
                "app.services.memory_estimate.resolve",
                AsyncMock(return_value=_memory_estimate(1024)),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(side_effect=lambda obj: (None, f"/data/{obj.name}")),
            ),
            patch(
                "app.services.pipeline_service.sidecar_payload",
                AsyncMock(return_value={}),
            ),
            patch(
                "app.services.pipeline_service._parse_fai",
                lambda _p: [("chr1", 100), ("chr2", 100)],
            ),
            patch("pathlib.Path.exists", lambda self: True),
            patch(
                "app.services.pipeline_service.blob_path",
                lambda _sha: fasta_path,
            ),
            patch("app.pipelines.align_buckets.pack_buckets", lambda **_kw: buckets),
            patch(
                "app.pipelines.align_buckets.write_bucket_fastas",
                lambda _f, b, _c: b,
            ),
            patch(
                "app.services.run_service.create_run",
                AsyncMock(return_value=SimpleNamespace(id="run1", owner="local")),
            ),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            await pipeline_service.launch_alignment(
                object_id=primary.id,
                reference_id=reference.id,
                owner="local",
                paired=False,
                params={"chunked": True},
            )

        assert enqueued["type"] == "align_reads_chunked"
        assert enqueued.get("object_id") == primary.id
        # project_id travels with it for the same reason: the project's own
        # activity view filters on it.
        assert enqueued.get("project_id") == project_id
