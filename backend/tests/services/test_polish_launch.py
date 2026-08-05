"""The launch path for Polypolish short-read polishing.

Same posture test_consensus_launch.py takes: run the whole way to the
enqueue rather than stopping at "the service raised nothing".

The thing worth watching in this file is what is *absent*. There is no
"refuses a BAM aligned to the wrong reference" test here, because there is
no BAM: Polypolish needs all-alignment SAM, so the handler aligns the reads
to the draft itself and the alignment target is correct by construction.
The provenance obligation shows up instead as facts recorded on the output
(see the handler), and as the read-eligibility checks below -- polishing
with the wrong *sample's* reads is the mistake actually available here.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.errors import ValidationError
from app.models import FormatKind, ObjectRole, ObjectStatus
from app.pipelines.tools import Tool
from app.services import pipeline_service

_PP = Tool(name="polypolish", path="/usr/local/bin/polypolish", version="0.7.1")
_BWA = Tool(name="bwa-mem2", path="/usr/local/bin/bwa-mem2", version="2.2.1")


def _obj(*, name, kind, role=None, status=ObjectStatus.READY, facts=None,
         project_id=None, metadata=None, mate=None):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name=name,
        format=SimpleNamespace(kind=kind),
        role=role,
        status=status,
        facts=facts or {},
        metadata=metadata or {},
        mate_object_id=mate,
        derived_from=[],
        project_id=project_id or PydanticObjectId(),
        owner="local",
        blob_sha256="a" * 64,
    )


def _draft(*, project_id=None, total_bases=5_000_000):
    return _obj(
        name="assembly.fasta",
        kind=FormatKind.FASTA,
        project_id=project_id,
        facts={"total_bases": total_bases} if total_bases else {},
    )


def _illumina(name, *, project_id, total_bases=150_000_000, mate=None):
    return _obj(
        name=name,
        kind=FormatKind.FASTQ,
        project_id=project_id,
        metadata={"platform": "Illumina NovaSeq X Plus"},
        facts={"qc_before_filtering": {"total_bases": total_bases}},
        mate=mate,
    )


def _nanopore(name, *, project_id):
    return _obj(
        name=name,
        kind=FormatKind.FASTQ,
        project_id=project_id,
        metadata={"platform": "MinION"},
        # The real-data trap: chemistry says short, platform says otherwise.
        facts={"qc_read_chemistry": "short"},
    )


async def _run(*, draft, project_objects, reads_object_id=None, mate_object_id=None):
    objects = {o.id: o for o in [draft, *project_objects]}

    async def _get_object(object_id, *, owner):
        return objects[object_id]

    async def _list_objects(project_id, *, owner, **kwargs):
        return [o for o in project_objects if o.project_id == project_id]

    enqueued = {}

    async def _enqueue(job_type, **kwargs):
        enqueued["type"] = job_type
        enqueued.update(kwargs)
        return SimpleNamespace(id="job1")

    with (
        patch("app.pipelines.tools.polypolish", return_value=_PP),
        patch("app.pipelines.tools.bwa_mem2", return_value=_BWA),
        patch("app.services.object_service.get_object", AsyncMock(side_effect=_get_object)),
        patch("app.services.object_service.list_objects", AsyncMock(side_effect=_list_objects)),
        patch("app.services.pipeline_service._resolve_readable",
              AsyncMock(return_value=("a" * 64, None))),
        patch("app.queue.queue.enqueue", _enqueue),
    ):
        job = await pipeline_service.launch_polish(
            draft_object_id=draft.id,
            reads_object_id=reads_object_id,
            mate_object_id=mate_object_id,
            owner="local",
        )
    return job, enqueued


class TestResolvingReadsFromTheProject:
    async def test_one_pair_reaches_the_queue(self):
        draft = _draft()
        pid = draft.project_id
        r1 = _illumina("r_1.fastq", project_id=pid)
        r2 = _illumina("r_2.fastq", project_id=pid, mate=r1.id)
        r1.mate_object_id = r2.id

        job, enqueued = await _run(draft=draft, project_objects=[r1, r2])

        assert job.id == "job1"
        assert enqueued["type"] == "polish_assembly"
        payload = enqueued["payload"]
        assert payload["draft_object_id"] == str(draft.id)
        assert {payload["reads_object_id"], payload["mate_object_id"]} == {
            str(r1.id), str(r2.id)
        }

    async def test_long_reads_alone_are_refused(self):
        """The project has reads, but none a short-read polisher may use."""
        draft = _draft()
        ont = _nanopore("ont.fastq", project_id=draft.project_id)

        with pytest.raises(ValidationError, match="short reads"):
            await _run(draft=draft, project_objects=[ont])

    async def test_several_read_sets_are_refused_rather_than_picked(self):
        """Polishing with the wrong sample's reads is a silent corruption,
        so ambiguity fails loudly instead of choosing."""
        draft = _draft()
        pid = draft.project_id
        a = _illumina("a.fastq", project_id=pid)
        b = _illumina("b.fastq", project_id=pid)

        with pytest.raises(ValidationError, match="several short-read sets"):
            await _run(draft=draft, project_objects=[a, b])

    async def test_nanopore_does_not_count_toward_ambiguity(self):
        """The MinION file must be invisible here, not merely rejected --
        counting it would make a launchable project look ambiguous."""
        draft = _draft()
        pid = draft.project_id
        r1 = _illumina("r_1.fastq", project_id=pid)
        r2 = _illumina("r_2.fastq", project_id=pid, mate=r1.id)
        r1.mate_object_id = r2.id
        ont = _nanopore("ont.fastq", project_id=pid)

        job, enqueued = await _run(draft=draft, project_objects=[r1, r2, ont])
        assert job.id == "job1"


class TestExplicitReads:
    async def test_named_reads_bypass_resolution(self):
        draft = _draft()
        pid = draft.project_id
        chosen = _illumina("chosen.fastq", project_id=pid)
        other = _illumina("other.fastq", project_id=pid)

        job, enqueued = await _run(
            draft=draft, project_objects=[chosen, other], reads_object_id=chosen.id
        )
        assert enqueued["payload"]["reads_object_id"] == str(chosen.id)
        assert enqueued["payload"].get("mate_object_id") is None

    async def test_named_long_reads_are_refused(self):
        """The ambiguity path is bypassed but the chemistry check is not."""
        draft = _draft()
        ont = _nanopore("ont.fastq", project_id=draft.project_id)

        with pytest.raises(ValidationError, match="not short-read data"):
            await _run(draft=draft, project_objects=[ont], reads_object_id=ont.id)


class TestDraftValidation:
    async def test_protein_fasta_is_refused(self):
        draft = _obj(name="protein.faa", kind=FormatKind.FASTA, role=ObjectRole.PROTEIN)
        r1 = _illumina("r.fastq", project_id=draft.project_id)

        with pytest.raises(ValidationError):
            await _run(draft=draft, project_objects=[r1])

    async def test_a_fastq_draft_is_refused(self):
        draft = _obj(name="reads.fastq", kind=FormatKind.FASTQ)
        r1 = _illumina("r.fastq", project_id=draft.project_id)

        with pytest.raises(ValidationError):
            await _run(draft=draft, project_objects=[r1])


class TestDepthAndCareful:
    async def test_depth_is_computed_from_facts(self):
        """150Mb of reads over a 5Mb draft is 30x."""
        draft = _draft(total_bases=5_000_000)
        r1 = _illumina("r.fastq", project_id=draft.project_id, total_bases=150_000_000)

        _, enqueued = await _run(draft=draft, project_objects=[r1])
        assert enqueued["payload"]["depth"] == pytest.approx(30.0)

    async def test_paired_read_bases_are_summed(self):
        draft = _draft(total_bases=5_000_000)
        pid = draft.project_id
        r1 = _illumina("r_1.fastq", project_id=pid, total_bases=50_000_000)
        r2 = _illumina("r_2.fastq", project_id=pid, total_bases=50_000_000, mate=r1.id)
        r1.mate_object_id = r2.id

        _, enqueued = await _run(draft=draft, project_objects=[r1, r2])
        assert enqueued["payload"]["depth"] == pytest.approx(20.0)

    async def test_depth_is_none_when_qc_has_not_run(self):
        """Which sends the handler down the non-careful path deliberately --
        see polypolish_runner.params_for_depth."""
        draft = _draft(total_bases=5_000_000)
        r1 = _illumina("r.fastq", project_id=draft.project_id, total_bases=None)
        r1.facts = {}

        _, enqueued = await _run(draft=draft, project_objects=[r1])
        assert enqueued["payload"]["depth"] is None

    async def test_depth_is_none_without_an_assembly_length(self):
        draft = _draft(total_bases=None)
        r1 = _illumina("r.fastq", project_id=draft.project_id)

        _, enqueued = await _run(draft=draft, project_objects=[r1])
        assert enqueued["payload"]["depth"] is None
