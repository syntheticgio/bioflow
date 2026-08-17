"""The DAG walk itself.

These tests use real DataObject documents because the walk is a database
operation and mocking Motor would test the mock. Everything downstream of
the walker is tested against hand-built chains instead, with no DB at all.
"""

import pytest
from app.models.object import DataObject, ObjectRole, ObjectStatus
from app.services.provenance_walker import GapKind, walk
from beanie import PydanticObjectId

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.usefixtures("beanie_models"),
]

OWNER = "test-profile"


async def _obj(name, *, derived_from=(), facts=None, role=None,
               produced_by_job=None, sidecar_of=None):
    obj = DataObject(
        project_id=PydanticObjectId(),
        name=name,
        owner=OWNER,
        status=ObjectStatus.READY,
        role=role,
        facts=facts or {},
        derived_from=list(derived_from),
        produced_by_job=produced_by_job,
        sidecar_of=sidecar_of,
    )
    await obj.insert()
    return obj


async def test_root_object_has_no_steps():
    reads = await _obj("reads.fastq.gz")
    chain = await walk(reads.id, owner=OWNER)

    assert chain.target.object_id == reads.id
    assert chain.target.produced_by is None
    # A root is not a gap: an uploaded file legitimately has no producing job.
    assert chain.gap_count == 0


async def test_two_step_chain_is_ordered_oldest_first():
    reads = await _obj("reads.fastq.gz")
    bam = await _obj(
        "aligned.bam",
        derived_from=[reads.id],
        role=ObjectRole.ALIGNMENT,
        produced_by_job=PydanticObjectId(),
        facts={"aligned_by": "bwa-mem2", "aligner_version": "2.2.1"},
    )
    chain = await walk(bam.id, owner=OWNER)

    assert chain.order == (reads.id, bam.id)
    assert chain.nodes[bam.id].produced_by.tool == "bwa-mem2"
    assert chain.nodes[bam.id].produced_by.tool_version == "2.2.1"


async def test_missing_version_is_a_gap_not_a_silent_omission():
    reads = await _obj("reads.fastq.gz")
    bam = await _obj(
        "aligned.bam",
        derived_from=[reads.id],
        role=ObjectRole.ALIGNMENT,
        produced_by_job=PydanticObjectId(),
        facts={"aligned_by": "bwa-mem2"},
    )
    chain = await walk(bam.id, owner=OWNER)

    kinds = {g.kind for g in chain.gaps}
    assert GapKind.VERSION_UNRECORDED in kinds
    # The step is still present -- a gap never removes a step.
    assert chain.nodes[bam.id].produced_by is not None


async def test_reconvergent_dag_renders_each_node_once():
    """The reference is reachable via both the BAM and the VCF."""
    ref = await _obj("ref.fasta", role=ObjectRole.REFERENCE)
    reads = await _obj("reads.fastq.gz")
    bam = await _obj(
        "aligned.bam",
        derived_from=[reads.id, ref.id],
        role=ObjectRole.ALIGNMENT,
        facts={"aligned_by": "bwa-mem2", "reference_object_id": str(ref.id)},
    )
    vcf = await _obj(
        "calls.vcf.gz",
        derived_from=[bam.id, ref.id],
        role=ObjectRole.VARIANTS,
        facts={"variants_called_by": "clair3", "reference_object_id": str(ref.id)},
    )
    chain = await walk(vcf.id, owner=OWNER)

    assert chain.order.count(ref.id) == 1
    assert chain.nodes[ref.id].kind == "supporting"
    assert chain.nodes[bam.id].kind == "spine"


async def test_sidecars_never_appear():
    ref = await _obj("ref.fasta", role=ObjectRole.REFERENCE)
    idx = await _obj("ref.fasta.fai", sidecar_of=ref.id, derived_from=[ref.id])
    reads = await _obj("reads.fastq.gz")
    bam = await _obj(
        "aligned.bam",
        derived_from=[reads.id, ref.id, idx.id],
        role=ObjectRole.ALIGNMENT,
    )
    chain = await walk(bam.id, owner=OWNER)

    assert idx.id not in chain.nodes


async def test_walking_a_sidecar_directly_redirects_to_its_parent():
    """A `.bai`/`.fai` is scaffolding with no narrative step of its own --
    walking it directly used to render a meaningless last row ("processed
    with None None") instead of the parent's real lineage."""
    reads = await _obj("reads.fastq.gz")
    bam = await _obj(
        "aligned.bam",
        derived_from=[reads.id],
        role=ObjectRole.ALIGNMENT,
        produced_by_job=PydanticObjectId(),
        facts={"aligned_by": "bwa-mem2", "aligner_version": "2.2.1"},
    )
    bai = await _obj(
        "aligned.bam.bai",
        sidecar_of=bam.id,
        derived_from=[bam.id],
        produced_by_job=PydanticObjectId(),
    )

    chain = await walk(bai.id, owner=OWNER)

    # The chain describes the parent, not the sidecar.
    assert chain.target.object_id == bam.id
    assert bai.id not in chain.nodes
    assert chain.order == (reads.id, bam.id)
    assert chain.nodes[bam.id].produced_by.tool == "bwa-mem2"

    # The substitution is recorded, naming the sidecar that was actually
    # requested, so a caller can say so rather than silently swapping targets.
    assert chain.redirected_from == (bai.id, "aligned.bam.bai")


async def test_walking_a_root_sidecar_redirects_with_no_extra_gaps():
    """The parent here is itself a root (no `produced_by_job`) -- the
    redirect must not invent a step or a gap that was never there."""
    ref = await _obj("ref.fasta", role=ObjectRole.REFERENCE)
    fai = await _obj("ref.fasta.fai", sidecar_of=ref.id, derived_from=[ref.id])

    chain = await walk(fai.id, owner=OWNER)

    assert chain.target.object_id == ref.id
    assert chain.target.produced_by is None
    assert chain.gap_count == 0
    assert chain.redirected_from == (fai.id, "ref.fasta.fai")


async def test_dangling_parent_is_a_gap_not_a_crash():
    ghost = PydanticObjectId()
    bam = await _obj("aligned.bam", derived_from=[ghost], role=ObjectRole.ALIGNMENT)
    chain = await walk(bam.id, owner=OWNER)

    assert GapKind.DANGLING_PARENT in {g.kind for g in chain.gaps}


async def test_multiple_spine_parents_record_a_branch():
    long_reads = await _obj("ont.fastq.gz")
    short_reads = await _obj("ill.fastq.gz")
    asm = await _obj(
        "polished.fasta",
        derived_from=[long_reads.id, short_reads.id],
        facts={"polished_by": "polypolish"},
    )
    chain = await walk(asm.id, owner=OWNER)

    assert chain.branches
    assert set(chain.branches[0][1:]) == {long_reads.id, short_reads.id}


async def test_walk_is_owner_scoped():
    other = await _obj("secret.fastq.gz")
    other.owner = "someone-else"
    await other.save()

    from app.errors import NotFoundError

    # `get_object` raises NotFoundError for a wrong owner, the same error a
    # missing id raises -- so one profile cannot confirm another's id exists.
    with pytest.raises(NotFoundError):
        await walk(other.id, owner=OWNER)


async def test_failed_runs_appear_in_the_chain():
    """`records_for_object` includes failures deliberately, and provenance is
    the reader that wants them: a step that failed and was retried is the
    most informative record a methods reader can have. A chain that quietly
    showed only successes would be describing a run that never happened."""
    from app.models import JobRunTiming

    reads = await _obj("reads.fastq.gz")
    job_id = PydanticObjectId()
    bam = await _obj(
        "aligned.bam",
        derived_from=[reads.id],
        role=ObjectRole.ALIGNMENT,
        produced_by_job=job_id,
        facts={"aligned_by": "bwa-mem2", "aligner_version": "2.2.1"},
    )
    await JobRunTiming(
        job_id=str(job_id),
        object_id=str(bam.id),
        job_type="align_reads",
        outcome="failed",
        duration_ms=1000,
        input_bytes=0,
    ).insert()

    chain = await walk(bam.id, owner=OWNER)
    assert chain.nodes[bam.id].produced_by.outcome == "failed"


async def test_download_step_without_params_is_not_a_gap():
    """Downloads structurally never record parameters -- the accession and
    source are facts, not knobs -- so the History tab must not imply a hole
    in tracked data where there is nothing to have recorded."""
    from app.models.job import Job, JobState

    job = Job(type="download_sra_run", owner=OWNER, state=JobState.SUCCEEDED)
    await job.insert()
    fastq = await _obj(
        "reads.fastq.gz",
        produced_by_job=job.id,
        facts={"sra_downloaded_from": "DRR106634", "sra_download_source": "ncbi"},
    )
    chain = await walk(fastq.id, owner=OWNER)

    kinds = {g.kind for g in chain.gaps}
    assert GapKind.PARAMS_UNRECORDED not in kinds


async def test_a_real_step_without_params_is_still_a_gap():
    """The exemption is specific to job types that structurally have no
    parameters -- a tool step that failed to record its params must still be
    flagged, or a silent omission would read as a complete record."""
    from app.models.job import Job, JobState

    job = Job(type="trim_reads", owner=OWNER, state=JobState.SUCCEEDED)
    await job.insert()
    trimmed = await _obj(
        "reads_trimmed.fastq.gz",
        produced_by_job=job.id,
        facts={"trimmed_by": "trimmomatic"},
    )
    chain = await walk(trimmed.id, owner=OWNER)

    kinds = {g.kind for g in chain.gaps}
    assert GapKind.PARAMS_UNRECORDED in kinds


async def test_step_carries_the_job_that_produced_it():
    """`job_id` is what lets two mates be recognized as one step downstream;
    without it on the Step, merging has nothing to key on."""
    job = PydanticObjectId()
    reads = await _obj("reads.fastq.gz")
    bam = await _obj(
        "aligned.bam",
        derived_from=[reads.id],
        role=ObjectRole.ALIGNMENT,
        produced_by_job=job,
        facts={"aligned_by": "bwa-mem2", "aligner_version": "2.2.1"},
    )
    chain = await walk(bam.id, owner=OWNER)

    assert chain.nodes[bam.id].produced_by.job_id == job


async def test_a_material_records_the_object_that_consumed_it():
    """The lineage orders materials by their own timestamp, so a reference
    downloaded long before the reads sorts above them. `used_by` is what lets
    the row say which step used it, rather than appearing to be an ancestor of
    everything below."""
    reference = await _obj("GCF_000146045.2_R64_genomic.fna", role=ObjectRole.REFERENCE)
    reads = await _obj("reads.fastq.gz")
    bam = await _obj(
        "aligned.bam",
        derived_from=[reads.id, reference.id],
        role=ObjectRole.ALIGNMENT,
        produced_by_job=PydanticObjectId(),
        facts={
            "aligned_by": "bwa-mem2",
            "aligner_version": "2.2.1",
            "reference_object_id": str(reference.id),
        },
    )
    chain = await walk(bam.id, owner=OWNER)

    assert chain.nodes[reference.id].kind == "supporting"
    assert chain.nodes[reference.id].used_by == bam.id
    # A spine node's consumer is just the next step down, which the ordering
    # already shows -- naming it would be noise on every row.
    assert chain.nodes[reads.id].used_by is None


async def test_a_reconvergent_material_still_names_a_consumer():
    """A reference reachable as spine on one edge and supporting on another is
    demoted to supporting at the end of the walk. Recording consumers only for
    edges that looked supporting at the time would leave exactly these nodes
    with no consumer to name."""
    reference = await _obj("ref.fna")
    bam = await _obj(
        "aligned.bam",
        derived_from=[reference.id],
        role=ObjectRole.ALIGNMENT,
        produced_by_job=PydanticObjectId(),
        facts={"aligned_by": "bwa-mem2", "reference_object_id": str(reference.id)},
    )
    chain = await walk(bam.id, owner=OWNER)

    assert chain.nodes[reference.id].kind == "supporting"
    assert chain.nodes[reference.id].used_by == bam.id
