"""Shaping a ProvenanceChain into the lineage list the History tab renders.

Two transformations live here, both pure over a walked chain so they can be
tested without a database:

- **Merging.** Two mates of a pair are two objects produced by one
  `download_sra_run`; the tab shows them as one row. The producing job id is
  the only evidence of that which does not involve guessing -- matching on
  job_type + tool + params + timestamp would also catch objects from before
  `produced_by_job` was recorded, but it merges runs that were never one run,
  which is a wrong claim rather than a missing one.

- **Ordering.** Materials (a reference, an annotation) are part of the
  lineage the user sees, ordered by when they were made rather than pushed
  into a separate list. A reference downloaded months before the reads
  therefore sorts above them, which is chronologically true but reads as
  though the reads descend from it -- `used_by` on the merged step is what
  lets the renderer say "used as reference by <consumer>" and settle that.

Nodes with no recorded `ran_at` are not given an invented timestamp. They
hold their position from the walker's topological order and timed nodes sort
around them; see `order_lineage`.
"""

from dataclasses import dataclass

from beanie import PydanticObjectId

from app.services.provenance_walker import Gap, Node, ProvenanceChain, Step


@dataclass(frozen=True)
class LineageEntry:
    """One numbered row in "How this file was made".

    `objects` holds every object this row covers -- more than one only when a
    single job produced several (the paired-mate case). `node` is the first of
    them, and carries the step, role and kind the row renders from; merging
    only ever groups objects that share a producing job, so any of them would
    give the same answer for those.
    """

    node: Node
    objects: tuple[Node, ...]
    gaps: tuple[Gap, ...] = ()

    @property
    def step(self) -> Step | None:
        return self.node.produced_by

    @property
    def kind(self) -> str:
        return self.node.kind

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(n.name for n in self.objects)


def merge_steps(nodes: list[Node]) -> list[LineageEntry]:
    """Group nodes produced by the same job into one entry each.

    A node with no `produced_by` (a root) or no `job_id` (produced before the
    walker recorded one) merges with nothing: there is no evidence tying it to
    another object, and inventing one would put two unrelated files on a row
    claiming they came from a single run.

    Order is preserved -- each group appears where its first member did.

    Within a row, objects are sorted by name rather than left in the order the
    walk reached them. The walker's order is a reversed BFS, so a read pair
    arrives `_2` before `_1` and would render "DRR1066343_2.fastq and
    DRR1066343_1.fastq" -- backwards on exactly the case this merging exists
    for. Name order is what a reader expects and is stable across walks.
    """
    entries: list[LineageEntry] = []
    by_job: dict[PydanticObjectId, int] = {}

    for node in nodes:
        step = node.produced_by
        job_id = step.job_id if step is not None else None

        if job_id is not None and job_id in by_job:
            at = by_job[job_id]
            existing = entries[at]
            objects = tuple(
                sorted(existing.objects + (node,), key=lambda n: n.name)
            )
            entries[at] = LineageEntry(
                # `node` carries the step, role and kind the row renders from,
                # and merging only groups objects sharing a producing job, so
                # any member gives the same answer -- keep the first by name so
                # the row's object_id is stable rather than walk-order.
                node=objects[0],
                objects=objects,
                gaps=existing.gaps,
            )
            continue

        if job_id is not None:
            by_job[job_id] = len(entries)
        entries.append(LineageEntry(node=node, objects=(node,)))

    return entries


def order_lineage(entries: list[LineageEntry]) -> list[LineageEntry]:
    """Sort entries oldest-first by when their step ran.

    Entries with no `ran_at` keep the relative position the walker gave them
    (its `order` is already oldest-first topologically), and timed entries sort
    around them. This is a stable sort over a key that is only defined for some
    rows, which is the honest shape here: a file with no timing row genuinely
    has no place on the timeline, and giving it one -- epoch, or its
    neighbour's timestamp -- would state something the record does not.

    The consequence worth knowing: a chain where nothing is timed comes back in
    exactly the walker's order, unchanged.
    """
    timed = [
        (i, e)
        for i, e in enumerate(entries)
        if e.step is not None and e.step.ran_at is not None
    ]
    if not timed:
        return list(entries)

    # The positions timed entries occupy are the slots they may be reordered
    # within; untimed entries stay exactly where they are.
    slots = [i for i, _ in timed]
    ordered = sorted(timed, key=lambda pair: pair[1].step.ran_at)  # type: ignore[union-attr]

    result = list(entries)
    for slot, (_, entry) in zip(slots, ordered, strict=False):
        result[slot] = entry
    return result


def lineage_for(chain: ProvenanceChain) -> list[LineageEntry]:
    """The full lineage list: spine and materials together, merged and ordered.

    Materials are included rather than split into their own list -- they are
    steps that produced an input, and the History tab numbers them alongside
    everything else.
    """
    nodes = [chain.nodes[oid] for oid in chain.order]
    entries = merge_steps(nodes)
    entries = [
        LineageEntry(
            node=e.node,
            objects=e.objects,
            gaps=tuple(
                g
                for g in chain.gaps
                if any(g.object_id == n.object_id for n in e.objects)
            ),
        )
        for e in entries
    ]
    return order_lineage(entries)


def format_names(names: tuple[str, ...] | list[str]) -> str:
    """How a merged row names the objects it covers.

    One name reads plainly, two join with "and", and three or more truncate --
    a run that produced eight files should not push its own description off the
    row.
    """
    names = tuple(names)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]}, {names[1]} and {len(names) - 2} more"
