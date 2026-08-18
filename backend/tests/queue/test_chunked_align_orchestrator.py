"""The chunked orchestrator's fan-out, and what the applier does with the merge.

Two pieces, both untested before #584. `align_reads_chunked` splits one
alignment into one `align_reads` sub-job per reference bucket and waits for all
of them; `apply_chunked_alignment` turns the merged BAM into a DataObject and
chains the post-alignment pipeline.

The orchestrator's fan-out is where a bucket goes missing at the *start* rather
than the end: a sub-job that inherits the parent's `bucket_specs`, or the wrong
`reference_path`, aligns against something other than its own slice, and the
merged BAM is wrong in a way no later step can detect. `_POLL_SECONDS` is
patched to zero throughout -- these tests are about which jobs get enqueued and
which outcomes end the wait, not about wall-clock polling.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.errors import RetryableError
from app.models import JobState
from app.queue import chunked_align_handlers as mod
from app.queue.registry import JobContext


class _Ctx(JobContext):
    def __init__(self, payload):
        self.job_id = "parent584"
        self.payload = payload
        self.epoch = 1
        self.attempts = 1
        self.owner = "local"
        self.progress_calls: list[dict] = []

    def progress(self, **kwargs):
        self.progress_calls.append(kwargs)

    def check_cancel(self):
        return None


def _payload(n=3, **extra):
    payload = {
        "bucket_specs": [
            {"index": i, "fasta_path": f"/refs/bucket{i}.fasta"} for i in range(n)
        ],
        "reads_object_id": str(PydanticObjectId()),
        "reference_id": str(PydanticObjectId()),
        "project_id": str(PydanticObjectId()),
        "aligner": "bwa-mem2",
        "params": {"threads": 4},
        "owner": "local",
        "output_name": "sample.bam",
    }
    payload.update(extra)
    return payload


@pytest.fixture
def orchestrate(monkeypatch):
    """Run align_reads_chunked with instant polling and scripted sub-job states."""
    monkeypatch.setattr(mod, "_POLL_SECONDS", 0)
    enqueued: list[dict] = []

    async def _enqueue(job_type, *, payload=None, owner=None, parent_job_id=None, **kw):
        jid = str(PydanticObjectId())
        enqueued.append(
            {"type": job_type, "payload": payload or {}, "owner": owner,
             "parent_job_id": parent_job_id, "id": jid}
        )
        return jid

    monkeypatch.setattr("app.queue.queue.enqueue", _enqueue)

    async def _run(states, payload=None):
        """states: a list of JobState per sub-job, in enqueue order."""

        async def _get(job_id):
            idx = next(
                (i for i, e in enumerate(enqueued) if e["id"] == str(job_id)), None
            )
            if idx is None or idx >= len(states):
                return None
            return SimpleNamespace(id=job_id, state=states[idx])

        ctx = _Ctx(payload or _payload())
        with patch.object(mod.Job, "get", AsyncMock(side_effect=_get)):
            result = await mod.align_reads_chunked(ctx)
        return result, enqueued, ctx

    return _run


class TestFanOut:
    async def test_one_sub_job_per_bucket(self, orchestrate):
        _, enqueued, _ = await orchestrate([JobState.SUCCEEDED] * 3)
        assert [e["type"] for e in enqueued] == ["align_reads"] * 3

    async def test_each_sub_job_aligns_against_its_own_bucket(self, orchestrate):
        """The whole point of the split. Two sub-jobs pointed at one bucket
        would leave the other slice of the reference unaligned, and the merged
        BAM would still look complete."""
        _, enqueued, _ = await orchestrate([JobState.SUCCEEDED] * 3)
        assert [e["payload"]["reference_path"] for e in enqueued] == [
            "/refs/bucket0.fasta", "/refs/bucket1.fasta", "/refs/bucket2.fasta",
        ]
        assert [e["payload"]["chunk_bucket_index"] for e in enqueued] == [0, 1, 2]

    async def test_sub_jobs_do_not_inherit_the_bucket_specs(self, orchestrate):
        """A sub-job carrying bucket_specs would be indistinguishable from a
        chunked launch, and the planner could fan out again from a leaf."""
        _, enqueued, _ = await orchestrate([JobState.SUCCEEDED] * 3)
        assert all("bucket_specs" not in e["payload"] for e in enqueued)

    async def test_every_sub_job_is_parented_to_the_orchestrator(self, orchestrate):
        """Cancellation and the provenance walk both follow this edge."""
        _, enqueued, _ = await orchestrate([JobState.SUCCEEDED] * 3)
        assert all(e["parent_job_id"] == "parent584" for e in enqueued)
        assert all(e["payload"]["parent_job_id"] == "parent584" for e in enqueued)

    async def test_each_sub_job_knows_the_total(self, orchestrate):
        _, enqueued, _ = await orchestrate([JobState.SUCCEEDED] * 3)
        assert all(e["payload"]["chunk_total_buckets"] == 3 for e in enqueued)

    async def test_a_launch_with_no_buckets_is_rejected(self, orchestrate):
        from app.errors import PermanentError

        with pytest.raises(PermanentError, match="bucket_specs"):
            await orchestrate([], _payload(0))


class TestWaitingForBuckets:
    async def test_all_succeeded_returns_the_merge_inputs(self, orchestrate):
        result, enqueued, _ = await orchestrate([JobState.SUCCEEDED] * 3)
        assert result["bucket_count"] == 3
        assert result["sub_job_ids"] == [e["id"] for e in enqueued]

    async def test_the_result_carries_what_the_merge_needs(self, orchestrate):
        """The applier reads these off the result to build the merge payload;
        a missing one strands the alignment between the buckets and the BAM."""
        payload = _payload()
        result, _, _ = await orchestrate([JobState.SUCCEEDED] * 3, payload)
        assert result["reads_object_id"] == payload["reads_object_id"]
        assert result["reference_id"] == payload["reference_id"]
        assert result["project_id"] == payload["project_id"]
        assert result["aligner"] == "bwa-mem2"
        assert result["output_name"] == "sample.bam"

    async def test_one_failed_bucket_fails_the_whole_alignment(self, orchestrate):
        """Not "merge what we have": a bucket is a slice of the reference, so a
        partial merge is a wrong alignment that reads as a right one."""
        with pytest.raises(RetryableError, match="cannot produce a complete result"):
            await orchestrate([JobState.SUCCEEDED, JobState.FAILED, JobState.RUNNING])

    async def test_a_cancelled_bucket_fails_the_whole_alignment(self, orchestrate):
        with pytest.raises(RetryableError, match="cannot produce a complete result"):
            await orchestrate([JobState.SUCCEEDED, JobState.CANCELLED, JobState.RUNNING])


class TestApplier:
    """apply_chunked_alignment: the merged BAM becomes the alignment object."""

    @pytest.fixture
    def applied(self, monkeypatch, tmp_path):
        from app.queue import chunked_align_results

        bam = tmp_path / "sample.bam"
        bam.touch()
        obj = SimpleNamespace(id=PydanticObjectId())
        ingested: dict = {}
        enqueued: list[dict] = []

        async def _ingest(**kwargs):
            ingested.update(kwargs)
            return obj

        async def _enqueue(job_type, *, payload=None, owner=None, **kw):
            enqueued.append({"type": job_type, "payload": payload or {}})
            return str(PydanticObjectId())

        monkeypatch.setattr("app.services.object_service.ingest_local_file", _ingest)
        monkeypatch.setattr("app.queue.queue.enqueue", _enqueue)

        async def _run(**extra):
            result = {
                "output_path": str(bam),
                "project_id": str(PydanticObjectId()),
                "reference_id": str(PydanticObjectId()),
                "reads_object_id": str(PydanticObjectId()),
                "aligner": "bwa-mem2",
                "params": {"threads": 4},
                "bucket_count": 3,
                "output_name": "sample.bam",
            }
            result.update(extra)
            await chunked_align_results.apply_chunked_alignment(result, owner="local")
            return ingested, enqueued, obj, result

        return _run

    async def test_the_bam_descends_from_both_the_reads_and_the_reference(self, applied):
        """Same lineage the single-shot path records -- the provenance walk and
        the "what was this built from?" panel both read it."""
        ingested, _, _, result = await applied()
        assert set(str(d) for d in ingested["derived_from"]) == {
            result["reads_object_id"], result["reference_id"],
        }

    async def test_the_object_records_that_it_was_chunked(self, applied):
        """The only place the chunking survives. Without it, a merged BAM is
        indistinguishable from a single-shot one when a result looks wrong."""
        ingested, _, _, _ = await applied()
        assert ingested["facts"]["chunked"] is True
        assert ingested["facts"]["chunk_bucket_count"] == 3
        assert ingested["facts"]["aligned_by"] == "bwa-mem2"

    async def test_it_is_ingested_as_an_alignment(self, applied):
        from app.models.object import ObjectRole

        ingested, _, _, _ = await applied()
        assert ingested["role"] == ObjectRole.ALIGNMENT

    async def test_the_post_alignment_pipeline_is_chained(self, applied):
        """A merged BAM with no index is unusable in every viewer; the chunked
        path must chain the same three jobs the single-shot path does."""
        _, enqueued, obj, _ = await applied()
        assert {e["type"] for e in enqueued} == {
            "index_bam", "ingest_headers", "run_bam_stats",
        }
        assert all(e["payload"]["object_id"] == str(obj.id) for e in enqueued)
