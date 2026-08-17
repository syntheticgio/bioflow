"""Merging sibling steps and ordering materials into the lineage.

Hand-built chains, no database -- same reasoning as `test_provenance_report`:
these are the rules that decide what the History tab claims happened, and the
wrong answer is a false claim rather than a missing one, so they need cheap
exhaustive tests.
"""

from datetime import datetime

from beanie import PydanticObjectId

from app.services.provenance_lineage import (
    format_names,
    lineage_for,
    merge_steps,
    order_lineage,
)
from app.services.provenance_walker import Node, ProvenanceChain, Step

DOWNLOAD_JOB = PydanticObjectId()
TRIM_JOB = PydanticObjectId()


def _node(name, *, step=None, kind="spine", object_id=None, used_by=None):
    return Node(
        object_id=object_id or PydanticObjectId(),
        name=name,
        role=None,
        kind=kind,
        produced_by=step,
        parents=(),
        used_by=used_by,
    )


def _step(job_type="download_sra_run", *, job_id=None, ran_at=None):
    return Step(
        job_type=job_type,
        verb="downloaded from the SRA",
        job_id=job_id,
        ran_at=ran_at,
    )


def _chain(*nodes, gaps=()):
    return ProvenanceChain(
        target=nodes[-1],
        nodes={n.object_id: n for n in nodes},
        order=tuple(n.object_id for n in nodes),
        gaps=tuple(gaps),
    )


def test_two_mates_from_one_job_become_one_step():
    """The case the layout was designed around: `_1.fastq` and `_2.fastq` are
    two objects, but one download run made both."""
    step = _step(job_id=DOWNLOAD_JOB)
    entries = merge_steps(
        [_node("DRR1066343_1.fastq", step=step), _node("DRR1066343_2.fastq", step=step)]
    )

    assert len(entries) == 1
    assert entries[0].names == ("DRR1066343_1.fastq", "DRR1066343_2.fastq")


def test_objects_without_a_producing_job_never_merge():
    """Two roots have no evidence tying them together. Merging them would put
    unrelated files on one row claiming a single run produced both -- a wrong
    claim, where leaving them apart is only a missed tidy-up."""
    entries = merge_steps([_node("a.fastq"), _node("b.fastq")])

    assert len(entries) == 2


def test_objects_with_a_step_but_no_job_id_never_merge():
    """Produced before the walker recorded `job_id`. Same reasoning: no
    evidence, so no merge."""
    entries = merge_steps(
        [
            _node("a.fastq", step=_step(job_id=None)),
            _node("b.fastq", step=_step(job_id=None)),
        ]
    )

    assert len(entries) == 2


def test_different_jobs_stay_separate():
    entries = merge_steps(
        [
            _node("reads.fastq", step=_step(job_id=DOWNLOAD_JOB)),
            _node("trimmed.fastq", step=_step("trim_reads", job_id=TRIM_JOB)),
        ]
    )

    assert len(entries) == 2


def test_merged_row_keeps_the_position_of_its_first_member():
    step = _step(job_id=DOWNLOAD_JOB)
    entries = merge_steps(
        [
            _node("mate_1.fastq", step=step),
            _node("other.fastq", step=_step("trim_reads", job_id=TRIM_JOB)),
            _node("mate_2.fastq", step=step),
        ]
    )

    assert [e.names for e in entries] == [
        ("mate_1.fastq", "mate_2.fastq"),
        ("other.fastq",),
    ]


def test_three_or_more_names_truncate():
    """A run producing eight files should not push its own description off
    the row."""
    assert format_names(["a", "b", "c"]) == "a, b and 1 more"
    assert format_names(["a", "b", "c", "d"]) == "a, b and 2 more"


def test_one_and_two_names_read_plainly():
    assert format_names(["only.fastq"]) == "only.fastq"
    assert format_names(["a.fastq", "b.fastq"]) == "a.fastq and b.fastq"


def test_timed_entries_sort_oldest_first():
    late = _node("late.bam", step=_step("align_reads", ran_at=datetime(2026, 8, 5)))
    early = _node("early.fastq", step=_step(ran_at=datetime(2026, 8, 1)))

    ordered = order_lineage(merge_steps([late, early]))

    assert [e.names[0] for e in ordered] == ["early.fastq", "late.bam"]


def test_untimed_entries_hold_their_position():
    """An untimed node gets no invented timestamp: it has no place on the
    timeline, and giving it one would state something the record does not.
    Timed entries sort within the slots they already occupy, around it."""
    untimed = _node("root.fastq")
    late = _node("late.bam", step=_step("align_reads", ran_at=datetime(2026, 8, 5)))
    early = _node("mid.fastq", step=_step(ran_at=datetime(2026, 8, 1)))

    ordered = order_lineage(merge_steps([untimed, late, early]))

    # The untimed root stays at index 0; the two timed rows swap into the
    # slots they held between them.
    assert [e.names[0] for e in ordered] == ["root.fastq", "mid.fastq", "late.bam"]


def test_a_chain_with_no_timings_is_returned_unchanged():
    nodes = [_node("a.fastq"), _node("b.fastq"), _node("c.fastq")]
    ordered = order_lineage(merge_steps(nodes))

    assert [e.names[0] for e in ordered] == ["a.fastq", "b.fastq", "c.fastq"]


def test_materials_are_ordered_into_the_lineage_not_separated():
    """The user's ask: a reference is a step under "How this file was made",
    positioned by when it was made. Downloaded before the reads, it sorts
    above them -- which is why the row needs `used_by` to say what consumed
    it."""
    reads = _node("reads.fastq", step=_step(ran_at=datetime(2026, 8, 2)))
    reference = _node(
        "GCF_000146045.2_R64_genomic.fna",
        step=_step("download_assembly", ran_at=datetime(2026, 1, 9)),
        kind="supporting",
    )

    entries = lineage_for(_chain(reads, reference))

    assert [e.names[0] for e in entries] == [
        "GCF_000146045.2_R64_genomic.fna",
        "reads.fastq",
    ]
    assert entries[0].kind == "supporting"


def test_gaps_follow_their_objects_onto_the_merged_row():
    from app.services.provenance_walker import Gap, GapKind

    step = _step(job_id=DOWNLOAD_JOB)
    mate_1 = _node("mate_1.fastq", step=step)
    mate_2 = _node("mate_2.fastq", step=step)
    gap = Gap(kind=GapKind.PARAMS_UNRECORDED, object_id=mate_2.object_id)

    entries = lineage_for(_chain(mate_1, mate_2, gaps=[gap]))

    assert len(entries) == 1
    assert entries[0].gaps == (gap,)


def test_merged_names_read_in_name_order_not_walk_order():
    """The walker's order is a reversed BFS, so a pair arrives `_2` before
    `_1`. Rendering that order gives "DRR1066343_2.fastq and
    DRR1066343_1.fastq" -- backwards on the exact case merging exists for."""
    step = _step(job_id=DOWNLOAD_JOB)
    entries = merge_steps(
        [_node("DRR1066343_2.fastq", step=step), _node("DRR1066343_1.fastq", step=step)]
    )

    assert entries[0].names == ("DRR1066343_1.fastq", "DRR1066343_2.fastq")
