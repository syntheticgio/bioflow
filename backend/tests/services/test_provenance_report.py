"""Rendering a chain as markdown.

Hand-built chains, no database: this is where the gap-honesty rules live and
they need exhaustive cheap tests. `_chain` below is the only fixture helper.
"""

from datetime import datetime

from beanie import PydanticObjectId

from app.services.provenance_report import _GAP_TEXT, render_markdown
from app.services.provenance_walker import (
    Gap,
    GapKind,
    Node,
    ProvenanceChain,
    Step,
)

READS = PydanticObjectId()
BAM = PydanticObjectId()


def _root(object_id=READS, name="reads.fastq.gz"):
    return Node(
        object_id=object_id,
        name=name,
        role=None,
        kind="spine",
        produced_by=None,
        parents=(),
    )


def _step_node(object_id=BAM, name="aligned.bam", step=None, parents=(READS,)):
    return Node(
        object_id=object_id,
        name=name,
        role=None,
        kind="spine",
        produced_by=step,
        parents=parents,
    )


def _chain(*nodes, gaps=(), branches=()):
    by_id = {n.object_id: n for n in nodes}
    return ProvenanceChain(
        target=nodes[-1],
        nodes=by_id,
        order=tuple(n.object_id for n in nodes),
        gaps=tuple(gaps),
        branches=tuple(branches),
    )


def test_complete_step_names_tool_and_version():
    step = Step(
        job_type="align_reads",
        verb="aligned with",
        tool="bwa-mem2",
        tool_version="2.2.1",
        ran_at=datetime(2026, 7, 14, 9, 0),
    )
    md = render_markdown(_chain(_root(), _step_node(step=step)))

    assert "aligned with bwa-mem2 2.2.1" in md
    assert "reads.fastq.gz" in md


def test_missing_version_is_stated_not_omitted():
    """The awkwardness is the point: a reader scanning for the version sees
    the question was asked and unanswered."""
    step = Step(
        job_type="align_reads",
        verb="aligned with",
        tool="bwa-mem2",
        tool_version=None,
        gaps=(Gap(kind=GapKind.VERSION_UNRECORDED, object_id=BAM),),
    )
    md = render_markdown(
        _chain(
            _root(),
            _step_node(step=step),
            gaps=(Gap(kind=GapKind.VERSION_UNRECORDED, object_id=BAM),),
        )
    )

    assert "version not recorded" in md
    assert "bwa-mem2" in md


def test_gap_text_is_exhaustive_over_gap_kind():
    """A `GapKind` member with no `_GAP_TEXT` entry fails silently for a
    step-level gap and raises KeyError for a chain-level one -- either way,
    nothing should rely on catching that later. See the "Hand-maintained
    registries keyed by an enum" note in CLAUDE.md: this is the same shape
    that let `results._SIDECAR_ROLES` silently drop a role."""
    assert set(_GAP_TEXT) == set(GapKind)


def test_every_gap_kind_renders_a_marker():
    expected_by_kind = {
        GapKind.VERSION_UNRECORDED: "version not recorded",
        GapKind.PARAMS_UNRECORDED: "parameters not recorded",
        GapKind.SHARE_BOUNDARY: "another profile",
        GapKind.DANGLING_PARENT: "no longer exists",
        GapKind.DEPTH_EXCEEDED: "truncated",
    }
    for kind in GapKind:
        expected = expected_by_kind[kind]
        chain = _chain(_root(), gaps=(Gap(kind=kind, object_id=READS),))
        md = render_markdown(chain)
        assert expected in md, f"{kind} rendered no marker"


def test_gap_count_is_shown():
    chain = _chain(
        _root(),
        gaps=(
            Gap(kind=GapKind.VERSION_UNRECORDED, object_id=READS),
            Gap(kind=GapKind.PARAMS_UNRECORDED, object_id=READS),
        ),
    )
    assert "2 facts not recorded" in render_markdown(chain)


def test_no_gaps_says_so_positively():
    assert "All facts recorded" in render_markdown(_chain(_root()))


def test_branch_renders_as_a_visible_fork():
    other = PydanticObjectId()
    chain = _chain(
        _root(),
        _root(object_id=other, name="ill.fastq.gz"),
        _step_node(parents=(READS, other)),
        branches=((READS, other),),
    )
    md = render_markdown(chain)
    assert "two inputs" in md.lower() or "branch" in md.lower()
    assert "ill.fastq.gz" in md


def test_root_is_labelled_input_not_a_gap():
    md = render_markdown(_chain(_root()))
    assert "Input" in md
    assert "not recorded" not in md
