"""Tool-parameterized node types.

Per CLAUDE.md's registry rules this is the third category -- keys owned by
something outside any one enum -- so the invariant runs from the registry
outward: every option a node type offers must resolve to a port set.
"""


from app.models.object import FormatKind
from app.models.workflow import WorkflowNode, WorkflowNodeKind
from app.pipelines.node_types import NODE_TYPES, ports_for


def _align_node(aligner: str | None = None) -> WorkflowNode:
    params = {"aligner": aligner} if aligner else {}
    return WorkflowNode(
        node_id="align_1",
        kind=WorkflowNodeKind.ACTION,
        node_type="align",
        params=params,
    )


def test_align_declares_a_tool_choice():
    choice = NODE_TYPES["align"].tool_choice
    assert choice is not None
    assert choice.param_key == "aligner"
    assert "minimap2" in [o.value for o in choice.options]
    assert "star" in [o.value for o in choice.options]


def test_star_gains_an_annotation_port():
    """STAR builds an annotation-aware index; the others have no such concept
    (see aligners.index_role, which raises for a non-STAR annotated index)."""
    inputs, _ = ports_for(_align_node("star"))
    annotation = next((p for p in inputs if p.name == "annotation"), None)
    assert annotation is not None
    assert annotation.type.format is FormatKind.GTF
    # Optional: STAR supports both index shapes deliberately, and a run
    # without an annotation is a normal run, not a broken one.
    assert annotation.required is False


def test_minimap2_has_no_annotation_port():
    inputs, _ = ports_for(_align_node("minimap2"))
    assert all(p.name != "annotation" for p in inputs)


def test_every_aligner_keeps_the_shared_ports():
    for option in NODE_TYPES["align"].tool_choice.options:
        inputs, outputs = ports_for(_align_node(option.value))
        names = {p.name for p in inputs}
        assert {"reads", "mate", "reference"} <= names, option.value
        assert [p.name for p in outputs] == ["alignment"], option.value


def test_reads_stays_multiple_for_every_aligner():
    for option in NODE_TYPES["align"].tool_choice.options:
        inputs, _ = ports_for(_align_node(option.value))
        reads = next(p for p in inputs if p.name == "reads")
        assert reads.multiple is True, option.value


def test_an_unset_tool_falls_back_to_the_default():
    """A node dropped from the palette and not yet touched still has ports --
    otherwise it could not be wired at all."""
    inputs, outputs = ports_for(_align_node(None))
    assert {p.name for p in inputs} >= {"reads", "reference"}
    assert outputs


def test_an_unknown_tool_falls_back_to_the_default():
    """A definition saved before an aligner was removed still opens."""
    inputs, _ = ports_for(_align_node("no-such-aligner"))
    assert {p.name for p in inputs} >= {"reads", "reference"}


def test_a_node_type_without_a_tool_choice_uses_its_static_ports():
    node = WorkflowNode(
        node_id="qc_1", kind=WorkflowNodeKind.ACTION, node_type="qc"
    )
    inputs, outputs = ports_for(node)
    assert [p.name for p in inputs] == ["reads"]
    assert outputs == ()


def test_every_option_of_every_tool_choice_resolves():
    """The exhaustiveness invariant. A tool offered in a dropdown that no
    resolver handles is a node the canvas can place and never wire."""
    for node_type, spec in NODE_TYPES.items():
        if spec.tool_choice is None:
            continue
        for option in spec.tool_choice.options:
            node = WorkflowNode(
                node_id="n",
                kind=WorkflowNodeKind.ACTION,
                node_type=node_type,
                params={spec.tool_choice.param_key: option.value},
            )
            inputs, outputs = ports_for(node)
            assert inputs or outputs, f"{node_type}/{option.value} resolves to no ports"
