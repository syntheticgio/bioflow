"""Deriving a canvas from runs the user already did (design §7).

A convenience that introduces no new persistence: read a set of `PipelineRun`s,
map each to its node type, create an INPUT node per `RunInput`, and infer edges
where one run's output id appears in another's inputs. The result is an
*unsaved* graph the user edits and saves.

The interesting requirement is the one that is easy to skip: runs that cannot be
represented are **reported as skipped, not silently dropped**. A user who
selects six runs and gets a four-node canvas with no explanation has been lied
to about what their history contains.
"""

import pytest
from beanie import PydanticObjectId, init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.models import ALL_MODELS
from app.models.run import PipelineRun, RunInput, RunKind, RunStatus
from app.services.workflow_derive import derive_definition

OWNER = "tester"


@pytest.fixture(autouse=True)
async def _init_beanie_models(monkeypatch):
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    db = client["biopipe_test"]
    await init_beanie(database=db, document_models=ALL_MODELS)
    monkeypatch.setattr("app.db.client.get_db", lambda: db)
    yield
    client.close()


async def _run(
    kind: RunKind,
    *,
    inputs: list[tuple[PydanticObjectId, str, str]] = (),
    outputs: list[PydanticObjectId] = (),
    label: str = "run",
) -> PipelineRun:
    run = PipelineRun(
        kind=kind,
        project_id=PydanticObjectId(),
        label=label,
        owner=OWNER,
        status=RunStatus.SUCCEEDED,
        inputs=[
            RunInput(object_id=oid, name=name, role=role)
            for oid, name, role in inputs
        ],
        outputs=list(outputs),
    )
    await run.insert()
    return run


class TestSingleRun:
    async def test_one_run_becomes_one_action_node(self):
        reads = PydanticObjectId()
        run = await _run(RunKind.TRIM, inputs=[(reads, "s.fastq", "reads")])

        result = await derive_definition([run.id], owner=OWNER)

        actions = [n for n in result.nodes if n.kind.value == "action"]
        assert [n.node_type for n in actions] == ["trim"]

    async def test_each_run_input_becomes_an_input_node(self):
        reads = PydanticObjectId()
        run = await _run(RunKind.TRIM, inputs=[(reads, "sample.fastq", "reads")])

        result = await derive_definition([run.id], owner=OWNER)

        inputs = [n for n in result.nodes if n.kind.value == "input"]
        assert len(inputs) == 1
        # The name is denormalized onto RunInput precisely so a run stays
        # readable after its inputs are deleted; the canvas should use it.
        assert inputs[0].label == "sample.fastq"

    async def test_the_input_node_is_wired_to_the_action(self):
        reads = PydanticObjectId()
        run = await _run(RunKind.TRIM, inputs=[(reads, "s.fastq", "reads")])

        result = await derive_definition([run.id], owner=OWNER)

        assert len(result.edges) == 1
        assert result.edges[0].to_port == "reads"


class TestChainedRuns:
    async def test_one_runs_output_feeding_another_becomes_an_edge(self):
        """The whole point: the user's actual pipeline, recovered. A trim whose
        output a later alignment consumed is a `trim -> align` edge, not two
        unconnected nodes each with their own input slot."""
        reads = PydanticObjectId()
        trimmed = PydanticObjectId()
        reference = PydanticObjectId()
        trim = await _run(
            RunKind.TRIM, inputs=[(reads, "s.fastq", "reads")], outputs=[trimmed]
        )
        align = await _run(
            RunKind.ALIGNMENT,
            inputs=[
                (trimmed, "s.trimmed.fastq", "reads"),
                (reference, "ref.fna", "reference"),
            ],
        )

        result = await derive_definition([trim.id, align.id], owner=OWNER)

        action_ids = {
            n.node_id for n in result.nodes if n.kind.value == "action"
        }
        derived = [
            e
            for e in result.edges
            if e.from_node in action_ids and e.to_node in action_ids
        ]
        assert len(derived) == 1
        assert derived[0].to_port == "reads"

    async def test_an_object_produced_upstream_does_not_also_become_an_input(self):
        """A file another selected run produced is an edge, not a slot. Making
        it both would ask the user to bind something the graph already
        computes."""
        reads = PydanticObjectId()
        trimmed = PydanticObjectId()
        reference = PydanticObjectId()
        trim = await _run(
            RunKind.TRIM, inputs=[(reads, "s.fastq", "reads")], outputs=[trimmed]
        )
        align = await _run(
            RunKind.ALIGNMENT,
            inputs=[
                (trimmed, "s.trimmed.fastq", "reads"),
                (reference, "ref.fna", "reference"),
            ],
        )

        result = await derive_definition([trim.id, align.id], owner=OWNER)

        labels = {n.label for n in result.nodes if n.kind.value == "input"}
        assert "s.trimmed.fastq" not in labels
        # The genuinely external inputs are still slots.
        assert {"s.fastq", "ref.fna"} <= labels

    async def test_a_shared_input_is_one_slot_not_two(self):
        """Two runs reading the same file describe one input, and duplicating
        it would make the user bind the same object twice."""
        reads = PydanticObjectId()
        a = await _run(RunKind.TRIM, inputs=[(reads, "s.fastq", "reads")])
        b = await _run(RunKind.ASSEMBLY, inputs=[(reads, "s.fastq", "reads")])

        result = await derive_definition([a.id, b.id], owner=OWNER)

        inputs = [n for n in result.nodes if n.kind.value == "input"]
        assert len(inputs) == 1


class TestSkipping:
    async def test_a_run_with_no_node_type_is_reported_not_dropped(self):
        """`reference_assembly` is a real RunKind that no node type covers.
        Silently omitting it leaves the user with a canvas quietly missing a
        step they selected."""
        run = await _run(RunKind.REFERENCE_ASSEMBLY, label="scaffolding")

        result = await derive_definition([run.id], owner=OWNER)

        assert [n for n in result.nodes if n.kind.value == "action"] == []
        assert len(result.skipped) == 1
        assert result.skipped[0].run_id == str(run.id)
        assert result.skipped[0].reason

    async def test_a_representable_run_alongside_a_skipped_one_still_derives(self):
        reads = PydanticObjectId()
        good = await _run(RunKind.TRIM, inputs=[(reads, "s.fastq", "reads")])
        bad = await _run(RunKind.REFERENCE_ASSEMBLY)

        result = await derive_definition([good.id, bad.id], owner=OWNER)

        assert len(result.skipped) == 1
        assert [n.node_type for n in result.nodes if n.kind.value == "action"] == [
            "trim"
        ]

    async def test_another_owners_run_is_not_derived_from(self):
        """Owner scoping: deriving is a read of someone's history."""
        run = await _run(RunKind.TRIM, inputs=[(PydanticObjectId(), "s.fastq", "reads")])

        result = await derive_definition([run.id], owner="someone-else")

        assert [n for n in result.nodes if n.kind.value == "action"] == []
        assert len(result.skipped) == 1


class TestTheResultIsUnsaved:
    async def test_nothing_is_persisted(self):
        """§7: 'introduces no new persistence'. The canvas is populated and the
        user decides whether it is worth keeping."""
        from app.models.workflow import WorkflowDefinition

        before = await WorkflowDefinition.find_all().count()
        run = await _run(RunKind.TRIM, inputs=[(PydanticObjectId(), "s.fastq", "reads")])

        await derive_definition([run.id], owner=OWNER)

        assert await WorkflowDefinition.find_all().count() == before


class TestLayout:
    async def test_upstream_runs_are_placed_left_of_what_they_feed(self):
        """The canvas reads left to right. Runs arrive newest-first from the
        activity list, so laying them out in selection order draws every wire
        backwards -- correct, but unreadable. Found by deriving a real
        trim -> align pair and seeing align land to the left of trim.
        """
        reads = PydanticObjectId()
        trimmed = PydanticObjectId()
        trim = await _run(
            RunKind.TRIM, inputs=[(reads, "s.fastq", "reads")], outputs=[trimmed]
        )
        align = await _run(
            RunKind.ALIGNMENT,
            inputs=[
                (trimmed, "s.trimmed.fastq", "reads"),
                (PydanticObjectId(), "ref.fna", "reference"),
            ],
        )

        # Selected newest-first, as the activity list presents them.
        result = await derive_definition([align.id, trim.id], owner=OWNER)

        by_type = {
            n.node_type: n for n in result.nodes if n.kind.value == "action"
        }
        assert by_type["trim"].position.x < by_type["align"].position.x

    async def test_a_cycle_in_the_selection_does_not_hang(self):
        """Runs cannot really form a cycle, but a corrupted or hand-edited
        selection must not spin forever -- the ordering has to terminate on
        whatever it is given."""
        a_out = PydanticObjectId()
        b_out = PydanticObjectId()
        a = await _run(RunKind.TRIM, inputs=[(b_out, "b.fastq", "reads")], outputs=[a_out])
        b = await _run(RunKind.TRIM, inputs=[(a_out, "a.fastq", "reads")], outputs=[b_out])

        result = await derive_definition([a.id, b.id], owner=OWNER)

        assert len([n for n in result.nodes if n.kind.value == "action"]) == 2
