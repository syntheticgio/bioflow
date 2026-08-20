"""The launch path for the minimap2 synteny alignment.

Same posture test_misassembly_qc_launch.py takes (its closest sibling: an
assembly-shaped draft plus a reference, identical ambiguity-resolution shape,
identical cross-project and self-reference guards) -- run the whole way to
the enqueue rather than stopping at "the service raised nothing".

Unlike misassembly QC, this launch's payload does carry `draft_name`: the
handler links the draft under that name for `synteny_runner.build_synteny_
command`'s query positional, where QUAST's launch uses a fixed label instead.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.errors import ValidationError
from app.models import FormatKind, ObjectRole
from app.pipelines.tools import Tool
from app.services import pipeline_service
from tests.services.conftest import _draft, _obj, _reference

_MINIMAP2 = Tool(name="minimap2", path="/usr/local/bin/minimap2", version="2.28")


async def _run(*, draft, project_objects, reference_object_id=None, divergence=None):
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
        patch("app.pipelines.tools.minimap2", return_value=_MINIMAP2),
        patch("app.services.object_service.get_object", AsyncMock(side_effect=_get_object)),
        patch("app.services.object_service.list_objects", AsyncMock(side_effect=_list_objects)),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("a" * 64, None)),
        ),
        patch("app.queue.queue.enqueue", _enqueue),
    ):
        job = await pipeline_service.launch_synteny(
            draft_object_id=draft.id,
            reference_object_id=reference_object_id,
            divergence=divergence,
            owner="local",
        )
    return job, enqueued


class TestResolvingReferenceFromTheProject:
    async def test_one_candidate_reaches_the_queue(self):
        draft = _draft()
        ref = _reference("ref.fasta", project_id=draft.project_id)

        job, enqueued = await _run(draft=draft, project_objects=[ref])

        assert job.id == "job1"
        assert enqueued["type"] == "analyze_synteny"
        payload = enqueued["payload"]
        assert payload["object_id"] == str(draft.id)
        assert payload["draft_name"] == draft.name
        assert payload["reference_object_id"] == str(ref.id)
        assert payload["reference_name"] == ref.name

    async def test_no_reference_is_refused(self):
        draft = _draft()
        with pytest.raises(ValidationError, match="reference assembly"):
            await _run(draft=draft, project_objects=[])

    async def test_two_references_are_refused_rather_than_picked(self):
        """The ordinary shape of a real project (both GCA and GCF genomic
        FASTA), and the reason this launch cannot silently choose."""
        draft = _draft()
        pid = draft.project_id
        a = _reference("ref_gca.fasta", project_id=pid)
        b = _reference("ref_gcf.fasta", project_id=pid)

        with pytest.raises(ValidationError, match="several reference assemblies"):
            await _run(draft=draft, project_objects=[a, b])

    async def test_the_draft_is_excluded_from_its_own_candidate_list(self):
        draft = _obj(
            name="d.fasta", kind=FormatKind.FASTA, role=ObjectRole.REFERENCE
        )
        with pytest.raises(ValidationError, match="reference assembly"):
            await _run(draft=draft, project_objects=[draft])


class TestExplicitReference:
    async def test_named_reference_bypasses_resolution(self):
        draft = _draft()
        pid = draft.project_id
        chosen = _reference("chosen.fasta", project_id=pid)
        other = _reference("other.fasta", project_id=pid)

        job, enqueued = await _run(
            draft=draft, project_objects=[chosen, other], reference_object_id=chosen.id
        )
        assert enqueued["payload"]["reference_object_id"] == str(chosen.id)

    async def test_named_reference_still_needs_the_reference_role(self):
        draft = _draft()
        not_a_reference = _obj(
            name="contigs.fasta", kind=FormatKind.FASTA, project_id=draft.project_id
        )

        with pytest.raises(ValidationError):
            await _run(
                draft=draft,
                project_objects=[not_a_reference],
                reference_object_id=not_a_reference.id,
            )

    async def test_the_draft_cannot_be_its_own_reference(self):
        draft = _obj(
            name="d.fasta", kind=FormatKind.FASTA, role=ObjectRole.REFERENCE
        )
        with pytest.raises(ValidationError, match="same object"):
            await _run(
                draft=draft, project_objects=[draft], reference_object_id=draft.id
            )

    async def test_named_reference_in_a_different_project_is_refused(self):
        draft = _draft()
        other_project_ref = _reference("ref.fasta", project_id=PydanticObjectId())

        with pytest.raises(ValidationError, match="same project"):
            await _run(
                draft=draft,
                project_objects=[other_project_ref],
                reference_object_id=other_project_ref.id,
            )


class TestDraftValidation:
    async def test_protein_fasta_is_refused(self):
        draft = _obj(name="protein.faa", kind=FormatKind.FASTA, role=ObjectRole.PROTEIN)
        ref = _reference("ref.fasta", project_id=draft.project_id)

        with pytest.raises(ValidationError):
            await _run(draft=draft, project_objects=[ref])

    async def test_a_fastq_draft_is_refused(self):
        draft = _obj(name="reads.fastq", kind=FormatKind.FASTQ)
        ref = _reference("ref.fasta", project_id=draft.project_id)

        with pytest.raises(ValidationError):
            await _run(draft=draft, project_objects=[ref])


class TestDivergence:
    async def test_defaults_to_same_species(self):
        draft = _draft()
        ref = _reference("ref.fasta", project_id=draft.project_id)

        _, enqueued = await _run(draft=draft, project_objects=[ref])

        assert enqueued["payload"]["divergence"] == "same_species"

    async def test_explicit_divergence_is_passed_through(self):
        draft = _draft()
        ref = _reference("ref.fasta", project_id=draft.project_id)

        _, enqueued = await _run(
            draft=draft, project_objects=[ref], divergence="distant"
        )

        assert enqueued["payload"]["divergence"] == "distant"
