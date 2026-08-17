"""The launch path for QUAST reference-based misassembly QC.

Same posture test_scaffold_launch.py takes (its closest sibling: an
assembly-shaped draft plus a reference, same ambiguity-resolution shape) --
run the whole way to the enqueue rather than stopping at "the service raised
nothing".

One check `launch_misassembly_qc` has that `launch_scaffold` does not: an
explicit cross-project rejection for a *named* reference, not just the
project-scoped candidate list used when none is given. `launch_align` has
this for reads+reference; this launch reuses that shape rather than
`launch_scaffold`'s, which the plan noted as worth adding here even though
its sibling does not have it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from app.errors import ValidationError
from app.models import FormatKind, ObjectRole, ObjectStatus
from app.pipelines.tools import Tool
from app.services import pipeline_service
from beanie import PydanticObjectId

_QUAST = Tool(name="quast", path="/usr/local/bin/quast.py", version="5.3.0")


def _obj(*, name, kind, role=None, status=ObjectStatus.READY, project_id=None):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name=name,
        format=SimpleNamespace(kind=kind),
        role=role,
        status=status,
        facts={},
        project_id=project_id or PydanticObjectId(),
        owner="local",
        blob_sha256="a" * 64,
    )


def _draft(*, project_id=None):
    return _obj(name="draft.fasta", kind=FormatKind.FASTA, project_id=project_id)


def _reference(name, *, project_id):
    return _obj(
        name=name, kind=FormatKind.FASTA, role=ObjectRole.REFERENCE, project_id=project_id
    )


async def _run(*, draft, project_objects, reference_object_id=None):
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
        patch("app.pipelines.tools.quast", return_value=_QUAST),
        patch("app.services.object_service.get_object", AsyncMock(side_effect=_get_object)),
        patch("app.services.object_service.list_objects", AsyncMock(side_effect=_list_objects)),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("a" * 64, None)),
        ),
        patch("app.queue.queue.enqueue", _enqueue),
    ):
        job = await pipeline_service.launch_misassembly_qc(
            draft_object_id=draft.id,
            reference_object_id=reference_object_id,
            owner="local",
        )
    return job, enqueued


class TestResolvingReferenceFromTheProject:
    async def test_one_candidate_reaches_the_queue(self):
        draft = _draft()
        ref = _reference("ref.fasta", project_id=draft.project_id)

        job, enqueued = await _run(draft=draft, project_objects=[ref])

        assert job.id == "job1"
        assert enqueued["type"] == "assess_misassemblies"
        payload = enqueued["payload"]
        assert payload["object_id"] == str(draft.id)
        assert payload["reference_object_id"] == str(ref.id)

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
        """An assembly BioFlow produced carries ObjectRole.REFERENCE, so a
        de novo assembly is in the reference pool by default. Without this
        exclusion, a project with one assembly and no other reference would
        offer to QUAST it against itself."""
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
        """check_reference_assembly's role requirement applies even when the
        reference is named explicitly, not just in the ambiguity-resolution
        path -- otherwise naming an id would be a way around the check."""
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
        """QUAST would happily report a perfect assembly against itself --
        the most misleading possible success, and nothing else in the
        validation stack catches this specific pairing."""
        draft = _obj(
            name="d.fasta", kind=FormatKind.FASTA, role=ObjectRole.REFERENCE
        )
        with pytest.raises(ValidationError, match="same object"):
            await _run(
                draft=draft, project_objects=[draft], reference_object_id=draft.id
            )

    async def test_named_reference_in_a_different_project_is_refused(self):
        """`launch_scaffold`, the closest sibling, has no explicit check
        here -- a gap the implementation plan called out to not repeat.
        This launch follows `launch_align`'s pattern instead: a
        cross-project reference must be refused with its own message, not
        silently accepted or left to fail some other way downstream."""
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


class TestPayloadNeverCarriesTheObjectsOwnName:
    """The seam Phase 3 depends on: this launch must never put the draft's
    name into the payload under a key the handler would use as the QUAST
    label or link filename. `assess_completeness`'s launch passes
    `assembly_name`; this one must not, or the fixed-label fix in
    `assess_misassemblies` would have nothing protecting it from a future
    caller that starts reading `assembly_name` again."""

    async def test_no_assembly_name_key_in_the_payload(self):
        draft = _draft()
        ref = _reference("ref.fasta", project_id=draft.project_id)

        _, enqueued = await _run(draft=draft, project_objects=[ref])

        assert "assembly_name" not in enqueued["payload"]
