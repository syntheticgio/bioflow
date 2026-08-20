"""The launch path for RagTag scaffolding.

Same posture test_polish_launch.py takes: run the whole way to the enqueue
rather than stopping at "the service raised nothing".

Same provenance shape as launch_polish, for the same reason -- see the
reference_assembly_handlers module docstring: RagTag invokes minimap2
itself, so there is no BAM to validate a target against. What this launch
path actually guards is *which reference*, which is the real ambiguity in
this slice (a project holding two reference-role FASTA is the ordinary
case, not an edge case), and the swap/self-reference mistakes that are
possible only because both inputs are the same FormatKind.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.errors import ConflictError, ValidationError
from app.models import FormatKind, ObjectRole
from app.models.run import RunInput, RunInputRole, RunKind
from app.pipelines.tools import Tool
from app.services import pipeline_service
from tests.services.conftest import _draft, _obj, _reference

_RT = Tool(name="ragtag", path="/usr/local/bin/ragtag.py", version="v2.1.0")


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

    created: dict = {}

    # Constructing the real RunInput is the assertion, the same way
    # test_assembly_launch.py does it: it is a pydantic model with a required
    # `name`, so a launcher that omits one raises here rather than in the
    # running app.
    async def _create_run(**kwargs):
        for item in kwargs["inputs"]:
            assert isinstance(item, RunInput)
            assert item.name
        created.update(kwargs)
        return SimpleNamespace(id="run1", owner="local")

    with (
        patch("app.pipelines.tools.ragtag", return_value=_RT),
        patch("app.services.object_service.get_object", AsyncMock(side_effect=_get_object)),
        patch("app.services.object_service.list_objects", AsyncMock(side_effect=_list_objects)),
        patch("app.services.pipeline_service._resolve_readable",
              AsyncMock(return_value=("a" * 64, None))),
        patch("app.services.run_service.create_run", _create_run),
        patch("app.services.run_service.link_job", AsyncMock()),
        patch("app.queue.queue.enqueue", _enqueue),
    ):
        job = await pipeline_service.launch_scaffold(
            draft_object_id=draft.id,
            reference_object_id=reference_object_id,
            divergence=divergence,
            owner="local",
        )
    return job, enqueued, created


class TestResolvingReferenceFromTheProject:
    async def test_one_candidate_reaches_the_queue(self):
        draft = _draft()
        ref = _reference("ref.fasta", project_id=draft.project_id)

        job, enqueued, created = await _run(draft=draft, project_objects=[ref])

        assert job.id == "job1"
        assert enqueued["type"] == "scaffold_assembly"
        payload = enqueued["payload"]
        assert payload["draft_object_id"] == str(draft.id)
        assert payload["reference_object_id"] == str(ref.id)
        assert payload["divergence"] == "same_species"

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


class TestExplicitReference:
    async def test_named_reference_bypasses_resolution(self):
        draft = _draft()
        pid = draft.project_id
        chosen = _reference("chosen.fasta", project_id=pid)
        other = _reference("other.fasta", project_id=pid)

        job, enqueued, created = await _run(
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
        """The degenerate case of the swap problem the design names."""
        draft = _obj(
            name="d.fasta", kind=FormatKind.FASTA, role=ObjectRole.REFERENCE
        )
        with pytest.raises(ValidationError, match="same object"):
            await _run(
                draft=draft, project_objects=[draft], reference_object_id=draft.id
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


class TestTheRunRecord:
    """GitHub #91: this launcher created no PipelineRun at all, which left a
    finished scaffold unrecoverable by derive-from-runs."""

    async def test_a_reference_assembly_run_is_created(self):
        draft = _draft()
        ref = _reference("ref.fasta", project_id=draft.project_id)

        _, _, created = await _run(draft=draft, project_objects=[ref])

        assert created["kind"] is RunKind.REFERENCE_ASSEMBLY
        # The tool is what tells the three REFERENCE_ASSEMBLY node types apart
        # when a run is derived back into a node -- see
        # workflow_derive._node_type_for.
        assert created["tool"] == "ragtag"
        assert created["project_id"] == draft.project_id
        roles = {i.role: i.object_id for i in created["inputs"]}
        assert roles[RunInputRole.DRAFT_ASSEMBLY] == draft.id
        assert roles[RunInputRole.REFERENCE] == ref.id

    async def test_a_deduplicated_launch_discards_its_run(self):
        """A run left behind by a launch that enqueued nothing would sit in the
        activity view implying work is happening."""
        draft = _draft()
        ref = _reference("ref.fasta", project_id=draft.project_id)
        objects = {o.id: o for o in (draft, ref)}
        discard = AsyncMock()

        with (
            patch("app.pipelines.tools.ragtag", return_value=_RT),
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=lambda object_id, *, owner: objects[object_id]),
            ),
            patch(
                "app.services.object_service.list_objects",
                AsyncMock(return_value=[ref]),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=("a" * 64, None)),
            ),
            patch(
                "app.services.run_service.create_run",
                AsyncMock(return_value=SimpleNamespace(id="run1", owner="local")),
            ),
            patch("app.services.run_service.discard_run", discard),
            # None is what enqueue returns when the dedup key already exists.
            patch("app.queue.queue.enqueue", AsyncMock(return_value=None)),
        ):
            with pytest.raises(ConflictError):
                await pipeline_service.launch_scaffold(
                    draft_object_id=draft.id, owner="local"
                )

        discard.assert_awaited_once()


class TestDivergence:
    async def test_a_chosen_divergence_is_threaded_through(self):
        draft = _draft()
        ref = _reference("ref.fasta", project_id=draft.project_id)

        _, enqueued, created = await _run(
            draft=draft, project_objects=[ref], divergence="distant"
        )
        assert enqueued["payload"]["divergence"] == "distant"
