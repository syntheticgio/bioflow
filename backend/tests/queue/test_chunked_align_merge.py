"""Chunked alignment must merge every bucket or none of them.

A chunked alignment splits one reference into N buckets, aligns the same reads
against each, and merges the per-bucket BAMs back into one file. The merged BAM
is then ingested as *the* alignment for those reads -- nothing downstream
records that it was assembled from pieces, and nothing re-checks that all the
pieces arrived.

That makes a dropped bucket the silent-partial-success shape described in #584:
the reads that mapped to the missing bucket's contigs are simply absent from the
alignment, and every surface downstream (flagstat, variant calls, coverage)
reports confidently on a file that is missing a slice of the genome. There is no
error to notice, because a BAM merged from 5 of 8 buckets is a perfectly valid
BAM.

So the contract these tests pin is refusal: `_apply_align_reads_chunked` merges
only when it resolved a BAM for every sub-job it launched, and otherwise enqueues
nothing. `bucket_count` in the merged object's facts is a claim about
completeness, and it must never be a claim the merge could not back up.

Refusing quietly was its own silent failure (#595): the applier returned early,
`_apply_result` swallowed nothing because nothing was raised, and the
orchestrator job reported *succeeded* with no alignment to show for it. So the
refusal now raises `PermanentError`, which fails the job with a reason the user
can read. Both halves matter -- not merging is what protects the data, and
raising is what tells anyone it happened.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.errors import PermanentError
from app.models import JobState
from app.queue import results


def _sub_job(job_id, *, state=JobState.SUCCEEDED, tmp_path="/data/tmp/bucket.bam"):
    """A finished align_reads sub-job as the applier reads it."""
    result = None
    if tmp_path is not None:
        result = {"output": {"tmp_path": tmp_path}}
    return SimpleNamespace(id=job_id, state=state, result=result)


def _orchestrator_result(sub_job_ids, *, bucket_count=None):
    """What `align_reads_chunked` returns to the applier."""
    return {
        "sub_job_ids": [str(j) for j in sub_job_ids],
        "bucket_count": bucket_count if bucket_count is not None else len(sub_job_ids),
        "reference_id": str(PydanticObjectId()),
        "project_id": str(PydanticObjectId()),
        "reads_object_id": str(PydanticObjectId()),
        "aligner": "bwa-mem2",
        "params": {"threads": 4},
        "output_name": "sample.bam",
    }


@pytest.fixture
def merge_enqueued(monkeypatch):
    """Capture what the applier enqueues, without a queue or a database."""
    calls: list[dict] = []

    async def _enqueue(job_type, *, payload=None, owner=None, **kwargs):
        calls.append({"type": job_type, "payload": payload or {}, "owner": owner})
        return str(PydanticObjectId())

    monkeypatch.setattr("app.queue.queue.enqueue", _enqueue)
    return calls


def _with_jobs(jobs: dict):
    """Patch Job.get to answer from a {id: job} map, None for anything else."""

    async def _get(job_id):
        return jobs.get(str(job_id))

    return patch.object(results.Job, "get", AsyncMock(side_effect=_get))


class TestEveryBucketReachesTheMerge:
    async def test_all_buckets_resolved_enqueues_the_merge(self, merge_enqueued):
        """The happy path: N sub-jobs in, N BAM paths on the merge payload."""
        ids = [PydanticObjectId() for _ in range(4)]
        jobs = {
            str(jid): _sub_job(jid, tmp_path=f"/data/tmp/bucket{i}.bam")
            for i, jid in enumerate(ids)
        }

        with _with_jobs(jobs):
            await results._apply_align_reads_chunked(
                _orchestrator_result(ids), owner="local"
            )

        assert len(merge_enqueued) == 1
        payload = merge_enqueued[0]["payload"]
        assert merge_enqueued[0]["type"] == "merge_chunked_buckets"
        assert payload["bucket_bam_paths"] == [
            f"/data/tmp/bucket{i}.bam" for i in range(4)
        ]

    async def test_the_merge_payload_accounts_for_every_sub_job(self, merge_enqueued):
        """bucket_count is what lands in the object's facts, so the number of
        BAMs actually merged has to equal it -- not merely be close to it."""
        ids = [PydanticObjectId() for _ in range(8)]
        jobs = {str(jid): _sub_job(jid, tmp_path=f"/t/{jid}.bam") for jid in ids}

        with _with_jobs(jobs):
            await results._apply_align_reads_chunked(
                _orchestrator_result(ids), owner="local"
            )

        payload = merge_enqueued[0]["payload"]
        assert len(payload["bucket_bam_paths"]) == payload["bucket_count"] == 8

    async def test_bucket_order_is_preserved(self, merge_enqueued):
        """samtools merge -c relies on headers agreeing; feeding the buckets in
        the order they were launched keeps the merged header deterministic
        rather than dependent on dict iteration."""
        ids = [PydanticObjectId() for _ in range(3)]
        jobs = {str(jid): _sub_job(jid, tmp_path=f"/t/b{i}.bam") for i, jid in enumerate(ids)}

        with _with_jobs(jobs):
            await results._apply_align_reads_chunked(
                _orchestrator_result(ids), owner="local"
            )

        assert merge_enqueued[0]["payload"]["bucket_bam_paths"] == [
            "/t/b0.bam", "/t/b1.bam", "/t/b2.bam",
        ]


class TestAPartialSetIsNeverMerged:
    """Each of these is a bucket the applier cannot account for. In every case
    the merge must not be enqueued: a missing slice of the reference produces a
    BAM that looks complete and is not."""

    async def test_a_failed_sub_job_blocks_the_merge(self, merge_enqueued):
        ids = [PydanticObjectId() for _ in range(3)]
        jobs = {str(jid): _sub_job(jid, tmp_path=f"/t/{i}.bam") for i, jid in enumerate(ids)}
        jobs[str(ids[1])] = _sub_job(ids[1], state=JobState.FAILED, tmp_path=None)

        with _with_jobs(jobs), pytest.raises(PermanentError):
            await results._apply_align_reads_chunked(
                _orchestrator_result(ids), owner="local"
            )

        assert merge_enqueued == []

    async def test_a_missing_sub_job_document_blocks_the_merge(self, merge_enqueued):
        """Job.get returning None -- the document was reaped or never written.
        Unknown is not the same as empty, and must not read as 'skip it'."""
        ids = [PydanticObjectId() for _ in range(3)]
        jobs = {str(jid): _sub_job(jid, tmp_path=f"/t/{i}.bam") for i, jid in enumerate(ids)}
        del jobs[str(ids[2])]

        with _with_jobs(jobs), pytest.raises(PermanentError):
            await results._apply_align_reads_chunked(
                _orchestrator_result(ids), owner="local"
            )

        assert merge_enqueued == []

    async def test_a_succeeded_sub_job_with_no_output_path_blocks_the_merge(
        self, merge_enqueued
    ):
        """The worst shape of the three: the sub-job says it succeeded, so
        nothing upstream flagged it, but it recorded no BAM to merge."""
        ids = [PydanticObjectId() for _ in range(3)]
        jobs = {str(jid): _sub_job(jid, tmp_path=f"/t/{i}.bam") for i, jid in enumerate(ids)}
        jobs[str(ids[0])] = _sub_job(ids[0], tmp_path=None)

        with _with_jobs(jobs), pytest.raises(PermanentError):
            await results._apply_align_reads_chunked(
                _orchestrator_result(ids), owner="local"
            )

        assert merge_enqueued == []

    async def test_a_sub_job_lookup_error_blocks_the_merge(self, merge_enqueued):
        """A transient Mongo error is not evidence that a bucket is empty."""
        ids = [PydanticObjectId() for _ in range(3)]

        async def _get(job_id):
            if str(job_id) == str(ids[1]):
                raise RuntimeError("connection reset")
            return _sub_job(job_id, tmp_path=f"/t/{job_id}.bam")

        with patch.object(
            results.Job, "get", AsyncMock(side_effect=_get)
        ), pytest.raises(PermanentError):
            await results._apply_align_reads_chunked(
                _orchestrator_result(ids), owner="local"
            )

        assert merge_enqueued == []

    async def test_no_sub_jobs_at_all_enqueues_nothing(self, merge_enqueued):
        """An orchestrator that launched no buckets has nothing to merge and
        nothing to show for itself either -- equally a job that must not
        report success."""
        with pytest.raises(PermanentError):
            await results._apply_align_reads_chunked(
                _orchestrator_result([]), owner="local"
            )
        assert merge_enqueued == []


class TestTheRefusalIsVisibleToTheUser:
    """#595: not merging protects the data, but the orchestrator job used to
    report succeeded anyway. These pin the half that reaches a person -- a
    failed job whose error says how much of the alignment was accounted for.
    """

    async def test_the_error_counts_resolved_against_expected(self, merge_enqueued):
        """A count like "3 of 4" is the whole diagnosis: it says the buckets
        ran and one went missing, which is a different problem from an
        alignment that never started."""
        ids = [PydanticObjectId() for _ in range(4)]
        jobs = {str(jid): _sub_job(jid, tmp_path=f"/t/{i}.bam") for i, jid in enumerate(ids)}
        del jobs[str(ids[3])]

        with _with_jobs(jobs), pytest.raises(PermanentError) as excinfo:
            await results._apply_align_reads_chunked(
                _orchestrator_result(ids), owner="local"
            )

        message = str(excinfo.value)
        assert "3" in message and "4" in message

    async def test_the_empty_orchestrator_says_so_distinctly(self, merge_enqueued):
        """A run that launched nothing must not be described as a partial
        merge -- the cause is upstream in bucketing, not in a lost BAM."""
        with pytest.raises(PermanentError) as excinfo:
            await results._apply_align_reads_chunked(
                _orchestrator_result([]), owner="local"
            )

        assert "no bucket" in str(excinfo.value).lower()
