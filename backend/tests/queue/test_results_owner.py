"""Owner propagation in the results appliers.

The appliers run in the worker, after a handler finished in a thread that could
not touch the database. They resolve a parent object by id and then ingest the
file the handler produced -- so the owner they pass is not a free choice, it is
whatever the parent already carries.

Every test here uses a non-"local" owner on purpose. A hardcoded `owner="local"`
passes any assertion made against a "local" project, which is exactly how the
placeholder these tests replace survived a green suite.
"""

import inspect
import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.models import DataObject, ObjectRole, SidecarRole
from app.queue import results
from app.services import object_service, project_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch):
    """Stub the enqueues these appliers reach.

    `ingest_local_file` finishes by queueing `ingest_headers`, and
    `_apply_align_reads` chains an `index_bam` job -- both want a live Redis
    this process never opens. Neither is the seam under test: carrying `owner`
    into the job document is Task 8's, so stubbing keeps a Redis outage from
    being reported as an owner-propagation failure.
    """

    async def _skip_ingest(obj, **kwargs):
        return ""

    async def _skip_enqueue(*args, **kwargs):
        return None

    monkeypatch.setattr(object_service, "enqueue_ingest", _skip_ingest)
    monkeypatch.setattr("app.queue.queue.enqueue", _skip_enqueue)


def _scratch_file() -> Path:
    """A file for an applier to ingest, under tmp_dir.

    Sync on purpose (ASYNC240), and unique bytes per call: a byte-identical
    file deduplicates onto an existing blob, and a deduped ingest would let a
    stale object from another test answer the assertion.
    """
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    path = settings.tmp_dir / f"results-owner-{uuid.uuid4().hex}.tmp"
    path.write_bytes(uuid.uuid4().bytes)
    return path


async def _parent(owner: str, name: str, *, role: ObjectRole | None = None) -> DataObject:
    """A project and one ingested object in it, both owned by `owner`."""
    project = await project_service.create_project(name=f"{owner}-{name}", owner=owner)
    path = _scratch_file()
    return await object_service.ingest_local_file(
        owner=owner,
        project_id=project.id,
        path=path,
        name=name,
        role=role,
    )


class TestSidecarsInheritTheirParentsOwner:
    """The `owner` passed in is deliberately a *different* string here.

    These appliers take their owner from the parent object they resolved, not
    from the job, and a matching value would make either source pass the
    assertion. Setting it to "someone-else" is what makes these tests say which
    of the two actually reached the ingest.
    """

    async def test_index_bam_attaches_the_bai_to_a_non_local_bam(self):
        """The bug this replaces, end to end.

        With `owner="local"` hardcoded, ingest_local_file's own
        `get_project(project_id, owner="local")` raises NotFoundError on a
        project owned by anyone else. `_apply_index_bam` catches that in its
        bare except and logs `bai_ingest_failed`, so the .bai is silently never
        registered and the BAM keeps reporting a missing index forever.
        """
        owner = "results-bai-a"
        bam = await _parent(owner, "sample.bam", role=ObjectRole.ALIGNMENT)
        output = _scratch_file()

        await results._apply_index_bam(
            {
                "bam_object_id": str(bam.id),
                "output": {"tmp_path": str(output), "name": "sample.bam.bai"},
                "facts": {},
            },
            owner="someone-else",
        )

        sidecars = await object_service.list_sidecars(bam.id, owner=owner)
        assert [s.sidecar_role for s in sidecars] == [SidecarRole.BAI]
        assert sidecars[0].owner == owner

    async def test_the_indexed_bam_is_marked_as_having_an_index(self):
        """`has_index` is only stamped when the ingest actually succeeded, so
        it doubles as a check that the applier did not silently swallow a
        NotFoundError and carry on."""
        owner = "results-hasindex-a"
        bam = await _parent(owner, "flagged.bam", role=ObjectRole.ALIGNMENT)
        output = _scratch_file()

        await results._apply_index_bam(
            {
                "bam_object_id": str(bam.id),
                "output": {"tmp_path": str(output), "name": "flagged.bam.bai"},
                "facts": {},
            },
            owner="someone-else",
        )

        refreshed = await DataObject.get(bam.id)
        assert refreshed is not None
        assert refreshed.facts.get("has_index") is True

    async def test_build_index_attaches_sidecars_to_a_non_local_reference(self):
        """Same shape as the .bai, via a different applier: the index files a
        reference needs before it can be aligned against."""
        owner = "results-faidx-a"
        reference = await _parent(owner, "genome.fna", role=ObjectRole.REFERENCE)
        output = _scratch_file()

        await results._apply_build_index(
            {
                "reference_object_id": str(reference.id),
                "aligner": "minimap2",
                "outputs": [{"tmp_path": str(output), "name": "genome.fna.fai", "role": "fai"}],
            },
            owner="someone-else",
        )

        sidecars = await object_service.list_sidecars(reference.id, owner=owner)
        assert [s.sidecar_role for s in sidecars] == [SidecarRole.FAI]
        assert sidecars[0].owner == owner


class TestDerivedOutputsInheritTheirParentsOwner:
    async def test_call_variants_writes_the_vcf_under_the_bams_owner(self):
        """A VCF ingested as "local" against a profile-owned BAM would not just
        be mislabelled -- ingest refuses the project outright, and the applier
        returns after logging, losing the call entirely."""
        owner = "results-vcf-a"
        bam = await _parent(owner, "calls.bam", role=ObjectRole.ALIGNMENT)
        output = _scratch_file()
        index = _scratch_file()

        await results._apply_call_variants(
            {
                "bam_object_id": str(bam.id),
                "caller": "bcftools",
                "output": {"tmp_path": str(output), "name": "calls.vcf.gz"},
                "index": {"tmp_path": str(index), "name": "calls.vcf.gz.tbi"},
            },
            owner="someone-else",
        )

        produced = await DataObject.find(
            DataObject.derived_from == bam.id, DataObject.owner == owner
        ).to_list()
        assert [p.name for p in produced] == ["calls.vcf.gz"]
        vcf = produced[0]
        # The .tbi hangs off the VCF, not the BAM -- so it is the VCF's owner
        # that has to reach it, which is the second of the two sites here.
        tbi = await object_service.list_sidecars(vcf.id, owner=owner)
        assert [t.sidecar_role for t in tbi] == [SidecarRole.TBI]
        assert tbi[0].owner == owner

    async def test_consensus_writes_the_fasta_under_the_bams_owner(self):
        """Same reasoning test_call_variants_writes_the_vcf_under_the_bams_owner
        gives: a consensus FASTA ingested as "local" against a profile-owned
        BAM would not be mislabelled, it would be refused outright and the
        applier would lose the run entirely after logging."""
        owner = "results-consensus-a"
        bam = await _parent(owner, "aln.bam", role=ObjectRole.ALIGNMENT)
        reference = await _parent(owner, "ref.fasta", role=ObjectRole.REFERENCE)
        output = _scratch_file()

        await results._apply_consensus_from_alignment(
            {
                "bam_object_id": str(bam.id),
                "reference_object_id": str(reference.id),
                "output": {"tmp_path": str(output), "name": "consensus.fasta"},
                "facts": {
                    "consensus_tool_version": "1.4.4",
                    "consensus_primers_trimmed": False,
                },
            },
            owner="someone-else",
        )

        produced = await DataObject.find(
            DataObject.derived_from == bam.id, DataObject.owner == owner
        ).to_list()
        # .gz: the scratch fixture's bytes are not actually gzip, so FASTA's
        # entry in compress.COMPRESSIBLE_KINDS compresses it at ingest -- see
        # docs/superpowers/specs/2026-08-05-object-compression-design.md.
        assert [p.name for p in produced] == ["consensus.fasta.gz"]
        consensus = produced[0]
        assert consensus.role == ObjectRole.REFERENCE
        assert reference.id in consensus.derived_from
        assert consensus.facts["consensus_tool_version"] == "1.4.4"

    async def test_consensus_derives_from_both_bam_and_reference(self):
        """Mirrors call_variants: a consensus is a claim about a reference
        made from an alignment, so deleting either parent should show the
        consensus as descending from it."""
        owner = "results-consensus-b"
        bam = await _parent(owner, "aln.bam", role=ObjectRole.ALIGNMENT)
        reference = await _parent(owner, "ref.fasta", role=ObjectRole.REFERENCE)
        output = _scratch_file()

        await results._apply_consensus_from_alignment(
            {
                "bam_object_id": str(bam.id),
                "reference_object_id": str(reference.id),
                "output": {"tmp_path": str(output), "name": "consensus.fasta"},
                "facts": {},
            },
            owner=owner,
        )

        [consensus] = await DataObject.find(
            DataObject.derived_from == bam.id
        ).to_list()
        assert set(consensus.derived_from) == {bam.id, reference.id}

    async def test_trim_outputs_carry_the_reads_owner(self):
        """The multi-output applier: each trimmed FASTQ is ingested in its own
        try/except, so a wrong owner loses them one at a time rather than
        failing the run."""
        owner = "results-trim-a"
        reads = await _parent(owner, "reads.fastq.gz")
        output = _scratch_file()

        await results._apply_trim_reads(
            {
                "object_id": str(reads.id),
                "tool": "fastp",
                "outputs": [{"tmp_path": str(output), "name": "reads.trimmed.fastq.gz"}],
                "report": {},
            },
            owner="someone-else",
        )

        produced = await DataObject.find(DataObject.derived_from == reads.id).to_list()
        assert [p.name for p in produced] == ["reads.trimmed.fastq.gz"]
        assert produced[0].owner == owner


class TestDownloadsTakeTheOwnerFromTheJob:
    """The two appliers with no parent object to read an owner off.

    A download produces the *first* object in its chain: nothing it ingests is
    derived from anything, so there is no `<parent>.owner` to inherit and the
    only owner available is the one on the job that launched it. That is why
    `apply` has to be told, and why these two are the sites Task 9 exists for.
    """

    async def test_apply_requires_an_owner(self):
        """A keyword-only, non-defaulted `owner` is the point of the change.

        A default would make every caller that forgets it silently write
        someone else's data to "local", which is the failure these tests were
        written to make impossible to reintroduce.
        """
        sig = inspect.signature(results.apply)
        owner_param = sig.parameters["owner"]
        assert owner_param.kind is inspect.Parameter.KEYWORD_ONLY
        assert owner_param.default is inspect.Parameter.empty

    async def test_an_sra_download_lands_under_the_jobs_owner(self):
        """Driven through `apply` rather than the applier directly, so the
        dispatch that has to forward the keyword is part of what is asserted."""
        owner = "results-sra-a"
        project = await project_service.create_project(name=f"{owner}-sra", owner=owner)
        staged = _scratch_file()

        await results.apply(
            "download_sra_run",
            {
                "project_id": str(project.id),
                "accession": "SRR000001",
                "platform": "ILLUMINA",
                "staged": [{"path": str(staged), "name": "SRR000001.fastq", "mate": "single"}],
            },
            owner=owner,
        )

        produced = await DataObject.find(DataObject.project_id == project.id).to_list()
        # .gz: FASTQ compresses at ingest -- see docs/superpowers/specs/
        # 2026-08-05-object-compression-design.md.
        assert [p.name for p in produced] == ["SRR000001.fastq.gz"]
        assert produced[0].owner == owner

    async def test_an_assembly_download_lands_under_the_jobs_owner(self):
        owner = "results-assembly-a"
        project = await project_service.create_project(name=f"{owner}-asm", owner=owner)
        staged = _scratch_file()

        await results.apply(
            "download_assembly",
            {
                "project_id": str(project.id),
                "accession": "GCF_000002445.2",
                "staged": [
                    {"path": str(staged), "name": "genome.fna", "component": "genome"}
                ],
            },
            owner=owner,
        )

        produced = await DataObject.find(DataObject.project_id == project.id).to_list()
        # .gz: FASTA compresses at ingest -- see docs/superpowers/specs/
        # 2026-08-05-object-compression-design.md.
        assert [p.name for p in produced] == ["genome.fna.gz"]
        assert produced[0].owner == owner
        # The role still comes from the component map -- threading the owner
        # must not disturb what the applier already decided about the file.
        assert produced[0].role == ObjectRole.REFERENCE


class TestTheExecutorSuppliesTheOwner:
    """The one production caller of `apply` is `JobExecutor._apply_result`.

    Every other test in this file calls `results.apply` directly, which means
    none of them touch the line that decides *what* owner production passes.
    That line could be changed to a literal and the whole file would stay
    green -- verified, which is why this class exists. `_apply_result` takes
    the `Job` as a plain argument, so it can be driven without a worker, a
    Redis connection, or a real dispatch.
    """

    async def test_every_applier_accepts_the_keyword_apply_dispatches_with(self):
        """`apply` calls every applier as `handler(result, owner=...)`, so one
        declaring that parameter under another name raises TypeError the moment
        its job finishes.

        The failure is invisible exactly where it matters. `_apply_result`
        catches it and logs `result_apply_failed`, so the *job* still reports
        succeeded -- the tool ran, the output is on disk, and only registration
        is lost. The user sees a pipeline that worked and produced nothing.

        Found by running a real DeepVariant job end to end, with eleven of the
        fourteen appliers still on the old keyword. Nothing caught it earlier
        because every other test here calls an applier *directly* with its own
        keyword, which is the seam this closes: they prove an applier works,
        never that dispatch can reach it.
        """
        for job_type, handler in results._APPLIERS.items():
            params = inspect.signature(handler).parameters
            assert "owner" in params, (
                f"{handler.__name__} (registered for {job_type!r}) does not take "
                "`owner`, the keyword apply() dispatches with. Its job would "
                "report succeeded while its output went unregistered."
            )
            assert params["owner"].kind is inspect.Parameter.KEYWORD_ONLY

    async def test_apply_result_forwards_the_jobs_owner(self, monkeypatch):
        """A literal here would write every profile's pipeline output into the
        adopted profile's library, with nothing failing to say so."""
        from app.models import Job
        from app.queue import results as results_mod
        from app.queue.executor import JobExecutor

        seen: dict[str, str] = {}

        async def _capture(job_type: str, result: dict, *, owner: str) -> None:
            seen["owner"] = owner

        monkeypatch.setattr(results_mod, "apply", _capture)

        job = Job(type="download_sra_run", owner="executor-owner-a")
        await JobExecutor("test-worker")._apply_result(job, {"anything": True})

        assert seen["owner"] == "executor-owner-a"
