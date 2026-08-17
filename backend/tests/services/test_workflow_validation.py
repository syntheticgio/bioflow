"""Graph validation: what the canvas refuses to save.

Every rule here has a failure it prevents. The type rules stop a protein FASTA
reaching an aligner's reference port; the cycle rule stops a graph that would
never launch a single node; the required-input rule stops a graph that looks
complete and cannot run.
"""

import pytest
from beanie import PydanticObjectId

from app.errors import AppError, NotFoundError
from app.models import FormatKind, ObjectRole
from app.models.workflow import (
    PortType,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
)
from app.services.workflow_service import (
    InvalidGraph,
    create_definition,
    update_definition,
    validate_definition,
)


def _input(node_id: str, fmt: FormatKind, role: ObjectRole | None = None) -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id,
        kind=WorkflowNodeKind.INPUT,
        label=node_id,
        accepts=PortType(format=fmt, role=role),
    )


def _action(node_id: str, node_type: str) -> WorkflowNode:
    return WorkflowNode(node_id=node_id, kind=WorkflowNodeKind.ACTION, node_type=node_type)


# WorkflowDefinition is a Beanie Document; instantiating one (even without
# saving it) requires init_beanie to have run first, same reason every other
# Document-backed test in this directory requests beanie_models.
pytestmark = pytest.mark.usefixtures("beanie_models")


class TestTypeRules:
    def test_a_matching_wire_validates(self):
        definition = WorkflowDefinition(
            name="ok",
            nodes=[_input("reads", FormatKind.FASTQ), _action("t", "trim")],
            edges=[
                WorkflowEdge(from_node="reads", from_port="object", to_node="t", to_port="reads")
            ],
        )
        assert validate_definition(definition) == []

    def test_a_protein_fasta_cannot_feed_an_alignment_reference(self):
        """The rule this whole typing scheme exists for."""
        definition = WorkflowDefinition(
            name="bad",
            nodes=[
                _input("reads", FormatKind.FASTQ),
                _input("prot", FormatKind.FASTA, ObjectRole.PROTEIN),
                _action("a", "align"),
            ],
            edges=[
                WorkflowEdge(from_node="reads", from_port="object", to_node="a", to_port="reads"),
                WorkflowEdge(
                    from_node="prot", from_port="object", to_node="a", to_port="reference"
                ),
            ],
        )
        errors = validate_definition(definition)
        assert any(e.code == "type_mismatch" and e.node_id == "a" for e in errors)

    def test_a_wire_to_an_unknown_port_is_rejected(self):
        definition = WorkflowDefinition(
            name="bad",
            nodes=[_input("reads", FormatKind.FASTQ), _action("t", "trim")],
            edges=[
                WorkflowEdge(from_node="reads", from_port="object", to_node="t", to_port="nope")
            ],
        )
        assert any(e.code == "unknown_port" for e in validate_definition(definition))

    def test_an_unknown_node_type_is_rejected(self):
        """A definition saved before a tool was removed must fail loudly
        rather than silently skipping the node at launch."""
        definition = WorkflowDefinition(name="bad", nodes=[_action("x", "no_such_tool")])
        assert any(e.code == "unknown_node_type" for e in validate_definition(definition))

    def test_a_multi_format_output_wired_into_a_compatible_multi_format_input_validates(self):
        """annotation_export's `subset` output (GFF/GTF/BED) into a second
        annotation_export's `annotation` input (same set) -- both ports are
        declared with `formats=`, so `port.type.format` is None on both
        sides. This must validate cleanly, not crash."""
        definition = WorkflowDefinition(
            name="ok",
            nodes=[
                _action("export1", "annotation_export"),
                _action("export2", "annotation_export"),
            ],
            edges=[
                WorkflowEdge(
                    from_node="export1", from_port="subset", to_node="export2", to_port="annotation"
                )
            ],
        )
        errors = validate_definition(definition)
        assert not any(e.code == "type_mismatch" for e in errors)

    def test_a_multi_format_output_wired_into_an_incompatible_input_is_rejected(self):
        """annotation_export's `subset` (GFF/GTF/BED) has no overlap with
        quantify's `alignment` port (BAM only) -- must report a clear
        type_mismatch, not crash, and must name all three producer formats
        rather than "None"."""
        definition = WorkflowDefinition(
            name="bad",
            nodes=[
                _action("export1", "annotation_export"),
                _action("q", "quantify"),
            ],
            edges=[
                WorkflowEdge(
                    from_node="export1", from_port="subset", to_node="q", to_port="alignment"
                )
            ],
        )
        errors = validate_definition(definition)
        mismatches = [e for e in errors if e.code == "type_mismatch" and e.node_id == "q"]
        assert len(mismatches) == 1
        message = mismatches[0].message
        assert "None" not in message
        assert "gff" in message and "gtf" in message and "bed" in message

    def test_an_input_node_with_multiple_accepted_formats_feeds_a_multi_format_port(self):
        """The real crash scenario: an INPUT node's `accepts` field is a
        multi-format PortType (formats=[gff, gtf, bed]), wired into
        annotation_export's `annotation` port. _output_type returns
        node.accepts directly for an INPUT node, so this exercises the exact
        code path that raised AttributeError in production."""
        definition = WorkflowDefinition(
            name="ok",
            nodes=[
                WorkflowNode(
                    node_id="ann",
                    kind=WorkflowNodeKind.INPUT,
                    label="ann",
                    accepts=PortType(
                        formats=(FormatKind.GFF, FormatKind.GTF, FormatKind.BED),
                        role=ObjectRole.ANNOTATION,
                    ),
                ),
                _action("export1", "annotation_export"),
            ],
            edges=[
                WorkflowEdge(
                    from_node="ann", from_port="object", to_node="export1", to_port="annotation"
                )
            ],
        )
        errors = validate_definition(definition)
        assert not any(e.code == "type_mismatch" for e in errors)


class TestStructuralRules:
    def test_a_cycle_is_rejected(self):
        definition = WorkflowDefinition(
            name="cyclic",
            nodes=[_action("a", "trim"), _action("b", "trim")],
            edges=[
                WorkflowEdge(from_node="a", from_port="trimmed", to_node="b", to_port="reads"),
                WorkflowEdge(from_node="b", from_port="trimmed", to_node="a", to_port="reads"),
            ],
        )
        assert any(e.code == "cycle" for e in validate_definition(definition))

    def test_a_missing_required_input_is_rejected(self):
        """align needs a reference; a graph without one looks complete on the
        canvas and cannot run."""
        definition = WorkflowDefinition(
            name="incomplete",
            nodes=[_input("reads", FormatKind.FASTQ), _action("a", "align")],
            edges=[
                WorkflowEdge(from_node="reads", from_port="object", to_node="a", to_port="reads")
            ],
        )
        errors = validate_definition(definition)
        assert any(e.code == "missing_required_input" and e.port == "reference" for e in errors)

    def test_an_optional_input_may_be_unwired(self):
        """Single-end reads: `mate` is genuinely absent, not an error."""
        definition = WorkflowDefinition(
            name="single end",
            nodes=[_input("reads", FormatKind.FASTQ), _action("t", "trim")],
            edges=[
                WorkflowEdge(from_node="reads", from_port="object", to_node="t", to_port="reads")
            ],
        )
        assert validate_definition(definition) == []

    def test_two_wires_into_one_port_is_rejected(self):
        """A port takes one object. Two would make the launch ambiguous."""
        definition = WorkflowDefinition(
            name="bad",
            nodes=[
                _input("r1", FormatKind.FASTQ),
                _input("r2", FormatKind.FASTQ),
                _action("t", "trim"),
            ],
            edges=[
                WorkflowEdge(from_node="r1", from_port="object", to_node="t", to_port="reads"),
                WorkflowEdge(from_node="r2", from_port="object", to_node="t", to_port="reads"),
            ],
        )
        assert any(e.code == "duplicate_wire" for e in validate_definition(definition))

    def test_an_edge_naming_a_missing_node_is_rejected(self):
        definition = WorkflowDefinition(
            name="bad",
            nodes=[_action("t", "trim")],
            edges=[
                WorkflowEdge(from_node="ghost", from_port="object", to_node="t", to_port="reads")
            ],
        )
        assert any(e.code == "unknown_node" for e in validate_definition(definition))

    def test_duplicate_node_ids_are_rejected(self):
        definition = WorkflowDefinition(
            name="bad", nodes=[_action("t", "trim"), _action("t", "qc")]
        )
        assert any(e.code == "duplicate_node_id" for e in validate_definition(definition))


@pytest.mark.asyncio(loop_scope="module")
class TestCrud:
    async def test_saving_an_invalid_graph_is_refused(self):
        """Invalid graphs must not reach storage: a saved graph that cannot
        run is a bug that surfaces much later, at launch."""
        with pytest.raises(InvalidGraph) as caught:
            await create_definition(
                name="bad",
                description="",
                nodes=[_input("reads", FormatKind.FASTQ), _action("a", "align")],
                edges=[
                    WorkflowEdge(
                        from_node="reads", from_port="object", to_node="a", to_port="reads"
                    )
                ],
                owner="test-owner",
            )
        assert any(e.code == "missing_required_input" for e in caught.value.errors)

    async def test_invalid_graph_is_a_real_app_error(self):
        """InvalidGraph belongs in the app's own error hierarchy (see
        app/errors.py) rather than being a bare Exception -- a future router
        calling create_definition/update_definition should get a proper 422,
        not an unhandled 500."""
        with pytest.raises(InvalidGraph) as caught:
            await create_definition(
                name="bad",
                description="",
                nodes=[_input("reads", FormatKind.FASTQ), _action("a", "align")],
                edges=[
                    WorkflowEdge(
                        from_node="reads", from_port="object", to_node="a", to_port="reads"
                    )
                ],
                owner="test-owner",
            )
        assert isinstance(caught.value, AppError)
        assert caught.value.status_code == 422

    async def test_editing_a_missing_definition_raises_not_found(self):
        """A stale definition id (deleted, or simply wrong) is a not-found
        case, not an invalid-graph case -- the two must not share an error."""
        with pytest.raises(NotFoundError):
            await update_definition(
                PydanticObjectId(),
                name="anything",
                description="",
                nodes=[_input("reads", FormatKind.FASTQ), _action("t", "trim")],
                edges=[
                    WorkflowEdge(
                        from_node="reads", from_port="object", to_node="t", to_port="reads"
                    )
                ],
            )

    async def test_an_edit_bumps_the_version(self):
        """Runs pin a version, so an edit must produce a new one."""
        created = await create_definition(
            name="ok",
            description="",
            nodes=[_input("reads", FormatKind.FASTQ), _action("t", "trim")],
            edges=[
                WorkflowEdge(from_node="reads", from_port="object", to_node="t", to_port="reads")
            ],
            owner="test-owner",
        )
        assert created.version == 1

        updated = await update_definition(
            created.id,
            name="ok, renamed",
            description="",
            nodes=created.nodes,
            edges=created.edges,
        )
        assert updated.version == 2

    async def test_a_saved_definition_carries_its_owner(self):
        """Non-'local' on purpose: every document defaults to 'local', so
        asserting that value would prove nothing."""
        created = await create_definition(
            name="owned",
            description="",
            nodes=[_input("reads", FormatKind.FASTQ), _action("t", "trim")],
            edges=[
                WorkflowEdge(from_node="reads", from_port="object", to_node="t", to_port="reads")
            ],
            owner="profile-123",
        )
        assert created.owner == "profile-123"
